#!/usr/bin/env python3
"""
Stop Noodling - Backend Server
Serves the web interface and provides API endpoints for the Eagle library
"""

import http.server
import socketserver
import json
import math
import os
import re
import random
import shutil
import urllib.parse
import urllib.request
import html
import time
import uuid
import threading
import hashlib
import socket
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Deque, Set

from http.cookiejar import MozillaCookieJar

try:
    from PIL import Image  # type: ignore
except Exception:
    Image = None

# Configuration
def load_config():
    """Load configuration from config.json, environment variables, or defaults"""
    config = {
        'port': 8081,
        'library_path': None
    }
    
    # Try to load from config.json first
    config_file = Path(__file__).parent / 'config.json'
    if config_file.exists():
        try:
            with open(config_file) as f:
                user_config = json.load(f)
                config.update(user_config)
                print(f"Loaded configuration from {config_file}")
        except Exception as e:
            print(f"Warning: Could not load config.json: {e}")
    
    # Override with environment variables if set
    if os.getenv('STOP_NOODLING_PORT'):
        config['port'] = int(os.getenv('STOP_NOODLING_PORT'))
    
    if os.getenv('STOP_NOODLING_LIBRARY_PATH'):
        config['library_path'] = Path(os.getenv('STOP_NOODLING_LIBRARY_PATH')).expanduser()

    if os.getenv('STOP_NOODLING_CROQUIS_COOKIES'):
        config['croquis_cookies'] = os.getenv('STOP_NOODLING_CROQUIS_COOKIES')

    if os.getenv('STOP_NOODLING_CROQUIS_USERNAME'):
        config['croquis_username'] = os.getenv('STOP_NOODLING_CROQUIS_USERNAME')

    if os.getenv('STOP_NOODLING_CROQUIS_PASSWORD'):
        config['croquis_password'] = os.getenv('STOP_NOODLING_CROQUIS_PASSWORD')
    
    # Auto-detect library path if not configured
    if not config['library_path']:
        if os.path.exists(Path.home() / "Pictures" / "Figure Drawing References.library"):
            config['library_path'] = Path.home() / "Pictures" / "Figure Drawing References.library"
        elif os.path.exists(Path.home() / "Figure Drawing References"):
            config['library_path'] = Path.home() / "Figure Drawing References"
        else:
            # Use default from config if provided
            default_path = '~/Pictures/Figure Drawing References.library'
            config['library_path'] = Path(default_path).expanduser()
            print(f"Warning: Using default library path (not found): {config['library_path']}")
    else:
        config['library_path'] = Path(config['library_path']).expanduser()
    
    return config

# Load configuration
CONFIG = load_config()
PORT = CONFIG['port']
LIBRARY_PATH = CONFIG['library_path']
IMAGES_DIR = LIBRARY_PATH / "images"
REMOTE_CACHE_DIR = Path(__file__).parent / ".remote_cache"
REMOTE_SESSIONS: Dict[str, Dict[str, object]] = {}
REMOTE_SESSIONS_LOCK = threading.Lock()
EAGLE_METADATA_LOCK = threading.Lock()

# Croquis Café – optional cookies file for subscription access
_croquis_cookies_raw = CONFIG.get('croquis_cookies')
CROQUIS_COOKIES_FILE: Optional[Path] = Path(_croquis_cookies_raw).expanduser() if _croquis_cookies_raw else None
if CROQUIS_COOKIES_FILE and not CROQUIS_COOKIES_FILE.exists():
    print(f"Warning: croquis_cookies file not found: {CROQUIS_COOKIES_FILE} (will attempt auto-login if credentials are configured)")
_croquis_username_raw = CONFIG.get('croquis_username')
CROQUIS_USERNAME: Optional[str] = str(_croquis_username_raw).strip() if _croquis_username_raw else None
_croquis_password_raw = CONFIG.get('croquis_password')
CROQUIS_PASSWORD: Optional[str] = str(_croquis_password_raw) if _croquis_password_raw else None

# In-memory cache for the Croquis model list — avoids re-fetching on every session start
_croquis_model_cache: Optional[Tuple[float, List[dict]]] = None
_croquis_model_cache_lock = threading.Lock()
CROQUIS_MODEL_CACHE_TTL_SECONDS = 3600

# Remote cache retention
# - TTL is a safety net: sessions normally clean up when the client requests it
# - We use the remote session directory mtime as the source of truth and "touch"
#   it on access (polling / image serving) so active sessions don't get reaped.
REMOTE_CACHE_MAX_AGE_SECONDS = int(os.getenv('STOP_NOODLING_REMOTE_CACHE_TTL_SECONDS', '86400'))
REMOTE_CACHE_REAPER_INTERVAL_SECONDS = int(os.getenv('STOP_NOODLING_REMOTE_CACHE_REAPER_INTERVAL_SECONDS', '3600'))

USER_AGENT = "StopNoodling/1.0 (https://github.com/vghpe/stop-noodeling)"
UNSPLASH_API_BASE = "https://api.unsplash.com"

UNSPLASH_RECENT_IDS_LOCK = threading.Lock()
UNSPLASH_RECENT_IDS: Deque[str] = deque(maxlen=1200)
UNSPLASH_RECENT_SET: Set[str] = set()

UNSPLASH_PREFERRED_TERMS = (
    'candid', 'natural', 'street', 'documentary', 'real', 'everyday',
    'lifestyle', 'unposed', 'authentic', 'expression', 'portrait'
)

UNSPLASH_REJECT_TERMS = (
    'stock', 'staged', 'posed', 'studio', 'product', 'mockup', 'template',
    'branding', 'advertising', 'commercial', 'catalog', 'wedding', 'glamour',
    'photoshoot', 'fashion shoot', 'headshot session', 'getty images',
    'shutterstock', 'istock', 'depositphotos', 'alamy'
)

UNSPLASH_QUERY_VARIANTS = (
    'candid portrait',
    'natural portrait',
    'street portrait',
    'documentary portrait',
    'environmental portrait',
    'unposed portrait',
    'lifestyle portrait',
    'people portrait'
)

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


def ensure_remote_cache_dir():
    REMOTE_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def touch_remote_cache_session(session_id: str):
    """Best-effort: update the session directory mtime so TTL cleanup keeps it."""
    try:
        session_dir = REMOTE_CACHE_DIR / session_id
        if session_dir.exists():
            os.utime(session_dir, None)
    except Exception:
        return


def cleanup_orphaned_remote_cache(max_age_seconds: int = 86400):
    ensure_remote_cache_dir()
    now = time.time()
    for entry in REMOTE_CACHE_DIR.iterdir():
        if not entry.is_dir():
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if now - mtime > max_age_seconds:
            shutil.rmtree(entry, ignore_errors=True)


def cleanup_remote_session(session_id: str):
    with REMOTE_SESSIONS_LOCK:
        session_info = REMOTE_SESSIONS.pop(session_id, None)
    if session_info and session_info.get('path'):
        session_dir = Path(session_info['path'])
    else:
        session_dir = REMOTE_CACHE_DIR / session_id

    if session_dir.exists():
        shutil.rmtree(session_dir, ignore_errors=True)


