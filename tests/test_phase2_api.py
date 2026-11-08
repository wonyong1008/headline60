"""Phase 2 API regression tests: local fixtures, no model loading or network calls."""
import asyncio
import json
from datetime import date

import httpx
import pytest

from app import main, studio_store
from app.config import Settings
from app.jobs.manager import Manager
from app.jobs.pipeline import job_folder
from app.script.models import Scene, Script
from app.studio_models import Evidence
from app.tts import engine, profiles
from app.tts.macos import TTSError


class QueueOnly:
    def __init__(self):
        self.submitted = []

    def submit(self, fn, *args):
        self.submitted.append((fn, args))


@pytest.fixture
def local_app(tmp_path, monkeypatch):
    config = Settings(root=tmp_path)
    service = Manager(config)
    service.pool.shutdown(wait=True)
    service.pool = QueueOnly()
    monkeypatch.setattr(main, 'settings', config)
    monkeypatch.setattr(main, 'manager', service)
    youtube = main.YoutubeService(config)
    monkeypatch.setattr(main, 'youtube', youtube)
    monkeypatch.setattr(main, 'submissions', main.SubmissionQueue(config, service, youtube))
    return config, service


def make_script():
    texts = [
        '확인한 자료를 차분하게 전해 드립니다.',
        '발표된 내용과 앞으로의 계획을 구분합니다.',
        '주요 내용을 원문 자료와 함께 살펴봅니다.',
        '숫자뿐 아니라 기준과 조건도 확인합니다.',
        '확정된 성과와 계획을 혼동하지 않습니다.',
        '추가로 확인할 점을 끝까지 살펴봅니다.',
    ]
    scenes = [Scene(headline='자료 확인', narration=text, duration_seconds=3 if i == 0 else 5.4,
                    claim_ids=['aabbccddeeff'], tts_text='확인한 자료를 정확하게 읽겠습니다.' if i == 0 else None)
              for i, text in enumerate(texts)]
    return Script(title='전체 뉴스 대본 확인', description='공식 자료의 기준과 계획을 직접 확인합니다.',
                  hook=texts[0], scenes=scenes, hashtags=['뉴스해설'])


def completed_fixture(config):
    script = make_script()
    today = date.today().isoformat()
    evidence = Evidence(id='aabbccddeeff', url='https://www.korea.kr/news/policyNewsView.do?newsId=1',
                        publisher='정책브리핑', title='공식 발표 자료', published_date=today,
                        accessed_date=today, fact='발표된 내용과 실제 성과를 구분해서 확인해야 합니다.',
                        excerpt='원문 자료에서 발표한 내용과 조건을 함께 확인합니다.',
                        conditions='실제 성과로 단정하지 않습니다.', reviewed=True)
    draft = studio_store.create(config, {'articles': [], 'category': 'tech', 'seconds': 30,
        'resolution': 720, 'voice': 'Yuna', 'voice_profile_id': 'reels-male-ryan', 'style': '뉴스 해설',
        'script': script.model_dump(), 'evidence': [evidence.model_dump(mode='json')]})
    draft.update(status='completed', approved_revision=1)
    studio_store.save(config, draft, 1)
    job_id = '20261028T230000-aabbccdd'
    folder = job_folder(config, job_id)
    folder.mkdir(parents=True)
    (folder / 'script.json').write_text(script.model_dump_json())
    (folder / 'job.json').write_text(json.dumps({'settings': {'voice': 'Sohee'},
        'request': {'seconds': 30}, 'draft_id': draft['id'], 'revision': 1}))
    (folder / 'timing.json').write_text(json.dumps({'frames': [90, 162, 162, 162, 162, 162]}))
    (folder / 'captions.json').write_text(json.dumps({'cues': [{'scene': 0, 'text': script.scenes[0].narration,
        'start': 0, 'end': 3, 'scene_start': 0, 'highlight': []}]}))
    return draft, job_id, folder, script


def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://testserver')


def headers():
    return {'X-Headline-Token': main.csrf_token}


def test_unavailable_neural_profile_does_not_fall_back(local_app):
    _, service = local_app

    async def check():
        async with client() as session:
            listed = (await session.get('/api/voice-profiles')).json()
            assert [p['voice_profile_id'] for p in listed] == ['reels-male-ryan']  # the only voice offered
            assert not listed[0]['available']  # no MLX runtime under the temporary root
            response = await session.post('/api/voice-samples', headers=headers(), json={
                'voice_profile_id': 'reels-male-ryan', 'text': '정확하고 차분하게 뉴스를 전해 드립니다.'})
            assert response.status_code == 400
            assert '직접 선택' in response.json()['detail']
            assert not service.pool.submitted
    asyncio.run(check())


def test_failed_neural_sample_keeps_selected_voice(local_app, monkeypatch):
    config, service = local_app
    selected = profiles.get(config, 'reels-male-ryan', require_available=False)
    monkeypatch.setattr(profiles, 'get', lambda *args, **kwargs: selected)
    calls = []

    def fail(document, folder, current_config, profile):
        calls.append(profile['engine'])
        raise TTSError('선택한 MLX 엔진 오류')

    monkeypatch.setattr(engine, 'synthesize', fail)

    async def check():
        async with client() as session:
            response = await session.post('/api/voice-samples', headers=headers(), json={
                'voice_profile_id': 'reels-male-ryan', 'text': '정확하고 차분하게 뉴스를 전해 드립니다.'})
            assert response.status_code == 200
            key = response.json()['id']
            assert len(service.pool.submitted) == 1
            fn, args = service.pool.submitted[0]
            fn(*args)
            record = (await session.get(f'/api/voice-samples/{key}')).json()
            assert record['status'] == 'failed'
            assert record['profile']['engine'] == 'qwen3_mlx'
            assert record['profile']['speaker'] == 'ryan'
            assert calls == ['qwen3_mlx']
            assert key not in service.events
            assert (await session.get(f'/api/voice-samples/{key}/audio')).status_code == 404
    asyncio.run(check())


