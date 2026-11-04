import http.server
import socketserver
import cgi
import os
import json
import shutil
import subprocess
import urllib.request
import re
import threading
import time
from urllib.parse import urlparse, parse_qs

UPLOAD_DIR = '/tmp/uploads'

if os.path.exists(UPLOAD_DIR):
    shutil.rmtree(UPLOAD_DIR)
os.makedirs(UPLOAD_DIR, exist_ok=True)

def ascii_qr_to_svg(qr_text, scale=6, quiet_zone=2):
    lines = [l for l in qr_text.splitlines() if any(c in l for c in ['█', '▀', '▄'])]
    if not lines:
        return ''

    # Terminal half-block characters represent 2 vertical modules:
    # '█': top black, bottom black
    # '▀': top black, bottom white
    # '▄': top white, bottom black
    # ' ': top white, bottom white
    matrix = []
    for line in lines:
        row_top = []
        row_bot = []
        for c in line:
            if c == '█':
                row_top.append(1)
                row_bot.append(1)
            elif c == '▀':
                row_top.append(1)
                row_bot.append(0)
            elif c == '▄':
                row_top.append(0)
                row_bot.append(1)
            else:
                row_top.append(0)
                row_bot.append(0)
        matrix.append(row_top)
        matrix.append(row_bot)

    if not any(1 in row for row in matrix):
        return ''

    # Find exact bounding box of active QR code modules
    min_r = min(r for r in range(len(matrix)) if 1 in matrix[r])
    max_r = max(r for r in range(len(matrix)) if 1 in matrix[r])
    min_c = min(c for r in matrix for c in range(len(r)) if r[c] == 1)
    max_c = max(c for r in matrix for c in range(len(r)) if r[c] == 1)

    qr_h = max_r - min_r + 1
    qr_w = max_c - min_c + 1
    dim = max(qr_h, qr_w)

    total_dim = dim + quiet_zone * 2
    svg_size = total_dim * scale

    rects = []
    for r_idx, r in enumerate(range(min_r, max_r + 1)):
        for c_idx, c in enumerate(range(min_c, max_c + 1)):
            if c < len(matrix[r]) and matrix[r][c] == 1:
                x = (c_idx + quiet_zone) * scale
                y = (r_idx + quiet_zone) * scale
                rects.append(f'<rect x="{x}" y="{y}" width="{scale}" height="{scale}" fill="#000"/>')

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {svg_size} {svg_size}" '
        f'width="{svg_size}" height="{svg_size}" '
        f'style="width: 100%; height: auto; max-width: 220px; aspect-ratio: 1 / 1; display: block;" '
        f'shape-rendering="crispEdges">'
        f'<rect width="100%" height="100%" fill="#fff"/>'
        + ''.join(rects) +
        '</svg>'
    )

_t3_connect_lock = threading.Lock()
_t3_connect_proc = None
_t3_connect_info = {
    'status': 'idle',
    'url': None,
    'code': None,
    'message': None
}

def _run_t3_connect_worker():
    global _t3_connect_proc, _t3_connect_info
    try:
        env = dict(os.environ, T3CODE_HOME='/data/.t3')
        # Notify tmux active terminal session
        subprocess.run(["tmux", "send-keys", "-t", "t3_lite_1", "t3 connect", "C-m"], capture_output=True)

        _t3_connect_proc = subprocess.Popen(
            ['/usr/local/bin/t3', 'connect', 'link', '--headless'],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )

        for line in _t3_connect_proc.stdout:
            line_str = line.strip()
            if not line_str:
                continue
            url_match = re.search(r'(https://accounts\.t3\.codes/device\S*)', line_str)
            if url_match:
                _t3_connect_info['url'] = url_match.group(1)
                _t3_connect_info['status'] = 'auth_required'
            code_match = re.search(r'Code:\s*([A-Z0-9-]+)', line_str)
            if code_match:
                _t3_connect_info['code'] = code_match.group(1)
            if "Authorized as" in line_str or "already authorized" in line_str:
                _t3_connect_info['status'] = 'authorized'
                _t3_connect_info['message'] = line_str

        _t3_connect_proc.wait()
        if _t3_connect_proc.returncode == 0:
            _t3_connect_info['status'] = 'completed'
            # Restart t3 serve to pick up newly provisioned credentials and establish tunnel
            t3_port = int(os.environ.get('T3_PORT', 3774))
            subprocess.run(['fuser', '-k', f'{t3_port}/tcp'], capture_output=True)
            subprocess.Popen(
                ['/usr/local/bin/t3', 'serve', '--lan', '--port', str(t3_port), '--auto-bootstrap-project-from-cwd', '/homeassistant'],
                env=env
            )
        else:
            if _t3_connect_info['status'] != 'auth_required':
                _t3_connect_info['status'] = 'error'
    except Exception as e:
        _t3_connect_info['status'] = 'error'
        _t3_connect_info['message'] = str(e)

