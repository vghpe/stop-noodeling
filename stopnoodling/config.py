"""Configuration loading and derived paths/constants shared across the app."""

import json
import os
from pathlib import Path

# Repo root (the package lives one level below it). All on-disk assets
# (config.json, index.html, .remote_cache) are resolved relative to this so the
# app does not depend on the current working directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = PROJECT_ROOT / "index.html"


def load_config():
    """Load configuration from config.json, environment variables, or defaults"""
    config = {
        'port': 8081,
        'library_path': None
    }

    # Try to load from config.json first
    config_file = PROJECT_ROOT / 'config.json'
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
REMOTE_CACHE_DIR = PROJECT_ROOT / ".remote_cache"

# Remote cache retention
# - TTL is a safety net: sessions normally clean up when the client requests it
# - We use the remote session directory mtime as the source of truth and "touch"
#   it on access (polling / image serving) so active sessions don't get reaped.
REMOTE_CACHE_MAX_AGE_SECONDS = int(os.getenv('STOP_NOODLING_REMOTE_CACHE_TTL_SECONDS', '86400'))
REMOTE_CACHE_REAPER_INTERVAL_SECONDS = int(os.getenv('STOP_NOODLING_REMOTE_CACHE_REAPER_INTERVAL_SECONDS', '3600'))

USER_AGENT = "StopNoodling/1.0 (https://github.com/vghpe/stop-noodeling)"
