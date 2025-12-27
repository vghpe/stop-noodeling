# Stop Noodling

A timed reference tool for figure drawing practice. Displays random images from your [Eagle](https://eagle.cool/) reference library on a timer.

**[Try the Demo](https://vghpe.github.io/stop-noodeling/demo/)**

## Features

- Timed sessions: 30s, 1min, 2min, 5min, or custom durations
- Touch-friendly interface
- Mark favorites during study → syncs back to Eagle
- Session review grid

## Setup

```bash
# Clone and configure
git clone https://github.com/YOUR_USERNAME/stop-noodling.git
cd stop-noodling
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

## License

MIT
