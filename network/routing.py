"""Static forwarding tables for the Mininet switches (and multi-homed hosts).

The topology has loops, so switches are not left to flood. For every
destination host, Dijkstra distances are computed once and every node forwards
to the neighbour that minimises (link cost + distance to destination). Because
each hop strictly reduces the remaining distance, forwarding is loop-free.
"""
from optimizer.dijkstra import dijkstra


def build_forwarding_table(graph, hosts):
    """Return {node: {destination_host: next_node}}."""
    hosts = set(hosts)
    table = {node: {} for node in graph}
    for dst in sorted(hosts):
        dist, _ = dijkstra(graph, dst, no_transit=hosts)
        for node, neighbours in graph.items():
            if node == dst or node not in dist:
                continue
            options = [
                (cost + dist[nbr], nbr)
                for nbr, cost in neighbours.items()
                if nbr in dist and (nbr == dst or nbr not in hosts)
            ]
            table[node][dst] = min(options)[1]
    return table


def forwarding_path(table, src, dst):
    path = [src]
    while path[-1] != dst:
        path.append(table[path[-1]][dst])
    return path
