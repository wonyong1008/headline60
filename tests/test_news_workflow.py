import asyncio
import json
import sys
import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app import main, studio_store
from app.config import Settings
from app.jobs import manager as manager_module
from app.jobs import pipeline, store
from app.jobs.manager import Manager
from app.news.evidence import official_url
from app.news.feeds import NewsError, article_id, group_articles, parse_rss, public_url
from app.process import JobCancelled, cancellation, run
from app.script.models import Scene, Script
from app.studio_models import Evidence, evidence_ready, script_issues

NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)


def rss_item(title='항만의 인공지능 전환 계획', url='https://example.org/news?a=1', publisher='뉴스A', age=1):
    date = (NOW - timedelta(hours=age)).strftime('%a, %d %b %Y %H:%M:%S GMT')
    return f'<item><title>{title} - {publisher}</title><link>{url}</link><source>{publisher}</source><pubDate>{date}</pubDate></item>'


def article():
    return {'id': article_id('https://example.org/news'), 'url': 'https://example.org/news',
            'title': '항만 인공지능 계획 발표', 'publisher': '뉴스A', 'published_at': NOW.isoformat(),
            'category': 'tech', 'origin': 'google_rss', 'verification': 'metadata_only'}


def evidence():
    return Evidence(id='aabbccddeeff', url='https://www.korea.kr/news/policyNewsView.do?newsId=1',
                    publisher='정책브리핑', title='해양수산 인공지능 전환 발표',
                    published_date='2026-10-28', accessed_date='2026-10-28',
                    fact='해양수산 분야에서 인공지능 전환 전략을 추진한다.',
                    excerpt='해양수산 전반의 경쟁력을 높이는 전략을 마련했다.',
                    conditions='발표된 계획이며 성과가 아니다.', reviewed=True)


def script():
    scenes = [Scene(headline='해양수산의 인공지능', narration='해양수산 분야의 인공지능 전환 계획을 살펴봅니다.',
                    claim_ids=['aabbccddeeff'], duration_seconds=3 if i == 0 else 5.4,
                    explanation='계획과 결과를 구분합니다.', visual_type='card') for i in range(6)]
    return Script(title='해양수산 인공지능 전환', description='공식 자료를 바탕으로 계획과 성과를 구분합니다.',
                  hook=scenes[0].narration, scenes=scenes, hashtags=['해양수산'])


def test_rss_dedup_date_range_and_publisher():
    body = '<rss><channel>' + rss_item() * 2 + rss_item(url='https://example.org/old', age=170) + '</channel></rss>'
    result = parse_rss(body.encode(), 'tech', 7, NOW)
    assert len(result) == 1
    assert result[0]['title'] == '항만의 인공지능 전환 계획'
    assert result[0]['verification'] == 'metadata_only'


def test_invalid_and_unsafe_xml_rejected():
    for data in [b'not xml', b'<!DOCTYPE x><rss/>', b'x' * 2_000_001]:
        with pytest.raises(NewsError):
            parse_rss(data, 'tech', 1, NOW)


@pytest.mark.parametrize('url', ['http://127.0.0.1/x', 'http://[::1]/', 'http://localhost/',
                                 'file:///etc/passwd', 'https://x:y@example.org/', 'https://example.org:8060/'] )
def test_internal_or_credential_urls_rejected(url):
    with pytest.raises(ValueError):
        public_url(url)


def test_canonical_url_removes_trackers():
    assert public_url('https://example.org/a?x=1&utm_source=rss#anchor') == 'https://example.org/a?x=1'
    assert not official_url('https://www.korea.kr.evil.example/a')
    assert official_url('https://www.korea.kr/news/a')


def test_related_articles_grouped_with_transparent_score():
    a = article()
    b = {**a, 'id': 'different', 'publisher': '뉴스B', 'url': 'https://example.net/a'}
    groups = group_articles([a, b], NOW)
    assert len(groups) == 1
    assert groups[0]['article_count'] == 2 and groups[0]['publisher_count'] == 2
    assert 0 <= groups[0]['score'] <= 100


def test_evidence_and_numeric_claim_gate():
    source = evidence()
    assert evidence_ready([], 'tech')
    assert evidence_ready([source.model_copy(update={'reviewed': False})], 'tech')
    assert evidence_ready([source.model_copy(update={'url': 'https://example.org/a', 'permission': 'allowed'})], 'health')
    assert not script_issues(script(), [source], 30)
    altered = script()
    altered.scenes[1].narration = '항만 생산성이 99퍼센트 향상됐다고 발표했습니다.'
    assert any('99' in x for x in script_issues(altered, [source], 30))
    altered.scenes[1].claim_ids = ['not-real']
    assert any('유효한 근거' in x for x in script_issues(altered, [source], 30))


