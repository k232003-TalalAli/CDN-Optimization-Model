"""Weighted CDN score. Lower is better.

    score = 0.40 * latency_n + 0.30 * bandwidth_penalty_n + 0.20 * load_n + 0.10 * path_n

Every term is normalised to 0..1 against the candidate set (the CDNs that hold
the requested content):

    latency_n           = latency / max latency among candidates
    bandwidth_penalty_n = 1 - available_bw / max link capacity among candidates
                          (available_bw is capped to [0, capacity])
    load_n              = server_load_pct / 100
    path_n              = Dijkstra path cost / max path cost among candidates
"""

WEIGHTS = {"latency": 0.40, "bandwidth": 0.30, "load": 0.20, "path": 0.10}


def _ratio(value, maximum):
    return value / maximum if maximum > 0 else 0.0


def normalise(candidates):
    """candidates: {cdn: {latency_ms, available_bandwidth_mbps, capacity_mbps,
    server_load_pct, path_cost_ms}} -> {cdn: {latency, bandwidth, load, path}}"""
    max_latency = max(c["latency_ms"] for c in candidates.values())
    max_capacity = max(c["capacity_mbps"] for c in candidates.values())
    max_path = max(c["path_cost_ms"] for c in candidates.values())
    out = {}
    for cdn, c in candidates.items():
        available = min(max(c["available_bandwidth_mbps"], 0.0), max_capacity)
        out[cdn] = {
            "latency": _ratio(c["latency_ms"], max_latency),
            "bandwidth": 1.0 - _ratio(available, max_capacity),
            "load": min(max(c["server_load_pct"], 0.0), 100.0) / 100.0,
            "path": _ratio(c["path_cost_ms"], max_path),
        }
    return out


def score(candidates):
    """Return {cdn: {"score", "normalised", "weighted"}}."""
    if not candidates:
        return {}
    result = {}
    for cdn, parts in normalise(candidates).items():
        weighted = {key: WEIGHTS[key] * parts[key] for key in WEIGHTS}
        result[cdn] = {"score": sum(weighted.values()), "normalised": parts, "weighted": weighted}
    return result
