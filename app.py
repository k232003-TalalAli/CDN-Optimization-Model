#!/usr/bin/env python3
"""Flask front end for the CDN optimizer.

    sudo python3 -m network.topology --no-cli     # terminal 1
    sudo python3 app.py                           # terminal 2, then open http://<vm-ip>:5000

Root is needed because metrics, traffic and streaming enter the Mininet host
namespaces. Browser sessions are in-memory profiles keyed by a `sid` carried in
the URL, so two tabs of one browser can be two different users.
"""
import argparse
import os
import secrets
import threading
import time
from collections import deque

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for

from network import hostexec, spec, stream, traffic
from network.manifest import load_manifest
from network.metrics import MetricsCollector
from optimizer import optimizer

SNAPSHOT_TTL_S = 1.0
WARMUP_S = 2.5
DEFAULT_CONTENT = "video.mp4"


class NetworkService:
    """Attaches lazily to the running Mininet network and caches per-user snapshots."""

    def __init__(self, collector_factory=None, pid_probe=None):
        self._factory = collector_factory or self._real_collector
        self._pid_probe = pid_probe or (lambda: hostexec.load_state()["mininet_pid"])
        self._lock = threading.Lock()
        self._collector = None
        self._pid = None
        self._cache = {}
        self._user_locks = {code: threading.Lock() for code in spec.LOCATIONS}

    @staticmethod
    def _real_collector():
        collector = MetricsCollector()
        collector.start()
        time.sleep(WARMUP_S)
        return collector

    def collector(self):
        pid = self._pid_probe()
        with self._lock:
            if self._collector is None or pid != self._pid:
                if self._collector is not None:
                    self._collector.stop()
                self._collector = self._factory()
                self._pid, self._cache = pid, {}
            return self._collector

    def snapshot(self, user):
        collector = self.collector()
        with self._user_locks[user]:
            cached = self._cache.get(user)
            if cached and time.monotonic() - cached[0] < SNAPSHOT_TTL_S:
                return cached[1]
            snap = collector.snapshot(user)
            self._cache[user] = (time.monotonic(), snap)
            return snap

    def bw_scale(self):
        return self.collector().state["bw_scale"]


def default_prefs(code):
    return {"content": DEFAULT_CONTENT, "cdn_mode": "auto", "cdn": code,
            "quality_mode": "auto", "quality": "720p"}


def uplink_mbps(code, bw_scale):
    caps = spec.capacities(bw_scale)
    host = spec.cdn_host(code)
    return max(caps[(host, sw)] for sw in spec.build_graph()[host])


