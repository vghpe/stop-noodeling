# Demo Version - Stop Noodling

This is the standalone demo version for the blog post. It uses 10 hardcoded free-to-use images and has limited functionality compared to the full version.

## Differences from Full Version

- **No backend required** - All images hardcoded in HTML
- **Limited images** - Only 10 images (vs unlimited with Eagle library)
- **Limited options** - Only 10 images count, Figure practice type
- **No favorites** - Favorite button removed (requires backend + Eagle)
- **Demo banner** - Visible indicator this is a demo

## Setup for Hugo Blog

1. Copy entire `demo/` folder contents to Hugo's `/static/tool/stop-noodling/`
2. Access at `yoursite.com/tool/stop-noodling/`

## Structure

```
demo/
├── index.html          # Modified version with hardcoded images
├── manifest.json       # PWA manifest
├── icon-192.png        # TODO: Create app icon
├── icon-512.png        # TODO: Create app icon
└── images/             # Free-to-use reference images
    ├── image1.jpg
    ├── image2.jpg
    └── ... (10 total)
```

## Images Source

All images are from Reference.pictures Sample Pack 3:
- URL: https://reference.pictures/Sample-Pack-3/
- License: Free for personal and commercial use
- High-quality reference photos specifically for artists
