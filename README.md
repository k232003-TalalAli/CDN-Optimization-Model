# CDN Optimization Model

Mininet-based CDN simulation. **Current scope: network, content and setup.**
Scoring, optimizer, Flask app and dashboard are in `optimizer/`, `app.py`, `templates/`, `static/`.

## Architecture

```
Flask (later) ──► Optimizer (Dijkstra + scoring, later)
      │                    │
      └──► network/metrics.py ──► Mininet ──► cdn-* HTTP servers ──► real files
                 (ping, interface counters, /proc)
```

Mininet runs in its own process (`network.topology`) and publishes
`run/network_state.json` (host PIDs, interface names). Everything else
(`metrics`, `traffic`, and later Flask) attaches to the hosts through
`nsenter`, so they can run as separate processes. All of them need `sudo`.

```
network/spec.py        topology, addresses, link table (single source of truth)
network/topology.py    Mininet network, static routing, CDN HTTP servers
network/routing.py     forwarding tables from Dijkstra distances
network/metrics.py     latency / available BW / throughput / server load
network/traffic.py     iperf3 background congestion
network/hostexec.py    run commands inside Mininet hosts
network/manifest.py    manifest helpers
optimizer/dijkstra.py  manual Dijkstra
setup_content.py       media + manifest.json + per-CDN directories
tests/                 unit tests (no Mininet needed)
```

## Topology

```
                  s1_________s6
               /    |        / \
              /     |       /   \
            s2______s3     s4     s5
           / \       |     / \    / |
   user-khi  cdn-khi  cdn-lhr cdn-isb  cdn-qta (attached to s4 AND s5)
```

| Host | IP | Switch |
|---|---|---|
| user-khi / user-lhr / user-isb / user-qta | 10.0.0.1 / .2 / .3 / .4 | s2 / s3 / s4 / s5 |
| cdn-khi / cdn-lhr / cdn-isb / cdn-qta | 10.0.0.5 / .6 / .7 / .8 | s2 / s3 / s4 / s4+s5 |

Link table is in `network/spec.py` (`LINKS`: bandwidth in Mbps, one-way delay in ms).

Design decisions to know about:

* **Each user host sits on its own region's switch** (Karachi user on s2, Lahore on s3, ...)
  so a user's location actually matters. To put every user on s2 as the single "User" in the
  diagram, change the four `user-*` entries in `LINKS`.
* **Bandwidth scale 0.1.** `LINKS` holds the full-size values (500/400/300 Mbps); Mininet runs
  them x0.1 (50/40/30 Mbps) so the 1.5/5/8 Mbps bitrate thresholds can be crossed with
  congestion a VM can generate. Use `--bw-scale 1.0` for full size.
* **Loops without flooding.** Switches run with no controller and static flow rules computed
  from Dijkstra distances; ARP is static. No broadcast storms, and Mininet uses the same
  paths the optimizer will reason about.
* **QTA is dual-homed** (s4 and s5) as drawn. It has one IP/MAC on two interfaces, with
  per-destination routes choosing the shortest-path interface.

## Install

Ubuntu/Debian VM:

```bash
cd CDN-Optimization
./setup.sh
```

This installs Mininet, Open vSwitch, iperf3, ffmpeg and Flask, runs `mn --test pingall`,
and generates the content. Manual equivalent:

```bash
sudo apt-get install -y mininet openvswitch-switch iperf3 ffmpeg curl net-tools python3-flask
sudo systemctl enable --now openvswitch-switch
sudo mn --test pingall          # Phase 1 check
python3 setup_content.py        # media + manifest + cdn dirs
```

## Run

Unit tests (Dijkstra, forwarding, metrics maths; no root needed):

```bash
python3 -m unittest discover -s tests -t . -v
```

Phase 2 and 3 automated check (pingall + HTTP HEAD from every user to every CDN):

```bash
sudo python3 -m network.topology --check
```

Start the network and get a Mininet CLI:

```bash
sudo python3 -m network.topology
mininet> pingall
mininet> user-khi curl -s -o /dev/null -w '%{http_code} %{speed_download}\n' http://10.0.0.5:8000/video_720p.mp4
mininet> user-khi ping -c 3 10.0.0.6         # RTT is about 2 x the 33 ms Dijkstra path cost
```

