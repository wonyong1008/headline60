"""Content safety/review gates; no model inference or publishing in these tests."""
import asyncio
from datetime import date

import httpx
import pytest
from pydantic import ValidationError

from app import main, studio_store
from app.config import Settings
from app.content_modes import mode_metadata, validation_issues
from app.jobs.manager import Manager
from app.script.models import Scene, Script
from app.studio_models import DraftCreate


def fiction_script(topic="주말 계획과 월요일의 차이"):
    scenes = [Scene(headline="창작 일상 풍자", narration="주말 계획과 현실의 차이를 가볍게 풀어봅니다.",
                    duration_seconds=length, kind="analysis") for length in (3, 9, 9, 9)]
    return Script(title=topic, description="실제 사건이 아닌 허구의 성인 일상 코미디입니다.",
                  hook=scenes[0].narration, scenes=scenes)


def make_draft(config, *, mode="adult_satire", topic="주말 계획과 현실"):
    return studio_store.create(config, {"content_mode": mode, "topic": topic, "fiction_acknowledged": True,
        "articles": [], "article_ids": [], "category": "trending", "seconds": 30, "resolution": 720,
        "voice": "Yuna", "voice_profile_id": None, "style": "일상 공감", "template": "story_explainer",
        "edit_style": "reels_v3", "narration_speed": 1.18, "script": fiction_script().model_dump(), "evidence": []})


def test_news_requires_real_article_selection():
    with pytest.raises(ValidationError, match="기사"):
        DraftCreate(content_mode="news", topic="주제만 쓴 뉴스")


def test_fiction_needs_explicit_fiction_acknowledgment():
    with pytest.raises(ValidationError, match="허구"):
        DraftCreate(content_mode="adult_satire", topic="직장인의 월요일")
    assert DraftCreate(content_mode="adult_satire", topic="직장인의 월요일", fiction_acknowledged=True).article_ids == []


def test_myth_without_reviewed_evidence_is_blocked():
    value = {"content_mode": "myth_check", "topic": "근거가 필요한 건강 속설", "category": "health", "evidence": [], "seconds": 30}
    assert any("근거" in error for error in validation_issues(value))


def test_fiction_has_permanent_clear_metadata_and_no_evidence_requirement(tmp_path):
    draft = make_draft(Settings(root=tmp_path))
    assert validation_issues(draft, fiction_script()) == []
    metadata = mode_metadata(draft)
    assert metadata["is_fiction"] and metadata["fiction_label"] == "허구 · 성인 공감 풍자"
    assert metadata["review_requirements"]["content_reviewed"]


def test_fiction_rejects_fake_fact_chart_and_medical_certainty(tmp_path):
    draft = make_draft(Settings(root=tmp_path))
    script = fiction_script()
    script.scenes[1].claim_ids = ["aabbccddeeff"]
    assert any("근거" in issue for issue in validation_issues(draft, script))
    script.scenes[1].claim_ids = []
    script.scenes[1].narration = "이 약을 먹으면 완치된다고 말합니다."
    assert any("의학" in issue for issue in validation_issues(draft, script))


def test_content_and_sexual_review_are_enforced_on_server(tmp_path):
    config = Settings(root=tmp_path)
    manager = Manager(config)
    manager.pool.shutdown()
    draft = make_draft(config, topic="성관계 관련 허구의 대화")
    with pytest.raises(ValueError, match="수동 검토"):
        manager.approve(draft["id"], 1)
    with pytest.raises(ValueError, match="성·연애"):
        manager.approve(draft["id"], 1, content_reviewed=True)
    approved = manager.approve(draft["id"], 1, content_reviewed=True, sexual_reviewed=True)
    assert approved["content_review"]["revision"] == 1
    assert approved["content_review"]["sexual_reviewed"] is True


def test_stale_review_is_rejected_even_if_old_approval_integer_was_copied(tmp_path):
    config = Settings(root=tmp_path)
    manager = Manager(config)
    manager.pool.shutdown()
    draft = make_draft(config)
    manager.approve(draft["id"], 1, content_reviewed=True)
    current = studio_store.get(config, draft["id"])
    current.update(revision=2, approved_revision=2)
    studio_store.save(config, current, 1)
    with pytest.raises(ValueError, match="수동 검토"):
        manager.render(draft["id"], 2)


