#!/usr/bin/env python3
"""Local Webex exporter and space member copier. Python 3.10+, no dependencies."""
import html
import json
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlparse, parse_qs
from space_copy import SpaceCopy
from urllib.request import Request, build_opener, HTTPRedirectHandler

ROOT = Path(__file__).resolve().parent
API = 'https://webexapis.com/v1/'
PORT = int(os.environ.get('WEBEX_EXPORT_PORT', '8766'))
CSRF = secrets.token_urlsafe(32)
STATE = {'status': 'idle', 'messages': 0, 'files': 0, 'warnings': []}
LOCK = threading.Lock()
CANCEL = threading.Event()

def token():
    values = {}
    if (ROOT / '.env').exists():
        for line in (ROOT / '.env').read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.removeprefix('export ').split('=', 1)
                values[k.strip()] = v.strip().strip('\"\'')
    value = values.get('WEBEX_BEARER_TOKEN') or os.environ.get('WEBEX_BEARER_TOKEN')
    if not value:
        raise ValueError('Set WEBEX_BEARER_TOKEN in .env first.')
    return value.removeprefix('Bearer ')

class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme != 'https':
            raise ValueError('Refused an insecure file redirect.')
        result = super().redirect_request(req, fp, code, msg, headers, newurl)
        if urlparse(req.full_url).netloc != urlparse(newurl).netloc:
            result.remove_header('Authorization')
        return result

OPENER = build_opener(SafeRedirect())

def fetch(url, secret, cancel=None):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname != 'webexapis.com' or parsed.port not in (None, 443):
        raise ValueError('Refused an unexpected Webex API or file URL.')
    for attempt in range(6):
        if cancel and cancel.is_set():
            raise InterruptedError('Export cancelled; partial files are preserved.')
        try:
            return OPENER.open(Request(url, headers={'Authorization': 'Bearer ' + secret}), timeout=45)
        except HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < 5:
                try:
                    delay = min(120, max(1, float(exc.headers.get('Retry-After', 2 ** attempt))))
                except ValueError:
                    delay = 2 ** attempt
                if cancel:
                    cancel.wait(delay)
                else:
                    time.sleep(delay)
                continue
            if exc.code in (401, 403):
                raise ValueError('Webex denied access. Check token expiry and your space permissions.') from None
            raise ValueError(f'Webex request failed (HTTP {exc.code}).') from None
    raise ValueError('Webex is unavailable; try again later.')

def pages(path, secret, cancel=None):
    url = API + path
    visited = set()
    while url:
        if url in visited:
            raise ValueError('Webex returned a repeated pagination link.')
        visited.add(url)
        with fetch(url, secret, cancel) as response:
            data = json.load(response)
            link = response.headers.get('Link', '')
        yield data.get('items', [])
        match = re.search(r'<([^>]+)>;\s*rel="?next"?', link)
        url = match.group(1) if match else None

def update(**values):
    with LOCK:
        STATE.update(values)

def safe_name(value):
    return re.sub(r'[^\w. -]', '_', value).strip(' .')[:100] or 'space'

def local_timestamp(value):
    if not value:
        return ''
    try:
        instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        return instant.astimezone(ZoneInfo('Europe/Prague')).strftime('%Y-%m-%d %H:%M:%S %Z')
    except (ValueError, TypeError):
        return value


def transcript(room, messages, files):
    esc = lambda v: html.escape(str(v), quote=True)
    rows = []
    for message in messages:
        mid = message.get('id', '')
        parent = message.get('parentId')
        reply = f'<a href="#{esc(parent)}">Reply to earlier message</a>' if parent else ''
        links = ''.join(f'<li><a href="{quote(f, safe="/")}" download>{esc(Path(f).name)}</a></li>' for f in files.get(mid, []))
        rows.append(f'<article id="{esc(mid)}"><header><b>{esc(message.get("personEmail", message.get("personId", "Unknown")))}</b> · {esc(local_timestamp(message.get("created", "")))}</header>{reply}<pre>{esc(message.get("text", message.get("markdown", "[Non-text message]")))}</pre><ul>{links}</ul></article>')
    return '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>' + esc(room['title']) + '</title><style>body{font:16px system-ui;max-width:950px;margin:40px auto;padding:20px;background:#f5f7fa;color:#172438}article{background:white;padding:20px;margin:12px 0;border:1px solid #dde3eb;border-radius:12px}header{font-size:13px;color:#516078}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}a{color:#185db7}</style><h1>' + esc(room['title']) + '</h1><p>Offline export · ' + str(len(messages)) + ' messages · timestamps in Europe/Prague (CET / CEST). See manifest.json for export status and missing files.</p>' + ''.join(rows)

