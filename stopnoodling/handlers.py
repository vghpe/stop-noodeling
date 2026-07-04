"""HTTP request handler wiring the API endpoints to the domain modules."""

import http.server
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .config import IMAGES_DIR, INDEX_HTML, REMOTE_CACHE_DIR
from .eagle_import import import_remote_favorite
from .library import PACK_CACHE
from .remote_cache import (
    REMOTE_SESSIONS,
    REMOTE_SESSIONS_LOCK,
    cleanup_orphaned_remote_cache,
    cleanup_remote_session,
    ensure_remote_cache_dir,
    is_valid_session_id,
    touch_remote_cache_session,
)
from .providers.common import public_image_fields
from .providers.croquis import fetch_croquis_photos_initial, get_croquis_opener
from .providers.unsplash import fetch_unsplash_photos
from .providers.wikimedia import fetch_wikimedia_photos_initial


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
            with open(INDEX_HTML, 'rb') as f:
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
        try:
            count = int(params.get('count', ['20'])[0])
        except ValueError:
            count = 20
        count = max(1, min(count, 500))
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
        try:
            count = int(params.get('count', ['20'])[0])
        except ValueError:
            count = 20
        # Remote sessions download files; keep the cap conservative
        count = max(1, min(count, 100))
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
                    'images': public_image_fields(images),
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

            images = public_image_fields(session['images'])
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
        if not is_valid_session_id(session_id) or not image_id:
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

        # Refuse anything that resolves outside the library (path traversal)
        try:
            image_path = image_path.resolve()
            image_path.relative_to(IMAGES_DIR.resolve())
        except (OSError, ValueError):
            self.send_error(404, "Image not found")
            return

        if not image_path.is_file():
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

        if not is_valid_session_id(session_id):
            self.send_error(404, "Image not found")
            return

        touch_remote_cache_session(session_id)

        session_dir = REMOTE_CACHE_DIR / session_id
        image_path = session_dir / filename

        # Refuse anything that resolves outside the session cache dir (path traversal)
        try:
            image_path = image_path.resolve()
            image_path.relative_to(session_dir.resolve())
        except (OSError, ValueError):
            self.send_error(404, "Image not found")
            return

        if not image_path.is_file():
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
                # Favoriting a remote image - copy it into the Eagle library
                status, payload = import_remote_favorite(image_data, data.get('session_id'))
                if status == 200:
                    self.send_json_response(payload)
                else:
                    self.send_json_error(status, payload.get('error', 'Import failed'))

            elif folder_name:
                # Local Eagle library image - just toggle tag
                # Folder must be a plain directory name inside the library (no path separators / traversal)
                if Path(folder_name).name != folder_name:
                    self.send_json_error(400, "Invalid folder name")
                    return

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
            if not is_valid_session_id(session_id):
                self.send_json_error(400, "Invalid session_id")
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
