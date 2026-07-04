#!/usr/bin/env python3
"""
Stop Noodling - Backend Server

Entry point. The implementation lives in the `stopnoodling` package:
  - config       configuration loading and derived paths/constants
  - eagle        Eagle library metadata helpers (IDs, thumbnails, folders)
  - library      local library index (PackCache) with √-weighted sampling
  - remote_cache remote session registry and on-disk cache cleanup
  - providers/   remote image sources (Unsplash, Wikimedia, Croquis)
  - handlers     HTTP request handler
  - app          server startup / main()
"""

from stopnoodling.app import main

if __name__ == "__main__":
    main()