Or start it headless for other tools (Flask later) and keep it running:

```bash
sudo python3 -m network.topology --no-cli
```

If a previous run crashed: `sudo mn -c`.

## CDN HTTP servers

Started automatically by `network.topology`: on each `cdn-*` host,
`python3 -m http.server 8000 --bind <ip> --directory cdn/cdn-<x>/content`.
Request logs: `logs/cdn-*.log`.

## Content and manifest

`setup_content.py` generates real H.264/AAC MP4s (30 s each, CBR at 1.5 / 5 / 8 Mbps),
a JPEG and a PDF, writes `content/manifest.json`, and copies files into
`cdn/cdn-*/content/` **according to the manifest**. Default replication:

| | 360p | 720p | 1080p | image / pdf |
|---|---|---|---|---|
| KHI | yes | yes | | yes |
| LHR | yes | yes | yes | yes |
| ISB | yes | yes | | yes |
| QTA | yes | | | yes |

Change placement by editing `REPLICATION` in `setup_content.py` and re-running it, or edit
`manifest.json` by hand and run `python3 setup_content.py --sync-only`.

Manifest shape (video entries have resolutions; others have replicas directly):

```json
{
  "video.mp4": {
    "type": "video", "mime": "video/mp4",
    "resolutions": {
      "720p": {"bitrate_mbps": 5.0, "size_bytes": 18750000,
               "replicas": {"KHI": "http://10.0.0.5:8000/video_720p.mp4", "LHR": "..."}}
    }
  },
  "image.jpg": {"type": "image", "size_bytes": 210268, "replicas": {"KHI": "..."}}
}
```

## Dijkstra

`optimizer/dijkstra.py`, no NetworkX. Graph = `{node: {neighbour: one-way latency ms}}` built
by `spec.build_graph()`. `shortest_path(graph, src, dst, no_transit=hosts)` returns
`(path, total_cost)`; `no_transit` stops dual-homed `cdn-qta` being used as a router.
Example: `user-khi > s2 > cdn-khi` = 8 + 5 = 13 ms. Costs from Karachi:
KHI 13, LHR 33, ISB 36, QTA 40.

## Metrics (real measurements)

```bash
sudo python3 -m network.topology --no-cli      # terminal 1
sudo python3 -m network.metrics KHI             # terminal 2: live table for a Karachi user
```

| Metric | Source |
|---|---|
| latency | `ping` inside the user host (RTT, includes queueing) |
| available bandwidth | min over the CDN->user path of (link capacity - rate from switch interface byte counters) |
| throughput | traffic currently leaving the CDN host (interface counters) |
| server load | max(CPU of the http.server process, busiest access-link utilisation, connections / 10) |

Formulas are documented at the top of `network/metrics.py`.

## Creating congestion

```bash
sudo python3 -m network.traffic start khi 45     # 45 Mbps UDP out of cdn-khi (50 Mbps uplink)
sudo python3 -m network.traffic status
sudo python3 -m network.traffic stop khi         # or: stop all
```

Traffic goes CDN -> nearest other CDN (`--sink isb` to override), so it eats the server's uplink
without touching the downlink of the users. Watch `network.metrics` while changing the level:
KHI's available bandwidth falls to ~5 Mbps, throughput and load rise, latency climbs.
Rough levels at scale 0.1 for the 50 Mbps KHI uplink: 41 Mbps leaves ~9 (1080p), 44 leaves ~6
(720p), 48 leaves ~2 (360p).

## Dashboard

```bash
sudo python3 -m network.topology --no-cli     # terminal 1
sudo python3 app.py                           # terminal 2 -> http://<vm-ip>:5000
```

Each browser tab is its own user (profile id `sid` is carried in the URL), so two tabs of one
browser can be Karachi and Lahore. Streams are relayed by running `curl` inside the user's
Mininet host, so playback really crosses the topology. The Congestion panel starts/stops
`network.traffic` flows. Selection changes apply to new requests only.

UI development without Mininet or root (simulated network built from the real topology):

```bash
python3 -m tests.dev_server                   # http://localhost:5000
python3 -m unittest discover -s tests -t . -v
```