def cleanup_stale_remote_sessions(max_age_seconds: int):
    """Cleanup stale in-memory sessions (and their cache dirs) based on dir mtime."""
    now = time.time()
    stale_ids: List[str] = []
    with REMOTE_SESSIONS_LOCK:
        for session_id, session_info in REMOTE_SESSIONS.items():
            session_path = session_info.get('path')
            session_dir = Path(session_path) if session_path else (REMOTE_CACHE_DIR / session_id)
            try:
                mtime = session_dir.stat().st_mtime
            except OSError:
                stale_ids.append(session_id)
                continue
            if now - mtime > max_age_seconds:
                stale_ids.append(session_id)

    for session_id in stale_ids:
        cleanup_remote_session(session_id)


def remote_cache_reaper_loop():
    """Background safety net that periodically deletes stale remote cache sessions."""
    while True:
        try:
            cleanup_stale_remote_sessions(REMOTE_CACHE_MAX_AGE_SECONDS)
            cleanup_orphaned_remote_cache(REMOTE_CACHE_MAX_AGE_SECONDS)
        except Exception as e:
            print(f"[Remote Cache Reaper] Error: {e}")
        time.sleep(REMOTE_CACHE_REAPER_INTERVAL_SECONDS)


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
    except Exception as e:
        return False


def normalize_unsplash_text(value: object) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    return " ".join(text.split()).strip()


def unsplash_reserve_recent_ids(ids: List[str]):
    """Reserve IDs in a bounded in-memory history to avoid near-term repeats across sessions."""
    if not ids:
        return

    with UNSPLASH_RECENT_IDS_LOCK:
        for image_id in ids:
            if image_id in UNSPLASH_RECENT_SET:
                continue
            if len(UNSPLASH_RECENT_IDS) == UNSPLASH_RECENT_IDS.maxlen:
                expired = UNSPLASH_RECENT_IDS.popleft()
                UNSPLASH_RECENT_SET.discard(expired)
            UNSPLASH_RECENT_IDS.append(image_id)
            UNSPLASH_RECENT_SET.add(image_id)


def unsplash_recent_ids_snapshot() -> Set[str]:
    with UNSPLASH_RECENT_IDS_LOCK:
        return set(UNSPLASH_RECENT_SET)


def unsplash_score_and_validate(photo: dict) -> Optional[Tuple[int, dict, str]]:
    """Return (score, mapped_image, photographer_key) for valid photos, otherwise None."""
    if not isinstance(photo, dict):
        return None

    photo_id = str(photo.get('id') or '').strip()
    urls = photo.get('urls') or {}
    links = photo.get('links') or {}
    user = photo.get('user') or {}

    if not photo_id:
        return None

    image_url = urls.get('regular') or urls.get('small') or urls.get('full')
    thumb_url = urls.get('small') or urls.get('thumb') or image_url
    if not image_url:
        return None

    width = int(photo.get('width') or 0)
    height = int(photo.get('height') or 0)
    # Portrait is a hard requirement.
    if width <= 0 or height <= 0:
        return None
    if height <= width:
        return None

    description = normalize_unsplash_text(photo.get('description'))
    alt_description = normalize_unsplash_text(photo.get('alt_description'))
    user_name = normalize_unsplash_text(user.get('name'))
    user_username = normalize_unsplash_text(user.get('username'))
    user_id = normalize_unsplash_text(user.get('id'))
    user_bio = normalize_unsplash_text(user.get('bio'))
    user_portfolio = normalize_unsplash_text(user.get('portfolio_url'))

    tags: List[str] = []
    raw_tags = photo.get('tags') or []
    if isinstance(raw_tags, list):
        for tag in raw_tags:
            if not isinstance(tag, dict):
                continue
            title = normalize_unsplash_text(tag.get('title'))
            if title:
                tags.append(title)

    searchable_text = " ".join([
        description.lower(),
        alt_description.lower(),
        " ".join(tags).lower(),
        user_name.lower(),
        user_username.lower(),
        user_bio.lower(),
        user_portfolio.lower(),
    ])

    # Hard block known stock/distributor indicators.
    if 'gettyimages' in searchable_text or 'getty images' in searchable_text:
        return None

    if any(term in searchable_text for term in UNSPLASH_REJECT_TERMS):
        return None

    score = 0
    if height > width and width >= 500 and height >= 700:
        score += 6

    preferred_hits = sum(1 for term in UNSPLASH_PREFERRED_TERMS if term in searchable_text)
    score += preferred_hits * 3

    # Slight preference for images that include person tags/descriptions.
    people_terms = ('person', 'people', 'portrait', 'face', 'man', 'woman')
    people_hits = sum(1 for term in people_terms if term in searchable_text)
    score += people_hits * 2
    score += random.randint(0, 2)

    name = description or alt_description or f"Unsplash {photo_id}"

    mapped = {
        'id': f"unsplash:{photo_id}",
        'name': name,
        'image_path': image_url,
        'thumbnail_path': thumb_url,
        'tags': tags,
        'folder': None,
        'is_remote': True,
        'source': 'unsplash',
        'attribution_url': links.get('html') or '',
        'download_location': links.get('download_location') or '',
        'attribution_name': user_name,
        'attribution_username': user_username
    }

    photographer_key = user_id or user_username or f"photo:{photo_id}"

    return score, mapped, photographer_key