def export(room_id, include_files):
    folder = None
    messages = []
    file_map = {}
    warnings = []
    status = 'failed'
    try:
        secret = token()
        with fetch(API + 'rooms/' + quote(room_id, safe=''), secret, CANCEL) as response:
            room = json.load(response)
        name = safe_name(room.get('title', 'space')) + '-' + datetime.now(ZoneInfo('Europe/Prague')).strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
        folder = ROOT / 'exports' / name
        folder.mkdir(parents=True, mode=0o700)
        (folder / 'room.json').write_text(json.dumps(room, indent=2))
        update(folder=name, phase='Reading message history')
        seen = set()
        with (folder / 'messages.partial.jsonl').open('w') as journal:
            for batch in pages('messages?' + urlencode({'roomId': room_id, 'max': 100}), secret, CANCEL):
                for item in batch:
                    if item['id'] not in seen:
                        seen.add(item['id'])
                        messages.append(item)
                        journal.write(json.dumps(item) + '\n')
                journal.flush()
                update(messages=len(messages))
        messages.sort(key=lambda m: (m.get('created', ''), m.get('id', '')))
        (folder / 'messages.json').write_text(json.dumps(messages, indent=2))
        if include_files:
            (folder / 'attachments').mkdir()
            total = sum(len(m.get('files', [])) for m in messages)
            update(phase='Downloading attachments', total_files=total)
            count = 0
            for m in messages:
                for index, url in enumerate(m.get('files', [])):
                    if CANCEL.is_set():
                        raise InterruptedError('Export cancelled; partial files are preserved.')
                    target = None
                    try:
                        with fetch(url, secret, CANCEL) as response:
                            filename = response.headers.get_filename() or 'attachment'
                            relative = 'attachments/' + uuid.uuid4().hex[:12] + '-' + safe_name(filename)
                            target = folder / (relative + '.part')
                            with target.open('wb') as output:
                                while True:
                                    if CANCEL.is_set():
                                        raise InterruptedError('Export cancelled; partial files are preserved.')
                                    chunk = response.read(1024 * 1024)
                                    if not chunk:
                                        break
                                    output.write(chunk)
                            target.rename(folder / relative)
                            file_map.setdefault(m['id'], []).append(relative)
                            count += 1
                            update(files=count)
                    except InterruptedError:
                        raise
                    except Exception:
                        warnings.append({'messageId': m['id'], 'fileIndex': index, 'reason': 'Attachment download failed; file may be unavailable or access denied.'})
                        update(warnings=list(warnings))
                    finally:
                        if target and target.exists():
                            target.unlink()
        if CANCEL.is_set():
            raise InterruptedError('Export cancelled; partial files are preserved.')
        status = 'completed_with_warnings' if warnings else 'completed'
    except InterruptedError as exc:
        status = 'cancelled'
        update(error=str(exc))
    except Exception as exc:
        update(error=str(exc) if isinstance(exc, ValueError) else 'Export failed due to a network or local file error. Partial files are preserved.')
    finally:
        if folder:
            try:
                messages.sort(key=lambda m: (m.get('created', ''), m.get('id', '')))
                (folder / 'messages.json').write_text(json.dumps(messages, indent=2))
                (folder / 'files.json').write_text(json.dumps(file_map, indent=2))
                (folder / 'index.html').write_text(transcript(room, messages, file_map))
                (folder / 'manifest.json').write_text(json.dumps({'status': status, 'exportedAt': datetime.now(timezone.utc).isoformat(), 'messages': len(messages), 'attachmentsRequested': include_files, 'downloadedFiles': sum(map(len, file_map.values())), 'warnings': warnings, 'scope': 'Messages currently available to this account; not a restorable Webex backup. External cloud documents and other space content are not downloaded.'}, indent=2))
                if status.startswith('completed'):
                    (folder / 'messages.partial.jsonl').unlink(missing_ok=True)
                update(url='/exports/' + quote(folder.name) + '/index.html')
            except Exception:
                status = 'failed'
                update(error='Unable to finish writing the export. Check available disk space.')
        update(status=status, phase='Finished')

