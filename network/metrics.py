"""Live network/server metrics measured from the running Mininet network.

    sudo python3 -m network.metrics KHI           # live table for a Karachi user
    sudo python3 -m network.metrics LHR --once

Per (user, CDN) pair:

  latency_ms      Real ICMP round-trip time, `ping` run inside the user host.
                  Includes queueing delay, so it rises under congestion.
  path_cost_ms    One-way Dijkstra path cost (sum of configured link delays).
                  RTT on an idle network is about 2x this.
  capacity_mbps   Smallest link capacity on the CDN -> user path.
  available_bandwidth_mbps
                  min over the links of the CDN -> user path of
                  (link capacity - current rate on that link). Rates come from
                  the switch interface byte counters, sampled every second
                  (exponential moving average, alpha=0.6).

Per CDN:

  throughput_mbps Current traffic leaving the CDN host, summed over its links.
  server_load_pct max(cpu_pct, nic_util_pct, connection_pct):
                    cpu_pct        CPU used by the CDN's http.server process
                    nic_util_pct   busiest CDN access link, rate / capacity
                    connection_pct established connections on the HTTP port,
                                   as a share of MAX_CONNECTIONS
                  The busiest resource defines how loaded the server is.
"""
import argparse
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import hostexec, spec
from .routing import build_forwarding_table, forwarding_path

SAMPLE_INTERVAL = 1.0
EMA_ALPHA = 0.6
MAX_CONNECTIONS = 10
PING_COUNT = 3
PING_TTL = 1.0
CLK_TCK = os.sysconf("SC_CLK_TCK")

RTT_RE = re.compile(r"rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms")
LOSS_RE = re.compile(r"([\d.]+)% packet loss")


def parse_ping(output):
    loss = LOSS_RE.search(output)
    rtt = RTT_RE.search(output)
    return {
        "latency_ms": float(rtt.group(2)) if rtt else None,
        "jitter_ms": float(rtt.group(4)) if rtt else None,
        "loss_pct": float(loss.group(1)) if loss else 100.0,
    }


def read_cpu_ticks(pid):
    with open(f"/proc/{pid}/stat") as f:
        fields = f.read().rsplit(")", 1)[1].split()
    return int(fields[11]) + int(fields[12])  # utime + stime


def count_connections(host_pid, port):
    """Established TCP connections on `port` inside the host's network namespace."""
    suffix = f":{port:04X}"
    count = 0
    with open(f"/proc/{host_pid}/net/tcp") as f:
        next(f)
        for line in f:
            parts = line.split()
            if parts[1].endswith(suffix) and parts[3] == "01":
                count += 1
    return count


def counter_map(links, hosts):
    """Directed edge (u, v) -> (switch-side interface, 'tx'|'rx') that counts traffic u -> v.

    Switch interfaces live in the root namespace, so their counters are readable
    directly. Traffic leaving a switch is its tx; traffic entering a switch from
    a host is the switch port's rx.
    """
    mapping = {}
    for link in links:
        a, b = link["a"], link["b"]
        for u, v, intf_u, intf_v in ((a, b, link["intf_a"], link["intf_b"]),
                                     (b, a, link["intf_b"], link["intf_a"])):
            mapping[(u, v)] = (intf_v, "rx") if u in hosts else (intf_u, "tx")
    return mapping


