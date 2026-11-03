import unittest

from network import spec
from optimizer import optimizer, scoring

GRAPH = spec.build_graph()


def metrics(latency, avail, load, capacity=30.0):
    return {"latency_ms": latency, "available_bandwidth_mbps": avail, "capacity_mbps": capacity,
            "server_load_pct": load, "path_cost_ms": 0, "throughput_mbps": 0.0}


def snapshot(**overrides):
    base = {"KHI": metrics(30, 28, 5), "LHR": metrics(70, 28, 5), "ISB": metrics(75, 28, 5), "QTA": metrics(85, 28, 5)}
    base.update(overrides)
    return base


MANIFEST = {
    "video.mp4": {"type": "video", "mime": "video/mp4", "resolutions": {
        "360p": {"bitrate_mbps": 1.5, "size_bytes": 1, "replicas": {"KHI": "k3", "LHR": "l3", "ISB": "i3", "QTA": "q3"}},
        "720p": {"bitrate_mbps": 5.0, "size_bytes": 1, "replicas": {"KHI": "k7", "LHR": "l7", "ISB": "i7"}},
        "1080p": {"bitrate_mbps": 8.0, "size_bytes": 1, "replicas": {"LHR": "l10"}}}},
    "test.pdf": {"type": "document", "mime": "application/pdf", "size_bytes": 1,
                 "replicas": {"KHI": "kp", "LHR": "lp"}},
}


class ScoringTests(unittest.TestCase):
    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(scoring.WEIGHTS.values()), 1.0)

    def test_hand_computed_score(self):
        cands = {
            "A": {"latency_ms": 20, "available_bandwidth_mbps": 30, "capacity_mbps": 30, "server_load_pct": 0, "path_cost_ms": 10},
            "B": {"latency_ms": 40, "available_bandwidth_mbps": 15, "capacity_mbps": 30, "server_load_pct": 50, "path_cost_ms": 20},
        }
        result = scoring.score(cands)
        self.assertAlmostEqual(result["A"]["score"], 0.4 * 0.5 + 0.3 * 0 + 0.2 * 0 + 0.1 * 0.5)
        self.assertAlmostEqual(result["B"]["score"], 0.4 * 1 + 0.3 * 0.5 + 0.2 * 0.5 + 0.1 * 1)

    def test_zero_maxima_do_not_divide_by_zero(self):
        cands = {"A": {"latency_ms": 0, "available_bandwidth_mbps": 0, "capacity_mbps": 0, "server_load_pct": 0, "path_cost_ms": 0}}
        self.assertEqual(scoring.score(cands)["A"]["score"], 0.3)


class ResolutionTests(unittest.TestCase):
    BITRATES = {"360p": 1.5, "720p": 5.0, "1080p": 8.0}

    def test_spec_examples(self):
        self.assertEqual(optimizer.pick_resolution(self.BITRATES, 12), ("1080p", True))
        self.assertEqual(optimizer.pick_resolution(self.BITRATES, 6), ("720p", True))
        self.assertEqual(optimizer.pick_resolution(self.BITRATES, 2), ("360p", True))
        self.assertEqual(optimizer.pick_resolution(self.BITRATES, 1.0), ("360p", False))


class DecisionTests(unittest.TestCase):
    def decide(self, snap=None, **kw):
        return optimizer.decide(MANIFEST, kw.pop("content", "video.mp4"), "KHI", snap or snapshot(), GRAPH, **kw)

    def test_nearest_idle_cdn_wins_and_quality_follows_bandwidth(self):
        d = self.decide(snapshot(KHI=metrics(30, 28, 5)))
        self.assertEqual((d["cdn"], d["resolution"], d["url"]), ("KHI", "720p", "k7"))
        self.assertEqual(d["path"], ["user-khi", "s2", "cdn-khi"])

    def test_congested_cdn_loses_and_1080p_possible_elsewhere(self):
        d = self.decide(snapshot(KHI=metrics(120, 2, 90)))
        self.assertNotEqual(d["cdn"], "KHI")
        self.assertEqual(d["cdn"], "LHR")
        self.assertEqual(d["resolution"], "1080p")

    def test_quality_steps_down_with_bandwidth(self):
        for avail, expected in ((9, "1080p"), (5.8, "720p"), (1.9, "360p")):
            d = self.decide(snapshot(LHR=metrics(70, avail, 5)), cdn_mode="manual", cdn="LHR")
            self.assertEqual(d["resolution"], expected)

    def test_manual_cdn_and_quality(self):
        d = self.decide(cdn_mode="manual", cdn="ISB", quality_mode="manual", quality="360p")
        self.assertEqual((d["cdn"], d["resolution"], d["url"]), ("ISB", "360p", "i3"))

    def test_manual_quality_missing_on_cdn_is_an_error_not_a_fallback(self):
        d = self.decide(cdn_mode="manual", cdn="KHI", quality_mode="manual", quality="1080p")
        self.assertFalse(d["ok"])
        self.assertEqual(d["error"]["code"], "quality_unavailable")
        self.assertIn("LHR", d["error"]["message"])
        self.assertIsNone(d["url"])

    def test_manual_cdn_without_replica(self):
        d = self.decide(content="test.pdf", cdn_mode="manual", cdn="QTA")
        self.assertEqual(d["error"]["code"], "cdn_no_replica")

    def test_non_video_content_only_considers_replica_holders(self):
        d = self.decide(content="test.pdf")
        self.assertEqual(set(d["ranking"]), {"KHI", "LHR"})
        self.assertIsNone(d["resolution"])
        self.assertEqual(d["url"], "kp")

    def test_failed_ping_ranks_last(self):
        d = self.decide(snapshot(KHI=metrics(None, 28, 5)))
        self.assertNotEqual(d["cdn"], "KHI")

    def test_manual_quality_above_bandwidth_warns(self):
        d = self.decide(snapshot(KHI=metrics(30, 3, 5)), cdn_mode="manual", cdn="KHI",
                        quality_mode="manual", quality="720p")
        self.assertTrue(d["ok"])
        self.assertTrue(d["warnings"])


if __name__ == "__main__":
    unittest.main()