def create_app(network=None, open_stream=stream.open_stream, traffic_api=traffic):
    app = Flask(__name__)
    app.secret_key = os.environ.get("CDN_SECRET", "cdn-optimizer-dev-key")
    network = network or NetworkService()
    graph = spec.build_graph()
    profiles, transfers = {}, stream.Transfers()
    recent = deque(maxlen=20)

    def profile():
        sid = request.args.get("sid") or (request.get_json(silent=True) or {}).get("sid")
        return sid, profiles.get(sid)

    def api(handler):
        def wrapper(*args, **kwargs):
            sid, prof = profile()
            if prof is None:
                return jsonify(ok=False, error={"code": "no_session", "message": "Select a location first."}), 401
            try:
                return handler(sid, prof, *args, **kwargs)
            except hostexec.NetworkNotRunning as exc:
                return jsonify(ok=False, error={"code": "network_down", "message": str(exc)}), 503
            except (PermissionError, SystemExit) as exc:
                return jsonify(ok=False, error={"code": "no_root", "message": str(exc) or "Run app.py with sudo."}), 503
        wrapper.__name__ = handler.__name__
        return wrapper

    def current_decision(prof):
        p = prof["prefs"]
        return optimizer.decide(load_manifest(), p["content"], prof["code"], network.snapshot(prof["code"]), graph,
                                p["cdn_mode"], p["cdn"], p["quality_mode"], p["quality"])

    def public_decision(d):
        keep = ("ok", "error", "warnings", "content", "cdn_mode", "quality_mode", "cdn", "best_cdn",
                "resolution", "url", "available_resolutions", "path", "path_cost_ms", "reason")
        return {k: d[k] for k in keep}

    def server_rows(prof, snapshot, decision):
        running = traffic_api.status()
        rows = []
        for code, name in spec.LOCATIONS.items():
            m = snapshot[code]
            ranked = decision["ranking"].get(code)
            injected = running.get(code)
            rows.append({
                "cdn": code, "name": f"{name} CDN", "has_replica": ranked is not None,
                "active": code == decision["cdn"],
                "latency_ms": m["latency_ms"], "available_bandwidth_mbps": m["available_bandwidth_mbps"],
                "capacity_mbps": m["capacity_mbps"], "throughput_mbps": m["throughput_mbps"],
                "server_load_pct": m["server_load_pct"], "path_cost_ms": m["path_cost_ms"],
                "score": ranked["score"] if ranked else None,
                "weighted": ranked["weighted"] if ranked else None,
                "path": ranked["path"] if ranked else m["path"],
                "injected_mbps": injected["mbps"] if injected and injected["running"] else 0,
            })
        return rows

    @app.get("/")
    def index():
        return render_template("index.html", locations=spec.LOCATIONS)

    @app.post("/")
    def choose_location():
        code = request.form.get("location", "").upper()
        if code not in spec.LOCATIONS:
            return redirect(url_for("index"))
        sid = secrets.token_urlsafe(8)
        profiles[sid] = {"code": code, "location": spec.LOCATIONS[code], "host": spec.user_host(code),
                         "ip": spec.host_ip(spec.user_host(code)), "prefs": default_prefs(code), "last": None}
        session["sid"] = sid
        return redirect(url_for("dashboard", sid=sid))

    @app.get("/dashboard")
    def dashboard():
        sid = request.args.get("sid") or session.get("sid")
        prof = profiles.get(sid)
        if prof is None:
            return redirect(url_for("index"))
        session["sid"] = sid
        return render_template("dashboard.html", sid=sid, prof=prof, locations=spec.LOCATIONS)

    @app.get("/api/content")
    @api
    def api_content(sid, prof):
        items = []
        for name, entry in load_manifest().items():
            replicas = optimizer.replica_map(entry)
            item = {"name": name, "type": entry["type"], "mime": entry["mime"], "replicas": sorted(replicas)}
            if "resolutions" in entry:
                item["resolutions"] = {r: {"bitrate_mbps": i["bitrate_mbps"], "size_bytes": i["size_bytes"],
                                           "replicas": sorted(i["replicas"])}
                                       for r, i in entry["resolutions"].items()}
            else:
                item["size_bytes"] = entry["size_bytes"]
            items.append(item)
        return jsonify(ok=True, content=items)

    @app.get("/api/metrics")
    @api
    def api_metrics(sid, prof):
        snapshot = network.snapshot(prof["code"])
        decision = current_decision(prof)
        return jsonify(
            ok=True, time=time.time(), bw_scale=network.bw_scale(), prefs=prof["prefs"],
            user={"code": prof["code"], "location": prof["location"], "host": prof["host"], "ip": prof["ip"]},
            selection=public_decision(decision),
            servers=server_rows(prof, snapshot, decision),
            transfers=transfers.active_for(sid), last=prof["last"], recent=list(recent)[::-1],
        )

    def update_prefs(prof, allowed):
        body = request.get_json(silent=True) or {}
        for key, valid in allowed.items():
            if key in body:
                if not valid(body[key]):
                    return jsonify(ok=False, error={"code": "bad_value", "message": f"Invalid {key}."}), 400
                prof["prefs"][key] = body[key]
        return jsonify(ok=True, prefs=prof["prefs"])

    @app.post("/api/select-content")
    @api
    def api_select_content(sid, prof):
        return update_prefs(prof, {"content": lambda v: v in load_manifest()})

    @app.post("/api/select-cdn")
    @api
    def api_select_cdn(sid, prof):
        return update_prefs(prof, {"cdn_mode": lambda v: v in ("auto", "manual"),
                                   "cdn": lambda v: v in spec.LOCATIONS})

    @app.post("/api/select-quality")
    @api
    def api_select_quality(sid, prof):
        return update_prefs(prof, {"quality_mode": lambda v: v in ("auto", "manual"),
                                   "quality": lambda v: isinstance(v, str) and len(v) < 10})

    @app.post("/api/optimize")
    @api
    def api_optimize(sid, prof):
        decision = current_decision(prof)
        out = public_decision(decision)
        out["ranking"] = {c: {"score": r["score"], "latency_ms": r["latency_ms"], "weighted": r["weighted"],
                              "available_bandwidth_mbps": r["available_bandwidth_mbps"]}
                          for c, r in decision["ranking"].items()}
        if decision["ok"]:
            query = f"content={decision['content']}&cdn={decision['cdn']}&sid={sid}"
            if decision["resolution"]:
                query += f"&res={decision['resolution']}"
            out["stream_url"] = url_for("stream_content") + "?" + query
            out["download_url"] = out["stream_url"] + "&download=1"
            entry = {"time": time.time(), "user": prof["code"], "content": decision["content"],
                     "cdn": decision["cdn"], "resolution": decision["resolution"],
                     "mode": decision["cdn_mode"], "reason": decision["reason"]}
            recent.append(entry)
            prof["last"] = entry
        return jsonify(out), 200 if decision["ok"] else 409

    @app.get("/stream")
    def stream_content():
        sid, prof = profile()
        if prof is None:
            return "No session", 401
        name, cdn, res = request.args.get("content"), request.args.get("cdn"), request.args.get("res")
        entry = load_manifest().get(name)
        if entry is None:
            return "Unknown content", 404
        replicas = optimizer.replica_map(entry).get(cdn, {})
        if (res or None) not in replicas:
            return "No such replica", 404
        info = entry["resolutions"][res] if res else entry
        size = info.get("size_bytes")
        url = replicas[res or None]
        rng = stream.parse_range(request.headers.get("Range"), size)
        try:
            proc = open_stream(prof["code"], url, *(rng or (None, None)))
        except hostexec.NetworkNotRunning as exc:
            return str(exc), 503
        tid = transfers.begin(sid, prof["code"], cdn, name, res)

        def relay():
            try:
                while True:
                    chunk = proc.stdout.read(stream.CHUNK)
                    if not chunk:
                        break
                    transfers.add(tid, len(chunk))
                    yield chunk
            finally:
                proc.kill()
                proc.wait()
                transfers.finish(tid)

        headers = {"Accept-Ranges": "bytes"}
        status = 200
        if size is not None:
            start, end = rng or (0, size - 1)
            headers["Content-Length"] = str(end - start + 1)
            if rng:
                status, headers["Content-Range"] = 206, f"bytes {start}-{end}/{size}"
        if request.args.get("download"):
            headers["Content-Disposition"] = f'attachment; filename="{url.rsplit("/", 1)[1]}"'
        return Response(relay(), status=status, headers=headers, mimetype=entry["mime"])

    @app.route("/api/congestion", methods=["GET", "POST"])
    @api
    def api_congestion(sid, prof):
        scale = network.bw_scale()
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            try:
                if body.get("action") == "stop":
                    traffic_api.stop(None if body.get("cdn") in (None, "all") else body["cdn"])
                else:
                    traffic_api.start(body["cdn"], float(body["mbps"]))
            except (ValueError, KeyError, RuntimeError) as exc:
                return jsonify(ok=False, error={"code": "traffic_failed", "message": str(exc)}), 400
        running = traffic_api.status()
        return jsonify(ok=True, cdns={
            code: {"name": f"{name} CDN", "uplink_mbps": uplink_mbps(code, scale),
                   "mbps": running[code]["mbps"] if code in running and running[code]["running"] else 0}
            for code, name in spec.LOCATIONS.items()})

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    hostexec.require_root()
    create_app().run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
