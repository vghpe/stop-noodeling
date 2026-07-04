"""Remote session cache: in-memory session registry plus on-disk cache cleanup."""

import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Dict, List

from .config import (
    REMOTE_CACHE_DIR,
    REMOTE_CACHE_MAX_AGE_SECONDS,
    REMOTE_CACHE_REAPER_INTERVAL_SECONDS,
)

# Shared registry of active remote sessions (Wikimedia / Croquis). Keyed by
# session_id; each entry tracks the cache dir, downloaded images and fetch state.
REMOTE_SESSIONS: Dict[str, Dict[str, object]] = {}
REMOTE_SESSIONS_LOCK = threading.Lock()

SESSION_ID_RE = re.compile(r'^[0-9a-f]{32}$')


def is_valid_session_id(session_id) -> bool:
    """Session IDs are always uuid4().hex; reject anything else before it reaches the filesystem."""
    return isinstance(session_id, str) and bool(SESSION_ID_RE.match(session_id))


def ensure_remote_cache_dir():
    REMOTE_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def touch_remote_cache_session(session_id: str):
    """Best-effort: update the session directory mtime so TTL cleanup keeps it."""
    if not is_valid_session_id(session_id):
        return
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
    elif is_valid_session_id(session_id):
        session_dir = REMOTE_CACHE_DIR / session_id
    else:
        return

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
