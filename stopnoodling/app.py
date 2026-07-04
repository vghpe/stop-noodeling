"""Application entry point: start background workers and the HTTP server."""

import http.server
import os
import socket
import threading

from .config import (
    LIBRARY_PATH,
    PORT,
    PROJECT_ROOT,
    REMOTE_CACHE_MAX_AGE_SECONDS,
    REMOTE_CACHE_REAPER_INTERVAL_SECONDS,
)
from .handlers import FigureStudyHandler
from .library import PACK_CACHE
from .remote_cache import (
    cleanup_orphaned_remote_cache,
    cleanup_stale_remote_sessions,
    ensure_remote_cache_dir,
    remote_cache_reaper_loop,
)


def main():
    """Start the server"""
    # Anchor the working directory at the repo root so relative paths in config
    # (e.g. a "./croquis.cafe_cookies.txt" cookie file) resolve as expected.
    os.chdir(PROJECT_ROOT)

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

    # Threading server: a slow remote fetch (e.g. Croquis HQ download) must not
    # block image serving for the session in progress.
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    with http.server.ThreadingHTTPServer(("", PORT), FigureStudyHandler) as httpd:
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