_account_lock = threading.Lock()
_account_proc = None
_account_info = {
    'status': 'idle',
    'email': None,
    'url': None,
    'code': None,
    'message': None
}

def _run_account_login_worker():
    global _account_proc, _account_info
    try:
        env = dict(os.environ, HOME='/data', T3CODE_HOME='/data/.t3')
        subprocess.run(["tmux", "send-keys", "-t", "t3_lite_1", "t3 connect login --headless", "C-m"], capture_output=True)

        _account_proc = subprocess.Popen(
            ['/usr/local/bin/t3', 'connect', 'login', '--headless'],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )

        for line in _account_proc.stdout:
            line_str = line.strip()
            if not line_str:
                continue

            signed_match = re.search(r'Signed in as\s+(\S+)', line_str)
            if signed_match:
                _account_info['email'] = signed_match.group(1)
                _account_info['status'] = 'signed_in'
                _account_info['message'] = line_str

            url_match = re.search(r'(https?://\S+)', line_str)
            if url_match and not _account_info.get('url'):
                found_url = url_match.group(1)
                if any(k in found_url for k in ['device', 'connect', 'clerk', 'accounts', 't3.codes']):
                    _account_info['url'] = found_url
                    _account_info['status'] = 'auth_required'

            code_match = re.search(r'Code:\s*([A-Za-z0-9-]+)', line_str)
            if code_match:
                _account_info['code'] = code_match.group(1)

        _account_proc.wait()
        if _account_proc.returncode == 0:
            if _account_info.get('status') != 'signed_in':
                _account_info['status'] = 'signed_in'
        else:
            if _account_info.get('status') != 'auth_required':
                _account_info['status'] = 'error'
    except Exception as e:
        _account_info['status'] = 'error'
        _account_info['message'] = str(e)

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
                subprocess.run(["tmux", "kill-session", "-t", f"t3_lite_{session_id}"], capture_output=True)
                subprocess.run(["tmux", "kill-session", "-t", f"t3_{session_id}"], capture_output=True)
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

                mobile_app_link = None
                if pairing_url:
                    encoded_url = urllib.parse.quote(pairing_url, safe='')
                    mobile_app_link = f"t3code://connections/new?pairingUrl={encoded_url}&autoConnect=1"
                elif app_link:
                    mobile_app_link = app_link.replace('t3code://pair?', 't3code://connections/new?')
                    if 'autoConnect' not in mobile_app_link:
                        mobile_app_link += '&autoConnect=1'

                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    'status': 'success',
                    'pairing_url': pairing_url,
                    'app_link': app_link,
                    'mobile_app_link': mobile_app_link,
                    'token': token,
                    'expires': expires,
                    'qr_svg': qr_svg
                }, ensure_ascii=True).encode())
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
                publish_match = re.search(r'Publish agent activity:\s*(\S+)', output)
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
                    'publish_agent_activity': publish_match.group(1).lower() if publish_match else 'disabled',
                    'raw': output
                }).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'status': 'error', 'message': str(e)}).encode())

        elif parsed_path.path == '/t3_publish':
            query = parse_qs(parsed_path.query)
            action = query.get('action', ['toggle'])[0]
            try:
                env = dict(os.environ, T3CODE_HOME='/data/.t3')
                if action == 'status':
                    res = subprocess.run(['/usr/local/bin/t3', 'connect', 'status'], env=env, capture_output=True, text=True, timeout=10)
                    pub_m = re.search(r'Publish agent activity:\s*(\S+)', res.stdout)
                    is_enabled = (pub_m.group(1).lower() == 'enabled') if pub_m else False
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        'status': 'success',
                        'enabled': is_enabled,
                        'message': 'enabled' if is_enabled else 'disabled'
                    }).encode())
                    return

                if action == 'toggle':
                    chk = subprocess.run(['/usr/local/bin/t3', 'connect', 'status'], env=env, capture_output=True, text=True, timeout=10)
                    if 'Publish agent activity: enabled' in chk.stdout:
                        action = 'disable'
                    else:
                        action = 'enable'

                cmd = ['/usr/local/bin/t3', 'connect', 'publish']
                if action == 'disable':
                    cmd.append('--disable')

                res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=10)
                msg = (res.stdout + res.stderr).strip()

                st_res = subprocess.run(['/usr/local/bin/t3', 'connect', 'status'], env=env, capture_output=True, text=True, timeout=10)
                pub_m = re.search(r'Publish agent activity:\s*(\S+)', st_res.stdout)
                is_enabled = (pub_m.group(1).lower() == 'enabled') if pub_m else ('enabled' in msg.lower())

                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    'status': 'success' if res.returncode == 0 else 'error',
                    'enabled': is_enabled,
                    'message': msg
                }).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'status': 'error', 'message': str(e)}).encode())

        elif parsed_path.path == '/t3_lan':
            query = parse_qs(parsed_path.query)
            action = query.get('action', ['status'])[0]
            try:
                env = dict(os.environ, HOME='/data', T3CODE_HOME='/data/.t3')

                def get_current_lan():
                    try:
                        st = subprocess.run(['/usr/local/bin/t3', 'status', '--json'], env=env, capture_output=True, text=True, timeout=5)
                        data = json.loads(st.stdout)
                        return data.get('lan', {}).get('enabled', False)
                    except Exception:
                        if os.path.exists('/data/.t3/service.env'):
                            with open('/data/.t3/service.env', 'r') as f:
                                c = f.read()
                                if 'T3CODE_LAN=false' in c: return False
                                if 'T3CODE_LAN=true' in c: return True
                        return False

                if action == 'status':
                    is_enabled = get_current_lan()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        'status': 'success',
                        'enabled': is_enabled,
                        'message': 'enabled' if is_enabled else 'disabled'
                    }).encode())
                    return

                if action == 'toggle':
                    current_enabled = get_current_lan()
                    target_enable = not current_enabled
                elif action == 'enable':
                    target_enable = True
                elif action == 'disable':
                    target_enable = False
                else:
                    target_enable = True

                flag = '--lan' if target_enable else '--no-lan'
                subprocess.run(['/usr/local/bin/t3', 'up', flag], env=env, capture_output=True, text=True, timeout=10)

                t3_port = os.environ.get('T3_PORT', '3774')
                subprocess.run(['pkill', '-f', '/usr/local/bin/t3 serve'], capture_output=True)
                time.sleep(1)

                serve_cmd = ['/usr/local/bin/t3', 'serve', '--port', str(t3_port), '--auto-bootstrap-project-from-cwd', '/homeassistant']
                if target_enable:
                    serve_cmd.insert(2, '--lan')

                with open('/data/t3_serve.log', 'a') as log_f:
                    subprocess.Popen(serve_cmd, env=env, stdout=log_f, stderr=subprocess.STDOUT)

                time.sleep(1)
                is_enabled = get_current_lan()

                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    'status': 'success',
                    'enabled': is_enabled,
                    'message': f"Local LAN connections {'enabled' if is_enabled else 'disabled'}"
                }).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'status': 'error', 'message': str(e)}).encode())

        elif parsed_path.path == '/t3_account':
            query = parse_qs(parsed_path.query)
            action = query.get('action', ['status'])[0]
            env = dict(os.environ, HOME='/data', T3CODE_HOME='/data/.t3')
            try:
                if action == 'status':
                    try:
                        res = subprocess.run(
                            ['/usr/local/bin/t3', 'connect', 'login', '--headless'],
                            env=env,
                            capture_output=True,
                            text=True,
                            timeout=3
                        )
                        signed_match = re.search(r'Signed in as\s+(\S+)', res.stdout)
                        if signed_match:
                            self.send_response(200)
                            self.send_header('Content-Type', 'application/json')
                            self.end_headers()
                            self.wfile.write(json.dumps({
                                'status': 'signed_in',
                                'email': signed_match.group(1)
                            }).encode())
                            return
                    except subprocess.TimeoutExpired:
                        pass
                    except Exception:
                        pass

                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({'status': 'signed_out'}).encode())
                    return

                elif action == 'login':
                    global _account_proc, _account_info
                    try:
                        res = subprocess.run(
                            ['/usr/local/bin/t3', 'connect', 'login', '--headless'],
                            env=env,
                            capture_output=True,
                            text=True,
                            timeout=3
                        )
                        signed_match = re.search(r'Signed in as\s+(\S+)', res.stdout)
                        if signed_match:
                            self.send_response(200)
                            self.send_header('Content-Type', 'application/json')
                            self.end_headers()
                            self.wfile.write(json.dumps({
                                'status': 'already_signed_in',
                                'email': signed_match.group(1)
                            }).encode())
                            return
                    except Exception:
                        pass

                    with _account_lock:
                        if _account_proc is None or _account_proc.poll() is not None:
                            _account_info = {'status': 'starting', 'email': None, 'url': None, 'code': None, 'message': None}
                            t = threading.Thread(target=_run_account_login_worker, daemon=True)
                            t.start()

                    for _ in range(30):
                        if _account_info.get('url') and _account_info.get('code'):
                            break
                        if _account_info.get('status') in ('signed_in', 'error'):
                            break
                        time.sleep(0.1)

                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps(_account_info).encode())
                    return

                elif action == 'logout':
                    res = subprocess.run(
                        ['/usr/local/bin/t3', 'connect', 'logout'],
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=10
                    )
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        'status': 'logged_out',
                        'message': res.stdout.strip()
                    }).encode())
                    return
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'status': 'error', 'message': str(e)}).encode())

        elif parsed_path.path == '/run_quick_cmd':
            query = parse_qs(parsed_path.query)
            cmd_key = query.get('cmd', [''])[0]
            allowed_cmds = {
                'doctor': ['/usr/local/bin/t3', 'doctor'],
                'doctor_fix': ['/usr/local/bin/t3', 'doctor', '--fix'],
                'status': ['/usr/local/bin/t3', 'status'],
                'connect_status': ['/usr/local/bin/t3', 'connect', 'status'],
                'version': ['/usr/local/bin/t3', '--version'],
                'tmux_sessions': ['tmux', 'list-sessions'],
                'login_headless': ['/usr/local/bin/t3', 'connect', 'login', '--headless'],
                'logout': ['/usr/local/bin/t3', 'connect', 'logout'],
                'connect_link_headless': ['/usr/local/bin/t3', 'connect', 'link', '--headless'],
            }
            if cmd_key in allowed_cmds:
                try:
                    env = dict(os.environ, T3CODE_HOME='/data/.t3')
                    res = subprocess.run(allowed_cmds[cmd_key], env=env, capture_output=True, text=True, timeout=20)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        'status': 'success',
                        'command': ' '.join(allowed_cmds[cmd_key]),
                        'output': res.stdout + res.stderr
                    }).encode())
                except Exception as e:
                    self.send_response(500)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({'status': 'error', 'message': str(e)}).encode())
            else:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"status": "error", "message": "Command not allowed"}')
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'status': 'error', 'message': str(e)}).encode())

        elif parsed_path.path == '/t3_connect_link':
            global _t3_connect_proc, _t3_connect_info
            # First check if already connected
            try:
                env = dict(os.environ, T3CODE_HOME='/data/.t3')
                chk = subprocess.run(['/usr/local/bin/t3', 'connect', 'status'], env=env, capture_output=True, text=True, timeout=5)
                if "Tunnel connected" in chk.stdout or "Other devices can reach this machine" in chk.stdout:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({'status': 'already_connected'}).encode())
                    return
            except Exception:
                pass

            with _t3_connect_lock:
                if _t3_connect_proc is None or _t3_connect_proc.poll() is not None:
                    _t3_connect_info = {'status': 'starting', 'url': None, 'code': None, 'message': None}
                    t = threading.Thread(target=_run_t3_connect_worker, daemon=True)
                    t.start()

            # Wait up to 3.5 seconds for URL / Code to be extracted
            for _ in range(35):
                if _t3_connect_info.get('url') and _t3_connect_info.get('code'):
                    break
                if _t3_connect_info.get('status') in ('completed', 'already_connected', 'error'):
                    break
                time.sleep(0.1)

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(_t3_connect_info).encode())

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
