"""Run the dashboard WITHOUT Mininet or root, for UI development.

    python3 -m tests.dev_server            # http://localhost:5000

Network conditions come from a deterministic model of the real topology
(spec.LINKS + Dijkstra routing): injected congestion eats the CDN uplink,
which lowers available bandwidth, raises load and adds queueing delay.
Streams are served from small local files. The real system uses
network.metrics / network.traffic instead; see app.py.
"""
import argparse
import json
import math
import subprocess
import tempfile
import time
from pathlib import Path

import app as app_module
from network import spec
from network.routing import build_forwarding_table, forwarding_path
from optimizer.dijkstra import shortest_path
import setup_content


class FakeTraffic:
    def __init__(self):
        self.flows = {}

    def start(self, cdn, mbps, sink=None):
        code = cdn.upper()
        if code not in spec.LOCATIONS:
            raise ValueError(f"unknown CDN '{cdn}'")
        self.flows[code] = {"mbps": mbps, "sink": "?", "running": True}

    def stop(self, cdn=None):
        for code in (list(self.flows) if cdn is None else [cdn.upper()]):
            self.flows.pop(code, None)

    def status(self):
        return dict(self.flows)


class FakeCollector:
    def __init__(self, traffic, bw_scale=spec.DEFAULT_BW_SCALE):
        self.traffic = traffic
        self.state = {"bw_scale": bw_scale, "mininet_pid": 1}
        self.graph = spec.build_graph()
        self.capacity = spec.capacities(bw_scale)
        self.table = build_forwarding_table(self.graph, set(spec.HOSTS))

    def start(self): pass
    def stop(self): pass

    def snapshot(self, user):
        t = time.time()
        out = {}
        for code in spec.LOCATIONS:
            path = forwarding_path(self.table, spec.cdn_host(code), spec.user_host(user))
            edges = list(zip(path, path[1:]))
            cost = sum(self.graph[u][v] for u, v in edges)
            uplink = max(self.capacity[(spec.cdn_host(code), sw)] for sw in self.graph[spec.cdn_host(code)])
            injected = self.traffic.flows.get(code, {}).get("mbps", 0.0)
            util = min(injected / uplink, 1.0)
            wobble = math.sin(t / 1.7 + len(code)) * 0.5
            free = min(self.capacity[e] - (injected if i == 0 else 0) for i, e in enumerate(edges))
            out[code] = {
                "cdn": code, "latency_ms": 2 * cost + 40 * util ** 3 + 1.5 + wobble, "jitter_ms": 0.3, "loss_pct": 0.0,
                "path": path, "path_cost_ms": cost, "capacity_mbps": min(self.capacity[e] for e in edges),
                "available_bandwidth_mbps": max(free + wobble / 4, 0.0), "bottleneck_link": "-",
                "throughput_mbps": injected + 0.2 + wobble / 5, "cpu_pct": 2.0, "nic_util_pct": 100 * util,
                "connections": 0, "server_load_pct": min(100.0, max(100 * util, 4 + wobble)),
            }
        return out


def make_manifest(directory):
    manifest = {}
    directory.mkdir(parents=True, exist_ok=True)
    for content, plan in setup_content.REPLICATION.items():
        ext = Path(content).suffix
        if isinstance(plan, dict):
            resolutions = {}
            for res, cdns in plan.items():
                name = setup_content.stem_name(content, res)
                width, height, mbps = setup_content.RESOLUTIONS[res]
                if not (directory / name).exists():
                    setup_content.make_video(directory / name, width // 4, height // 4, mbps, 4)
                resolutions[res] = {"bitrate_mbps": mbps, "size_bytes": (directory / name).stat().st_size,
                                    "replicas": setup_content.replica_urls(content, res, cdns)}
            manifest[content] = {"type": "video", "mime": "video/mp4", "resolutions": resolutions}
        else:
            if not (directory / content).exists():
                (setup_content.make_image if ext == ".jpg" else setup_content.make_pdf)(directory / content)
            kind = "image" if ext == ".jpg" else "document"
            manifest[content] = {"type": kind, "mime": setup_content.MIME[ext],
                                 "size_bytes": (directory / content).stat().st_size,
                                 "replicas": setup_content.replica_urls(content, None, plan)}
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    media = Path(tempfile.gettempdir()) / "cdn-dev-media"
    manifest = make_manifest(media)
    app_module.load_manifest = lambda: manifest

    def local_stream(user_code, url, start=None, end=None):
        path = media / url.rsplit("/", 1)[1]
        data = path.read_bytes()
        data = data[start or 0:(None if end is None else end + 1)]
        return subprocess.Popen(["cat"], stdin=subprocess.PIPE, stdout=subprocess.PIPE) if False else _Pipe(data)

    traffic = FakeTraffic()
    network = app_module.NetworkService(lambda: FakeCollector(traffic), pid_probe=lambda: 1)
    flask_app = app_module.create_app(network, open_stream=local_stream, traffic_api=traffic)
    flask_app.run(host="127.0.0.1", port=args.port, threaded=True)


class _Pipe:
    """Minimal stand-in for the curl process: .stdout.read(n), .kill(), .wait()."""
    def __init__(self, data):
        import io
        self.stdout = io.BytesIO(data)
    def kill(self): pass
    def wait(self): pass


if __name__ == "__main__":
    main()
