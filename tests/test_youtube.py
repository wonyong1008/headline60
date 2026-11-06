import json
import stat
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.config import Settings
from app.integrations import youtube


@pytest.fixture
def service(tmp_path):
    result = youtube.YoutubeService(Settings(root=tmp_path, youtube_channel_id=''))
    result.configure({'installed': {'client_id': 'local-test.apps.googleusercontent.com', 'client_secret': 'unit-test-secret'}})
    youtube.private_json(result.root / 'token.json', {'refresh_token': 'unit-refresh', 'access_token': 'unit-access',
        'expires_at': time.time() + 3600, 'channel': {'id': 'UCexample', 'title': 'Test Channel'}})
    return result


@pytest.fixture
def video(tmp_path):
    folder = tmp_path / 'outputs' / 'example'
    folder.mkdir(parents=True)
    (folder / 'output.mp4').write_bytes(b'0123456789')
    (folder / 'ffprobe.json').write_text('{}')
    (folder / 'thumbnail.jpg').write_bytes(b'fixture-image')
    (folder / 'script.json').write_text(json.dumps({'title': '계획과 실제는 어떻게 다를까?',
        'scenes': [{'narration': '첫 장면의 전체 대본입니다.'}, {'narration': '두 번째 장면도 생략 없이 담습니다.'}],
        'hashtags': ['헤드라인60']}))
    (folder / 'metadata.json').write_text(json.dumps({'sources': [{'publisher': 'Official', 'url': 'https://www.korea.kr/news/example'}]}))
    (folder / 'CREDITS.md').write_text('# 자료 출처\n\nOriginal Author · CC BY 4.0')
    return folder


def mock_client(monkeypatch, responder):
    original = httpx.Client
    monkeypatch.setattr(youtube.httpx, 'Client', lambda **kwargs: original(transport=httpx.MockTransport(responder)))


def test_no_token_or_client_secret_in_status(service):
    text = json.dumps(service.status())
    assert service.status()['connected']
    assert all(value not in text for value in ['unit-refresh', 'unit-access', 'unit-test-secret'])
    assert stat.S_IMODE((service.root / 'token.json').stat().st_mode) == 0o600
    assert stat.S_IMODE(service.root.stat().st_mode) == 0o700


def test_description_keeps_full_narration_and_attribution_private(video):
    data = youtube.video_metadata(video)
    description = data['snippet']['description']
    assert '첫 장면의 전체 대본입니다.' in description and '두 번째 장면도 생략 없이 담습니다.' in description
    assert 'Original Author' in description and 'https://www.korea.kr/news/example' in description
    assert data['status']['privacyStatus'] == 'private'
    assert data['status']['containsSyntheticMedia'] is True


@pytest.mark.parametrize('url', ['http://www.googleapis.com/upload', 'https://evil.example/upload',
    'https://www.googleapis.com.evil.example/upload', 'https://127.0.0.1/upload', 'https://www.googleapis.com:8060/upload'])
def test_upload_session_never_sends_token_to_untrusted_host(url):
    with pytest.raises(ValueError):
        youtube.session_url(url)


def test_resumable_chunks_and_duplicate_retry_do_not_insert_twice(service, video, monkeypatch):
    calls = []
    monkeypatch.setattr(youtube, 'CHUNK_BYTES', 4)
    def respond(request):
        calls.append(request)
        if request.url.path.endswith('/channels'):
            return httpx.Response(200, json={'items': [{'id': 'UCexample'}]})
        if request.url.path.endswith('/thumbnails/set'):
            return httpx.Response(200, json={})
        if request.method == 'POST':
            payload = json.loads(request.content)
            assert payload['status']['privacyStatus'] == 'private'
            return httpx.Response(200, headers={'location': 'https://www.googleapis.com/upload/session?upload_id=fixture'})
        content_range = request.headers['content-range']
        if content_range == 'bytes 0-3/10':
            assert request.content == b'0123'
            return httpx.Response(308, headers={'Range': 'bytes=0-3'})
        if content_range == 'bytes 4-7/10':
            assert request.content == b'4567'
            return httpx.Response(308, headers={'Range': 'bytes=0-7'})
        assert content_range == 'bytes 8-9/10' and request.content == b'89'
        return httpx.Response(201, json={'id': 'video123456'})
    mock_client(monkeypatch, respond)
    result = service.upload(video)
    assert result['video_id'] == 'video123456' and result['thumbnail_status'] == 'completed'
    number = len(calls)
    assert service.upload(video) == result and len(calls) == number
    assert sum(r.method == 'POST' and r.url.path.endswith('/videos') for r in calls) == 1