COPIER = SpaceCopy(ROOT, token, fetch, pages)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, value, status=200, mime='application/json'):
        data = json.dumps(value).encode() if mime == 'application/json' else value.encode()
        self.send_response(status)
        self.send_header('Content-Type', mime + '; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def allowed(self):
        return self.headers.get('Host') in (f'127.0.0.1:{PORT}', f'localhost:{PORT}')

    def do_GET(self):
        if not self.allowed():
            return self.send({'error': 'Invalid host'}, 403)
        path = urlparse(self.path).path
        try:
            if path == '/':
                return self.send((ROOT / 'ui.html').read_text().replace('__CSRF__', CSRF), mime='text/html')
            if path == '/api/copy/status':
                operation_id = parse_qs(urlparse(self.path).query).get('id', [''])[0]
                return self.send(COPIER.status(operation_id))
            if path == '/api/status':
                with LOCK:
                    return self.send(dict(STATE))
            if path == '/api/rooms':
                secret = token()
                rooms = [r for batch in pages('rooms?max=100', secret) for r in batch]
                return self.send({'rooms': [{'id': r['id'], 'title': r.get('title', 'Untitled'), 'type': r.get('type', '')} for r in rooms]})
            if path.startswith('/exports/'):
                from urllib.parse import unquote
                file = (ROOT / unquote(path.lstrip('/'))).resolve()
                if not file.is_relative_to((ROOT / 'exports').resolve()) or not file.is_file():
                    return self.send({'error': 'Not found'}, 404)
                data = file.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8' if file.name == 'index.html' else 'application/octet-stream')
                self.send_header('Content-Security-Policy', "sandbox; default-src 'none'; style-src 'unsafe-inline'")
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
            self.send({'error': 'Not found'}, 404)
        except Exception as exc:
            self.send({'error': str(exc) if isinstance(exc, ValueError) else 'Could not connect to Webex. Check your connection and try again.'}, 400)

    def do_POST(self):
        if not self.allowed() or self.headers.get('X-CSRF-Token') != CSRF:
            return self.send({'error': 'Invalid request'}, 403)
        if self.path in ('/api/copy/members', '/api/copy/preview', '/api/copy/create'):
            try:
                size = int(self.headers.get('Content-Length', 0))
                if not 0 < size <= 262144:
                    raise ValueError('Invalid request size.')
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError('Invalid request body.')
                if self.path.endswith('/members'):
                    result = COPIER.members(body.get('roomId'))
                elif self.path.endswith('/preview'):
                    result = COPIER.preview(body)
                else:
                    result = COPIER.start(body.get('id'))
                return self.send(result)
            except ValueError as exc:
                return self.send({'error': str(exc)}, 400)
            except Exception:
                return self.send({'error': 'Could not finish the request. Check your connection and local file permissions.'}, 400)
        if self.path == '/api/cancel':
            CANCEL.set()
            return self.send({'ok': True})
        if self.path != '/api/export':
            return self.send({'error': 'Not found'}, 404)
        try:
            size = int(self.headers.get('Content-Length', 0))
            if not 0 < size < 8192:
                raise ValueError()
            body = json.loads(self.rfile.read(size))
            room_id = body['roomId']
            if not isinstance(room_id, str) or not room_id or len(room_id) > 2000:
                raise ValueError()
        except Exception:
            return self.send({'error': 'Choose a space first.'}, 400)
        with LOCK:
            if STATE['status'] == 'running':
                return self.send({'error': 'An export is already running.'}, 409)
            STATE.clear()
            STATE.update(status='running', phase='Starting', messages=0, files=0, warnings=[])
            CANCEL.clear()
        threading.Thread(target=export, args=(room_id, bool(body.get('attachments', True))), daemon=True).start()
        self.send({'ok': True})

if __name__ == '__main__':
    os.umask(0o077)
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    print(f'Webex Tools: http://127.0.0.1:{PORT}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        CANCEL.set()
        server.server_close()
