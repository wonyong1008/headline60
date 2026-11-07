"""Local desktop OAuth and resumable YouTube uploads (visibility from settings); no credentials in APIs."""
import base64
import fcntl
import hashlib
import json
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from app.config import Settings
from app.process import check_cancel, file_digest, run

SCOPES = ['https://www.googleapis.com/auth/youtube.upload', 'https://www.googleapis.com/auth/youtube.readonly']
TOKEN_URL = 'https://oauth2.googleapis.com/token'
UPLOAD_URL = 'https://www.googleapis.com/upload/youtube/v3/videos'
CHUNK_BYTES = 8 * 1024 * 1024


class YoutubeAuthError(RuntimeError):
    pass


def private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = path.with_suffix(path.suffix + '.tmp')
    # Create with owner-only permissions before writing any credential.
    import os
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    temporary.chmod(0o600)
    temporary.replace(path)


def read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    if path.is_symlink() or path.stat().st_size > 1_000_000:
        raise ValueError('연결 정보 파일 경로 또는 크기가 올바르지 않습니다.')
    return json.loads(path.read_text())


def api_error(response: httpx.Response) -> RuntimeError:
    try:
        error = response.json().get('error', {})
        reason = error.get('errors', [{}])[0].get('reason', '') if isinstance(error, dict) else error
    except (ValueError, KeyError, IndexError):
        reason = ''
    if response.status_code == 401 or reason in {'invalid_grant', 'invalid_token', 'unauthorized_client'}:
        return YoutubeAuthError('Google 연결이 만료되었거나 취소되었습니다. YouTube를 다시 연결하세요.')
    return RuntimeError(f'YouTube 요청 실패 (HTTP {response.status_code}, {str(reason)[:100]}). API 활성화·할당량·채널 권한을 확인하세요.')


def checked(response: httpx.Response, codes=(200,)) -> httpx.Response:
    if response.status_code not in codes:
        raise api_error(response)
    return response


def session_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname not in {'www.googleapis.com', 'youtube.googleapis.com'} or parts.username or parts.password or parts.port not in {None, 443}:
        raise ValueError('Google에서 제공한 업로드 세션 주소가 아닙니다.')
    return url


def video_metadata(folder: Path, privacy: str = 'private') -> dict:
    script = read_json(folder / 'script.json')
    metadata = read_json(folder / 'metadata.json')
    title = str(script.get('title', '')).strip()
    if not title or len(title) > 100 or '<' in title or '>' in title:
        raise ValueError('YouTube 제목은 1~100자이며 꺾쇠 괄호를 포함할 수 없습니다.')
    narration = '\n\n'.join(s['narration'] for s in script['scenes'])
    sources = '\n'.join(dict.fromkeys(f"{e['publisher']} · {e['url']}" for e in metadata.get('sources', [])))
    credits = (folder / 'CREDITS.md').read_text() if (folder / 'CREDITS.md').is_file() else ''
    description = narration + ('\n\n출처\n' + sources if sources else '')
    if credits:
        description += '\n\n' + credits.removeprefix('# 자료 출처\n').strip()
    description += '\n\nHEADLINE 60 · 자동 편집 콘텐츠입니다.\n#Shorts #헤드라인60'
    if len(description) > 5000:
        raise ValueError('전체 대본과 필수 출처가 설명 5,000자 제한을 넘습니다. 대본·출처 표시를 정리하세요.')
    # YouTube requires the altered/synthetic disclosure for realistic synthetic people: the AI
    # presenter. Photo + caption + narration videos contain no realistic synthetic scene.
    anchor = read_json(folder / 'anchor-status.json')
    synthetic = anchor.get('status') == 'completed'
    return {'snippet': {'title': title, 'description': description, 'categoryId': '25',
                        'defaultLanguage': 'ko', 'tags': [str(t).lstrip('#') for t in script.get('hashtags', [])][:12]},
            'status': {'privacyStatus': privacy, 'selfDeclaredMadeForKids': False, 'containsSyntheticMedia': synthetic}}


