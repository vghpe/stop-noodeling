"""Unsplash provider: search, score and select portrait candids."""

import html
import json
import random
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from typing import Deque, Dict, List, Optional, Set, Tuple

from ..config import CONFIG, USER_AGENT

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
