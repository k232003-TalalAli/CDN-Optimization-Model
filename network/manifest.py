import json

from . import spec


def load_manifest(path=spec.MANIFEST_PATH):
    with open(path) as f:
        return json.load(f)


def iter_replicas(manifest):
    """Yield (content, resolution_or_None, cdn_code, url) for every replica."""
    for content, entry in manifest.items():
        if "resolutions" in entry:
            for resolution, info in entry["resolutions"].items():
                for cdn, url in info["replicas"].items():
                    yield content, resolution, cdn, url
        else:
            for cdn, url in entry["replicas"].items():
                yield content, None, cdn, url
