# TODO List for Demo Version

## Code Tasks
- [ ] Copy full CSS from main index.html to demo/index.html
- [ ] Copy full HTML structure (study screen, review screen) to demo/index.html
- [ ] Copy and modify JavaScript from main index.html:
  - [ ] Remove all `/api/session` API calls
  - [ ] Remove all `/api/favorite` API calls
  - [ ] Use hardcoded DEMO_IMAGES array instead of API response
  - [ ] Remove favorite button and functionality
  - [ ] Keep timer, controls, and review functionality
  - [ ] Ensure count is locked to 10 images
- [ ] Test demo locally on Mac
- [ ] Test on iPhone via local network

## Image Tasks
- [ ] Find 10 CC0/Public Domain figure reference images
  - Suggested sources:
    - Line of Action (check licensing)
    - Unsplash (search: figure, anatomy, gesture)
    - Pexels
    - Pixabay
    - Classical art museums (Met, Rijksmuseum)
- [ ] Optimize images for web (~500KB max each)
- [ ] Rename as image1.jpg through image10.jpg
- [ ] Place in demo/images/ folder
- [ ] Document image sources in demo/README.md

## Icon Tasks
- [ ] Create 192x192px app icon (icon-192.png)
- [ ] Create 512x512px app icon (icon-512.png)
- [ ] Place in demo/ folder

## Blog Post Tasks
- [ ] Fix slug in frontmatter (change "paper-designs" to "stop-noodling")
- [ ] Add screenshot of app interface
- [ ] Record video demo on iPhone
  - [ ] Start local server on Mac
  - [ ] Connect iPhone via local network
  - [ ] Add to home screen for PWA demo
  - [ ] Record session showing setup and practice
- [ ] Add video to blog post
- [ ] Add link to live demo: `yoursite.com/tool/stop-noodling/`
- [ ] Add link to GitHub repository
- [ ] Add browser compatibility notes
- [ ] Expand PWA installation instructions

## Hugo Deployment
- [ ] Copy demo/ contents to Hugo's /static/tool/stop-noodling/
- [ ] Test live demo on deployed blog
- [ ] Verify PWA works on mobile devices

## Git
- [ ] Create new branch for demo work: `git checkout -b demo-version`
- [ ] Commit demo files
- [ ] Keep main branch as full Pi-server version

## Video Recording Checklist
- [ ] Start demo server locally: `python3 -m http.server 8082` in demo/ folder
- [ ] Get Mac local IP: `ipconfig getifaddr en0`
- [ ] On iPhone, visit: `http://[MAC_IP]:8082`
- [ ] Add to Home Screen
- [ ] Start screen recording on iPhone
- [ ] Show:
  - Home screen with app icon
  - Opening app
  - Selecting timer (try 30s for quick demo)
  - Starting session
  - Timer counting down
  - Image transitions
  - Pause/play controls
  - Review screen at end
