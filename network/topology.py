#!/usr/bin/env python3
"""Build the CDN topology in Mininet, start the CDN HTTP servers, publish state.

    sudo python3 -m network.topology               # start, then open the Mininet CLI
    sudo python3 -m network.topology --no-cli      # start and keep running (for Flask)
    sudo python3 -m network.topology --check       # start, test pingall + HTTP, exit

All switches run with no controller. The topology has loops, so instead of
flooding, static per-destination flow rules are installed from the Dijkstra
distances in routing.py, and all ARP entries are static.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from functools import partial

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import error, info, setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch

from . import hostexec, spec, traffic
from .manifest import iter_replicas, load_manifest
from .routing import build_forwarding_table


def build_network(bw_scale):
    net = Mininet(controller=None, switch=partial(OVSSwitch, failMode="secure"),
                  link=TCLink, autoSetMacs=False, autoStaticArp=False)
    nodes = {}
    for name in spec.SWITCHES:
        nodes[name] = net.addSwitch(name)
    for name in spec.HOSTS:
        nodes[name] = net.addHost(name, ip=f"{spec.host_ip(name)}/24", mac=spec.host_mac(name))
    links = []
    for a, b, bw, delay in spec.LINKS:
        link = net.addLink(nodes[a], nodes[b], bw=bw * bw_scale, delay=f"{delay}ms")
        links.append({"a": a, "b": b, "bw_mbps": bw * bw_scale, "delay_ms": delay,
                      "intf_a": link.intf1.name, "intf_b": link.intf2.name})
    return net, links


def configure_hosts(net):
    for host in net.hosts:
        host.cmd("sysctl -w net.ipv6.conf.all.disable_ipv6=1 "
                 "net.ipv4.conf.all.rp_filter=0 net.ipv4.conf.default.rp_filter=0")
        for intf in host.intfList():
            host.cmd(f"sysctl -w net.ipv4.conf.{intf.name}.rp_filter=0")


def install_static_arp(net):
    for src in net.hosts:
        for dst in net.hosts:
            if src is not dst:
                src.setARP(dst.IP(), spec.host_mac(dst.name))


def configure_multihomed(net, table):
    """A host with two links (cdn-qta) shares one IP and MAC across both
    interfaces; per-destination host routes pick the interface that Dijkstra
    says is on the shortest path."""
    for host in net.hosts:
        intfs = host.intfList()
        if len(intfs) < 2:
            continue
        for extra in intfs[1:]:
            extra.setMAC(spec.host_mac(host.name))
        for dst in net.hosts:
            if dst is host:
                continue
            next_node = net.get(table[host.name][dst.name])
            intf = host.connectionsTo(next_node)[0][0]
            host.cmd(f"ip route replace {dst.IP()}/32 dev {intf.name} src {host.IP()}")
            host.cmd(f"ip neigh replace {dst.IP()} lladdr {spec.host_mac(dst.name)} "
                     f"dev {intf.name} nud permanent")


def install_flows(net, table):
    for sw in net.switches:
        sw.dpctl("del-flows")
        for dst in net.hosts:
            next_node = net.get(table[sw.name][dst.name])
            intf = sw.connectionsTo(next_node)[0][0]
            sw.dpctl("add-flow",
                     f"priority=100,dl_dst={spec.host_mac(dst.name)},actions=output:{sw.ports[intf]}")


def start_http_servers(net):
    spec.LOG_DIR.mkdir(exist_ok=True)
    servers = {}
    for code in spec.LOCATIONS:
        host = net.get(spec.cdn_host(code))
        root = spec.cdn_dir(code)
        root.mkdir(parents=True, exist_ok=True)
        log = open(spec.LOG_DIR / f"{host.name}.log", "ab")
        proc = host.popen(
            ["python3", "-m", "http.server", str(spec.CONTENT_PORT),
             "--bind", host.IP(), "--directory", str(root)],
            stdout=log, stderr=subprocess.STDOUT,
        )
        servers[host.name] = proc
    time.sleep(1)
    for name, proc in servers.items():
        if proc.poll() is not None:
            error(f"HTTP server on {name} exited; see logs/{name}.log\n")
    return servers


def write_state(net, links, servers, bw_scale):
    state = {
        "mininet_pid": os.getpid(),
        "bw_scale": bw_scale,
        "port": spec.CONTENT_PORT,
        "hosts": {
            h.name: {"pid": h.pid, "ip": h.IP(), "mac": spec.host_mac(h.name),
                     "role": spec.HOSTS[h.name][1], "location": spec.HOSTS[h.name][2]}
            for h in net.hosts
        },
        "links": links,
        "http_servers": {name: proc.pid for name, proc in servers.items()},
    }
    spec.RUN_DIR.mkdir(exist_ok=True)
    tmp = spec.STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, spec.STATE_PATH)


def run_checks(net):
    ok = True
    info("*** pingall\n")
    if net.pingAll() > 0:
        ok = False
    info("*** HTTP reachability (HEAD of one real file per CDN, from every user)\n")
    urls = {}
    if spec.MANIFEST_PATH.exists():
        for _content, _res, code, url in iter_replicas(load_manifest()):
            urls.setdefault(code, url)
    else:
        info("manifest.json missing, run setup_content.py; testing directory listing only\n")
    for user_code in spec.LOCATIONS:
        user = net.get(spec.user_host(user_code))
        results = []
        for cdn_code in spec.LOCATIONS:
            url = urls.get(cdn_code, spec.cdn_base_url(cdn_code) + "/")
            status = user.cmd(f"curl -sI -o /dev/null -w '%{{http_code}}' --max-time 10 {url}").strip()
            results.append(f"{cdn_code}={status}")
            ok = ok and status == "200"
        info(f"{user.name}: {'  '.join(results)}\n")
    info(f"*** check {'PASSED' if ok else 'FAILED'}\n")
    return ok


def wait_until_signalled():
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(True))
    try:
        while not stop:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


def shutdown(net, servers):
    try:
        traffic.stop()
    except Exception:
        pass
    for proc in servers.values():
        proc.terminate()
    spec.STATE_PATH.unlink(missing_ok=True)
    net.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bw-scale", type=float, default=spec.DEFAULT_BW_SCALE,
                        help=f"multiply all link bandwidths (default {spec.DEFAULT_BW_SCALE}; 1.0 = full size)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--no-cli", action="store_true", help="run until Ctrl-C / SIGTERM, no Mininet CLI")
    mode.add_argument("--check", action="store_true", help="run pingall + HTTP checks, then exit")
    args = parser.parse_args()

    hostexec.require_root()
    setLogLevel("info")

    if spec.STATE_PATH.exists():
        try:
            os.kill(hostexec.load_state()["mininet_pid"], 0)
            sys.exit("Network already running. Stop it first (Ctrl-C / exit in its terminal).")
        except (ProcessLookupError, KeyError, json.JSONDecodeError):
            spec.STATE_PATH.unlink()

    net, links = build_network(args.bw_scale)
    servers = {}
    exit_code = 0
    try:
        net.start()
        configure_hosts(net)
        table = build_forwarding_table(spec.build_graph(), set(spec.HOSTS))
        install_static_arp(net)
        configure_multihomed(net, table)
        install_flows(net, table)
        servers = start_http_servers(net)
        write_state(net, links, servers, args.bw_scale)
        info(f"*** CDN servers listening on :{spec.CONTENT_PORT}; state in {spec.STATE_PATH}\n")
        if args.check:
            exit_code = 0 if run_checks(net) else 1
        elif args.no_cli:
            wait_until_signalled()
        else:
            CLI(net)
    finally:
        shutdown(net, servers)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
