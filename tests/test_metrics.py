import os
import socket
import unittest

from network import spec
from network.metrics import (MetricsCollector, count_connections, counter_map,
                             parse_ping, read_cpu_ticks)

PING_OK = """--- 10.0.0.5 ping statistics ---
3 packets transmitted, 3 received, 0% packet loss, time 405ms
rtt min/avg/max/mdev = 26.1/26.4/26.9/0.3 ms
"""
PING_DEAD = "3 packets transmitted, 0 received, 100% packet loss, time 2040ms\n"


def fake_state():
    links = []
    counts = {}
    for a, b, bw, delay in spec.LINKS:
        counts[a] = counts.get(a, 0) + 1
        counts[b] = counts.get(b, 0) + 1
        links.append({"a": a, "b": b, "bw_mbps": bw * 0.1, "delay_ms": delay,
                      "intf_a": f"{a}-eth{counts[a]}", "intf_b": f"{b}-eth{counts[b]}"})
    return {"bw_scale": 0.1, "links": links, "hosts": {}, "http_servers": {}}


class ParsingTests(unittest.TestCase):
    def test_ping_ok(self):
        self.assertEqual(parse_ping(PING_OK), {"latency_ms": 26.4, "jitter_ms": 0.3, "loss_pct": 0.0})

    def test_ping_total_loss(self):
        self.assertEqual(parse_ping(PING_DEAD)["latency_ms"], None)
        self.assertEqual(parse_ping(PING_DEAD)["loss_pct"], 100.0)

    def test_cpu_ticks_readable(self):
        first = read_cpu_ticks(os.getpid())
        sum(i * i for i in range(300000))
        self.assertGreaterEqual(read_cpu_ticks(os.getpid()), first)

    def test_connection_count(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        self.assertEqual(count_connections(os.getpid(), port), 0)
        client = socket.create_connection(("127.0.0.1", port))
        conn, _ = server.accept()
        self.assertEqual(count_connections(os.getpid(), port), 1)
        client.close(); conn.close(); server.close()


class CounterMapTests(unittest.TestCase):
    def test_directions(self):
        mapping = counter_map(fake_state()["links"], set(spec.HOSTS))
        self.assertEqual(mapping[("s1", "s2")], ("s1-eth1", "tx"))
        self.assertEqual(mapping[("s2", "s1")], ("s2-eth1", "tx"))
        self.assertEqual(mapping[("cdn-khi", "s2")][1], "rx")
        self.assertEqual(mapping[("s2", "cdn-khi")][1], "tx")
        self.assertEqual(mapping[("s2", "cdn-khi")][0].split("-")[0], "s2")


class CalculationTests(unittest.TestCase):
    def setUp(self):
        self.m = MetricsCollector(state=fake_state())

    def test_idle_path(self):
        p = self.m.path_metrics("KHI", "KHI")
        self.assertEqual(p["path"], ["cdn-khi", "s2", "user-khi"])
        self.assertEqual(p["path_cost_ms"], 13)
        self.assertAlmostEqual(p["available_bandwidth_mbps"], 30)
        self.assertEqual(p["bottleneck_link"], "s2->user-khi")

    def test_congested_uplink_shrinks_available_bandwidth(self):
        self.m._rates[("cdn-khi", "s2")] = 44.2
        p = self.m.path_metrics("KHI", "KHI")
        self.assertAlmostEqual(p["available_bandwidth_mbps"], 5.8)
        self.assertEqual(p["bottleneck_link"], "cdn-khi->s2")

    def test_other_direction_is_unaffected(self):
        self.m._rates[("s2", "s3")] = 29.0   # KHI -> Lahore direction only
        lhr_for_khi = self.m.path_metrics("KHI", "LHR")   # LHR -> KHI user uses s3 -> s2
        self.assertAlmostEqual(lhr_for_khi["available_bandwidth_mbps"], 30)
        khi_for_lhr = self.m.path_metrics("LHR", "KHI")
        self.assertAlmostEqual(khi_for_lhr["available_bandwidth_mbps"], 1.0)

    def test_server_load_uses_busiest_resource(self):
        self.m._rates[("cdn-khi", "s2")] = 40.0
        s = self.m.server_metrics("KHI")
        self.assertAlmostEqual(s["throughput_mbps"], 40.0)
        self.assertAlmostEqual(s["nic_util_pct"], 80.0)
        self.assertAlmostEqual(s["server_load_pct"], 80.0)
        self.m._connections["cdn-khi"] = 5
        self.m._cpu["cdn-khi"] = 95.0
        self.assertAlmostEqual(self.m.server_metrics("KHI")["server_load_pct"], 95.0)


if __name__ == "__main__":
    unittest.main()