def test_medical_review_is_required_for_myth_even_after_evidence_review(tmp_path):
    draft = make_draft(Settings(root=tmp_path), mode="myth_check", topic="공식 근거 범위 확인")
    now = str(date.today())
    draft["evidence"] = [{"id": "aabbccddeeff", "url": "https://www.mfds.go.kr/",
        "publisher": "식품의약품안전처", "title": "공식 자료 확인", "published_date": now,
        "accessed_date": now, "excerpt": "공식 자료에서 확인할 수 있는 범위를 함께 검토합니다.",
        "fact": "확인한 자료와 주장 범위를 구분해서 검토합니다.", "conditions": "일반적인 자료 확인 절차입니다.", "reviewed": True}]
    script = fiction_script()
    for scene in script.scenes:
        scene.claim_ids = ["aabbccddeeff"]
    draft["content_review"] = {"revision": 1, "content_reviewed": True}
    assert any("의학" in issue for issue in validation_issues(draft, script, require_review=True))
    draft["content_review"]["medical_reviewed"] = True
    assert validation_issues(draft, script, require_review=True) == []


def test_api_creation_and_edit_reset_review_without_article_crash(tmp_path, monkeypatch):
    config = Settings(root=tmp_path)
    service = Manager(config)
    service.pool.shutdown()
    monkeypatch.setattr(main, "settings", config)
    monkeypatch.setattr(main, "manager", service)
    async def flow():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://testserver") as client:
            headers = {"X-Headline-Token": main.csrf_token}
            response = await client.post('/api/drafts', headers=headers, json={"content_mode":"adult_satire", "topic":"직장인 주말 계획", "fiction_acknowledged":True, "category":"trending"})
            assert response.status_code == 200
            draft = response.json()
            assert draft["is_fiction"] and draft["articles"] == []
            value = fiction_script()
            edit = {"revision":1, "script":value.model_dump(), "evidence":[], "content_mode":"adult_satire", "topic":draft["topic"], "fiction_acknowledged":True, "template":"story_explainer"}
            draft = (await client.put('/api/drafts/'+draft["id"],headers=headers,json=edit)).json()
            denied = await client.post(f'/api/drafts/{draft["id"]}/approve',headers=headers,json={"revision":draft["revision"]})
            assert denied.status_code == 400
            approved = await client.post(f'/api/drafts/{draft["id"]}/approve',headers=headers,json={"revision":draft["revision"],"content_reviewed":True})
            assert approved.status_code == 200
            edit.update(revision=draft["revision"], script={**value.model_dump(),"title":"월요일의 창작 반전"})
            updated = (await client.put('/api/drafts/'+draft["id"],headers=headers,json=edit)).json()
            assert updated["approved_revision"] is None and updated["content_review"] is None
            assert (await client.post(f'/api/drafts/{draft["id"]}/render',headers=headers,json={"revision":updated["revision"]})).status_code == 400
    asyncio.run(flow())


def test_fiction_cannot_use_an_old_renderer_without_its_disclosure(tmp_path):
    draft = make_draft(Settings(root=tmp_path))
    draft["template"] = "legacy"
    assert any("릴스 템플릿" in issue for issue in validation_issues(draft, fiction_script()))


def test_core_job_cannot_drop_a_fiction_mode_to_remove_its_disclosure(tmp_path):
    from app.jobs.pipeline import prepare_news
    from app.script.models import Script, VideoRequest
    config = Settings(root=tmp_path)
    service = Manager(config)
    service.pool.shutdown()
    draft = make_draft(config)
    draft = service.approve(draft['id'], 1, content_reviewed=True)
    request = VideoRequest(topic='모드 일치 검증', template='story_explainer', content_mode='news')
    with pytest.raises(ValueError, match='콘텐츠 모드'):
        prepare_news(config, request, Script.model_validate(draft['script']), draft)
