import unittest

from network import spec
from network.routing import build_forwarding_table, forwarding_path
from optimizer.dijkstra import shortest_path

HOSTS = set(spec.HOSTS)
GRAPH = spec.build_graph()
TABLE = build_forwarding_table(GRAPH, HOSTS)


def path(user, cdn):
    return shortest_path(GRAPH, spec.user_host(user), spec.cdn_host(cdn), HOSTS)


class DijkstraTests(unittest.TestCase):
    def test_local_cdn_costs(self):
        self.assertEqual(path("KHI", "KHI"), (["user-khi", "s2", "cdn-khi"], 13))
        self.assertEqual(path("LHR", "LHR"), (["user-lhr", "s3", "cdn-lhr"], 13))
        self.assertEqual(path("ISB", "ISB"), (["user-isb", "s4", "cdn-isb"], 14))
        self.assertEqual(path("QTA", "QTA"), (["user-qta", "s5", "cdn-qta"], 16))

    def test_karachi_reaches_every_cdn_with_distinct_costs(self):
        costs = {c: path("KHI", c)[1] for c in spec.LOCATIONS}
        self.assertEqual(costs, {"KHI": 13, "LHR": 33, "ISB": 36, "QTA": 40})

    def test_karachi_does_not_route_through_lahore(self):
        p, _ = path("KHI", "ISB")
        self.assertNotIn("cdn-lhr", p)
        self.assertNotIn("s3", p)

    def test_hosts_are_never_transit(self):
        p, cost = shortest_path(GRAPH, "s4", "s5", HOSTS)
        self.assertEqual(p, ["s4", "s6", "s5"])
        self.assertEqual(cost, 20)


class ForwardingTests(unittest.TestCase):
    def test_every_pair_reachable_without_loops(self):
        for src in HOSTS:
            for dst in HOSTS - {src}:
                p = forwarding_path(TABLE, src, dst)
                self.assertEqual(len(p), len(set(p)), f"loop {src}->{dst}: {p}")
                for mid in p[1:-1]:
                    self.assertNotIn(mid, HOSTS)

    def test_forwarding_cost_matches_dijkstra(self):
        for src in HOSTS:
            for dst in HOSTS - {src}:
                p = forwarding_path(TABLE, src, dst)
                cost = sum(GRAPH[a][b] for a, b in zip(p, p[1:]))
                self.assertEqual(cost, shortest_path(GRAPH, src, dst, HOSTS)[1])

    def test_quetta_cdn_uses_both_attachments(self):
        self.assertEqual(TABLE["s1"]["cdn-qta"], "s4")
        self.assertEqual(TABLE["s6"]["cdn-qta"], "s5")
        self.assertEqual(TABLE["cdn-qta"]["user-qta"], "s5")
        self.assertEqual(TABLE["cdn-qta"]["user-isb"], "s4")


class SpecTests(unittest.TestCase):
    def test_addressing(self):
        self.assertEqual(spec.host_ip("cdn-khi"), "10.0.0.5")
        self.assertEqual(spec.host_mac("cdn-qta"), "00:00:00:00:00:08")

    def test_capacity_scaling(self):
        caps = spec.capacities(0.1)
        self.assertAlmostEqual(caps[("s2", "cdn-khi")], 50)
        self.assertAlmostEqual(caps[("user-khi", "s2")], 30)


if __name__ == "__main__":
    unittest.main()
