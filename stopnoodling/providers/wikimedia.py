"""Wikimedia Commons provider: random photographic images with light filtering."""

import json
import random
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from pathlib import Path
from typing import List, Optional

from ..config import USER_AGENT
from ..remote_cache import REMOTE_SESSIONS, REMOTE_SESSIONS_LOCK
from .common import download_file


def download_wikimedia_image(page: dict, session_dir: Path, session_id: str) -> Optional[dict]:
    """Download a single Wikimedia image and return image metadata if successful"""
    page_id = page.get('pageid')
    if not page_id:
        return None

    image_info = (page.get('imageinfo') or [{}])[0]
    url = image_info.get('url')
    if not url:
        return None

    mime = image_info.get('mime', '')
    mediatype = image_info.get('mediatype', '')
    if mediatype != 'BITMAP' or not mime.startswith('image/'):
        return None

    # Heuristic filtering to bias toward photographic content.
    # Keep it lightweight: prefer JPEGs and avoid common non-photo keywords.
    # (Commons metadata is messy; this is best-effort.)
    if (mime or '').lower() not in ('image/jpeg', 'image/jpg'):
        return None

    width = int(image_info.get('width') or 0)
    height = int(image_info.get('height') or 0)
    size_bytes = int(image_info.get('size') or 0)
    if width and height:
        # Avoid tiny assets (icons, diagrams, UI elements)
        if min(width, height) < 700:
            return None
    if size_bytes:
        if size_bytes < 120 * 1024:
            return None

    ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if ext not in ['.jpg', '.jpeg']:
        return None

    title = page.get('title', '')
    if title.startswith('File:'):
        title = title[5:]

    lower_title = (title or '').lower()
    description = ''
    extmeta = image_info.get('extmetadata') or {}
    if isinstance(extmeta, dict):
        desc_obj = extmeta.get('ImageDescription')
        if isinstance(desc_obj, dict):
            description = str(desc_obj.get('value') or '')
        elif isinstance(desc_obj, str):
            description = desc_obj
    lower_text = (lower_title + ' ' + (description or '').lower())
    reject_keywords = (
        'newspaper', 'clipping', 'logo', 'icon', 'diagram', 'infographic',
        'map', 'coat of arms', 'flag', 'poster', 'screenshot', 'scan of',
        'vector', 'svg'
    )
    if any(k in lower_text for k in reject_keywords):
        return None

    filename = f"{page_id}{ext}"
    image_path = session_dir / filename
    if not download_file(url, image_path):
        return None

    thumb_filename = None
    thumb_url = image_info.get('thumburl')
    if thumb_url:
        thumb_filename = f"{page_id}_thumb{ext}"
        thumb_path = session_dir / thumb_filename
        if not download_file(thumb_url, thumb_path):
            thumb_filename = None

    return {
        'id': f"wikimedia:{page_id}",
        'name': title or f"Wikimedia {page_id}",
        'image_path': f"/api/remote-image/{session_id}/{urllib.parse.quote(filename)}",
        'thumbnail_path': f"/api/remote-image/{session_id}/{urllib.parse.quote(thumb_filename)}" if thumb_filename else None,
        'tags': [],
        'folder': None,
        'is_remote': True,
        'source': 'wikimedia',
        'attribution_url': image_info.get('descriptionurl')
    }


def fetch_wikimedia_random_pages(limit: int = 50) -> List[dict]:
    params = {
        'action': 'query',
        'format': 'json',
        'formatversion': '2',
        'list': 'random',
        'rnnamespace': '6',
        'rnlimit': str(limit)
    }

    url = f"https://commons.wikimedia.org/w/api.php?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.load(response)
        return data.get('query', {}).get('random', [])
    except (TimeoutError, socket.timeout, urllib.error.URLError):
        return []


def fetch_wikimedia_imageinfo(titles: List[str]) -> List[dict]:
    if not titles:
        return []

    params = {
        'action': 'query',
        'format': 'json',
        'formatversion': '2',
        'prop': 'imageinfo',
        'iiprop': 'url|mime|mediatype|extmetadata|size|dimensions',
        'iiurlwidth': '400',
        'titles': '|'.join(titles)
    }

    url = f"https://commons.wikimedia.org/w/api.php?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.load(response)
        return data.get('query', {}).get('pages', [])
    except (TimeoutError, socket.timeout, urllib.error.URLError):
        return []


