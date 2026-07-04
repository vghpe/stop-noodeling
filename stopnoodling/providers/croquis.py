"""Croquis Café provider: authenticated WP REST access to figure-drawing photos."""

import json
import math
import random
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..config import CONFIG
from ..remote_cache import REMOTE_SESSIONS, REMOTE_SESSIONS_LOCK
from .common import download_file  # noqa: F401  (kept available for callers/tests)

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

CROQUIS_LOGIN_URL = "https://croquis.cafe/my-account/"
CROQUIS_API_BASE = "https://croquis.cafe/wp-json/wp/v2"
CROQUIS_PREFERRED_SIZES = ("large", "medium_large", "medium")  # in preference order
CROQUIS_FINE_ARTS_CAT_ID = 1424  # "Fine Arts Photos" taxonomy term — filters out landscapes/still life


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
