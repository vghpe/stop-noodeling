"""Local Eagle library index (PackCache) with √-weighted pack sampling."""

import json
import math
import random
import threading
import time
import urllib.parse
from typing import Dict, List, Set

from .config import IMAGES_DIR, LIBRARY_PATH


def _weighted_sample_without_replacement(population: List, weights: List[float], k: int) -> List:
    """Weighted random sample of k distinct items without replacement."""
    population = list(population)
    weights = list(weights)
    result = []
    for _ in range(min(k, len(population))):
        total = sum(weights)
        if total <= 0:
            break
        r = random.uniform(0, total)
        cumulative = 0.0
        chosen_idx = len(population) - 1
        for idx, w in enumerate(weights):
            cumulative += w
            if r <= cumulative:
                chosen_idx = idx
                break
        result.append(population[chosen_idx])
        population.pop(chosen_idx)
        weights.pop(chosen_idx)
    return result


class PackCache:
    """Pre-built index of Eagle pack -> image list with √-weighted sampling support."""

    def __init__(self):
        self._lock = threading.Lock()
        self._ready = False
        self._pack_images: Dict[str, List[dict]] = {}
        self._pack_names: Dict[str, str] = {}

    def is_ready(self) -> bool:
        with self._lock:
            return self._ready

    def build(self):
        """Scan the full library and populate the pack index. Called once from a background thread."""
        print("[PackCache] Starting build...")
        start = time.time()
        try:
            # Load folder names and build child→root mapping from library metadata
            pack_names: Dict[str, str] = {}
            child_to_root: Dict[str, str] = {}  # maps any descendant folder ID to its top-level ancestor ID
            lib_meta_file = LIBRARY_PATH / "metadata.json"
            if lib_meta_file.exists():
                try:
                    with open(lib_meta_file, 'r', encoding='utf-8') as f:
                        lib_meta = json.load(f)

                    def _walk(folders, root_id=None):
                        for folder in folders:
                            fid = folder.get('id')
                            if not fid:
                                continue
                            pack_names[fid] = folder.get('name', '')
                            effective_root = root_id or fid  # top-level folders are their own root
                            child_to_root[fid] = effective_root
                            _walk(folder.get('children', []), effective_root)

                    _walk(lib_meta.get('folders', []))
                except Exception as e:
                    print(f"[PackCache] Warning: could not read library metadata: {e}")

            # Walk all image directories
            pack_images: Dict[str, List[dict]] = {}
            if not IMAGES_DIR.exists():
                raise FileNotFoundError(f"Images dir not found: {IMAGES_DIR}")

            for img_dir in IMAGES_DIR.iterdir():
                if not img_dir.is_dir():
                    continue
                md_file = img_dir / "metadata.json"
                if not md_file.exists():
                    continue
                try:
                    with open(md_file, 'r', encoding='utf-8') as f:
                        metadata = json.load(f)
                except Exception:
                    continue

                if metadata.get('isDeleted', False):
                    continue
                tags = metadata.get('tags', [])
                if 'ignore' in tags:
                    continue

                image_files = [
                    f for f in img_dir.iterdir()
                    if f.is_file()
                    and not f.name.endswith('_thumbnail.png')
                    and f.name != 'metadata.json'
                    and f.suffix.lower() in ('.jpg', '.jpeg', '.png', '.gif', '.webp')
                ]
                if not image_files:
                    continue

                image_file = image_files[0]
                thumbnail_files = [
                    f for f in img_dir.iterdir()
                    if f.is_file() and f.name.endswith('_thumbnail.png')
                ]
                thumbnail_file = thumbnail_files[0] if thumbnail_files else None

                image_dict = {
                    'id': metadata['id'],
                    'name': metadata.get('name', image_file.stem),
                    'image_path': f"/api/image/{img_dir.name}/{urllib.parse.quote(image_file.name)}",
                    'thumbnail_path': (
                        f"/api/image/{img_dir.name}/{urllib.parse.quote(thumbnail_file.name)}"
                        if thumbnail_file else None
                    ),
                    'tags': tags,
                    'folder': img_dir.name,
                }

                folder_ids = metadata.get('folders', [])
                if folder_ids:
                    # Roll up to root pack — deduplicate in case multiple subfolders share a root
                    root_ids = set(child_to_root.get(fid, fid) for fid in folder_ids)
                    for rid in root_ids:
                        pack_images.setdefault(rid, []).append(image_dict)
                else:
                    pack_images.setdefault('__unassigned__', []).append(image_dict)

            elapsed = time.time() - start
            total_entries = sum(len(v) for v in pack_images.values())
            print(f"[PackCache] Ready: {len(pack_images)} packs, {total_entries} image entries in {elapsed:.1f}s")

            with self._lock:
                self._pack_names = pack_names
                self._pack_images = pack_images
                self._ready = True

        except Exception as e:
            print(f"[PackCache] Build failed: {e}")

    @staticmethod
    def _image_category(tags: List[str]) -> str:
        if 'handsfeet' in tags:
            return 'handsfeet'
        if 'costumes' in tags:
            return 'costumes'
        if 'portraits' in tags:
            return 'portraits'
        return 'figure'

    def sample(self, count: int, pack_mode: str, enabled_categories: Set[str]) -> List[dict]:
        """
        Sample `count` images using √-weighted pack selection.
        pack_mode: 'all' | '1' | '3'
        enabled_categories: set of category strings ('figure', 'handsfeet', 'costumes', 'portraits')
        """
        with self._lock:
            pack_images_snapshot = dict(self._pack_images)

        # Filter each pack's images to matching categories first
        filtered: Dict[str, List[dict]] = {}
        for pack_id, images in pack_images_snapshot.items():
            pool = [img for img in images if self._image_category(img['tags']) in enabled_categories]
            if pool:
                filtered[pack_id] = pool

        if not filtered:
            return []

        pack_ids = list(filtered.keys())
        weights = [math.sqrt(len(filtered[pid])) for pid in pack_ids]

        selected_images: List[dict] = []
        seen_image_ids: Set[str] = set()

        if pack_mode == 'all':
            # Weighted sampling with replacement on packs, one image per draw
            attempts = 0
            max_attempts = count * 6
            while len(selected_images) < count and attempts < max_attempts:
                attempts += 1
                drawn_pack = random.choices(pack_ids, weights=weights, k=1)[0]
                img = random.choice(filtered[drawn_pack])
                if img['id'] not in seen_image_ids:
                    selected_images.append(img)
                    seen_image_ids.add(img['id'])
            # Top-up from remaining images if library is very small
            if len(selected_images) < count:
                remaining = [img for imgs in filtered.values() for img in imgs if img['id'] not in seen_image_ids]
                random.shuffle(remaining)
                selected_images.extend(remaining[:count - len(selected_images)])

        else:
            n_packs = int(pack_mode)
            n_packs = min(n_packs, len(pack_ids))
            chosen_packs = _weighted_sample_without_replacement(pack_ids, weights, n_packs)
            quota_base = count // n_packs
            remainder = count % n_packs
            for i, pack_id in enumerate(chosen_packs):
                quota = quota_base + (1 if i < remainder else 0)
                pool = filtered[pack_id]
                drawn = random.sample(pool, min(quota, len(pool)))
                for img in drawn:
                    if img['id'] not in seen_image_ids:
                        selected_images.append(img)
                        seen_image_ids.add(img['id'])
            # Top-up if small packs couldn't fill their quota
            if len(selected_images) < count:
                remaining = [img for imgs in filtered.values() for img in imgs if img['id'] not in seen_image_ids]
                random.shuffle(remaining)
                selected_images.extend(remaining[:count - len(selected_images)])

        random.shuffle(selected_images)
        return selected_images


PACK_CACHE = PackCache()
