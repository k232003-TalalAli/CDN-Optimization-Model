"""CDN selection: manifest lookup -> Dijkstra -> weighted score -> CDN -> resolution.

Pure functions. The caller supplies the manifest, the graph and a metrics
snapshot ({cdn: metrics}) so everything here can be tested without Mininet.
"""
from network import spec

from . import scoring
from .dijkstra import shortest_path

# Used as the latency of a CDN whose pings all failed, so it ranks last.
UNREACHABLE_LATENCY_MS = 1000.0


def replica_map(entry):
    """Manifest entry -> {cdn: {resolution_or_None: url}}."""
    out = {}
    if "resolutions" in entry:
        for resolution, info in entry["resolutions"].items():
            for cdn, url in info["replicas"].items():
                out.setdefault(cdn, {})[resolution] = url
    else:
        for cdn, url in entry["replicas"].items():
            out.setdefault(cdn, {})[None] = url
    return out


def pick_resolution(bitrates, available_mbps):
    """Highest resolution whose bitrate fits; the lowest one if none fit.

    Returns (resolution, fits)."""
    fitting = [r for r, mbps in bitrates.items() if mbps <= available_mbps]
    if fitting:
        return max(fitting, key=bitrates.get), True
    return min(bitrates, key=bitrates.get), False


def rank_cdns(graph, user, snapshot, cdns):
    """Dijkstra path + weighted score for each candidate CDN."""
    hosts = set(spec.HOSTS)
    candidates, paths = {}, {}
    for cdn in cdns:
        m = snapshot[cdn]
        paths[cdn], cost = shortest_path(graph, spec.user_host(user), spec.cdn_host(cdn), no_transit=hosts)
        latency = m["latency_ms"]
        candidates[cdn] = {
            "latency_ms": latency if latency is not None else UNREACHABLE_LATENCY_MS,
            "available_bandwidth_mbps": m["available_bandwidth_mbps"],
            "capacity_mbps": m["capacity_mbps"],
            "server_load_pct": m["server_load_pct"],
            "path_cost_ms": cost,
        }
    scores = scoring.score(candidates)
    return {cdn: {**candidates[cdn], **scores[cdn], "path": paths[cdn]} for cdn in cdns}


def _fail(decision, code, message):
    decision["ok"] = False
    decision["error"] = {"code": code, "message": message}
    return decision


def decide(manifest, content, user, snapshot, graph,
           cdn_mode="auto", cdn=None, quality_mode="auto", quality=None):
    """Full decision for one request.

    Always returns the ranking so the UI can show it; `ok` is False with an
    `error` when the request cannot be served (unknown content, manual CDN
    without a replica, manual quality missing on the chosen CDN).
    """
    decision = {"ok": True, "error": None, "warnings": [], "content": content, "user": user,
                "cdn_mode": cdn_mode, "quality_mode": quality_mode, "cdn": None, "best_cdn": None,
                "resolution": None, "url": None, "ranking": {}, "available_resolutions": [],
                "path": [], "path_cost_ms": None, "reason": ""}
    entry = manifest.get(content)
    if entry is None:
        return _fail(decision, "unknown_content", f"{content} is not in the manifest.")

    replicas = replica_map(entry)
    ranking = rank_cdns(graph, user, snapshot, list(replicas))
    best = min(ranking, key=lambda c: ranking[c]["score"])
    decision["ranking"], decision["best_cdn"] = ranking, best

    if cdn_mode == "manual":
        if cdn not in replicas:
            holders = ", ".join(sorted(replicas))
            return _fail(decision, "cdn_no_replica", f"{cdn} does not hold {content}. Replicas: {holders}.")
        chosen = cdn
        reason = f"Manual selection of {chosen}."
        if chosen != best:
            reason += f" The optimizer would have chosen {best} (score {ranking[best]['score']:.3f})."
    else:
        chosen = best
        others = sorted((ranking[c]["score"], c) for c in ranking if c != chosen)
        reason = f"{chosen} has the lowest score ({ranking[chosen]['score']:.3f})"
        reason += f"; next is {others[0][1]} ({others[0][0]:.3f})." if others else "; it is the only replica."

    decision["cdn"] = chosen
    decision["path"] = ranking[chosen]["path"]
    decision["path_cost_ms"] = ranking[chosen]["path_cost_ms"]
    available = ranking[chosen]["available_bandwidth_mbps"]
    resolutions = replicas[chosen]

    if "resolutions" not in entry:
        decision["url"] = resolutions[None]
        decision["reason"] = reason
        return decision

    bitrates = {r: entry["resolutions"][r]["bitrate_mbps"] for r in resolutions}
    decision["available_resolutions"] = sorted(bitrates, key=bitrates.get)

    if quality_mode == "manual":
        if quality not in bitrates:
            holders = sorted(c for c, rs in replicas.items() if quality in rs)
            where = f"Only {', '.join(holders)} hold it." if holders else "No CDN holds it."
            decision["reason"] = reason
            return _fail(decision, "quality_unavailable",
                         f"{quality} is not available on {chosen}. {where}")
        resolution = quality
        reason += f" Manual quality {resolution}."
        if bitrates[resolution] > available:
            decision["warnings"].append(
                f"{resolution} needs {bitrates[resolution]:g} Mbps but only {available:.1f} Mbps is available; playback may stall.")
    else:
        resolution, fits = pick_resolution(bitrates, available)
        if fits:
            reason += f" {resolution} ({bitrates[resolution]:g} Mbps) is the highest quality that fits {available:.1f} Mbps available."
        else:
            decision["warnings"].append(
                f"Even {resolution} ({bitrates[resolution]:g} Mbps) exceeds the {available:.1f} Mbps available.")
            reason += f" {resolution} is the lowest quality on {chosen}."

    decision["resolution"] = resolution
    decision["url"] = resolutions[resolution]
    decision["reason"] = reason
    return decision