def test_unknown_final_response_checks_existing_session_before_new_upload(service, video, monkeypatch):
    initialized, attempts = [], []
    def interrupted(request):
        if request.url.path.endswith('/channels'):
            return httpx.Response(200, json={'items': [{'id': 'UCexample'}]})
        if request.method == 'POST':
            initialized.append(True)
            return httpx.Response(200, headers={'location': 'https://www.googleapis.com/upload/session?upload_id=fixture'})
        raise httpx.ReadTimeout('simulated lost completion', request=request)
    mock_client(monkeypatch, interrupted)
    with pytest.raises(httpx.ReadTimeout):
        service.upload(video)
    def recovered(request):
        attempts.append(request)
        if request.url.path.endswith('/channels'):
            return httpx.Response(200, json={'items': [{'id': 'UCexample'}]})
        if request.url.path.endswith('/thumbnails/set'):
            return httpx.Response(200, json={})
        assert request.method == 'PUT' and request.headers['Content-Range'] == 'bytes */10'
        return httpx.Response(201, json={'id': 'existing1234'})
    # Use a captured real class, since mock_client was already installed.
    original = httpx.Client
    monkeypatch.undo()
    mock_client(monkeypatch, recovered)
    result = service.upload(video)
    assert result['video_id'] == 'existing1234' and len(initialized) == 1
    assert not any(r.url.path.endswith('/videos') for r in attempts)
    assert original is not None


def test_expired_google_grant_becomes_reconnect_required(service, monkeypatch):
    token = youtube.read_json(service.root / 'token.json')
    token['expires_at'] = 0
    youtube.private_json(service.root / 'token.json', token)
    mock_client(monkeypatch, lambda request: httpx.Response(400, json={'error': 'invalid_grant'}))
    with pytest.raises(youtube.YoutubeAuthError, match='다시 연결'):
        service.access_token()
    assert service.status()['connected'] is False


def test_oauth_uses_loopback_state_pkce_and_only_requested_scopes(tmp_path):
    service = youtube.YoutubeService(Settings(root=tmp_path))
    service.configure({'installed': {'client_id': 'local-test.apps.googleusercontent.com', 'client_secret': 'unit-test-secret'}})
    url = service.begin()['authorization_url']
    query = parse_qs(urlsplit(url).query)
    assert urlsplit(url).hostname == 'accounts.google.com'
    assert urlsplit(query['redirect_uri'][0]).hostname == '127.0.0.1'
    assert query['code_challenge_method'] == ['S256'] and query['state'][0]
    assert set(query['scope'][0].split()) == set(youtube.SCOPES)
    assert 'unit-test-secret' not in url
    service.pending['expires'] = 0


@pytest.mark.parametrize('live, pinned', [(['UCpersonal'], ''), (['UCexample'], 'UCother'), ([], '')])
def test_upload_refuses_any_channel_other_than_the_connected_or_pinned_one(tmp_path, video, monkeypatch, live, pinned):
    service = youtube.YoutubeService(Settings(root=tmp_path, youtube_channel_id=pinned))
    service.configure({'installed': {'client_id': 'local-test.apps.googleusercontent.com', 'client_secret': 'unit-test-secret'}})
    youtube.private_json(service.root / 'token.json', {'refresh_token': 'r', 'access_token': 'a',
        'expires_at': time.time() + 3600, 'channel': {'id': 'UCexample', 'title': 'Test Channel'}})
    sent = []
    def respond(request):
        sent.append(request)
        return httpx.Response(200, json={'items': [{'id': item} for item in live]})
    mock_client(monkeypatch, respond)
    with pytest.raises(youtube.YoutubeAuthError):
        service.upload(video)
    assert all(request.url.path.endswith('/channels') for request in sent)


def test_concurrent_upload_calls_for_one_job_create_one_video(service, video, monkeypatch):
    import threading
    inserted = []
    def respond(request):
        if request.url.path.endswith('/channels'):
            return httpx.Response(200, json={'items': [{'id': 'UCexample'}]})
        if request.url.path.endswith('/thumbnails/set'):
            return httpx.Response(200, json={})
        if request.method == 'POST':
            inserted.append(1)
            time.sleep(0.2)
            return httpx.Response(200, headers={'location': 'https://www.googleapis.com/upload/session?upload_id=one'})
        return httpx.Response(201, json={'id': 'single12345'})
    mock_client(monkeypatch, respond)
    results = []
    threads = [threading.Thread(target=lambda: results.append(service.upload(video)['video_id'])) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results == ['single12345', 'single12345'] and len(inserted) == 1