def fetch_unsplash_photos(count: int, query: str) -> List[dict]:
    access_key = str(CONFIG.get('unsplash_access_key') or '').strip()
    if not access_key:
        raise ValueError("Unsplash access key missing. Set 'unsplash_access_key' in config.json")

    safe_count = max(1, min(count, 30))
    safe_query = query.strip() or str(CONFIG.get('unsplash_query') or 'people')

    # Pull several portrait search batches with randomized query/page to avoid repetitive top results.
    candidates_by_id: Dict[str, Tuple[int, dict]] = {}
    candidate_photographer: Dict[str, str] = {}
    recent_ids = unsplash_recent_ids_snapshot()
    attempts = 0
    max_attempts = 4

    query_variants: List[str] = []
    if safe_query:
        query_variants.extend([
            f"{safe_query} portrait",
            f"candid {safe_query} portrait",
            f"natural {safe_query} portrait",
            f"street {safe_query} portrait"
        ])
    query_variants.extend(UNSPLASH_QUERY_VARIANTS)
    # De-duplicate while preserving order.
    deduped_query_variants: List[str] = []
    seen_queries = set()
    for q in query_variants:
        cleaned = " ".join(q.split()).strip()
        if not cleaned or cleaned in seen_queries:
            continue
        seen_queries.add(cleaned)
        deduped_query_variants.append(cleaned)

    if not deduped_query_variants:
        deduped_query_variants = ['people portrait']

    while len(candidates_by_id) < safe_count and attempts < max_attempts:
        attempts += 1
        batch_size = min(30, max(safe_count * 2, 20))
        selected_query = random.choice(deduped_query_variants)
        random_page = random.randint(2, 120)
        order_by = random.choice(['latest', 'relevant'])

        params = {
            'query': selected_query,
            'page': str(random_page),
            'per_page': str(batch_size),
            'order_by': order_by,
            'content_filter': 'high',
            'orientation': 'portrait'
        }
        url = f"{UNSPLASH_API_BASE}/search/photos?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url,
            headers={
                'Authorization': f'Client-ID {access_key}',
                'Accept-Version': 'v1',
                'Accept': 'application/json',
                'User-Agent': USER_AGENT
            }
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                remaining = response.headers.get('X-Ratelimit-Remaining')
                if remaining is not None:
                    print(f"[Unsplash] Rate limit remaining: {remaining}")
                data = json.load(response)
        except (TimeoutError, socket.timeout, urllib.error.URLError) as e:
            print(f"[Unsplash] API call failed (attempt {attempts}): {e}")
            continue
        except urllib.error.HTTPError as e:
            print(f"[Unsplash] API error {e.code} (attempt {attempts}): {e.reason}")
            continue

        results = data.get('results') if isinstance(data, dict) else []
        if not isinstance(results, list):
            results = []

        for photo in results:
            scored = unsplash_score_and_validate(photo)
            if not scored:
                continue

            score, mapped, photographer_key = scored
            photo_id = str(mapped.get('id') or '')
            if not photo_id or photo_id in recent_ids:
                continue

            existing = candidates_by_id.get(photo_id)
            if not existing or score > existing[0]:
                candidates_by_id[photo_id] = (score, mapped)
                candidate_photographer[photo_id] = photographer_key

    ranked = sorted(candidates_by_id.values(), key=lambda item: item[0], reverse=True)

    selected: List[dict] = []
    photographers_in_session: Set[str] = set()
    for score, mapped in ranked:
        if len(selected) >= safe_count:
            break
        photo_id = str(mapped.get('id') or '')
        photographer_key = candidate_photographer.get(photo_id, photo_id)
        if photographer_key in photographers_in_session:
            continue
        selected.append(mapped)
        photographers_in_session.add(photographer_key)

    # If strict photographer uniqueness leaves a gap, fill from remaining ranked items.
    if len(selected) < safe_count:
        selected_ids = {str(img.get('id') or '') for img in selected}
        for score, mapped in ranked:
            if len(selected) >= safe_count:
                break
            photo_id = str(mapped.get('id') or '')
            if photo_id in selected_ids:
                continue
            selected.append(mapped)
            selected_ids.add(photo_id)

    selected_ids = [str(img.get('id') or '') for img in selected]
    unsplash_reserve_recent_ids([image_id for image_id in selected_ids if image_id])

    return selected


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
    except (TimeoutError, socket.timeout, urllib.error.URLError) as e:
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
    except (TimeoutError, socket.timeout, urllib.error.URLError) as e:
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

# ---------------------------------------------------------------------------
# Croquis Café source
# ---------------------------------------------------------------------------

CROQUIS_LOGIN_URL = "https://croquis.cafe/my-account/"


def _build_croquis_opener(jar: MozillaCookieJar):
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [
        ("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:150.0) Gecko/20100101 Firefox/150.0"),
        ("Referer", "https://croquis.cafe/"),
    ]
    return opener


def croquis_auto_login_available() -> bool:
    return bool(CROQUIS_COOKIES_FILE and CROQUIS_USERNAME and CROQUIS_PASSWORD)


def refresh_croquis_cookies() -> Optional[object]:
    """Log in to Croquis Café and persist a fresh cookie jar when credentials are configured."""
    if not croquis_auto_login_available():
        return None

    jar = MozillaCookieJar(str(CROQUIS_COOKIES_FILE))
    opener = _build_croquis_opener(jar)

    try:
        with opener.open(urllib.request.Request(CROQUIS_LOGIN_URL), timeout=30) as resp:
            login_html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[Croquis] Auto-login setup failed: {e}")
        return None

    nonce_match = re.search(
        r'name="woocommerce-login-nonce"\s+value="([^"]+)"', login_html
    )
    if not nonce_match:
        print("[Croquis] Auto-login failed: login nonce not found")
        return None

    form_data = urllib.parse.urlencode({
        "username": CROQUIS_USERNAME,
        "password": CROQUIS_PASSWORD,
        "rememberme": "forever",
        "woocommerce-login-nonce": nonce_match.group(1),
        "_wp_http_referer": "/my-account/",
        "login": "Log in",
    }).encode("utf-8")

    request = urllib.request.Request(
        CROQUIS_LOGIN_URL,
        data=form_data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://croquis.cafe",
            "Referer": CROQUIS_LOGIN_URL,
        },
    )

    try:
        with opener.open(request, timeout=30) as resp:
            response_html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[Croquis] Auto-login request failed: {e}")
        return None

    if not any(marker in response_html for marker in ("customer-logout", "Log out", "edit-account")):
        print("[Croquis] Auto-login failed: login markers not found in response")
        return None

    try:
        jar.save(ignore_discard=True, ignore_expires=True)
    except Exception as e:
        print(f"[Croquis] Auto-login succeeded but cookies could not be saved: {e}")
        return None

    print(f"[Croquis] Refreshed cookies at {CROQUIS_COOKIES_FILE}")
    return opener


def get_croquis_opener():
    """Return a urllib opener with croquis.cafe cookies, or None if not configured."""
    if CROQUIS_COOKIES_FILE is None:
        return None
    if not CROQUIS_COOKIES_FILE.exists():
        if croquis_auto_login_available():
            print("[Croquis] Cookies file missing; attempting automatic login")
            refreshed = refresh_croquis_cookies()
            if refreshed is not None:
                return refreshed
        return None
    jar = MozillaCookieJar(str(CROQUIS_COOKIES_FILE))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as e:
        print(f"[Croquis] Warning: could not load cookies: {e}")
        return None
    return _build_croquis_opener(jar)


CROQUIS_API_BASE = "https://croquis.cafe/wp-json/wp/v2"
CROQUIS_PREFERRED_SIZES = ("large", "medium_large", "medium")  # in preference order
CROQUIS_FINE_ARTS_CAT_ID = 1424  # "Fine Arts Photos" taxonomy term — filters out landscapes/still life


def _croquis_best_size_url(media_item: dict) -> Optional[str]:
    """Return the best usable image URL from a WP REST media item."""
    sizes = media_item.get("media_details", {}).get("sizes", {})
    for sz in CROQUIS_PREFERRED_SIZES:
        if sz in sizes:
            return sizes[sz].get("source_url")
    # Fall back to full/original
    return media_item.get("source_url")


def fetch_croquis_model_list(opener) -> List[dict]:
    """
    Fetch all active model taxonomy terms from the WP REST API.
    Returns list of dicts with keys: id, slug, name, count.
    Page 1 is fetched first to determine total_pages; remaining pages are fetched in parallel.
    """
    def _fetch_page(page: int) -> Tuple[List[dict], int]:
        url = f"{CROQUIS_API_BASE}/croq_model_name?per_page=100&page={page}&_fields=id,slug,name,count"
        try:
            with opener.open(urllib.request.Request(url), timeout=15) as resp:
                batch = json.loads(resp.read())
                total = int(resp.headers.get("X-WP-TotalPages", 1))
                return batch, total
        except Exception as e:
            print(f"[Croquis] Failed to fetch model list page {page}: {e}")
            return [], 1

    first_batch, total_pages = _fetch_page(1)
    all_models: List[dict] = list(first_batch)

    if total_pages > 1:
        with ThreadPoolExecutor(max_workers=min(total_pages - 1, 5)) as ex:
            for batch, _ in ex.map(_fetch_page, range(2, total_pages + 1)):
                all_models.extend(batch)

    return [m for m in all_models if m.get("count", 0) > 0]