class MetricsCollector:
    def __init__(self, state=None):
        self.state = state or hostexec.load_state()
        self.graph = spec.build_graph()
        self.capacity = spec.capacities(self.state["bw_scale"])
        self.table = build_forwarding_table(self.graph, set(spec.HOSTS))
        self.counters = counter_map(self.state["links"], set(spec.HOSTS))
        self._last = {}
        self._rates = {}
        self._cpu_last = {}
        self._cpu = {}
        self._connections = {}
        self._ping_cache = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            self.sample_once()
            self._stop.wait(SAMPLE_INTERVAL)

    def sample_once(self):
        now = time.monotonic()
        rates = {}
        for edge, (intf, field) in self.counters.items():
            try:
                with open(f"/sys/class/net/{intf}/statistics/{field}_bytes") as f:
                    value = int(f.read())
            except OSError:
                continue
            prev = self._last.get(edge)
            self._last[edge] = (value, now)
            if prev is None or now <= prev[1]:
                continue
            mbps = (value - prev[0]) * 8 / (now - prev[1]) / 1e6
            old = self._rates.get(edge)
            rates[edge] = mbps if old is None else EMA_ALPHA * mbps + (1 - EMA_ALPHA) * old

        cpu, connections = {}, {}
        for code in spec.LOCATIONS:
            host = spec.cdn_host(code)
            try:
                ticks = read_cpu_ticks(self.state["http_servers"][host])
                prev = self._cpu_last.get(host)
                self._cpu_last[host] = (ticks, now)
                if prev and now > prev[1]:
                    cpu[host] = 100.0 * (ticks - prev[0]) / CLK_TCK / (now - prev[1])
                connections[host] = count_connections(self.state["hosts"][host]["pid"], spec.CONTENT_PORT)
            except (OSError, KeyError):
                continue
        with self._lock:
            self._rates.update(rates)
            self._cpu.update(cpu)
            self._connections.update(connections)

    def ping(self, user, cdn):
        key = (user, cdn)
        cached = self._ping_cache.get(key)
        if cached and time.monotonic() - cached[0] < PING_TTL:
            return cached[1]
        result = hostexec.run(
            spec.user_host(user),
            ["ping", "-c", str(PING_COUNT), "-i", "0.2", "-W", "1", "-q",
             spec.host_ip(spec.cdn_host(cdn))],
            timeout=PING_COUNT + 5, state=self.state,
        )
        parsed = parse_ping(result.stdout)
        self._ping_cache[key] = (time.monotonic(), parsed)
        return parsed

    def path_metrics(self, user, cdn):
        src, dst = spec.cdn_host(cdn), spec.user_host(user)
        path = forwarding_path(self.table, src, dst)
        edges = list(zip(path, path[1:]))
        with self._lock:
            free = [(max(self.capacity[e] - self._rates.get(e, 0.0), 0.0), e) for e in edges]
        available, bottleneck = min(free)
        return {
            "path": path,
            "path_cost_ms": sum(self.graph[u][v] for u, v in edges),
            "capacity_mbps": min(self.capacity[e] for e in edges),
            "available_bandwidth_mbps": available,
            "bottleneck_link": f"{bottleneck[0]}->{bottleneck[1]}",
        }

    def server_metrics(self, cdn):
        host = spec.cdn_host(cdn)
        edges = [(host, sw) for sw in self.graph[host]]
        with self._lock:
            rates = [self._rates.get(e, 0.0) for e in edges]
            cpu = self._cpu.get(host, 0.0)
            connections = self._connections.get(host, 0)
        nic = max(100.0 * r / self.capacity[e] for r, e in zip(rates, edges))
        conn_pct = min(100.0, 100.0 * connections / MAX_CONNECTIONS)
        return {
            "throughput_mbps": sum(rates),
            "cpu_pct": cpu,
            "nic_util_pct": nic,
            "connections": connections,
            "server_load_pct": min(100.0, max(cpu, nic, conn_pct)),
        }

    def snapshot(self, user):
        """Metrics from every CDN's point of view for one user location code."""
        codes = list(spec.LOCATIONS)
        with ThreadPoolExecutor(max_workers=len(codes)) as pool:
            pings = list(pool.map(lambda c: self.ping(user, c), codes))
        return {
            code: {"cdn": code, **ping, **self.path_metrics(user, code), **self.server_metrics(code)}
            for code, ping in zip(codes, pings)
        }


def probe_http_throughput(user, cdn, filename="video_360p.mp4", seconds=2, state=None):
    """Active probe: download from the CDN inside the user host and report Mbps.

    Expensive (it uses the bandwidth it measures), so it is not part of the
    periodic snapshot. The dashboard should use the speed of real user
    downloads instead and call this only for on-demand testing.
    """
    url = f"{spec.cdn_base_url(cdn)}/{filename}"
    result = hostexec.run(
        spec.user_host(user),
        ["curl", "-s", "-o", "/dev/null", "-w", "%{speed_download}", "--max-time", str(seconds), url],
        timeout=seconds + 5, state=state,
    )
    try:
        return float(result.stdout) * 8 / 1e6
    except ValueError:
        return 0.0


def _fmt(value, unit, digits=1):
    return "   n/a  " if value is None else f"{value:7.{digits}f}{unit}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("user", help="user location code: " + ", ".join(spec.LOCATIONS))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    hostexec.require_root()
    user = args.user.upper()
    if user not in spec.LOCATIONS:
        raise SystemExit(f"unknown user location {args.user}")

    collector = MetricsCollector()
    collector.start()
    time.sleep(2 * SAMPLE_INTERVAL)
    try:
        while True:
            print(f"\nuser {user} ({spec.user_host(user)})")
            print("CDN   latency   path     avail BW  throughput   load    cpu   path")
            for code, m in collector.snapshot(user).items():
                print(f"{code}  {_fmt(m['latency_ms'], ' ms')} {_fmt(m['path_cost_ms'], ' ms', 0)} "
                      f"{_fmt(m['available_bandwidth_mbps'], '')} {_fmt(m['throughput_mbps'], '')} "
                      f"{_fmt(m['server_load_pct'], '%')}{_fmt(m['cpu_pct'], '%')}  "
                      f"{' > '.join(m['path'])}")
            if args.once:
                break
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        collector.stop()


if __name__ == "__main__":
    main()
