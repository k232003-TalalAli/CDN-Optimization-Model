"""Controlled background traffic (congestion) between CDN hosts.

    sudo python3 -m network.traffic start khi 45          # 45 Mbps UDP out of cdn-khi
    sudo python3 -m network.traffic start khi 45 --sink isb
    sudo python3 -m network.traffic status
    sudo python3 -m network.traffic stop khi              # or: stop all

Why CDN -> CDN: serving content is upload from the CDN, so a UDP flow leaving
cdn-X eats that server's uplink (and the links along its path) exactly where
real client traffic would compete. The sink only receives, and its downlink
direction is unused by content serving, so it is not disturbed. By default the
sink is the nearest other CDN by Dijkstra path cost.
"""
import argparse
import json
import os
import signal
import time

from optimizer.dijkstra import shortest_path

from . import hostexec, spec

IPERF_BASE_PORT = 5300


def _load():
    try:
        with open(spec.TRAFFIC_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _save(data):
    spec.RUN_DIR.mkdir(exist_ok=True)
    with open(spec.TRAFFIC_PATH, "w") as f:
        json.dump(data, f, indent=2)


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _kill(pid):
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def default_sink(cdn):
    graph = spec.build_graph()
    hosts = set(spec.HOSTS)
    src = spec.cdn_host(cdn)
    cost, sink = min(
        (shortest_path(graph, src, spec.cdn_host(other), hosts)[1], other)
        for other in spec.LOCATIONS if other != cdn
    )
    return sink


def _code(value):
    code = value.upper()
    if code not in spec.LOCATIONS:
        raise ValueError(f"unknown CDN '{value}', choose from {', '.join(spec.LOCATIONS)}")
    return code


def start(cdn, mbps, sink=None):
    cdn = _code(cdn)
    sink = _code(sink) if sink else default_sink(cdn)
    if sink == cdn:
        raise ValueError("sink must be a different CDN")
    if mbps <= 0:
        raise ValueError("mbps must be positive")
    stop(cdn)

    state = hostexec.load_state()
    port = IPERF_BASE_PORT + list(spec.LOCATIONS).index(cdn)
    sink_host, src_host = spec.cdn_host(sink), spec.cdn_host(cdn)

    server = hostexec.spawn(sink_host, ["iperf3", "-s", "-p", str(port)],
                            spec.LOG_DIR / f"iperf-server-{cdn.lower()}.log", state)
    time.sleep(0.5)
    client = hostexec.spawn(
        src_host,
        ["iperf3", "-c", spec.host_ip(sink_host), "-p", str(port),
         "-u", "-b", f"{mbps:g}M", "-t", "86400"],
        spec.LOG_DIR / f"iperf-client-{cdn.lower()}.log", state,
    )
    time.sleep(0.5)
    if client.poll() is not None:
        _kill(server.pid)
        raise RuntimeError(
            f"iperf3 client exited immediately; see logs/iperf-client-{cdn.lower()}.log "
            "(is iperf3 installed?)"
        )

    data = _load()
    data[cdn] = {"sink": sink, "mbps": mbps, "port": port,
                 "server_pid": server.pid, "client_pid": client.pid,
                 "started": time.time()}
    _save(data)
    return data[cdn]


def stop(cdn=None):
    data = _load()
    targets = list(data) if cdn is None else [_code(cdn)]
    for code in targets:
        entry = data.pop(code, None)
        if entry:
            _kill(entry["client_pid"])
            _kill(entry["server_pid"])
    _save(data)
    return targets


def status():
    return {
        code: {**entry, "running": _alive(entry["client_pid"])}
        for code, entry in _load().items()
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p_start = sub.add_parser("start")
    p_start.add_argument("cdn")
    p_start.add_argument("mbps", type=float)
    p_start.add_argument("--sink")
    p_stop = sub.add_parser("stop")
    p_stop.add_argument("cdn", nargs="?", default="all")
    sub.add_parser("status")
    args = parser.parse_args()
    hostexec.require_root()

    if args.command == "start":
        entry = start(args.cdn, args.mbps, args.sink)
        print(f"congesting {args.cdn.upper()} with {args.mbps:g} Mbps UDP -> cdn-{entry['sink'].lower()}")
    elif args.command == "stop":
        stop(None if args.cdn.lower() == "all" else args.cdn)
        print("stopped", args.cdn)
    else:
        info = status()
        if not info:
            print("no background traffic running")
        for code, entry in info.items():
            state = "running" if entry["running"] else "dead"
            print(f"{code}: {entry['mbps']:g} Mbps -> {entry['sink']} ({state})")


if __name__ == "__main__":
    main()
