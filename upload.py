import http.server
import socketserver
import cgi
import os
import json
import shutil
import subprocess
import urllib.request
import re
from urllib.parse import urlparse, parse_qs

UPLOAD_DIR = '/tmp/uploads'

if os.path.exists(UPLOAD_DIR):
    shutil.rmtree(UPLOAD_DIR)
os.makedirs(UPLOAD_DIR, exist_ok=True)

def ascii_qr_to_svg(qr_text, scale=8):
    lines = [l for l in qr_text.splitlines() if l.strip()]
    if not lines:
        return ''
    min_indent = min(len(l) - len(l.lstrip()) for l in lines)
    trimmed = [l[min_indent:] for l in lines]
    max_w = max(len(l) for l in trimmed)
    h = len(trimmed) * 2
    w = max_w
    
    rects = []
    for row_idx, line in enumerate(trimmed):
        y_top = row_idx * 2
        y_bot = row_idx * 2 + 1
        for col_idx, char in enumerate(line):
            x = col_idx
            if char == '█':
                rects.append(f'<rect x="{x*scale}" y="{y_top*scale}" width="{scale}" height="{scale*2}" fill="#000"/>')
            elif char == '▀':
                rects.append(f'<rect x="{x*scale}" y="{y_top*scale}" width="{scale}" height="{scale*2}" fill="#000"/>')
            elif char == '▄':
                rects.append(f'<rect x="{x*scale}" y="{y_bot*scale}" width="{scale}" height="{scale*2}" fill="#000"/>')
    
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w*scale} {h*scale}" width="{w*scale}" height="{h*scale}"><rect width="100%" height="100%" fill="#fff"/>' + ''.join(rects) + '</svg>'

