#!/usr/bin/env python3
"""Create the demo content, write content/manifest.json and fill the CDN directories.

    python3 setup_content.py              generate missing media, rewrite manifest, sync CDN dirs
    python3 setup_content.py --force      regenerate all media first
    python3 setup_content.py --sync-only  keep manifest.json as is (e.g. hand-edited) and just
                                          re-copy files into cdn/cdn-*/content/ according to it

The manifest is the authority for placement: sync copies a file to a CDN
directory only if the manifest lists that CDN as a replica.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

from network import spec
from network.manifest import iter_replicas, load_manifest

ALL = list(spec.LOCATIONS)

# name -> {resolution: [CDNs holding it]} for video, [CDNs] for everything else
REPLICATION = {
    "video.mp4": {
        "360p": ["KHI", "LHR", "ISB", "QTA"],
        "720p": ["KHI", "LHR", "ISB"],
        "1080p": ["LHR"],
    },
    "image.jpg": ALL,
    "test.pdf": ALL,
}

RESOLUTIONS = {  # width, height, bitrate in Mbps
    "360p": (640, 360, 1.5),
    "720p": (1280, 720, 5.0),
    "1080p": (1920, 1080, 8.0),
}

MIME = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".pdf": "application/pdf"}


def stem_name(content, resolution):
    stem, ext = os.path.splitext(content)
    return f"{stem}_{resolution}{ext}" if resolution else content


def require_ffmpeg():
    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg is required to generate media: sudo apt install ffmpeg")


def make_video(path, width, height, mbps, seconds):
    kbps = int(mbps * 1000)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
         "-t", str(seconds),
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
         "-b:v", f"{kbps}k", "-minrate", f"{kbps}k", "-maxrate", f"{kbps}k",
         "-bufsize", f"{2 * kbps}k", "-x264-params", "nal-hrd=cbr:force-cfr=1",
         "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(path)],
        check=True,
    )


def make_image(path):
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc2=size=2560x1440", "-frames:v", "1", "-q:v", "2", str(path)],
        check=True,
    )


def make_pdf(path, pad_bytes=2_000_000):
    text = b"BT /F1 24 Tf 72 700 Td (CDN Optimization test document) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(text), text),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (pad_bytes, os.urandom(pad_bytes)),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    path.write_bytes(bytes(out))


def generate_media(seconds, force):
    spec.CONTENT_DIR.mkdir(exist_ok=True)
    for content, plan in REPLICATION.items():
        ext = os.path.splitext(content)[1]
        variants = plan.keys() if isinstance(plan, dict) else [None]
        for resolution in variants:
            path = spec.CONTENT_DIR / stem_name(content, resolution)
            if path.exists() and not force:
                continue
            print(f"generating {path.name}")
            if ext == ".mp4":
                require_ffmpeg()
                width, height, mbps = RESOLUTIONS[resolution]
                make_video(path, width, height, mbps, seconds)
            elif ext == ".jpg":
                require_ffmpeg()
                make_image(path)
            else:
                make_pdf(path)


def replica_urls(content, resolution, cdns):
    name = stem_name(content, resolution)
    return {code: f"{spec.cdn_base_url(code)}/{name}" for code in cdns}


def build_manifest():
    manifest = {}
    for content, plan in REPLICATION.items():
        ext = os.path.splitext(content)[1]
        if isinstance(plan, dict):
            resolutions = {}
            for resolution, cdns in plan.items():
                size = (spec.CONTENT_DIR / stem_name(content, resolution)).stat().st_size
                resolutions[resolution] = {
                    "bitrate_mbps": RESOLUTIONS[resolution][2],
                    "size_bytes": size,
                    "replicas": replica_urls(content, resolution, cdns),
                }
            manifest[content] = {"type": "video", "mime": MIME[ext], "resolutions": resolutions}
        else:
            size = (spec.CONTENT_DIR / content).stat().st_size
            kind = "image" if ext == ".jpg" else "document"
            manifest[content] = {
                "type": kind, "mime": MIME[ext], "size_bytes": size,
                "replicas": replica_urls(content, None, plan),
            }
    return manifest


def sync_cdn_dirs(manifest):
    for code in spec.LOCATIONS:
        shutil.rmtree(spec.cdn_dir(code), ignore_errors=True)
        spec.cdn_dir(code).mkdir(parents=True)
    for _content, _resolution, code, url in iter_replicas(manifest):
        filename = url.rsplit("/", 1)[1]
        source = spec.CONTENT_DIR / filename
        if not source.exists():
            sys.exit(f"{source} is missing; run setup_content.py without --sync-only")
        shutil.copy2(source, spec.cdn_dir(code) / filename)
    for code in spec.LOCATIONS:
        files = sorted(p.name for p in spec.cdn_dir(code).iterdir())
        print(f"{spec.cdn_host(code)}: {', '.join(files)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="regenerate media even if present")
    parser.add_argument("--sync-only", action="store_true", help="only copy files according to the existing manifest")
    parser.add_argument("--seconds", type=int, default=30, help="video duration (default 30)")
    args = parser.parse_args()

    if args.sync_only:
        manifest = load_manifest()
    else:
        generate_media(args.seconds, args.force)
        manifest = build_manifest()
        spec.MANIFEST_PATH.write_text(json.dumps(manifest, indent=4) + "\n")
        print(f"wrote {spec.MANIFEST_PATH}")
    sync_cdn_dirs(manifest)


if __name__ == "__main__":
    main()