class YoutubeService:
    def __init__(self, config: Settings):
        self.config = config
        self.root = config.data / 'integrations' / 'youtube'
        self.lock = threading.RLock()
        self.on_connected = None
        self.pending = None
        self.last_error = None

    def client(self) -> dict:
        data = read_json(self.root / 'client.json')
        installed = data.get('installed', {})
        if not installed.get('client_id') or not installed.get('client_secret'):
            raise YoutubeAuthError('설정에서 데스크톱 OAuth JSON을 먼저 등록하세요.')
        return installed

    def configure(self, data: dict) -> dict:
        installed = data.get('installed', {})
        if (not str(installed.get('client_id', '')).endswith('.apps.googleusercontent.com')
                or not isinstance(installed.get('client_secret'), str) or not installed['client_secret']):
            raise ValueError('Google에서 받은 데스크톱 앱 OAuth JSON을 등록하세요.')
        with self.lock:
            previous = read_json(self.root / 'client.json').get('installed', {})
            if previous and previous.get('client_id') != installed['client_id'] and (self.root / 'token.json').is_file():
                raise ValueError('다른 OAuth 앱으로 바꾸기 전에 연결 해제를 해주세요.')
            private_json(self.root / 'client.json', {'installed': {key: installed[key] for key in ('client_id', 'client_secret')}})
        return self.status()

    def status(self) -> dict:
        token = read_json(self.root / 'token.json')
        return {'client_configured': bool(read_json(self.root / 'client.json').get('installed')),
                'connected': bool(token.get('refresh_token')) and not token.get('invalid', False),
                'channel': token.get('channel'), 'privacy': self.config.youtube_privacy, 'error': self.last_error,
                'authorizing': bool(self.pending and self.pending['expires'] > time.time())}

    def begin(self) -> dict:
        with self.lock:
            client = self.client()
            if self.pending and self.pending['expires'] > time.time():
                return {'authorization_url': self.pending['url']}
            owner = self
            state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
            class Callback(BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    pass  # Authorization codes must never enter request logs.

                def do_GET(self):
                    if urlsplit(self.path).path != '/oauth/callback':
                        self.send_error(404)
                        return
                    params = parse_qs(urlsplit(self.path).query)
                    valid = secrets.compare_digest(params.get('state', [''])[0], state)
                    if not valid:
                        self.send_error(400, 'Invalid OAuth state')
                        return
                    try:
                        if params.get('error'):
                            raise YoutubeAuthError('Google 권한 승인이 취소되었습니다.')
                        owner.finish(params.get('code', [''])[0], verifier, redirect)
                        message = 'HEADLINE 60 YouTube connection complete. You may close this tab.'
                        self.send_response(200)
                    except (RuntimeError, ValueError, OSError, httpx.HTTPError) as exc:
                        owner.last_error = str(exc) if not isinstance(exc, httpx.HTTPError) else 'Google 연결 요청에 실패했습니다.'
                        message = 'YouTube connection failed. Return to HEADLINE 60 settings.'
                        self.send_response(400)
                    self.send_header('Content-Type', 'text/plain; charset=utf-8')
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    self.wfile.write(message.encode())
                    done.set()
            server = HTTPServer(('127.0.0.1', 0), Callback)
            server.timeout = 1
            redirect = f'http://127.0.0.1:{server.server_port}/oauth/callback'
            url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode({
                'client_id': client['client_id'], 'redirect_uri': redirect, 'response_type': 'code',
                'scope': ' '.join(SCOPES), 'access_type': 'offline', 'prompt': 'consent',
                'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'})
            done = threading.Event()
            self.pending = {'url': url, 'expires': time.time() + 600}
            def serve():
                try:
                    while not done.is_set() and self.pending and self.pending['expires'] > time.time():
                        server.handle_request()
                finally:
                    server.server_close()
                    self.pending = None
            threading.Thread(target=serve, daemon=True, name='youtube-oauth-callback').start()
            return {'authorization_url': url}

    def finish(self, code: str, verifier: str, redirect: str) -> None:
        if not code:
            raise YoutubeAuthError('Google 승인 코드가 없습니다.')
        client = self.client()
        with httpx.Client(timeout=30, trust_env=False) as http:
            response = checked(http.post(TOKEN_URL, data={**client, 'code': code, 'code_verifier': verifier,
                'redirect_uri': redirect, 'grant_type': 'authorization_code'}))
            token = response.json()
            if not token.get('refresh_token'):
                raise YoutubeAuthError('갱신 토큰을 받지 못했습니다. Google 권한 동의를 다시 진행하세요.')
            response = checked(http.get('https://www.googleapis.com/youtube/v3/channels',
                params={'part': 'snippet', 'mine': 'true'}, headers={'Authorization': 'Bearer ' + token['access_token']}))
            channels = response.json().get('items', [])
            if not channels:
                raise YoutubeAuthError('승인한 Google 계정에 YouTube 채널이 없습니다.')
            token.update(expires_at=time.time() + token.get('expires_in', 3600),
                channel={'id': channels[0]['id'], 'title': channels[0]['snippet']['title']})
            private_json(self.root / 'token.json', token)
        self.last_error = None
        if self.on_connected:
            self.on_connected()

    def access_token(self) -> str:
        with self.lock:
            token = read_json(self.root / 'token.json')
            if not token.get('refresh_token') or token.get('invalid'):
                raise YoutubeAuthError('설정에서 YouTube 계정을 먼저 연결하세요.')
            if token.get('expires_at', 0) > time.time() + 90:
                return token['access_token']
            try:
                with httpx.Client(timeout=30, trust_env=False) as http:
                    response = checked(http.post(TOKEN_URL, data={**self.client(),
                        'refresh_token': token['refresh_token'], 'grant_type': 'refresh_token'}))
                    renewed = response.json()
            except YoutubeAuthError:
                token['invalid'] = True
                private_json(self.root / 'token.json', token)
                raise
            token.update(renewed, expires_at=time.time() + renewed.get('expires_in', 3600))
            private_json(self.root / 'token.json', token)
            return token['access_token']

    def upload(self, folder: Path) -> dict:
        # One upload per job even across processes: a second caller waits, then sees the video ID.
        with (folder / 'youtube-upload.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                return self._upload(folder)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _upload(self, folder: Path) -> dict:
        path, journal = folder / 'output.mp4', folder / 'youtube-upload.json'
        if not path.is_file() or not (folder / 'ffprobe.json').is_file():
            raise ValueError('검증을 완료한 영상만 업로드할 수 있습니다.')
        record = read_json(journal)
        if record.get('video_id'):
            self._thumbnail(folder, record)
            return {key: record.get(key) for key in ('video_id', 'url', 'privacy', 'thumbnail_status')}
        payload = video_metadata(folder, self.config.youtube_privacy)
        current_hash, total = file_digest(path), path.stat().st_size
        if record.get('sha256') and record['sha256'] != current_hash:
            raise ValueError('업로드 중인 원본 영상이 변경되었습니다.')
        headers = {'Authorization': 'Bearer ' + self.access_token()}
        with httpx.Client(timeout=90, trust_env=False, follow_redirects=False) as http:
            self._verify_channel(http, headers)
            if record.get('session_url'):
                url = session_url(record['session_url'])
                response = checked(http.put(url, headers={**headers, 'Content-Length': '0', 'Content-Range': f'bytes */{total}'}), (200, 201, 308, 404, 410))
                if response.status_code in {200, 201}:
                    record.update(video_id=response.json()['id'])
                elif response.status_code == 308:
                    record['offset'] = acknowledged(response, total)
                else:
                    record.pop('session_url', None)
            if not record.get('session_url') and not record.get('video_id'):
                response = checked(http.post(UPLOAD_URL, params={'uploadType': 'resumable', 'part': 'snippet,status'},
                    headers={**headers, 'X-Upload-Content-Type': 'video/mp4', 'X-Upload-Content-Length': str(total)}, json=payload), (200, 201))
                record.update(session_url=session_url(response.headers['location']), offset=0, sha256=current_hash,
                              requested_privacy=payload['status']['privacyStatus'])
                private_json(journal, record)
            with path.open('rb') as video:
                while not record.get('video_id'):
                    check_cancel()
                    offset = record['offset']
                    video.seek(offset)
                    chunk = video.read(CHUNK_BYTES)
                    if not chunk:
                        raise RuntimeError('업로드 완료 응답을 받지 못했습니다. 재시도로 서버 상태를 확인하세요.')
                    response = checked(http.put(session_url(record['session_url']), content=chunk,
                        headers={**headers, 'Content-Type': 'video/mp4',
                                 'Content-Range': f'bytes {offset}-{offset + len(chunk) - 1}/{total}'}), (200, 201, 308))
                    if response.status_code in {200, 201}:
                        record.update(video_id=response.json()['id'])
                    else:
                        next_offset = acknowledged(response, total)
                        if next_offset <= offset:
                            raise RuntimeError('업로드 진행 위치가 증가하지 않았습니다. 네트워크를 확인하고 재시도하세요.')
                        record['offset'] = next_offset
                    private_json(journal, record)
        video_id = record.get('video_id', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{6,40}', video_id):
            raise RuntimeError('YouTube가 유효한 영상 ID를 반환하지 않았습니다.')
        record.update(url='https://www.youtube.com/watch?v=' + video_id, uploaded_at=time.time(),
                      privacy=self._actual_privacy(video_id, record.get('requested_privacy') or self.config.youtube_privacy))
        private_json(journal, record)
        self._thumbnail(folder, record)
        return {key: record.get(key) for key in ('video_id', 'url', 'privacy', 'thumbnail_status')}

    def _actual_privacy(self, video_id: str, requested: str) -> str:
        """Record what YouTube applied: unaudited API projects may force uploads to private."""
        try:
            with httpx.Client(timeout=30, trust_env=False) as http:
                items = checked(http.get('https://www.googleapis.com/youtube/v3/videos',
                    params={'part': 'status', 'id': video_id},
                    headers={'Authorization': 'Bearer ' + self.access_token()})).json().get('items', [])
            return items[0]['status']['privacyStatus'] if items else requested
        except (httpx.HTTPError, RuntimeError, KeyError, IndexError, ValueError):
            return requested

    def _verify_channel(self, http, headers) -> None:
        """Check the live account before every upload so a personal channel is never used."""
        stored = (read_json(self.root / 'token.json').get('channel') or {}).get('id')
        response = checked(http.get('https://www.googleapis.com/youtube/v3/channels',
            params={'part': 'id', 'mine': 'true'}, headers=headers))
        live = [item.get('id') for item in response.json().get('items', [])]
        expected = self.config.youtube_channel_id or stored
        if not expected or live != [expected] or (stored and stored != expected):
            raise YoutubeAuthError('연결된 YouTube 채널이 지정 채널과 다릅니다. 올바른 채널로 다시 연결하세요.')

    def _thumbnail(self, folder, record):
        if record.get('thumbnail_status') == 'completed':
            return
        thumbnail = folder / 'thumbnail.jpg'
        try:
            if not thumbnail.is_file():
                run(['ffmpeg', '-v', 'error', '-y', '-ss', '0.8', '-i', str(folder / 'output.mp4'),
                     '-frames:v', '1', '-q:v', '3', str(thumbnail)])
            with httpx.Client(timeout=30, trust_env=False) as http:
                checked(http.post('https://www.googleapis.com/upload/youtube/v3/thumbnails/set',
                    params={'videoId': record['video_id'], 'uploadType': 'media'},
                    headers={'Authorization': 'Bearer ' + self.access_token(), 'Content-Type': 'image/jpeg'},
                    content=thumbnail.read_bytes()))
            record['thumbnail_status'] = 'completed'
            record.pop('thumbnail_error', None)
        except (RuntimeError, OSError, ValueError, httpx.HTTPError):
            record.update(thumbnail_status='failed', thumbnail_error='썸네일 설정 실패. 영상은 업로드됐으며 채널의 맞춤 썸네일 사용 권한을 확인하세요.')
        private_json(folder / 'youtube-upload.json', record)


def acknowledged(response: httpx.Response, total: int) -> int:
    match = re.fullmatch(r'bytes=0-(\d+)', response.headers.get('Range', ''))
    offset = int(match[1]) + 1 if match else 0
    if not 0 <= offset <= total:
        raise RuntimeError('Google 업로드 진행 위치가 파일 크기와 일치하지 않습니다.')
    return offset
