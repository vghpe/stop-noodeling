"""Shared helpers used by more than one remote provider."""

import urllib.request
from pathlib import Path
from typing import List

from ..config import USER_AGENT


def download_file(url: str, dest_path: Path) -> bool:
    try:
        req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as response:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with open(dest_path, 'wb') as f:
                while True:
                    chunk = response.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
        return True
    except Exception:
        return False


def public_image_fields(images: List[dict]) -> List[dict]:
    """Strip server-internal fields (prefixed with _) before sending image lists to the client."""
    return [{k: v for k, v in img.items() if not k.startswith('_')} for img in images]
