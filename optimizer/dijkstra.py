"""Dijkstra's shortest-path algorithm, implemented manually.

Graph format: {node: {neighbour: cost}}. Edge cost is one-way link latency in ms.

`no_transit` lists nodes a path may end at but never pass through. Hosts go
here, so a multi-homed CDN host (QTA) is never used as a router between switches.
"""
import heapq

INF = float("inf")


def dijkstra(graph, source, no_transit=frozenset()):
    dist = {source: 0}
    prev = {}
    heap = [(0, source)]
    done = set()
    while heap:
        d, u = heapq.heappop(heap)
        if u in done:
            continue
        done.add(u)
        if u != source and u in no_transit:
            continue
        for v, cost in graph[u].items():
            nd = d + cost
            if nd < dist.get(v, INF):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(heap, (nd, v))
    return dist, prev


def shortest_path(graph, source, target, no_transit=frozenset()):
    """Return (path, total_cost). Unreachable targets give ([], inf)."""
    dist, prev = dijkstra(graph, source, no_transit)
    if target not in dist:
        return [], INF
    path = [target]
    while path[-1] != source:
        path.append(prev[path[-1]])
    path.reverse()
    return path, dist[target]