def _get_croquis_models_cached(opener) -> List[dict]:
    """Return model list from in-memory cache, re-fetching if stale or absent."""
    global _croquis_model_cache
    with _croquis_model_cache_lock:
        if _croquis_model_cache is not None:
            ts, models = _croquis_model_cache
            if time.time() - ts < CROQUIS_MODEL_CACHE_TTL_SECONDS:
                print(f"[Croquis] Using cached model list ({len(models)} models)")
                return models
    models = fetch_croquis_model_list(opener)
    with _croquis_model_cache_lock:
        _croquis_model_cache = (time.time(), models)
    return models


def fetch_croquis_model_urls(model: dict, opener, per_page: int = 30) -> List[str]:
    """
    Fetch image URLs for a model via the WP REST media API.
    Picks a random page so repeated calls yield variety.
    """
    total_count = model.get("count", per_page)
    total_pages = max(1, math.ceil(total_count / per_page))
    rand_page = random.randint(1, total_pages)
    url = (
        f"{CROQUIS_API_BASE}/media"
        f"?croq_model_name={model['id']}&per_page={per_page}&page={rand_page}"
        f"&_fields=id,source_url,media_details,croq_image_cats"
    )
    try:
        with opener.open(urllib.request.Request(url), timeout=15) as resp:
            items = json.loads(resp.read())
    except Exception as e:
        print(f"[Croquis] Failed to fetch images for {model['slug']}: {e}")
        return []
    # Keep only Fine Arts Photos (poses/figure drawing); excludes landscapes, still life, etc.
    items = [item for item in items if CROQUIS_FINE_ARTS_CAT_ID in item.get("croq_image_cats", [])]
    urls = [_croquis_best_size_url(item) for item in items]
    return [u for u in urls if u]


def download_croquis_image(
    url: str, session_dir: Path, session_id: str, opener
) -> Optional[dict]:
    """Download one croquis image to the session cache. Returns an image dict or None."""
    filename = url.split("/")[-1]
    dest = session_dir / filename
    try:
        with opener.open(urllib.request.Request(url), timeout=30) as resp:
            if resp.status != 200:
                print(f"[Croquis] HTTP {resp.status} for {filename}")
                return None
            dest.write_bytes(resp.read())
    except Exception as e:
        print(f"[Croquis] Download failed {filename}: {e}")
        return None

    # Derive HQ URL by stripping the size suffix (e.g. -1024x684) before the extension.
    # Stored server-side only (_hq_cdn_url) for on-demand fetch; never sent to the client.
    hq_cdn_url = re.sub(r'-\d+x\d+(?=\.[^.]+$)', '', url)

    # Clean display name: strip size suffix from stem
    name = re.sub(r'-\d+x\d+$', '', dest.stem)
    return {
        'id': f"croquis:{filename}",
        'name': name,
        'image_path': f"/api/remote-image/{session_id}/{urllib.parse.quote(filename)}",
        '_hq_cdn_url': hq_cdn_url if hq_cdn_url != url else None,
        'thumbnail_path': None,
        'tags': [],
        'folder': None,
        'is_remote': True,
        'source': 'croquis',
        'attribution_url': 'https://croquis.cafe/',
    }


def fetch_croquis_photos_background(
    count: int, session_id: str, session_dir: Path, opener
):
    """Continue downloading croquis images in background until count is reached."""
    try:
        with REMOTE_SESSIONS_LOCK:
            session = REMOTE_SESSIONS.get(session_id)
            if not session:
                return
            images = session['images']  # shared list
            pool: List[str] = session['_pool']  # remaining URLs; only this thread pops

        with ThreadPoolExecutor(max_workers=4) as executor:
            pending: Dict = {}

            def _submit():
                if not pool:
                    return False
                url = pool.pop(0)
                pending[executor.submit(
                    download_croquis_image, url, session_dir, session_id, opener
                )] = url
                return True

            # Prime the pool
            while len(pending) < 8:
                with REMOTE_SESSIONS_LOCK:
                    if len(images) + len(pending) >= count:
                        break
                if not _submit():
                    break

            while pending:
                done, _ = wait(list(pending.keys()), return_when=FIRST_COMPLETED, timeout=30)
                if not done:
                    break
                for fut in list(done):
                    pending.pop(fut, None)
                    result = fut.result()
                    with REMOTE_SESSIONS_LOCK:
                        if session_id not in REMOTE_SESSIONS:
                            return
                        if result and len(images) < count:
                            images.append(result)
                    with REMOTE_SESSIONS_LOCK:
                        current = len(images)
                    if current >= count or not pool:
                        continue
                    _submit()

        with REMOTE_SESSIONS_LOCK:
            if session_id in REMOTE_SESSIONS:
                REMOTE_SESSIONS[session_id]['fetching'] = False
                print(f"[Croquis Background] Done: {len(images)} images")
    except Exception as e:
        print(f"[Croquis Background] Error: {e}")
        with REMOTE_SESSIONS_LOCK:
            if session_id in REMOTE_SESSIONS:
                REMOTE_SESSIONS[session_id]['fetching'] = False