def fetch_wikimedia_photos_background(count: int, session_id: str, session_dir: Path):
    """Background thread that continues fetching images"""
    try:
        with REMOTE_SESSIONS_LOCK:
            session = REMOTE_SESSIONS.get(session_id)
            if not session:
                return
            images = session['images']
            seen_page_ids = session['seen_page_ids']

        attempts = 0
        max_attempts = min(count // 3 + 3, 10)

        while len(images) < count and attempts < max_attempts:
            with REMOTE_SESSIONS_LOCK:
                if session_id not in REMOTE_SESSIONS:
                    return  # Session was cleaned up
                current_count = len(images)

            if current_count >= count:
                break

            print(f"[Wikimedia Background] Attempt {attempts + 1}/{max_attempts}, found {current_count}/{count}")

            random_pages = fetch_wikimedia_random_pages(50)
            titles = [page.get('title') for page in random_pages if page.get('title')]
            pages = fetch_wikimedia_imageinfo(titles)

            if not pages:
                attempts += 1
                continue

            random.shuffle(pages)

            candidates = []
            with REMOTE_SESSIONS_LOCK:
                for page in pages:
                    if len(images) >= count:
                        break
                    page_id = page.get('pageid')
                    if not page_id or page_id in seen_page_ids:
                        continue
                    seen_page_ids.add(page_id)
                    candidates.append(page)

            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = {executor.submit(download_wikimedia_image, page, session_dir, session_id): page for page in candidates}

                for future in as_completed(futures):
                    result = future.result()
                    if result:
                        with REMOTE_SESSIONS_LOCK:
                            if session_id in REMOTE_SESSIONS and len(images) < count:
                                images.append(result)

            attempts += 1

        with REMOTE_SESSIONS_LOCK:
            if session_id in REMOTE_SESSIONS:
                REMOTE_SESSIONS[session_id]['fetching'] = False
                print(f"[Wikimedia Background] Finished with {len(images)} images")
    except Exception as e:
        print(f"[Wikimedia Background] Error: {e}")
        with REMOTE_SESSIONS_LOCK:
            if session_id in REMOTE_SESSIONS:
                REMOTE_SESSIONS[session_id]['fetching'] = False


def fetch_wikimedia_photos_initial(count: int, session_id: str, session_dir: Path) -> List[dict]:
    """Fetch initial batch of images quickly, then continue in background"""
    images: List[dict] = []
    seen_page_ids = set()
    min_images = min(3, count)

    print(f"[Wikimedia] Fetching initial images (need {min_images} to start)...")

    # A few quick attempts to get min_images to start.
    for attempt in range(3):
        if len(images) >= min_images:
            break

        random_pages = fetch_wikimedia_random_pages(50)
        titles = [page.get('title') for page in random_pages if page.get('title')]
        pages = fetch_wikimedia_imageinfo(titles)

        if not pages:
            continue

        random.shuffle(pages)

        candidates = []
        for page in pages:
            page_id = page.get('pageid')
            if not page_id or page_id in seen_page_ids:
                continue
            seen_page_ids.add(page_id)
            candidates.append(page)

        executor = ThreadPoolExecutor(max_workers=10)
        futures = set()
        candidate_iter = iter(candidates)

        def submit_next():
            try:
                page = next(candidate_iter)
            except StopIteration:
                return False
            futures.add(executor.submit(download_wikimedia_image, page, session_dir, session_id))
            return True

        # Prime the pool
        for _ in range(10):
            if not submit_next():
                break

        try:
            while futures and len(images) < min_images:
                done, _ = wait(futures, return_when=FIRST_COMPLETED, timeout=10)
                if not done:
                    break
                for fut in done:
                    futures.discard(fut)
                    try:
                        result = fut.result()
                    except Exception:
                        result = None
                    if result:
                        images.append(result)
                        if len(images) >= min_images:
                            break
                    submit_next()
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    print(f"[Wikimedia] Returning {len(images)} initial images, continuing in background...")

    # Store session data for background thread
    with REMOTE_SESSIONS_LOCK:
        REMOTE_SESSIONS[session_id] = {
            'path': str(session_dir),
            'created_at': time.time(),
            'images': images,
            'seen_page_ids': seen_page_ids,
            'target_count': count,
            'fetching': True
        }

    # Start background fetch
    thread = threading.Thread(
        target=fetch_wikimedia_photos_background,
        args=(count, session_id, session_dir),
        daemon=True
    )
    thread.start()

    return images
