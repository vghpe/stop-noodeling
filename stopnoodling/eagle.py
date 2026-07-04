"""Helpers for reading and writing Eagle library metadata."""

import hashlib
import json
import random
import threading
import time
from pathlib import Path

from .config import LIBRARY_PATH

try:
    from PIL import Image  # type: ignore
except Exception:
    Image = None

EAGLE_METADATA_LOCK = threading.Lock()


def now_ms() -> int:
    return int(time.time() * 1000)


def generate_eagle_id(length: int = 13) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(random.choice(alphabet) for _ in range(length))


def stable_eagle_id(seed: str, length: int = 13) -> str:
    """Generate a deterministic Eagle-like ID (A-Z0-9) of given length."""
    digest = hashlib.sha1(seed.encode('utf-8')).digest()
    value = int.from_bytes(digest, byteorder='big', signed=False)
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    chars = []
    while value > 0 and len(chars) < length:
        value, rem = divmod(value, 36)
        chars.append(alphabet[rem])
    while len(chars) < length:
        chars.append('0')
    return "".join(reversed(chars))


def try_write_thumbnail_png(source_path: Path, dest_png_path: Path, max_size: int = 512) -> bool:
    """Best-effort thumbnail writer.

    Eagle commonly expects `<name>_thumbnail.png` inside the `.info` folder.
    If Pillow is unavailable, this returns False and we skip the thumbnail.
    """
    if Image is None:
        return False

    try:
        with Image.open(source_path) as img:
            img = img.convert('RGB')
            img.thumbnail((max_size, max_size))
            img.save(dest_png_path, format='PNG', optimize=True)
        return True
    except Exception:
        return False


def get_or_create_eagle_folder_id(folder_name: str) -> str:
    """Ensure a real Eagle folder exists and return its ID.

    Eagle stores folders in the library root metadata.json as objects with `id` and `name`.
    Items reference folders by ID in their per-item metadata.json `folders` array.
    """
    library_metadata_file = LIBRARY_PATH / "metadata.json"
    if not library_metadata_file.exists():
        raise FileNotFoundError(f"Eagle library metadata not found: {library_metadata_file}")

    with EAGLE_METADATA_LOCK:
        with open(library_metadata_file, 'r', encoding='utf-8') as f:
            library_metadata = json.load(f)

        folders = library_metadata.get('folders')
        if not isinstance(folders, list):
            folders = []
            library_metadata['folders'] = folders

        for folder in folders:
            if isinstance(folder, dict) and folder.get('name') == folder_name and folder.get('id'):
                return folder['id']

        existing_ids = {f.get('id') for f in folders if isinstance(f, dict)}
        folder_id = generate_eagle_id()
        while folder_id in existing_ids:
            folder_id = generate_eagle_id()
        folders.append({
            'id': folder_id,
            'name': folder_name,
            'description': '',
            'children': [],
            'modificationTime': now_ms(),
            'tags': [],
            'password': '',
            'passwordTips': ''
        })

        library_metadata['modificationTime'] = now_ms()

        with open(library_metadata_file, 'w', encoding='utf-8') as f:
            json.dump(library_metadata, f, ensure_ascii=False, indent=2)

        return folder_id