def test_startup_recovers_only_unfinished_samples(local_app):
    config, _ = local_app
    for status in ['queued', 'running', 'completed', 'failed']:
        profiles.sample_record(config, status, {'id': status, 'status': status, 'error': None})

    async def check():
        async with main.lifespan(main.app), client() as session:
            for key in ['queued', 'running']:
                record = (await session.get(f'/api/voice-samples/{key}')).json()
                assert record['status'] == 'failed'
                assert '서버 재시작' in record['error']
            assert (await session.get('/api/voice-samples/completed')).json()['status'] == 'completed'
            assert (await session.get('/api/voice-samples/failed')).json()['error'] is None
    asyncio.run(check())


def test_whole_script_view_and_text_preserve_original_narration(local_app):
    config, _ = local_app
    _, job_id, _, script = completed_fixture(config)

    async def check():
        async with client() as session:
            response = await session.get(f'/jobs/{job_id}/script')
            assert response.status_code == 200
            assert '전체 내레이션' in response.text and '전체 대본 복사' in response.text
            for scene in script.scenes:
                assert scene.narration in response.text
            text = await session.get(f'/jobs/{job_id}/narration.txt')
            assert text.status_code == 200
            assert text.text == script.title + '\n\n' + '\n\n'.join(s.narration for s in script.scenes)
            assert script.scenes[0].tts_text not in text.text
            assert 'attachment' in text.headers['content-disposition']
            raw = await session.get(f'/jobs/{job_id}/script.json')
            assert raw.json()['scenes'][0]['narration'] == script.scenes[0].narration
            assert (await session.get('/jobs/not-an-id/script')).status_code == 404
    asyncio.run(check())


@pytest.mark.parametrize('cues, expected', [
    ([{'scene': 0, 'text': '첫 번째 구절입니다.', 'start': .1, 'end': 2, 'highlight': []},
      {'scene': 0, 'text': '다음 구절입니다.', 'start': 1, 'end': 2.8, 'highlight': []}], '겹치'),
    ([{'scene': 0, 'text': '가' * 70, 'start': 0, 'end': 3, 'highlight': []}], '두 줄'),
    ([{'scene': 0, 'text': '종료가 빠른 구절입니다.', 'start': 2, 'end': 1, 'highlight': []}], '종료'),
    ([{'scene': 0, 'text': '범위를 벗어난 구절입니다.', 'start': 0, 'end': 4, 'highlight': []}], '안에서'),
])
def test_bad_caption_edit_does_not_advance_revision(local_app, cues, expected):
    config, _ = local_app
    draft, job_id, folder, _ = completed_fixture(config)
    original = (folder / 'script.json').read_bytes()

    async def check():
        async with client() as session:
            response = await session.post(f'/api/jobs/{job_id}/captions', headers=headers(), json={'cues': cues})
            assert response.status_code == 400
            assert expected in response.json()['detail']
            unchanged = studio_store.get(config, draft['id'])
            assert unchanged['revision'] == 1 and unchanged['approved_revision'] == 1
            assert not (config.data / 'drafts' / draft['id'] / 'revision-0001.json').exists()
            assert (folder / 'script.json').read_bytes() == original
    asyncio.run(check())


def test_valid_caption_edit_creates_unapproved_revision_and_keeps_finished_files(local_app):
    config, _ = local_app
    draft, job_id, folder, _ = completed_fixture(config)
    original = (folder / 'script.json').read_bytes()

    async def check():
        async with client() as session:
            response = await session.post(f'/api/jobs/{job_id}/captions', headers=headers(), json={'cues': [
                {'scene': 0, 'text': '확인한 자료를 전합니다.', 'start': .1, 'end': 2.9, 'highlight': ['자료']}]})
            assert response.status_code == 200
            assert response.json()['revision'] == 2
            updated = studio_store.get(config, draft['id'])
            assert updated['approved_revision'] is None
            assert updated['script']['scenes'][0]['caption_overrides'][0]['start'] == .1
            assert updated['script']['scenes'][0]['caption_overrides'][0]['highlight'] == ['자료']
            assert (folder / 'script.json').read_bytes() == original
            conflict = await session.post(f'/api/jobs/{job_id}/captions', headers=headers(), json={'cues': [
                {'scene': 0, 'text': '다시 수정한 구절입니다.', 'start': 0, 'end': 3, 'highlight': []}]})
            assert conflict.status_code == 400
            assert studio_store.get(config, draft['id'])['revision'] == 2
    asyncio.run(check())


def test_app_lifespan_in_tests_never_touches_the_real_project_queue(local_app):
    from app.config import ROOT
    assert main.submissions.config.root != ROOT and main.youtube.root.is_relative_to(main.settings.root)