def test_old_sqlite_schema_migrates_and_preserves_data(tmp_path):
    import sqlite3
    config = Settings(root=tmp_path)
    config.data.mkdir()
    with sqlite3.connect(config.data / 'jobs.sqlite3') as db:
        db.execute('CREATE TABLE jobs (id TEXT PRIMARY KEY, topic TEXT, status TEXT, stage TEXT, error TEXT, updated_at TEXT)')
        db.execute("INSERT INTO jobs VALUES ('old', '이전 작업', 'completed', 'verify', NULL, '2026-10-28')")
    assert store.recent(config)[0]['topic'] == '이전 작업'
    store.update(config, 'old', '이전 작업', 'completed', 'verify', details={'category': '경제'})
    assert store.get(config, 'old')['details']['category'] == '경제'


def test_cancellation_kills_running_child():
    event = threading.Event()
    timer = threading.Timer(0.15, event.set)
    timer.start()
    try:
        with cancellation(event), pytest.raises(JobCancelled):
            run([sys.executable, '-c', 'import time; time.sleep(20)'])
    finally:
        timer.cancel()


class ImmediatePool:
    def submit(self, fn, *args):
        fn(*args)


def test_browser_api_flow_with_mock_inference_and_media(tmp_path, monkeypatch):
    config = Settings(root=tmp_path)
    service = Manager(config)
    service.pool.shutdown(wait=True)
    service.pool = ImmediatePool()
    monkeypatch.setattr(main, 'settings', config)
    monkeypatch.setattr(main, 'manager', service)
    monkeypatch.setattr(main, 'collect', lambda *args: [article()])
    monkeypatch.setattr(manager_module, 'generate_news', lambda *args: (script(), {'response': 'mock'}))
    monkeypatch.setattr(manager_module, 'korean_voices', lambda: ['Yuna'])
    monkeypatch.setattr(pipeline, 'synthesize', lambda *args: [3, 5, 5, 5, 5, 5])
    monkeypatch.setattr(pipeline, 'create_cards', lambda *args: None)

    def render(folder, *args):
        (folder / 'output.mp4').write_bytes(b'mocked media')
    monkeypatch.setattr(pipeline, 'render', render)
    monkeypatch.setattr(pipeline, 'verify', lambda *args: {'format': {'duration': '30.0'}})

    async def flow():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://testserver') as client:
            headers = {'X-Headline-Token': main.csrf_token}
            assert (await client.post('/api/news/collect', json={})).status_code == 403
            rejected = await client.post('/api/news/collect', headers={**headers, 'Origin': 'https://evil.example'}, json={})
            assert rejected.status_code == 403
            result = await client.post('/api/news/collect', headers=headers, json={'category': 'tech'})
            assert result.status_code == 200 and result.json()['collected'] == 1
            response = await client.post('/api/drafts', headers=headers, json={'article_ids': [article()['id']], 'category': 'tech'})
            draft = response.json()
            key = draft['id']
            gate = await client.post(f'/api/drafts/{key}/generate', headers=headers, json={'revision': 1})
            assert gate.status_code == 400  # RSS title is insufficient evidence.
            edit = {'revision': 1, 'script': None, 'evidence': [evidence().model_dump(mode='json')],
                    'seconds': 30, 'resolution': 720, 'voice': 'Yuna'}
            response = await client.put(f'/api/drafts/{key}', headers=headers, json=edit)
            draft = response.json()
            await client.post(f'/api/drafts/{key}/generate', headers=headers, json={'revision': draft['revision']})
            draft = (await client.get(f'/api/drafts/{key}')).json()
            assert draft['status'] == 'review'
            draft['script']['title'] = '사용자가 직접 수정한 뉴스 제목'
            edit.update(revision=draft['revision'], script=draft['script'])
            draft = (await client.put(f'/api/drafts/{key}', headers=headers, json=edit)).json()
            revision = draft['revision']
            assert (await client.post(f'/api/drafts/{key}/render', headers=headers, json={'revision': revision})).status_code == 400
            assert (await client.post(f'/api/drafts/{key}/approve', headers=headers, json={'revision': revision})).status_code == 200
            response = await client.post(f'/api/drafts/{key}/render', headers=headers, json={'revision': revision})
            job = response.json()
            assert job['status'] == 'completed'
            folder = pipeline.job_folder(config, job['id'])
            assert json.loads((folder / 'script.json').read_text())['title'] == '사용자가 직접 수정한 뉴스 제목'
            metadata = json.loads((folder / 'metadata.json').read_text())
            assert metadata['sources'][0]['id'] == evidence().id
            assert metadata['review_status'] == 'human_reviewed'
            again = (await client.post(f'/api/drafts/{key}/render', headers=headers, json={'revision': revision})).json()
            assert again['id'] == job['id']  # Prevent accidental duplicate videos.
            edit.update(revision=revision, script={**draft['script'], 'title': '다시 수정한 뉴스 제목'})
            updated = (await client.put(f'/api/drafts/{key}', headers=headers, json=edit)).json()
            assert updated['approved_revision'] is None
            assert (await client.put(f'/api/drafts/{key}', headers=headers, json=edit)).status_code == 400
    asyncio.run(flow())


