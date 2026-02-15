#!/usr/bin/env python3
"""
Stop Noodling - Backend Server
Serves the web interface and provides API endpoints for the Eagle library
"""

import http.server
import socketserver
import json
import os
import random
import shutil
import urllib.parse
import urllib.request
import time
import uuid
import threading
import hashlib
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from pathlib import Path
from typing import List, Dict, Optional, Tuple

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

# Remote cache retention
# - TTL is a safety net: sessions normally clean up when the client requests it
# - We use the remote session directory mtime as the source of truth and "touch"
#   it on access (polling / image serving) so active sessions don't get reaped.
REMOTE_CACHE_MAX_AGE_SECONDS = int(os.getenv('STOP_NOODLING_REMOTE_CACHE_TTL_SECONDS', '86400'))
REMOTE_CACHE_REAPER_INTERVAL_SECONDS = int(os.getenv('STOP_NOODLING_REMOTE_CACHE_REAPER_INTERVAL_SECONDS', '3600'))

WIKIMEDIA_USER_AGENT = "StopNoodling/1.0 (https://github.com/vghpe/stop-noodeling)"


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
        req = urllib.request.Request(url, headers={'User-Agent': WIKIMEDIA_USER_AGENT})
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
    req = urllib.request.Request(url, headers={'User-Agent': WIKIMEDIA_USER_AGENT})

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
    req = urllib.request.Request(url, headers={'User-Agent': WIKIMEDIA_USER_AGENT})

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
        practice_type = params.get('practice_type', ['figure'])[0]  # 'figure' or 'hands'
        
        print(f"\n[Session Request] practice_type={practice_type}, count={count}")
        
        try:
            # Get all image folder names (fast - just listing directories)
            if not IMAGES_DIR.exists():
                self.send_json_error(500, f"Library not found at {IMAGES_DIR}")
                return
            
            all_folders = [d for d in IMAGES_DIR.iterdir() if d.is_dir()]
            
            if len(all_folders) == 0:
                self.send_json_error(500, "No images found in library")
                return
            
            print(f"[Filtering] Scanning {len(all_folders)} folders...")
            
            # Randomly sample folders, filtering out deleted ones, until we have enough
            images = []
            available_folders = all_folders.copy()
            random.shuffle(available_folders)
            
            folders_checked = 0
            
            # Read metadata only for selected folders
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
                    
                    # Skip deleted images
                    if metadata.get('isDeleted', False):
                        continue
                    
                    # Filter by practice type
                    image_tags = metadata.get('tags', [])
                    
                    # Skip ignored images
                    if 'ignore' in image_tags:
                        continue
                    
                    has_hands_tag = 'hands' in image_tags
                    
                    if practice_type == 'figure' and has_hands_tag:
                        # Figure practice: exclude images with 'hands' tag
                        continue
                    elif practice_type == 'hands' and not has_hands_tag:
                        # Hands practice: only include images with 'hands' tag
                        continue
                    
                    # Find the actual image file (not thumbnail, not metadata.json)
                    image_files = [
                        f for f in folder.iterdir()
                        if f.is_file() 
                        and not f.name.endswith('_thumbnail.png')
                        and f.name != 'metadata.json'
                        and f.suffix.lower() in ['.jpg', '.jpeg', '.png', '.gif', '.webp']
                    ]
                    
                    if image_files:
                        image_file = image_files[0]
                        
                        # Find the actual thumbnail file (Eagle may not create one for all images)
                        thumbnail_files = [
                            f for f in folder.iterdir()
                            if f.is_file() and f.name.endswith('_thumbnail.png')
                        ]
                        thumbnail_file = thumbnail_files[0] if thumbnail_files else None
                        
                        # Use URL encoding for the actual filenames
                        images.append({
                            'id': metadata['id'],
                            'name': metadata.get('name', image_file.stem),
                            'image_path': f"/api/image/{folder.name}/{urllib.parse.quote(image_file.name)}",
                            'thumbnail_path': f"/api/image/{folder.name}/{urllib.parse.quote(thumbnail_file.name)}" if thumbnail_file else None,
                            'tags': metadata.get('tags', []),
                            'folder': folder.name
                        })
                except (json.JSONDecodeError, KeyError) as e:
                    print(f"Error reading metadata for {folder.name}: {e}")
                    continue
            
            # Shuffle the final list
            random.shuffle(images)
            
            elapsed_time = time.time() - start_time
            
            if len(images) < count:
                print(f"[Warning] Only found {len(images)} images (requested {count}) after checking all {folders_checked} folders in {elapsed_time:.3f}s")
            else:
                print(f"[Performance] Found {len(images)} images after checking {folders_checked} folders in {elapsed_time:.3f}s")
            
            # Return warning in response if we couldn't fulfill the request
            response = {
                'success': True,
                'images': images,
                'total': len(images)
            }
            
            if len(images) < count:
                response['warning'] = f'Only found {len(images)} images matching "{practice_type}" practice type (you requested {count})'
            
            self.send_json_response(response)
            
        except Exception as e:
            self.send_json_error(500, f"Error creating session: {str(e)}")

    def create_remote_session(self, query_string: str):
        """Create a remote session using Wikimedia photos"""
        start_time = time.time()

        params = urllib.parse.parse_qs(query_string)
        count = int(params.get('count', ['20'])[0])
        source = params.get('source', ['wikimedia'])[0]

        if source != 'wikimedia':
            self.send_json_error(400, f"Unsupported source: {source}")
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
            
            images = list(session['images'])  # Copy
            fetching = session['fetching']

        touch_remote_cache_session(session_id)
        
        response = {
            'success': True,
            'images': images,
            'total': len(images),
            'fetching': fetching
        }
        
        self.send_json_response(response)
    
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
                if not session_id:
                    self.send_json_error(400, "Missing session_id for remote image")
                    return
                
                # Ensure Eagle images directory exists
                if not IMAGES_DIR.exists():
                    self.send_json_error(500, f"Eagle library not found at {IMAGES_DIR}")
                    return
                
                # Derive a stable ID from the Wikimedia page id to avoid duplicates on repeated starring.
                remote_id = str(image_data.get('id', ''))
                page_id = remote_id.split(':', 1)[1] if remote_id.startswith('wikimedia:') and ':' in remote_id else None
                if not page_id:
                    self.send_json_error(400, "Invalid remote image id")
                    return

                try:
                    wikimedia_folder_id = get_or_create_eagle_folder_id("Wikimedia Imports")
                except Exception as e:
                    self.send_json_error(500, f"Could not create/find Eagle folder: {str(e)}")
                    return

                # Eagle item IDs are typically short A-Z0-9 strings; use a deterministic one.
                new_folder_id = stable_eagle_id(f"wikimedia:{page_id}")
                new_folder_name = f"{new_folder_id}.info"
                new_folder = IMAGES_DIR / new_folder_name

                if new_folder.exists():
                    # Already imported
                    self.send_json_response({
                        'success': True,
                        'favorited': True,
                        'eagle_folder': new_folder_name,
                        'eagle_folder_id': wikimedia_folder_id,
                        'already_imported': True
                    })
                    return

                new_folder.mkdir(parents=True, exist_ok=True)
                
                # Copy image file from cache
                source_path_match = image_data.get('image_path', '').split(f'/api/remote-image/{session_id}/')
                if len(source_path_match) < 2:
                    self.send_json_error(400, "Invalid image path")
                    return
                
                source_filename = urllib.parse.unquote(source_path_match[1])
                # Prevent path traversal
                if Path(source_filename).name != source_filename:
                    self.send_json_error(400, "Invalid source filename")
                    return
                source_path = REMOTE_CACHE_DIR / session_id / source_filename
                
                if not source_path.exists():
                    self.send_json_error(404, "Source image not found")
                    return
                
                # Use Eagle-style naming convention:
                # - metadata.name should match the main file base name
                # - thumbnail should be `<name>_thumbnail.png`
                eagle_name = str(page_id)
                original_title = image_data.get('name', '')
                attribution_url = image_data.get('attribution_url', '')

                # Copy to Eagle library
                dest_filename = f"{eagle_name}{source_path.suffix.lower()}"
                dest_path = new_folder / dest_filename
                shutil.copy2(source_path, dest_path)
                
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
                    if thumb_path_data:
                        thumb_match = thumb_path_data.split(f'/api/remote-image/{session_id}/')
                        if len(thumb_match) >= 2:
                            thumb_filename = urllib.parse.unquote(thumb_match[1])
                            if Path(thumb_filename).name == thumb_filename:
                                source_thumb = REMOTE_CACHE_DIR / session_id / thumb_filename
                                if source_thumb.exists() and source_thumb.suffix.lower() == '.png':
                                    shutil.copy2(source_thumb, thumbnail_path)
                
                # Create metadata.json with favorite tag
                attribution_url = image_data.get('attribution_url', '')
                annotation_parts = [
                    "Imported from Wikimedia Commons" + (f"\nOriginal: {original_title}" if original_title else "")
                ]
                metadata = {
                    'id': new_folder_id,
                    'name': eagle_name,
                    'size': dest_path.stat().st_size,
                    'ext': dest_path.suffix.lstrip('.'),
                    'tags': ['study-favorite', 'wikimedia'],
                    'folders': [wikimedia_folder_id],
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
                
                print(f"[Favorite] Copied Wikimedia image to Eagle library: {new_folder_id}")
                
                self.send_json_response({
                    'success': True,
                    'favorited': True,
                    'eagle_folder': new_folder_name,
                    'eagle_folder_id': wikimedia_folder_id
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

    # Start remote cache safety-net cleanup
    try:
        ensure_remote_cache_dir()
        cleanup_stale_remote_sessions(REMOTE_CACHE_MAX_AGE_SECONDS)
        cleanup_orphaned_remote_cache(REMOTE_CACHE_MAX_AGE_SECONDS)
        threading.Thread(target=remote_cache_reaper_loop, daemon=True).start()
        print(f"Remote cache reaper: interval={REMOTE_CACHE_REAPER_INTERVAL_SECONDS}s ttl={REMOTE_CACHE_MAX_AGE_SECONDS}s")
    except Exception as e:
        print(f"Warning: could not start remote cache reaper: {e}")
    
    with socketserver.TCPServer(("", PORT), FigureStudyHandler) as httpd:
        import socket
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