def fetch_croquis_photos_initial(
    count: int, session_id: str, session_dir: Path
) -> List[dict]:
    """
    Use the WP REST API to build a URL pool from random models,
    download an initial batch synchronously, then hand the rest to a background thread.
    """
    opener = get_croquis_opener()
    if opener is None:
        raise RuntimeError(
            "Croquis cookies not configured. Add 'croquis_cookies' to config.json."
        )

    def _build_pool() -> List[str]:
        all_models = _get_croquis_models_cached(opener)
        if not all_models:
            return []
        n_models = max(3, (count // 15) + 2)
        chosen = random.sample(all_models, min(n_models, len(all_models)))
        print(f"[Croquis] Fetching from {len(chosen)} models via REST API: {[m['slug'] for m in chosen]}")
        urls: List[str] = []
        with ThreadPoolExecutor(max_workers=len(chosen)) as ex:
            for batch in ex.map(lambda m: fetch_croquis_model_urls(m, opener), chosen):
                urls.extend(batch)
        return urls

    pool = _build_pool()

    if not pool and croquis_auto_login_available():
        print("[Croquis] No images from REST API; attempting automatic cookie refresh")
        refreshed_opener = refresh_croquis_cookies()
        if refreshed_opener is not None:
            opener = refreshed_opener
            pool = _build_pool()

    if not pool:
        if croquis_auto_login_available():
            raise RuntimeError(
                "No images found from Croquis after automatic login refresh. Check Croquis credentials."
            )
        raise RuntimeError(
            "No images found from Croquis. Cookies likely expired; refresh the Croquis cookie file or configure Croquis credentials for auto-login."
        )

    random.shuffle(pool)
    pool = pool[: count * 2]  # cap pool; plenty of variety
    print(f"[Croquis] Pool: {len(pool)} URLs, targeting {count}")

    min_initial = min(1, count)
    images: List[dict] = []

    # Register session early so the background thread can find it
    with REMOTE_SESSIONS_LOCK:
        REMOTE_SESSIONS[session_id] = {
            'path': str(session_dir),
            'created_at': time.time(),
            'images': images,
            '_pool': pool,
            'target_count': count,
            'fetching': True,
        }

    # Download initial batch synchronously
    with ThreadPoolExecutor(max_workers=4) as executor:
        pending: Dict = {}

        def _submit_initial():
            if not pool:
                return False
            url = pool.pop(0)
            pending[executor.submit(
                download_croquis_image, url, session_dir, session_id, opener
            )] = url
            return True

        for _ in range(min(8, len(pool))):
            _submit_initial()

        while pending and len(images) < min_initial:
            done, _ = wait(list(pending.keys()), return_when=FIRST_COMPLETED, timeout=15)
            if not done:
                break
            for fut in list(done):
                pending.pop(fut, None)
                result = fut.result()
                if result:
                    images.append(result)
                if len(images) < min_initial:
                    _submit_initial()

        # Cancel remaining futures; background thread will pick up from the pool
        for fut in list(pending.keys()):
            fut.cancel()

    fetching = len(images) < count and bool(pool)
    with REMOTE_SESSIONS_LOCK:
        if session_id in REMOTE_SESSIONS:
            REMOTE_SESSIONS[session_id]['fetching'] = fetching

    if fetching:
        thread = threading.Thread(
            target=fetch_croquis_photos_background,
            args=(count, session_id, session_dir, opener),
            daemon=True,
        )
        thread.start()

    print(f"[Croquis] Returning {len(images)} initial images")
    return images


def _public_image_fields(images: List[dict]) -> List[dict]:
    """Strip server-internal fields (prefixed with _) before sending image lists to the client."""
    return [{k: v for k, v in img.items() if not k.startswith('_')} for img in images]


# ---------------------------------------------------------------------------
# Pack cache — built once at startup in a background thread
# ---------------------------------------------------------------------------

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


class FigureStudyHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP request handler for Stop Noodling"""
    
    def do_GET(self):
        """Handle GET requests"""
        parsed_path = urllib.parse.urlparse(self.path)
        
        if parsed_path.path == '/':
            # Serve the main HTML file
            self.serve_index()
        elif parsed_path.path == '/api/session':
            # Create a new study session
            self.create_session(parsed_path.query)
        elif parsed_path.path == '/api/remote-session':
            # Create a new remote study session (Wikimedia)
            self.create_remote_session(parsed_path.query)
        elif parsed_path.path.startswith('/api/remote-session/'):
            # Get additional images for a remote session
            session_id = parsed_path.path.split('/')[-1]
            self.get_remote_session_images(session_id)
        elif parsed_path.path.startswith('/api/image/'):
            # Serve an image file
            self.serve_image(parsed_path.path)
        elif parsed_path.path.startswith('/api/remote-image/'):
            # Serve a cached remote image file
            self.serve_remote_image(parsed_path.path)
        elif parsed_path.path.startswith('/api/croquis-hq/'):
            # On-demand HQ fetch for a Croquis image
            session_id = parsed_path.path.split('/')[-1]
            params = urllib.parse.parse_qs(parsed_path.query)
            image_id = params.get('id', [''])[0]
            self.fetch_croquis_hq_image(session_id, image_id)
        else:
            self.send_error(404, "Not Found")
    
    def do_POST(self):
        """Handle POST requests"""
        if self.path == '/api/favorite':
            self.toggle_favorite()
        elif self.path == '/api/remote-session/cleanup':
            self.cleanup_remote_session()
        else:
            self.send_error(404, "Not Found")
    
    def serve_index(self):
        """Serve the main HTML file"""
        try:
            with open('index.html', 'rb') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', len(content))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_error(404, "index.html not found")
    
    def create_session(self, query_string: str):
        """Create a new study session with random images"""
        start_time = time.time()

        # Parse query parameters
        params = urllib.parse.parse_qs(query_string)
        count = int(params.get('count', ['20'])[0])
        enabled_tags_param = params.get('enabled_tags', ['figure,handsfeet,costumes,portraits'])[0]
        enabled_tags = set(t.strip() for t in enabled_tags_param.split(',') if t.strip())
        pack_mode_raw = params.get('packs', ['all'])[0]
        pack_mode = pack_mode_raw if pack_mode_raw in ('1', '3', 'all') else 'all'

        print(f"\n[Session Request] enabled_tags={enabled_tags}, count={count}, packs={pack_mode}")

        try:
            if not IMAGES_DIR.exists():
                self.send_json_error(500, f"Library not found at {IMAGES_DIR}")
                return

            # Use the pre-built pack cache when ready (√-weighted, category-filtered)
            if PACK_CACHE.is_ready():
                images = PACK_CACHE.sample(count, pack_mode, enabled_tags)
                elapsed_time = time.time() - start_time
                print(f"[Performance] PackCache returned {len(images)} images in {elapsed_time:.3f}s (packs={pack_mode})")
            else:
                # Cache still warming — fall back to legacy per-image shuffle
                print("[PackCache] Not ready yet, falling back to legacy scan")
                all_folders = [d for d in IMAGES_DIR.iterdir() if d.is_dir()]
                if not all_folders:
                    self.send_json_error(500, "No images found in library")
                    return

                images = []
                available_folders = all_folders.copy()
                random.shuffle(available_folders)
                folders_checked = 0

                for folder in available_folders:
                    if len(images) >= count:
                        break
                    folders_checked += 1
                    metadata_file = folder / "metadata.json"
                    if not metadata_file.exists():
                        continue
                    try:
                        with open(metadata_file, 'r', encoding='utf-8') as f:
                            metadata = json.load(f)
                        if metadata.get('isDeleted', False):
                            continue
                        image_tags = metadata.get('tags', [])
                        if 'ignore' in image_tags:
                            continue
                        if 'handsfeet' in image_tags:
                            image_category = 'handsfeet'
                        elif 'costumes' in image_tags:
                            image_category = 'costumes'
                        elif 'portraits' in image_tags:
                            image_category = 'portraits'
                        else:
                            image_category = 'figure'
                        if image_category not in enabled_tags:
                            continue
                        image_files = [
                            f for f in folder.iterdir()
                            if f.is_file()
                            and not f.name.endswith('_thumbnail.png')
                            and f.name != 'metadata.json'
                            and f.suffix.lower() in ('.jpg', '.jpeg', '.png', '.gif', '.webp')
                        ]
                        if image_files:
                            image_file = image_files[0]
                            thumbnail_files = [f for f in folder.iterdir() if f.is_file() and f.name.endswith('_thumbnail.png')]
                            thumbnail_file = thumbnail_files[0] if thumbnail_files else None
                            images.append({
                                'id': metadata['id'],
                                'name': metadata.get('name', image_file.stem),
                                'image_path': f"/api/image/{folder.name}/{urllib.parse.quote(image_file.name)}",
                                'thumbnail_path': f"/api/image/{folder.name}/{urllib.parse.quote(thumbnail_file.name)}" if thumbnail_file else None,
                                'tags': image_tags,
                                'folder': folder.name,
                            })
                    except (json.JSONDecodeError, KeyError) as e:
                        print(f"Error reading metadata for {folder.name}: {e}")
                        continue

                random.shuffle(images)
                elapsed_time = time.time() - start_time
                print(f"[Performance] Legacy scan: {len(images)} images from {folders_checked} folders in {elapsed_time:.3f}s")

            response = {
                'success': True,
                'images': images,
                'total': len(images),
            }
            if len(images) < count:
                response['warning'] = f'Only found {len(images)} images matching the selected filters (requested {count})'

            self.send_json_response(response)

        except Exception as e:
            self.send_json_error(500, f"Error creating session: {str(e)}")

    def create_remote_session(self, query_string: str):
        """Create a remote session using supported remote providers"""
        start_time = time.time()

        params = urllib.parse.parse_qs(query_string)
        count = int(params.get('count', ['20'])[0])
        source = params.get('source', ['wikimedia'])[0]
        query = params.get('query', [''])[0]

        if source not in ('wikimedia', 'unsplash', 'croquis'):
            self.send_json_error(400, f"Unsupported source: {source}")
            return

        if source == 'croquis':
            cleanup_orphaned_remote_cache()
            ensure_remote_cache_dir()
            session_id = uuid.uuid4().hex
            session_dir = REMOTE_CACHE_DIR / session_id
            session_dir.mkdir(parents=True, exist_ok=True)
            touch_remote_cache_session(session_id)
            try:
                images = fetch_croquis_photos_initial(count, session_id, session_dir)
                elapsed_time = time.time() - start_time
                if not images:
                    cleanup_remote_session(session_id)
                    self.send_json_error(500, "No images found from Croquis")
                    return
                print(f"[Performance] Croquis returned {len(images)} initial images in {elapsed_time:.3f}s")
                self.send_json_response({
                    'success': True,
                    'images': _public_image_fields(images),
                    'total': len(images),
                    'source': 'croquis',
                    'session_id': session_id,
                    'fetching': REMOTE_SESSIONS.get(session_id, {}).get('fetching', False),
                })
            except RuntimeError as e:
                cleanup_remote_session(session_id)
                self.send_json_error(503, str(e))
            except Exception as e:
                cleanup_remote_session(session_id)
                self.send_json_error(500, f"Error creating Croquis session: {str(e)}")
            return

        if source == 'unsplash':
            try:
                images = fetch_unsplash_photos(count, query)
                if not images:
                    self.send_json_error(500, "No images found from Unsplash")
                    return

                elapsed_time = time.time() - start_time
                print(f"[Performance] Unsplash returned {len(images)} images in {elapsed_time:.3f}s")

                self.send_json_response({
                    'success': True,
                    'images': images,
                    'total': len(images),
                    'source': 'unsplash',
                    'session_id': None,
                    'fetching': False,
                    'warning': f"Only found {len(images)} portrait candid/natural images (requested {count})" if len(images) < count else None
                })
                return
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode('utf-8', errors='replace')
                except Exception:
                    detail = ""
                self.send_json_error(502, f"Unsplash API error ({e.code}): {detail or e.reason}")
                return
            except Exception as e:
                self.send_json_error(500, f"Error creating Unsplash session: {str(e)}")
                return

        cleanup_orphaned_remote_cache()
        ensure_remote_cache_dir()

        session_id = uuid.uuid4().hex
        session_dir = REMOTE_CACHE_DIR / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        touch_remote_cache_session(session_id)

        try:
            images = fetch_wikimedia_photos_initial(count, session_id, session_dir)
            elapsed_time = time.time() - start_time

            if not images:
                cleanup_remote_session(session_id)
                self.send_json_error(500, "No images found from Wikimedia")
                return

            print(f"[Performance] Returned {len(images)} initial images in {elapsed_time:.3f}s (fetching continues in background)")

            response = {
                'success': True,
                'images': images,
                'total': len(images),
                'source': 'wikimedia',
                'session_id': session_id,
                'fetching': True  # Indicates more images coming
            }

            self.send_json_response(response)
        except Exception as e:
            cleanup_remote_session(session_id)
            self.send_json_error(500, f"Error creating remote session: {str(e)}")
    
    def get_remote_session_images(self, session_id: str):
        """Get current images for a remote session"""
        with REMOTE_SESSIONS_LOCK:
            session = REMOTE_SESSIONS.get(session_id)
            if not session:
                self.send_json_error(404, "Session not found")
                return
            
            images = _public_image_fields(session['images'])
            fetching = session['fetching']

        touch_remote_cache_session(session_id)

        response = {
            'success': True,
            'images': images,
            'total': len(images),
            'fetching': fetching
        }
        
        self.send_json_response(response)

    def fetch_croquis_hq_image(self, session_id: str, image_id: str):
        """On-demand: download (and cache) the HQ version of a Croquis session image."""
        if not session_id or not image_id:
            self.send_json_error(400, "Missing session_id or image id")
            return

        with REMOTE_SESSIONS_LOCK:
            session = REMOTE_SESSIONS.get(session_id)
            if not session:
                self.send_json_error(404, "Session not found or already cleaned up")
                return
            target = next((img for img in session['images'] if img.get('id') == image_id), None)
            if not target:
                self.send_json_error(404, "Image not found in session")
                return
            # Already cached from a previous request — return immediately
            if target.get('hq_path'):
                self.send_json_response({'success': True, 'hq_path': target['hq_path']})
                return
            hq_cdn_url = target.get('_hq_cdn_url')

        if not hq_cdn_url:
            self.send_json_error(400, "No HQ version available for this image")
            return

        opener = get_croquis_opener()
        if not opener:
            self.send_json_error(503, "Croquis not configured or session expired")
            return

        session_dir = REMOTE_CACHE_DIR / session_id
        hq_filename = hq_cdn_url.split('/')[-1]
        hq_dest = session_dir / hq_filename

        try:
            with opener.open(urllib.request.Request(hq_cdn_url), timeout=60) as resp:
                if resp.status != 200:
                    self.send_json_error(502, f"CDN returned HTTP {resp.status}")
                    return
                hq_dest.write_bytes(resp.read())
        except Exception as e:
            self.send_json_error(502, f"HQ download failed: {e}")
            return

        hq_path = f"/api/remote-image/{session_id}/{urllib.parse.quote(hq_filename)}"
        with REMOTE_SESSIONS_LOCK:
            if session_id in REMOTE_SESSIONS:
                for img in REMOTE_SESSIONS[session_id]['images']:
                    if img.get('id') == image_id:
                        img['hq_path'] = hq_path
                        break

        touch_remote_cache_session(session_id)
        print(f"[Croquis HQ] Fetched on demand: {hq_filename}")
        self.send_json_response({'success': True, 'hq_path': hq_path})

    def serve_image(self, path: str):
        """Serve an image file from the library"""
        # Extract folder and filename from path
        # Format: /api/image/{folder_name}/{filename}
        parts = path.split('/')
        if len(parts) < 4:
            self.send_error(400, "Invalid image path")
            return
        
        folder_name = parts[3]
        filename = '/'.join(parts[4:])  # Handle filenames with slashes
        
        # URL decode the filename
        filename = urllib.parse.unquote(filename)
        
        image_path = IMAGES_DIR / folder_name / filename
        
        if not image_path.exists() or not image_path.is_file():
            self.send_error(404, "Image not found")
            return
        
        try:
            with open(image_path, 'rb') as f:
                content = f.read()
            
            # Determine content type
            ext = image_path.suffix.lower()
            content_types = {
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.png': 'image/png',
                '.gif': 'image/gif',
                '.webp': 'image/webp'
            }
            content_type = content_types.get(ext, 'application/octet-stream')
            
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', len(content))
            self.send_header('Cache-Control', 'public, max-age=86400')  # Cache for 1 day
            self.end_headers()
            self.wfile.write(content)
            
        except Exception as e:
            self.send_error(500, f"Error serving image: {str(e)}")

    def serve_remote_image(self, path: str):
        """Serve an image file from the remote cache"""
        parts = path.split('/')
        if len(parts) < 4:
            self.send_error(400, "Invalid remote image path")
            return

        session_id = parts[3]
        filename = '/'.join(parts[4:])
        filename = urllib.parse.unquote(filename)

        touch_remote_cache_session(session_id)

        session_dir = REMOTE_CACHE_DIR / session_id
        image_path = session_dir / filename

        try:
            if not image_path.resolve().is_file() or not str(image_path.resolve()).startswith(str(session_dir.resolve())):
                self.send_error(404, "Image not found")
                return
        except FileNotFoundError:
            self.send_error(404, "Image not found")
            return

        try:
            with open(image_path, 'rb') as f:
                content = f.read()

            ext = image_path.suffix.lower()
            content_types = {
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.png': 'image/png',
                '.gif': 'image/gif',
                '.webp': 'image/webp'
            }
            content_type = content_types.get(ext, 'application/octet-stream')

            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', len(content))
            self.send_header('Cache-Control', 'public, max-age=86400')
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_error(500, f"Error serving remote image: {str(e)}")
    
    def toggle_favorite(self):
        """Toggle the 'study-favorite' tag on an image"""
        try:
            # Read request body
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            
            folder_name = data.get('folder')
            is_remote = data.get('is_remote', False)
            image_data = data.get('image_data')  # Full image metadata for remote images
            
            if is_remote and image_data:
                # Favoriting a remote image - copy it to Eagle library
                session_id = data.get('session_id')
                remote_source = str(image_data.get('source', 'wikimedia')).lower()
                if remote_source in ('wikimedia', 'croquis') and not session_id:
                    self.send_json_error(400, f"Missing session_id for {remote_source} image. Session may have been cleared.")
                    return
                
                # Ensure Eagle images directory exists
                if not IMAGES_DIR.exists():
                    self.send_json_error(500, f"Eagle library not found at {IMAGES_DIR}")
                    return
                
                # Derive a stable ID from the provider item id to avoid duplicates on repeated starring.
                remote_id = str(image_data.get('id', ''))
                page_id = remote_id.split(':', 1)[1] if ':' in remote_id else None
                if not page_id:
                    self.send_json_error(400, "Invalid remote image id")
                    return

                try:
                    _src_map = {
                        'wikimedia': ("Wikimedia Imports", "wikimedia", "Wikimedia Commons"),
                        'croquis':   ("Croquis Caf\u00e9 Imports", "croquis", "Croquis Caf\u00e9"),
                    }
                    _sinfo = _src_map.get(remote_source, ("Unsplash Imports", "unsplash", "Unsplash"))
                    import_folder_name, import_folder_tag, provider_label = _sinfo
                    import_folder_id = get_or_create_eagle_folder_id(import_folder_name)
                except Exception as e:
                    self.send_json_error(500, f"Could not create/find Eagle folder: {str(e)}")
                    return

                # Eagle item IDs are typically short A-Z0-9 strings; use a deterministic one.
                new_folder_id = stable_eagle_id(f"{remote_source}:{page_id}")
                new_folder_name = f"{new_folder_id}.info"
                new_folder = IMAGES_DIR / new_folder_name

                if new_folder.exists():
                    # Already imported - but verify it's in the correct folder
                    metadata_file = new_folder / 'metadata.json'
                    if metadata_file.exists():
                        try:
                            with open(metadata_file, 'r', encoding='utf-8') as f:
                                existing_metadata = json.load(f)
                            # Check if the image is properly assigned to the import folder
                            current_folders = existing_metadata.get('folders', [])
                            if import_folder_id not in current_folders:
                                # Add the import folder reference if missing
                                existing_metadata['folders'] = list(set(current_folders + [import_folder_id]))
                                with open(metadata_file, 'w', encoding='utf-8') as f:
                                    json.dump(existing_metadata, f, ensure_ascii=False, indent=2)
                                print(f"[Favorite] Updated existing image folder reference: {new_folder_id}")
                        except Exception as e:
                            print(f"[Warning] Could not verify/update existing image metadata: {e}")
                    
                    self.send_json_response({
                        'success': True,
                        'favorited': True,
                        'eagle_folder': new_folder_name,
                        'eagle_folder_id': import_folder_id,
                        'already_imported': True
                    })
                    return

                new_folder.mkdir(parents=True, exist_ok=True)

                source_path: Optional[Path] = None
                source_suffix = '.jpg'

                if remote_source in ('wikimedia', 'croquis'):
                    source_path_match = image_data.get('image_path', '').split(f'/api/remote-image/{session_id}/')
                    if len(source_path_match) < 2:
                        self.send_json_error(400, "Invalid image path")
                        return

                    source_filename = urllib.parse.unquote(source_path_match[1])
                    if Path(source_filename).name != source_filename:
                        self.send_json_error(400, "Invalid source filename")
                        return
                    source_path = REMOTE_CACHE_DIR / session_id / source_filename
                    if not source_path.exists():
                        self.send_json_error(404, "Source image not found")
                        return
                    source_suffix = source_path.suffix.lower() or '.jpg'
                elif remote_source == 'unsplash':
                    image_url = str(image_data.get('image_path') or '').strip()
                    if not image_url:
                        self.send_json_error(400, "Missing Unsplash image URL")
                        return
                    parsed = urllib.parse.urlparse(image_url)
                    source_suffix = Path(parsed.path).suffix.lower() or '.jpg'
                else:
                    self.send_json_error(400, f"Unsupported remote source: {remote_source}")
                    return
                
                # Use Eagle-style naming convention:
                # - metadata.name should match the main file base name
                # - thumbnail should be `<name>_thumbnail.png`
                eagle_name = str(page_id)
                original_title = image_data.get('name', '')
                attribution_url = image_data.get('attribution_url', '')

                # Copy to Eagle library
                dest_filename = f"{eagle_name}{source_suffix}"
                dest_path = new_folder / dest_filename
                if remote_source in ('wikimedia', 'croquis'):
                    shutil.copy2(source_path, dest_path)
                else:
                    if not download_file(str(image_data.get('image_path')), dest_path):
                        self.send_json_error(502, "Failed to download image from Unsplash")
                        return

                    download_location = str(image_data.get('download_location') or '').strip()
                    if download_location:
                        try:
                            ack_req = urllib.request.Request(
                                download_location,
                                headers={
                                    'Authorization': f"Client-ID {str(CONFIG.get('unsplash_access_key') or '').strip()}",
                                    'Accept-Version': 'v1',
                                    'Accept': 'application/json',
                                    'User-Agent': USER_AGENT
                                }
                            )
                            with urllib.request.urlopen(ack_req, timeout=10):
                                pass
                        except Exception:
                            # Best-effort tracking endpoint; failure should not block saving.
                            pass
                
                # Create Eagle thumbnail if possible
                thumbnail_path = new_folder / f"{eagle_name}_thumbnail.png"
                wrote_thumbnail = try_write_thumbnail_png(dest_path, thumbnail_path)
                width = 0
                height = 0
                if Image is not None:
                    try:
                        with Image.open(dest_path) as img:
                            width, height = img.size
                    except Exception:
                        width = 0
                        height = 0
                if not wrote_thumbnail:
                    # Best-effort: if we have a cached thumbnail and it's already PNG, copy it into place.
                    thumb_path_data = image_data.get('thumbnail_path', '')
                    if remote_source == 'wikimedia' and thumb_path_data:
                        thumb_match = thumb_path_data.split(f'/api/remote-image/{session_id}/')
                        if len(thumb_match) >= 2:
                            thumb_filename = urllib.parse.unquote(thumb_match[1])
                            if Path(thumb_filename).name == thumb_filename:
                                source_thumb = REMOTE_CACHE_DIR / session_id / thumb_filename
                                if source_thumb.exists() and source_thumb.suffix.lower() == '.png':
                                    shutil.copy2(source_thumb, thumbnail_path)
                
                # Create metadata.json with favorite tag
                attribution_url = image_data.get('attribution_url', '')
                attribution_name = str(image_data.get('attribution_name') or '').strip()
                attribution_username = str(image_data.get('attribution_username') or '').strip()
                credit_line = ""
                if remote_source == 'unsplash':
                    if attribution_name and attribution_username:
                        credit_line = f"Photo by {attribution_name} (@{attribution_username}) on Unsplash"
                    elif attribution_name:
                        credit_line = f"Photo by {attribution_name} on Unsplash"
                    else:
                        credit_line = "Photo on Unsplash"
                annotation_parts = [
                    f"Imported from {provider_label}" + (f"\nOriginal: {original_title}" if original_title else "")
                ]
                if credit_line:
                    annotation_parts.append(credit_line)
                metadata = {
                    'id': new_folder_id,
                    'name': eagle_name,
                    'size': dest_path.stat().st_size,
                    'ext': dest_path.suffix.lstrip('.'),
                    'tags': ['study-favorite', import_folder_tag],
                    'folders': [import_folder_id],
                    'isDeleted': False,
                    'url': attribution_url or '',
                    'annotation': "\n".join(annotation_parts),
                    'btime': now_ms(),
                    'mtime': now_ms(),
                    'modificationTime': now_ms(),
                    'lastModified': now_ms(),
                    'width': int(width),
                    'height': int(height)
                }
                
                metadata_file = new_folder / 'metadata.json'
                with open(metadata_file, 'w', encoding='utf-8') as f:
                    json.dump(metadata, f, ensure_ascii=False, indent=2)
                
                print(f"[Favorite] Successfully copied {provider_label} image to Eagle library:")
                print(f"  - Folder ID: {new_folder_id}")
                print(f"  - Folder name: {new_folder_name}")
                print(f"  - Parent folder: {import_folder_name} (ID: {import_folder_id})")
                print(f"  - Tags: {metadata.get('tags')}")
                print(f"  - Location: {new_folder}")
                
                self.send_json_response({
                    'success': True,
                    'favorited': True,
                    'eagle_folder': new_folder_name,
                    'eagle_folder_id': import_folder_id
                })
                
            elif folder_name:
                # Local Eagle library image - just toggle tag
                metadata_file = IMAGES_DIR / folder_name / "metadata.json"
                
                if not metadata_file.exists():
                    self.send_json_error(404, "Metadata file not found")
                    return
                
                # Read current metadata
                with open(metadata_file, 'r', encoding='utf-8') as f:
                    metadata = json.load(f)
                
                # Toggle the 'study-favorite' tag
                tags = metadata.get('tags', [])
                favorite_tag = 'study-favorite'
                
                if favorite_tag in tags:
                    tags.remove(favorite_tag)
                    is_favorited = False
                else:
                    tags.append(favorite_tag)
                    is_favorited = True
                
                metadata['tags'] = tags
                
                # Write back to file
                with open(metadata_file, 'w', encoding='utf-8') as f:
                    json.dump(metadata, f, ensure_ascii=False, indent=2)
                
                self.send_json_response({
                    'success': True,
                    'favorited': is_favorited
                })
            else:
                self.send_json_error(400, "Missing required parameters")
            
        except json.JSONDecodeError:
            self.send_json_error(400, "Invalid JSON")
        except Exception as e:
            self.send_json_error(500, f"Error toggling favorite: {str(e)}")

    def cleanup_remote_session(self):
        """Cleanup cached remote images for a session"""
        try:
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))

            session_id = data.get('session_id')
            if not session_id:
                self.send_json_error(400, "Missing session_id parameter")
                return

            cleanup_remote_session(session_id)

            self.send_json_response({
                'success': True
            })
        except json.JSONDecodeError:
            self.send_json_error(400, "Invalid JSON")
        except Exception as e:
            self.send_json_error(500, f"Error cleaning up remote session: {str(e)}")
    
    def send_json_response(self, data: dict):
        """Send a JSON response"""
        response = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(response))
        self.end_headers()
        self.wfile.write(response)
    
    def send_json_error(self, code: int, message: str):
        """Send a JSON error response"""
        response = json.dumps({
            'success': False,
            'error': message
        }).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(response))
        self.end_headers()
        self.wfile.write(response)
    
    def log_message(self, format, *args):
        """Override to customize log format"""
        print(f"[{self.log_date_time_string()}] {format % args}")


def main():
    """Start the server"""
    # Change to script directory
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    
    # Verify library path exists
    if not LIBRARY_PATH.exists():
        print(f"WARNING: Library path not found: {LIBRARY_PATH}")
        print("Make sure Syncthing has synced the Eagle library to the Pi")
        print("Server will start anyway, but won't work until library is available\n")
    
    print(f"Stop Noodling Server")
    print(f"====================")
    print(f"Library: {LIBRARY_PATH}")
    print(f"Port: {PORT}")
    print(f"\nStarting server...")

    # Start pack cache build in background (√-weighted sampling)
    threading.Thread(target=PACK_CACHE.build, daemon=True).start()

    # Start remote cache safety-net cleanup
    try:
        ensure_remote_cache_dir()
        cleanup_stale_remote_sessions(REMOTE_CACHE_MAX_AGE_SECONDS)
        cleanup_orphaned_remote_cache(REMOTE_CACHE_MAX_AGE_SECONDS)
        threading.Thread(target=remote_cache_reaper_loop, daemon=True).start()
        print(f"Remote cache reaper: interval={REMOTE_CACHE_REAPER_INTERVAL_SECONDS}s ttl={REMOTE_CACHE_MAX_AGE_SECONDS}s")
    except Exception as e:
        print(f"Warning: could not start remote cache reaper: {e}")
    
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), FigureStudyHandler) as httpd:
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
        
        print(f"\n✓ Server running at:")
        print(f"  Local:     http://localhost:{PORT}")
        print(f"  Network:   http://{local_ip}:{PORT}")
        print(f"  Hostname:  http://{hostname}:{PORT}")
        print(f"\nPress Ctrl+C to stop\n")
        
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n\nShutting down server...")
            httpd.shutdown()
            print("Server stopped.")


if __name__ == "__main__":
    main()
