"""Import a favorited remote image into the local Eagle library.

Extracted from the HTTP handler so `handlers.py` stays a thin request/response
layer. `import_remote_favorite` returns an ``(http_status, payload)`` tuple:
a 200 payload is sent verbatim; any other status carries an ``error`` message.
"""

import json
import shutil
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

from .config import CONFIG, IMAGES_DIR, REMOTE_CACHE_DIR, USER_AGENT
from .eagle import (
    Image,
    get_or_create_eagle_folder_id,
    now_ms,
    stable_eagle_id,
    try_write_thumbnail_png,
)
from .providers.common import download_file
from .remote_cache import is_valid_session_id


def import_remote_favorite(image_data: dict, session_id) -> Tuple[int, dict]:
    """Copy a favorited remote image into the Eagle library.

    Returns (200, success_payload) on success, or (status, {'error': msg}) on failure.
    """
    remote_source = str(image_data.get('source', 'wikimedia')).lower()
    if remote_source in ('wikimedia', 'croquis'):
        if not session_id:
            return 400, {'error': f"Missing session_id for {remote_source} image. Session may have been cleared."}
        if not is_valid_session_id(session_id):
            return 400, {'error': "Invalid session_id"}

    # Ensure Eagle images directory exists
    if not IMAGES_DIR.exists():
        return 500, {'error': f"Eagle library not found at {IMAGES_DIR}"}

    # Derive a stable ID from the provider item id to avoid duplicates on repeated starring.
    remote_id = str(image_data.get('id', ''))
    page_id = remote_id.split(':', 1)[1] if ':' in remote_id else None
    if not page_id:
        return 400, {'error': "Invalid remote image id"}

    try:
        _src_map = {
            'wikimedia': ("Wikimedia Imports", "wikimedia", "Wikimedia Commons"),
            'croquis':   ("Croquis Café Imports", "croquis", "Croquis Café"),
        }
        _sinfo = _src_map.get(remote_source, ("Unsplash Imports", "unsplash", "Unsplash"))
        import_folder_name, import_folder_tag, provider_label = _sinfo
        import_folder_id = get_or_create_eagle_folder_id(import_folder_name)
    except Exception as e:
        return 500, {'error': f"Could not create/find Eagle folder: {str(e)}"}

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

        return 200, {
            'success': True,
            'favorited': True,
            'eagle_folder': new_folder_name,
            'eagle_folder_id': import_folder_id,
            'already_imported': True
        }

    new_folder.mkdir(parents=True, exist_ok=True)

    source_path: Optional[Path] = None
    source_suffix = '.jpg'

    if remote_source in ('wikimedia', 'croquis'):
        source_path_match = image_data.get('image_path', '').split(f'/api/remote-image/{session_id}/')
        if len(source_path_match) < 2:
            return 400, {'error': "Invalid image path"}

        source_filename = urllib.parse.unquote(source_path_match[1])
        if Path(source_filename).name != source_filename:
            return 400, {'error': "Invalid source filename"}
        source_path = REMOTE_CACHE_DIR / session_id / source_filename
        if not source_path.exists():
            return 404, {'error': "Source image not found"}
        source_suffix = source_path.suffix.lower() or '.jpg'
    elif remote_source == 'unsplash':
        image_url = str(image_data.get('image_path') or '').strip()
        if not image_url:
            return 400, {'error': "Missing Unsplash image URL"}
        parsed = urllib.parse.urlparse(image_url)
        # Only fetch from Unsplash's CDN — the URL comes from the client
        if parsed.scheme != 'https' or parsed.hostname not in ('images.unsplash.com', 'plus.unsplash.com'):
            return 400, {'error': "Invalid Unsplash image URL"}
        source_suffix = Path(parsed.path).suffix.lower() or '.jpg'
    else:
        return 400, {'error': f"Unsupported remote source: {remote_source}"}

    # Use Eagle-style naming convention:
    # - metadata.name should match the main file base name
    # - thumbnail should be `<name>_thumbnail.png`
    eagle_name = str(page_id)
    original_title = image_data.get('name', '')

    # Copy to Eagle library
    dest_filename = f"{eagle_name}{source_suffix}"
    dest_path = new_folder / dest_filename
    if remote_source in ('wikimedia', 'croquis'):
        shutil.copy2(source_path, dest_path)
    else:
        if not download_file(str(image_data.get('image_path')), dest_path):
            return 502, {'error': "Failed to download image from Unsplash"}

        download_location = str(image_data.get('download_location') or '').strip()
        # Never send the API key anywhere but Unsplash's own API
        dl_parsed = urllib.parse.urlparse(download_location) if download_location else None
        if dl_parsed and (dl_parsed.scheme != 'https' or dl_parsed.hostname != 'api.unsplash.com'):
            download_location = ''
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

    return 200, {
        'success': True,
        'favorited': True,
        'eagle_folder': new_folder_name,
        'eagle_folder_id': import_folder_id
    }
