"""Single source of truth for the topology, addressing and file locations.

Mininet (topology.py), the routing tables, the Dijkstra graph and the manifest
generator all read from here, so they cannot drift apart.

Topology:

                  s1_________s6
               /    |        / \\
              /     |       /   \\
            s2______s3     s4     s5
           / \\       |     / \\    / |
       User   KHI    LHR  ISB QTA---+  (QTA is attached to both s4 and s5)
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTENT_DIR = ROOT / "content"
CDN_ROOT = ROOT / "cdn"
RUN_DIR = ROOT / "run"
LOG_DIR = ROOT / "logs"
MANIFEST_PATH = CONTENT_DIR / "manifest.json"
STATE_PATH = RUN_DIR / "network_state.json"
TRAFFIC_PATH = RUN_DIR / "traffic.json"

CONTENT_PORT = 8000

# Link capacities below are the "full size" values. Mininet runs them multiplied
# by this factor so the 1.5 / 5 / 8 Mbps video bitrates can be pushed across
# the interesting thresholds with iperf3 traffic that a VM can actually generate.
DEFAULT_BW_SCALE = 0.1

SWITCHES = ["s1", "s2", "s3", "s4", "s5", "s6"]

LOCATIONS = {
    "KHI": "Karachi",
    "LHR": "Lahore",
    "ISB": "Islamabad",
    "QTA": "Quetta",
}

HOSTS = {
    "user-khi": ("10.0.0.1", "user", "KHI"),
    "user-lhr": ("10.0.0.2", "user", "LHR"),
    "user-isb": ("10.0.0.3", "user", "ISB"),
    "user-qta": ("10.0.0.4", "user", "QTA"),
    "cdn-khi": ("10.0.0.5", "cdn", "KHI"),
    "cdn-lhr": ("10.0.0.6", "cdn", "LHR"),
    "cdn-isb": ("10.0.0.7", "cdn", "ISB"),
    "cdn-qta": ("10.0.0.8", "cdn", "QTA"),
}

# (node_a, node_b, bandwidth_mbps, one_way_delay_ms)
# The first link listed for a host is its primary interface (eth0).
LINKS = [
    ("s1", "s2", 500, 10),
    ("s1", "s3", 400, 15),
    ("s1", "s4", 500, 12),
    ("s1", "s6", 400, 8),
    ("s2", "s3", 300, 20),
    ("s4", "s6", 300, 10),
    ("s5", "s6", 300, 10),
    ("s2", "user-khi", 300, 8),
    ("s2", "cdn-khi", 500, 5),
    ("s3", "user-lhr", 300, 8),
    ("s3", "cdn-lhr", 500, 5),
    ("s4", "user-isb", 300, 8),
    ("s4", "cdn-isb", 500, 6),
    ("s4", "cdn-qta", 300, 10),
    ("s5", "cdn-qta", 300, 8),
    ("s5", "user-qta", 300, 8),
]


def user_host(code):
    return f"user-{code.lower()}"


def cdn_host(code):
    return f"cdn-{code.lower()}"


def host_ip(name):
    return HOSTS[name][0]


def host_mac(name):
    return "00:00:00:00:00:%02x" % int(host_ip(name).rsplit(".", 1)[1])


def cdn_dir(code):
    return CDN_ROOT / cdn_host(code) / "content"


def cdn_base_url(code):
    return f"http://{host_ip(cdn_host(code))}:{CONTENT_PORT}"


def build_graph():
    """Undirected graph {node: {neighbour: one_way_latency_ms}}."""
    graph = {name: {} for name in SWITCHES + list(HOSTS)}
    for a, b, _bw, delay in LINKS:
        graph[a][b] = delay
        graph[b][a] = delay
    return graph


def capacities(bw_scale=DEFAULT_BW_SCALE):
    """Directed link capacities {(u, v): mbps} after scaling."""
    caps = {}
    for a, b, bw, _delay in LINKS:
        caps[(a, b)] = caps[(b, a)] = bw * bw_scale
    return caps
