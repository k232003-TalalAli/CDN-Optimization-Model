"""Relay a CDN file to the browser through a real Mininet user host.

Flask runs `curl` inside the user's host namespace, so the bytes cross the
simulated topology from the CDN host, and relays curl's stdout to the browser.
Active transfers are tracked so the dashboard can show their real rate.
"""
import threading
import time
from collections import deque

from . import hostexec, spec

CHUNK = 64 * 1024
RATE_WINDOW_S = 3.0


def open_stream(user_code, url, start=None, end=None):
    cmd = ["curl", "-sS", "--fail", "--connect-timeout", "5"]
    if start is not None:
        cmd += ["-r", f"{start}-{'' if end is None else end}"]
    return hostexec.popen_pipe(spec.user_host(user_code), cmd + [url])


def parse_range(header, size):
    """'bytes=a-b' -> (start, end) inclusive, or None if absent or unusable."""
    if not header or not header.startswith("bytes=") or size is None:
        return None
    first, _, last = header[6:].split(",")[0].partition("-")
    try:
        if first == "":
            start, end = max(size - int(last), 0), size - 1
        else:
            start, end = int(first), int(last) if last else size - 1
    except ValueError:
        return None
    end = min(end, size - 1)
    return (start, end) if 0 <= start <= end else None


class Transfers:
    def __init__(self):
        self._lock = threading.Lock()
        self._items = {}
        self._next = 1

    def begin(self, sid, user, cdn, content, resolution):
        with self._lock:
            tid, self._next = self._next, self._next + 1
            self._items[tid] = {"id": tid, "sid": sid, "user": user, "cdn": cdn, "content": content,
                                "resolution": resolution, "bytes": 0, "samples": deque(),
                                "started": time.time(), "active": True}
        return tid

    def add(self, tid, nbytes):
        now = time.monotonic()
        with self._lock:
            item = self._items[tid]
            item["bytes"] += nbytes
            item["samples"].append((now, nbytes))
            while item["samples"] and now - item["samples"][0][0] > RATE_WINDOW_S:
                item["samples"].popleft()

    def finish(self, tid):
        with self._lock:
            self._items[tid]["active"] = False
            self._items[tid]["finished"] = time.time()

    def active_for(self, sid):
        now = time.monotonic()
        with self._lock:
            out = []
            for item in self._items.values():
                if item["sid"] != sid:
                    continue
                recent = sum(n for t, n in item["samples"] if now - t <= RATE_WINDOW_S)
                rate = recent * 8 / RATE_WINDOW_S / 1e6 if item["active"] else 0.0
                out.append(dict({k: item[k] for k in ("id", "user", "cdn", "content", "resolution", "bytes", "active")}, rate_mbps=rate))
            return sorted(out, key=lambda i: -i["id"])[:5]
