# Stop Noodling

A timed reference tool for artists. Runs on Raspberry Pi, works with Eagle libraries, accessible from anywhere via Tailscale.

## What It Does

- Displays random images from your Eagle reference library on a timer
- Supports 30s, 1min, 2min, 5min sessions (or custom durations)
- Touch-friendly interface optimized for iPad
- Mark favorites during study → syncs back to Eagle via Syncthing
- Review session images in a thumbnail grid after completion

## Quick Start

### On the Pi (berry)

```bash
# SSH into the Pi
ssh berry

# Navigate to project
cd ~/stop-noodling

# Start the server
python3 server.py
```

### Access the Tool

Open in browser: `http://berry:8081` (local) or `http://berry.tail8c3b22.ts.net:8081` (Tailscale)

## Setup

### Prerequisites

- Raspberry Pi 5 with Raspberry Pi OS
- Tailscale installed and connected
- Syncthing syncing Eagle library from Mac to Pi
- Python 3 (pre-installed on Pi OS)

### Installation

```bash
# On the Pi
cd ~
git clone https://github.com/YOUR_USERNAME/stop-noodling.git
cd stop-noodling

# Set up configuration
cp config.example.json config.json
nano config.json  # Edit with your library path

# Run the server
python3 server.py
```

### Run on Boot (Optional)

Create a systemd service to keep it running:

```bash
sudo nano /etc/systemd/system/stop-noodling.service
```

```ini
[Unit]
Description=Stop Noodling
After=network.target

[Service]
Type=simple
User=henrikpettersson
WorkingDirectory=/home/henrikpettersson/stop-noodling
ExecStart=/usr/bin/python3 server.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable stop-noodling
sudo systemctl start stop-noodling
```

## Configuration

### Quick Setup

1. Copy the example config:
   ```bash
   cp config.example.json config.json
   ```

2. Edit `config.json` with your settings:
   ```json
   {
     "port": 8081,
     "library_path": "/path/to/your/Eagle/library"
   }
   ```

### Configuration Methods

**Option 1: config.json** (recommended):
- Copy `config.example.json` to `config.json`
- Update with your paths
- File is gitignored, won't be committed

**Option 2: Environment Variables**:
```bash
export STOP_NOODLING_PORT=8081
export STOP_NOODLING_LIBRARY_PATH="/path/to/library"
python3 server.py
```

**Option 3: Auto-detection**:
If no config is provided, the server looks for:
- macOS: `~/Pictures/Figure Drawing References.library`
- Pi: `~/Figure Drawing References`

### Configuration Options

| Option | Default | Description |
|--------|---------|-------------|
| `port` | `8081` | HTTP server port |
| `library_path` | Auto-detected | Full path to Eagle library folder |

## Project Structure

```
stop-noodling/
├── server.py            # Python HTTP server + API
├── index.html           # Single-page web app
├── config.example.json  # Example configuration (commit this)
├── config.json          # Your config (gitignored)
├── README.md            # This file
└── .gitignore
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Serves the web app |
| `/api/session` | GET | Get random images for a session |
| `/api/favorite` | POST | Toggle favorite tag on image |
| `/images/<path>` | GET | Serve image files |

## Eagle Integration

### How Favoriting Works

1. You tap the star on an image during study
2. The server adds `"study-favorite"` to that image's `metadata.json` tags array
3. Syncthing syncs the change back to your Mac
4. The image appears with the tag in Eagle

### Library Structure Expected

```
Figure Drawing References.library/
├── images/
│   └── [image-id].info/
│       ├── [name].[ext]           # Full resolution
│       ├── [name]_thumbnail.png   # Thumbnail
│       └── metadata.json          # Metadata with tags
├── tags.json
└── metadata.json
```

## Syncthing Setup

Sync the Eagle library folder between Mac and Pi:

**Mac folder:** `/Users/henrikpettersson/Pictures/Figure Drawing References.library/`
**Pi folder:** `/home/henrikpettersson/Pictures/Figure Drawing References.library/`

Set sync type to "Send & Receive" for bidirectional favorite syncing.

## Development

### Local Development (Mac)

You can run the server locally for testing:

```bash
cd ~/Ref\ Session
python3 server.py
# Access at http://localhost:8081
```

### Deploying Changes

```bash
# On Mac - push changes
git add .
git commit -m "Your changes"
git push

# On Pi - pull changes
ssh berry
cd ~/stop-noodling
git pull
sudo systemctl restart stop-noodling  # if using systemd
```

## License

MIT

## Related

- [Eagle](https://eagle.cool/) - Image organization software
- [Syncthing](https://syncthing.net/) - File synchronization
- [Tailscale](https://tailscale.com/) - VPN mesh network
