# Stop Noodling

A timed reference tool for figure drawing practice. Displays random images from your [Eagle](https://eagle.cool/) reference library on a timer.

**[Try the Demo](https://vghpe.github.io/stop-noodeling/demo/)**

## Features

- Timed sessions: 30s, 1min, 2min, 5min, or custom durations
- Touch-friendly interface
- Mark favorites during study → syncs back to Eagle
- Session review grid
- Works with [Eagle](https://eagle.cool/) library format

## Quick Start

```bash
# Clone and configure
git clone https://github.com/vghpe/stop-noodeling.git
cd stop-noodeling
cp config.example.json config.json

# Edit config.json with your Eagle library path
# Run server
python3 server.py
```

Access at `http://localhost:PORT` (port shown in terminal output)

## Configuration

Edit `config.json`:
```json
{
  "port": 8081,
  "library_path": "/path/to/your/Eagle.library"
}
```

Or use environment variables:
```bash
export STOP_NOODLING_LIBRARY_PATH="/path/to/library"
python3 server.py
```

## Requirements

- Python 3.7+
- Eagle app with an organized library
- Modern web browser

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Serves the web app |
| `/api/session` | GET | Get random images for session. Params: `count` (number of images) |
| `/api/favorite` | POST | Toggle favorite tag. Body: `{"folder": "image-folder-id"}` |
| `/images/<path>` | GET | Serve image files from library |

## Eagle Integration

### Favoriting

When you tap the star during study:
1. Server adds `"study-favorite"` to the image's `metadata.json` tags array
2. Changes sync back to Eagle (via Syncthing or manual sync)
3. Image appears with the tag in Eagle

### Library Structure

```
Your Eagle Library.library/
├── images/
│   └── [image-id].info/
│       ├── [name].[ext]           # Full resolution
│       ├── [name]_thumbnail.png   # Thumbnail
│       └── metadata.json          # Metadata with tags
├── tags.json
└── metadata.json
```

## Running as a Service

### macOS (LaunchAgent)

Create `~/Library/LaunchAgents/com.stopnoodling.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.stopnoodling</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/path/to/stop-noodeling/server.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>/path/to/stop-noodeling</string>
</dict>
</plist>
```

Load: `launchctl load ~/Library/LaunchAgents/com.stopnoodling.plist`

### Linux/Home Server (systemd)

Create `/etc/systemd/system/stop-noodling.service`:
```ini
[Unit]
Description=Stop Noodling
After=network.target

[Service]
Type=simple
User=your-username
WorkingDirectory=/path/to/stop-noodeling
ExecStart=/usr/bin/python3 server.py
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable: `sudo systemctl enable stop-noodling && sudo systemctl start stop-noodling`

## Remote Access

### Option 1: Tailscale (Recommended)
1. Install [Tailscale](https://tailscale.com/) on server and devices
2. Access via: `http://[tailscale-hostname]:8081`

### Option 2: Local Network Only
Find your computer's local IP:
- Mac: `ifconfig | grep "inet " | grep -v 127.0.0.1`
- Linux: `hostname -I`
- Windows: `ipconfig`

Access via: `http://[local-ip]:8081`

## Development

Server is a simple Python HTTP server with three main endpoints. All image serving happens through the `/images/` route which reads directly from the Eagle library.

The web app is a single-page application in `index.html` - no build process needed.

## License

MIT
