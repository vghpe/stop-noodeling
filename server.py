#!/usr/bin/env python3
"""
Stop Noodling - Backend Server
Serves the web interface and provides API endpoints for the Eagle library
"""

import http.server
import socketserver
import json
import os
import random
import urllib.parse
import time
from pathlib import Path
from typing import List, Dict, Optional

# Configuration
def load_config():
    """Load configuration from config.json, environment variables, or defaults"""
    config = {
        'port': 8081,
        'library_path': None
    }
    
    # Try to load from config.json first
    config_file = Path(__file__).parent / 'config.json'
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

class FigureStudyHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP request handler for Stop Noodling"""
    
    def do_GET(self):
        """Handle GET requests"""
        parsed_path = urllib.parse.urlparse(self.path)
        
        if parsed_path.path == '/':
            # Serve the main HTML file
            self.serve_index()
        elif parsed_path.path == '/api/session':
            # Create a new study session
            self.create_session(parsed_path.query)
        elif parsed_path.path.startswith('/api/image/'):
            # Serve an image file
            self.serve_image(parsed_path.path)
        else:
            self.send_error(404, "Not Found")
    
    def do_POST(self):
        """Handle POST requests"""
        if self.path == '/api/favorite':
            self.toggle_favorite()
        else:
            self.send_error(404, "Not Found")
    
    def serve_index(self):
        """Serve the main HTML file"""
        try:
            with open('index.html', 'rb') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', len(content))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_error(404, "index.html not found")
    
    def create_session(self, query_string: str):
        """Create a new study session with random images"""
        start_time = time.time()
        
        # Parse query parameters
        params = urllib.parse.parse_qs(query_string)
        count = int(params.get('count', ['20'])[0])
        practice_type = params.get('practice_type', ['figure'])[0]  # 'figure' or 'hands'
        
        print(f"\n[Session Request] practice_type={practice_type}, count={count}")
        
        try:
            # Get all image folder names (fast - just listing directories)
            if not IMAGES_DIR.exists():
                self.send_json_error(500, f"Library not found at {IMAGES_DIR}")
                return
            
            all_folders = [d for d in IMAGES_DIR.iterdir() if d.is_dir()]
            
            if len(all_folders) == 0:
                self.send_json_error(500, "No images found in library")
                return
            
            print(f"[Filtering] Scanning {len(all_folders)} folders...")
            
            # Randomly sample folders, filtering out deleted ones, until we have enough
            images = []
            available_folders = all_folders.copy()
            random.shuffle(available_folders)
            
            folders_checked = 0
            
            # Read metadata only for selected folders
            for folder in available_folders:
                if len(images) >= count:
                    break
                
                folders_checked += 1
                    
                metadata_file = folder / "metadata.json"
                if not metadata_file.exists():
                    continue
                
                try:
                    with open(metadata_file, 'r', encoding='utf-8') as f:
                        metadata = json.load(f)
                    
                    # Skip deleted images
                    if metadata.get('isDeleted', False):
                        continue
                    
                    # Filter by practice type
                    image_tags = metadata.get('tags', [])
                    has_hands_tag = 'hands' in image_tags
                    
                    if practice_type == 'figure' and has_hands_tag:
                        # Figure practice: exclude images with 'hands' tag
                        continue
                    elif practice_type == 'hands' and not has_hands_tag:
                        # Hands practice: only include images with 'hands' tag
                        continue
                    
                    # Find the actual image file (not thumbnail, not metadata.json)
                    image_files = [
                        f for f in folder.iterdir()
                        if f.is_file() 
                        and not f.name.endswith('_thumbnail.png')
                        and f.name != 'metadata.json'
                        and f.suffix.lower() in ['.jpg', '.jpeg', '.png', '.gif', '.webp']
                    ]
                    
                    if image_files:
                        image_file = image_files[0]
                        
                        # Find the actual thumbnail file (Eagle may not create one for all images)
                        thumbnail_files = [
                            f for f in folder.iterdir()
                            if f.is_file() and f.name.endswith('_thumbnail.png')
                        ]
                        thumbnail_file = thumbnail_files[0] if thumbnail_files else None
                        
                        # Use URL encoding for the actual filenames
                        images.append({
                            'id': metadata['id'],
                            'name': metadata.get('name', image_file.stem),
                            'image_path': f"/api/image/{folder.name}/{urllib.parse.quote(image_file.name)}",
                            'thumbnail_path': f"/api/image/{folder.name}/{urllib.parse.quote(thumbnail_file.name)}" if thumbnail_file else None,
                            'tags': metadata.get('tags', []),
                            'folder': folder.name
                        })
                except (json.JSONDecodeError, KeyError) as e:
                    print(f"Error reading metadata for {folder.name}: {e}")
                    continue
            
            # Shuffle the final list
            random.shuffle(images)
            
            elapsed_time = time.time() - start_time
            
            if len(images) < count:
                print(f"[Warning] Only found {len(images)} images (requested {count}) after checking all {folders_checked} folders in {elapsed_time:.3f}s")
            else:
                print(f"[Performance] Found {len(images)} images after checking {folders_checked} folders in {elapsed_time:.3f}s")
            
            # Return warning in response if we couldn't fulfill the request
            response = {
                'success': True,
                'images': images,
                'total': len(images)
            }
            
            if len(images) < count:
                response['warning'] = f'Only found {len(images)} images matching "{practice_type}" practice type (you requested {count})'
            
            self.send_json_response(response)
            
        except Exception as e:
            self.send_json_error(500, f"Error creating session: {str(e)}")
    
    def serve_image(self, path: str):
        """Serve an image file from the library"""
        # Extract folder and filename from path
        # Format: /api/image/{folder_name}/{filename}
        parts = path.split('/')
        if len(parts) < 4:
            self.send_error(400, "Invalid image path")
            return
        
        folder_name = parts[3]
        filename = '/'.join(parts[4:])  # Handle filenames with slashes
        
        # URL decode the filename
        filename = urllib.parse.unquote(filename)
        
        image_path = IMAGES_DIR / folder_name / filename
        
        if not image_path.exists() or not image_path.is_file():
            self.send_error(404, "Image not found")
            return
        
        try:
            with open(image_path, 'rb') as f:
                content = f.read()
            
            # Determine content type
            ext = image_path.suffix.lower()
            content_types = {
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.png': 'image/png',
                '.gif': 'image/gif',
                '.webp': 'image/webp'
            }
            content_type = content_types.get(ext, 'application/octet-stream')
            
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', len(content))
            self.send_header('Cache-Control', 'public, max-age=86400')  # Cache for 1 day
            self.end_headers()
            self.wfile.write(content)
            
        except Exception as e:
            self.send_error(500, f"Error serving image: {str(e)}")
    
    def toggle_favorite(self):
        """Toggle the 'study-favorite' tag on an image"""
        try:
            # Read request body
            content_length = int(self.headers['Content-Length'])
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))
            
            folder_name = data.get('folder')
            if not folder_name:
                self.send_json_error(400, "Missing folder parameter")
                return
            
            metadata_file = IMAGES_DIR / folder_name / "metadata.json"
            
            if not metadata_file.exists():
                self.send_json_error(404, "Metadata file not found")
                return
            
            # Read current metadata
            with open(metadata_file, 'r', encoding='utf-8') as f:
                metadata = json.load(f)
            
            # Toggle the 'study-favorite' tag
            tags = metadata.get('tags', [])
            favorite_tag = 'study-favorite'
            
            if favorite_tag in tags:
                tags.remove(favorite_tag)
                is_favorited = False
            else:
                tags.append(favorite_tag)
                is_favorited = True
            
            metadata['tags'] = tags
            
            # Write back to file
            with open(metadata_file, 'w', encoding='utf-8') as f:
                json.dump(metadata, f, ensure_ascii=False, indent=2)
            
            self.send_json_response({
                'success': True,
                'favorited': is_favorited
            })
            
        except json.JSONDecodeError:
            self.send_json_error(400, "Invalid JSON")
        except Exception as e:
            self.send_json_error(500, f"Error toggling favorite: {str(e)}")
    
    def send_json_response(self, data: dict):
        """Send a JSON response"""
        response = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(response))
        self.end_headers()
        self.wfile.write(response)
    
    def send_json_error(self, code: int, message: str):
        """Send a JSON error response"""
        response = json.dumps({
            'success': False,
            'error': message
        }).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(response))
        self.end_headers()
        self.wfile.write(response)
    
    def log_message(self, format, *args):
        """Override to customize log format"""
        print(f"[{self.log_date_time_string()}] {format % args}")


def main():
    """Start the server"""
    # Change to script directory
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    
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
    
    with socketserver.TCPServer(("", PORT), FigureStudyHandler) as httpd:
        import socket
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


if __name__ == "__main__":
    main()