class T3UploadHandler(http.server.SimpleHTTPRequestHandler):
    def do_POST(self):
        if self.path == '/upload':
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={'REQUEST_METHOD': 'POST',
                         'CONTENT_TYPE': self.headers['Content-Type']}
            )
            fileitem = form['file']
            if fileitem.filename:
                filename = os.path.basename(fileitem.filename)
                filepath = os.path.join(UPLOAD_DIR, filename)
                with open(filepath, 'wb') as f:
                    shutil.copyfileobj(fileitem.file, f)
                
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'path': filepath}).encode())
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error": "No file uploaded"}')

        elif self.path.startswith('/import_chunk'):
            parsed_path = urlparse(self.path)
            query = parse_qs(parsed_path.query)
            chunk_index = int(query.get('chunk_index', ['0'])[0])
            total_chunks = int(query.get('total_chunks', ['1'])[0])
            
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={'REQUEST_METHOD': 'POST',
                         'CONTENT_TYPE': self.headers['Content-Type']}
            )
            fileitem = form['file']
            if fileitem.filename:
                filepath = "/tmp/import_backup.tar.gz"
                mode = 'wb' if chunk_index == 0 else 'ab'
                with open(filepath, mode) as f:
                    shutil.copyfileobj(fileitem.file, f)
                
                if chunk_index == total_chunks - 1:
                    result = subprocess.run(["tar", "-xzf", filepath, "-C", "/"], capture_output=True)
                    if result.returncode == 0:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(b'{"status": "success"}')
                        
                        token = os.environ.get("SUPERVISOR_TOKEN")
                        if token:
                            subprocess.Popen(["bash", "-c", f"sleep 2 && curl -s -X POST -H 'Authorization: Bearer {token}' http://supervisor/addons/self/restart"])
                    else:
                        self.send_response(500)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(json.dumps({'error': result.stderr.decode()}).encode())
                else:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(b'{"status": "chunk_received"}')
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error": "No file uploaded"}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        parsed_path = urlparse(self.path)
        
        if parsed_path.path == '/kill':
            query = parse_qs(parsed_path.query)
            session_id = query.get('session_id', [None])[0]
            if session_id and session_id.isdigit():
                session_name = f"t3_{session_id}"
                subprocess.run(["tmux", "kill-session", "-t", session_name], capture_output=True)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"status": "killed"}')
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error": "Invalid session_id"}')

        elif parsed_path.path == '/t3_pair':
            # Mint a fresh pairing token from running t3 server
            try:
                env = dict(os.environ, T3CODE_HOME='/data/.t3')
                res = subprocess.run(
                    ['/usr/local/bin/t3', 'pair'],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                output = res.stdout
                url_match = re.search(r'Pairing URL:\s*(\S+)', output)
                app_match = re.search(r'App link:\s*(\S+)', output)
                token_match = re.search(r'Token:\s*(\S+)', output)
                expires_match = re.search(r'Expires:\s*(\S+)', output)
                
                # Extract ASCII QR lines
                qr_lines = [l for l in output.splitlines() if any(c in l for c in ['█', '▀', '▄'])]
                qr_text = '\n'.join(qr_lines)
                qr_svg = ascii_qr_to_svg(qr_text, scale=6) if qr_lines else ''

                pairing_url = url_match.group(1) if url_match else None
                app_link = app_match.group(1) if app_match else None
                token = token_match.group(1) if token_match else None
                expires = expires_match.group(1) if expires_match else None

                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    'status': 'success',
                    'pairing_url': pairing_url,
                    'app_link': app_link,
                    'token': token,
                    'expires': expires,
                    'qr_svg': qr_svg,
                    'raw': output
                }).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'status': 'error', 'message': str(e)}).encode())

        elif parsed_path.path == '/t3_status':
            t3_port = int(os.environ.get('T3_PORT', 3774))
            is_running = False
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{t3_port}/', timeout=2) as r:
                    if r.status in (200, 301, 302, 404):
                        is_running = True
            except:
                is_running = False

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                'running': is_running,
                'port': t3_port
            }).encode())

        elif parsed_path.path == '/t3_restart':
            t3_port = int(os.environ.get('T3_PORT', 3774))
            try:
                subprocess.run(['fuser', '-k', f'{t3_port}/tcp'], capture_output=True)
                subprocess.Popen(
                    ['/usr/local/bin/t3', 'serve', '--lan', '--port', str(t3_port), '--auto-bootstrap-project-from-cwd', '/homeassistant'],
                    env=dict(os.environ, T3CODE_HOME='/data/.t3')
                )
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"status": "restarting"}')
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'status': 'error', 'message': str(e)}).encode())

        elif parsed_path.path == '/t3_connect_status':
            # Query T3 Connect status
            try:
                env = dict(os.environ, T3CODE_HOME='/data/.t3')
                res = subprocess.run(
                    ['/usr/local/bin/t3', 'connect', 'status'],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                output = res.stdout + res.stderr
                exposure_match = re.search(r'Exposure:\s*(\S+)', output)
                auth_match = re.search(r'Authorization:\s*(.+)', output)
                link_match = re.search(r'Environment link:\s*(\S+)', output)
                relay_match = re.search(r'Relay:\s*(\S+)', output)
                tunnel_connected = "Tunnel connected" in output or "Other devices can reach this machine" in output

                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    'status': 'success',
                    'connected': tunnel_connected,
                    'exposure': exposure_match.group(1) if exposure_match else 'unknown',
                    'authorization': auth_match.group(1).strip() if auth_match else 'none',
                    'link': link_match.group(1) if link_match else 'unlinked',
                    'relay': relay_match.group(1) if relay_match else 'https://relay.t3.codes',
                    'raw': output
                }).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'status': 'error', 'message': str(e)}).encode())

        elif parsed_path.path == '/t3_connect_link':
            try:
                env = dict(os.environ, T3CODE_HOME='/data/.t3')
                res = subprocess.run(
                    ['/usr/local/bin/t3', 'up', '--connect', '--yes'],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=15
                )
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    'status': 'success',
                    'output': res.stdout + res.stderr
                }).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'status': 'error', 'message': str(e)}).encode())

        elif parsed_path.path == '/version_check':
            try:
                t3_ver = subprocess.check_output(['/usr/local/bin/t3', '--version'], stderr=subprocess.STDOUT).decode().strip()
            except:
                t3_ver = "unknown"

            try:
                gh_ver = subprocess.check_output(['/usr/local/bin/gh', '--version'], stderr=subprocess.STDOUT).decode().splitlines()[0].strip()
            except:
                gh_ver = "unknown"

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                't3_version': t3_ver,
                'gh_version': gh_ver
            }).encode())

        elif parsed_path.path == '/update_cli':
            try:
                subprocess.run(['bash', '-c', 'curl -fsSL https://t3.codes/install.sh | T3CODE_INSTALL_BIN_DIR=/usr/local/bin T3CODE_HOME=/opt/t3 sh'], check=True)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"status": "success"}')
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({'status': 'error', 'message': str(e)}).encode())

        elif parsed_path.path == '/export_backup':
            backup_file = "/tmp/t3code_backup.tar.gz"
            if os.path.exists(backup_file):
                os.remove(backup_file)
            
            targets = []
            if os.path.exists("/data/.t3"): targets.append("data/.t3")
            
            if targets:
                self.send_response(200)
                self.send_header('Content-Type', 'application/gzip')
                self.send_header('Content-Disposition', 'attachment; filename="t3code_backup.tar.gz"')
                self.end_headers()
                
                excludes = [
                    "--exclude=session_*.log",
                    "--exclude=*.log",
                    "--exclude=backup_extract",
                    "--exclude=node_modules",
                    "--exclude=*.zip",
                    "--exclude=__pycache__",
                    "--exclude=.cache"
                ]
                proc = subprocess.Popen(["tar", "-czf", "-"] + excludes + ["-C", "/"] + targets, stdout=subprocess.PIPE)
                shutil.copyfileobj(proc.stdout, self.wfile)
                proc.stdout.close()
                proc.wait()
            else:
                self.send_response(404)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"error": "Nothing to backup"}')
        else:
            self.send_response(404)
            self.end_headers()

if __name__ == "__main__":
    PORT = int(os.environ.get('API_PORT', 62906))
    print(f"Starting T3 Code upload & API server on port {PORT}")
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), T3UploadHandler) as httpd:
        httpd.serve_forever()
