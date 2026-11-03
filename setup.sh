#!/usr/bin/env bash
# One-shot setup for Ubuntu/Debian (tested target: Ubuntu 22.04/24.04).
set -euo pipefail
cd "$(dirname "$0")"

sudo DEBIAN_FRONTEND=noninteractive apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    mininet openvswitch-switch iperf3 ffmpeg curl net-tools python3-flask
sudo systemctl enable --now openvswitch-switch 2>/dev/null || sudo service openvswitch-switch start

echo "== Phase 1: verify Mininet =="
sudo mn -c >/dev/null 2>&1 || true
sudo mn --test pingall

echo "== Generating content, manifest and CDN directories =="
python3 setup_content.py

echo "Done. Next: sudo python3 -m network.topology --check"
