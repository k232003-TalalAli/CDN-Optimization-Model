"""Run commands inside Mininet host namespaces from any process (e.g. Flask).

topology.py records every host's PID in run/network_state.json. nsenter joins
that PID's network namespace, so traffic really originates from the Mininet
host. Requires root.
"""
import json
import os
import subprocess

from . import spec


class NetworkNotRunning(RuntimeError):
    pass


def require_root():
    if os.geteuid() != 0:
        raise SystemExit("This needs root: run it with sudo.")


def load_state(path=spec.STATE_PATH):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        raise NetworkNotRunning(
            "Mininet is not running. Start it with: sudo python3 -m network.topology"
        )


def _prefix(host, state):
    return ["nsenter", "-t", str(state["hosts"][host]["pid"]), "-n", "--"]


def run(host, cmd, timeout=10, state=None):
    state = state or load_state()
    return subprocess.run(
        _prefix(host, state) + list(cmd),
        capture_output=True, text=True, timeout=timeout,
    )


def spawn(host, cmd, log_path, state=None):
    """Start a long-running process in the host's namespace, detached from us."""
    state = state or load_state()
    spec.LOG_DIR.mkdir(exist_ok=True)
    log = open(log_path, "ab")
    return subprocess.Popen(
        _prefix(host, state) + list(cmd),
        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def popen_pipe(host, cmd, state=None):
    """Start a command in the host's namespace and return it with stdout piped to us."""
    state = state or load_state()
    return subprocess.Popen(
        _prefix(host, state) + list(cmd),
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
    )