def test_rss_headline_numbers_do_not_enter_llm_prompt(tmp_path, monkeypatch):
    from app.llm import news_writer
    requests = []
    original_client = httpx.Client

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'done': True, 'done_reason': 'stop',
                                       'response': script().model_dump_json()})

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(news_writer.httpx, 'Client', lambda **kwargs: original_client(transport=transport))
    draft = {'category': 'tech', 'seconds': 30,
             'articles': [{**article(), 'title': '미확인 수치 2035년 99퍼센트'}],
             'evidence': [evidence().model_dump(mode='json')]}
    generated, _ = news_writer.generate_news(draft, Settings(root=tmp_path))
    assert '2035' not in requests[0]['prompt']
    assert '99퍼센트' not in requests[0]['prompt']
    assert requests[0]['keep_alive'] == 0
    assert generated.scenes[0].duration_seconds == 3


def test_only_changed_narration_regenerates_tts(tmp_path, monkeypatch):
    from app.tts import macos
    config = Settings(root=tmp_path)
    calls = []
    monkeypatch.setattr(macos, 'korean_voices', lambda: ['Yuna'])
    monkeypatch.setattr(macos, 'duration', lambda path: 3.0)

    def synth(args, **kwargs):
        calls.append(args)
        from pathlib import Path
        Path(args[args.index('-o') + 1]).write_bytes(b'mock audio')
        return ''

    monkeypatch.setattr(macos, 'run', synth)
    base = script()
    # Different text in each scene makes reuse count unambiguous.
    for i, scene in enumerate(base.scenes):
        scene.narration = f'서로 다른 장면 번호 {i}의 한국어 내레이션입니다.'
    first = tmp_path / 'first'
    first.mkdir()
    macos.synthesize(base, first, config)
    assert len(calls) == 6
    changed = base.model_copy(deep=True)
    changed.scenes[2].narration = '이 장면의 내레이션만 새롭게 수정했습니다.'
    second = tmp_path / 'second'
    second.mkdir()
    macos.synthesize(changed, second, config)
    assert len(calls) == 7
    assert json.loads((second / 'tts-cache.json').read_text()) == {'reused': 5, 'total': 6}


def test_manager_resume_and_cancel_keep_draft_state(tmp_path, monkeypatch):
    config = Settings(root=tmp_path)
    service = Manager(config)
    service.pool.shutdown(wait=True)
    submitted = []

    class QueueOnly:
        def submit(self, fn, *args):
            submitted.append((fn, args))

    service.pool = QueueOnly()
    d = studio_store.create(config, {'articles': [article()], 'category': 'tech', 'evidence': [evidence().model_dump(mode='json')],
        'seconds': 30, 'resolution': 720, 'voice': 'Yuna', 'style': '뉴스 해설', 'script': script().model_dump()})
    d.update(approved_revision=1, status='approved')
    studio_store.save(config, d, 1)
    job_id = pipeline.prepare_news(config, manager_module.VideoRequest(topic='뉴스 해설'), script(), d)
    job = store.get(config, job_id)
    store.update(config, job_id, job['topic'], 'cancelled', 'render', '사용자 취소', job['details'])
    service.resume(job_id)
    assert studio_store.get(config, d['id'])['status'] == 'rendering'
    assert submitted

    def cancelled(*args, **kwargs):
        current = store.get(config, job_id)
        store.update(config, job_id, current['topic'], 'cancelled', 'render', '사용자 취소', current['details'])
        raise JobCancelled('사용자 취소')

    monkeypatch.setattr(manager_module, 'execute', cancelled)
    fn, args = submitted[0]
    fn(*args)
    assert studio_store.get(config, d['id'])['status'] == 'cancelled'
