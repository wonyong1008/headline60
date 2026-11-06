"""Durable inbox lifecycle without fetching news, model inference, media, or publishing."""
import json
import threading
from datetime import date

import pytest

from app import studio_store
from app.config import Settings
from app.integrations.youtube import YoutubeAuthError
from app.jobs import manager as manager_module
from app.jobs import pipeline, store
from app.jobs.manager import Manager
from app.jobs.submissions import SubmissionQueue
from app.process import save_json
from app.script.models import Scene, Script, VideoRequest


def source():
    fact = "공식 자료에서 발표한 계획과 실제 성과를 구분합니다."
    return {"id": "aabbccddeeff", "url": "https://www.korea.kr/news/a", "publisher": "정책브리핑",
            "title": "공식 계획 발표", "published_date": str(date.today()), "accessed_date": str(date.today()),
            "excerpt": fact, "fact": fact, "conditions": "발표된 계획입니다.", "reviewed": False,
            "verification": "source_matched", "source_sha256": "a" * 64}


def script():
    scenes = [Scene(headline="계획과 성과", narration="발표한 계획과 실제 성과를 구분해서 살펴봅니다.",
                    claim_ids=["aabbccddeeff"], duration_seconds=length) for length in (3, 9, 9, 9)]
    return Script(title="계획과 성과의 구분", description="공식 자료의 계획과 성과를 구분합니다.",
                  hook=scenes[0].narration, scenes=scenes)


class WaitingPool:
    def __init__(self):
        self.pending = []

    def submit(self, fn, *args):
        self.pending.append((fn, args))

    def drain(self):
        while self.pending:
            fn, args = self.pending.pop(0)
            fn(*args)


class Youtube:
    def __init__(self, connected=True):
        self.connected = connected
        self.calls = []
        self.error = None

    def status(self):
        return {"connected": self.connected}

    def upload(self, folder):
        self.calls.append(folder.name)
        if self.error:
            raise self.error
        return {"video_id": folder.name, "url": "https://youtu.be/" + folder.name, "privacy": "private"}


@pytest.fixture
def system(tmp_path, monkeypatch):
    config = Settings(root=tmp_path)
    service = Manager(config)
    service.pool.shutdown()
    service.pool = WaitingPool()
    youtube = Youtube()
    queue = SubmissionQueue(config, service, youtube)
    renders = []

    def prepare(draft):
        draft.update(status="review", category="tech", topic="공식 계획", evidence=[source()], script=script().model_dump())
        studio_store.save(config, draft, draft["revision"])

    def execute(config, resume):
        renders.append(resume)
        folder = pipeline.job_folder(config, resume)
        manifest = json.loads((folder / "job.json").read_text())
        manifest.update(status="completed", stage="verify")
        save_json(folder / "job.json", manifest)
        (folder / "output.mp4").write_bytes(b"mock verified video")
        job = store.get(config, resume)
        store.update(config, resume, job["topic"], "completed", "verify", details=job["details"])
        return folder

    monkeypatch.setattr(service, "_prepare", prepare)
    monkeypatch.setattr(manager_module, "execute", execute)
    return config, service, youtube, queue, renders


def test_persistent_fifo_and_one_background_worker(system):
    config, manager, youtube, queue, renders = system
    first = queue.create("https://example.org/first")
    second = queue.create("https://example.org/second")
    assert len(manager.pool.pending) == 1
    assert [row["sequence"] for row in queue.listing()] == [1, 2]
    assert SubmissionQueue(config, manager, youtube).listing() == queue.listing()
    manager.pool.drain()
    assert [row["status"] for row in queue.listing()] == ["completed", "completed"]
    assert youtube.calls == renders
    assert queue.get(first["id"])["job_id"] != queue.get(second["id"])["job_id"]
    draft = studio_store.get(config, first["draft_id"])
    assert draft["approved_revision"] is None and draft.get("content_review") is None
    assert draft["evidence"][0]["reviewed"] is False
    job = store.get(config, renders[0])
    assert job["details"]["review_status"] == "unreviewed_preview"


def test_auth_wait_keeps_finished_video_and_does_not_block_next(system):
    _, manager, youtube, queue, renders = system
    youtube.connected = False
    first, second = queue.create("https://example.org/a"), queue.create("https://example.org/b")
    manager.pool.drain()
    assert len(renders) == 2 and not youtube.calls
    assert [row["status"] for row in queue.listing()] == ["needs_auth", "needs_auth"]
    youtube.connected = True
    queue.start()
    manager.pool.drain()
    assert len(renders) == 2
    assert youtube.calls == [queue.get(row["id"])["job_id"] for row in (first, second)]
    assert all(row["status"] == "completed" for row in queue.listing())


def test_upload_failure_and_auth_error_are_durable_and_reuse_completed_job(system):
    config, manager, youtube, queue, renders = system
    first, second = queue.create("https://example.org/a"), queue.create("https://example.org/b")
    youtube.error = RuntimeError("upload quota")
    manager.pool.drain()
    assert [row["status"] for row in queue.listing()] == ["failed", "failed"]
    assert len(renders) == 2
    youtube.error = YoutubeAuthError("expired")
    queue.retry(first["id"])
    manager.pool.drain()
    assert queue.get(first["id"])["status"] == "needs_auth"
    youtube.error = None
    recovered = SubmissionQueue(config, manager, youtube)
    recovered.recover()
    recovered.start()
    manager.pool.drain()
    assert recovered.get(first["id"])["status"] == "completed"
    assert recovered.get(second["id"])["status"] == "failed"
    assert len(renders) == 2


@pytest.mark.parametrize("interrupted_stage", ["preparing", "rendering", "uploading"])
def test_restart_recovers_active_items_without_recreating_finished_video(system, interrupted_stage):
    config, manager, youtube, queue, renders = system
    row = queue.create("https://example.org/a")
    manager.pool.drain()
    queue._update(row["id"], status=interrupted_stage, youtube_url=None)
    new_queue = SubmissionQueue(config, manager, youtube)
    new_queue.recover()
    assert new_queue.get(row["id"])["status"] == "queued"
    new_queue.start()
    manager.pool.drain()
    assert new_queue.get(row["id"])["status"] == "completed" and len(renders) == 1


def test_preparation_failure_continues_and_cancelled_rows_are_skipped(system, monkeypatch):
    _, manager, _, queue, renders = system
    original = manager._prepare

    def prepare(draft):
        if draft["source_url"].endswith("bad"):
            draft.update(status="needs_evidence", error="원문 근거가 부족합니다.")
            studio_store.save(manager.config, draft, draft["revision"])
        else:
            original(draft)

    monkeypatch.setattr(manager, "_prepare", prepare)
    bad = queue.create("https://example.org/bad")
    cancelled = queue.create("https://example.org/cancelled")
    queue.cancel(cancelled["id"])
    good = queue.create("https://example.org/good")
    manager.pool.drain()
    assert queue.get(bad["id"])["status"] == "needs_review"
    assert queue.get(cancelled["id"])["status"] == "cancelled"
    assert queue.get(good["id"])["status"] == "completed" and len(renders) == 1


def test_queue_registration_continues_while_worker_is_busy(system, monkeypatch):
    _, manager, _, queue, renders = system
    from concurrent.futures import ThreadPoolExecutor
    manager.pool = ThreadPoolExecutor(max_workers=1)
    started, release = threading.Event(), threading.Event()
    original = manager._prepare

    def prepare(draft):
        started.set()
        assert release.wait(5)
        original(draft)

    monkeypatch.setattr(manager, "_prepare", prepare)
    try:
        queue.create("https://example.org/a")
        assert started.wait(5)
        second = queue.create("https://example.org/b")
        assert queue.get(second["id"])["status"] == "queued"
        release.set()
        manager.pool.submit(lambda: None).result(timeout=10)
        assert len(renders) == 2
    finally:
        release.set()
        manager.pool.shutdown()


def test_selected_articles_preserve_all_sources_in_draft(system):
    config, manager, _, queue, _ = system
    articles = [{"id": "a", "title": "첫 뉴스", "url": "https://example.org/a"},
                {"id": "b", "title": "두 번째 뉴스", "url": "https://example.org/b"}]
    studio_store.save_articles(config, articles)
    row = queue.create_selected(["a", "b"], "tech")
    draft = studio_store.get(config, row["draft_id"])
    assert draft["articles"] == articles and "source_url" not in draft
    assert draft["article_ids"] == ["a", "b"] and draft["category"] == "tech"
    with pytest.raises(ValueError, match="뉴스가 없습니다"):
        queue.create_selected(["missing"], "tech")
    manager.pool.drain()


def test_private_preview_gate_requires_server_authorization_and_valid_evidence(tmp_path, monkeypatch):
    config = Settings(root=tmp_path)
    request = VideoRequest(topic="계획과 성과")
    context = {"id": "draft", "revision": 1, "approved_revision": None, "category": "tech",
               "seconds": 30, "evidence": [source()], "script": script().model_dump(),
               "preview_only": True, "automation_private_preview": True, "render_authorized_revision": 1}
    for changes in ({"automation_private_preview": False}, {"render_authorized_revision": 2},
                    {"evidence": []}, {"content_review": {"content_reviewed": True}}):
        with pytest.raises(ValueError):
            pipeline.prepare_news(config, request, script(), {**context, **changes})
    job_id = pipeline.prepare_news(config, request, script(), context)
    folder = pipeline.job_folder(config, job_id)
    (folder / "output.mp4").write_bytes(b"mock")
    monkeypatch.setattr(pipeline, "STAGES", ("verify",))
    monkeypatch.setattr(pipeline, "verify", lambda *a: {"format": {"duration": "30"}})
    pipeline.execute(config, resume=job_id)
    metadata = json.loads((folder / "metadata.json").read_text())
    assert metadata["review_status"] == "unreviewed_preview"
    assert metadata["review_required"] is True and metadata["publication_status"] == "private_preview_only"
    assert metadata["content_review"] is None and metadata["sources"][0]["reviewed"] is False
    assert json.loads((folder / "news-context.json").read_text())["approved_revision"] is None


def test_job_config_is_restored_from_manifest(system):
    config, manager, _, queue, _ = system
    row = queue.create("https://example.org/a")
    manager.pool.drain()
    job_id = queue.get(row["id"])["job_id"]
    original = manager.config_for_job(job_id)
    manager.config = config.model_copy(update={"model": "new", "tts_rate": 300, "encoder": "libx264"})
    restored = manager.config_for_job(job_id)
    assert pipeline.config_signature(restored) == pipeline.config_signature(original)


def test_restart_recovers_prepared_job_before_submission_link_was_saved(system, monkeypatch):
    config, manager, youtube, queue, renders = system
    row = queue.create("https://example.org/a", anchor_id="headline60-brown-adult-01")
    draft = studio_store.get(config, row["draft_id"])
    manager._prepare(draft)
    draft = studio_store.get(config, draft["id"])
    context = {**draft, "preview_only": True, "automation_private_preview": True,
               "render_authorized_revision": draft["revision"], "approved_revision": None}
    job_id = pipeline.prepare_news(config, VideoRequest(topic=script().title, anchor_id=draft["anchor_id"]),
                                   script(), context)
    folder = pipeline.job_folder(config, job_id)
    manifest = json.loads((folder / "job.json").read_text())
    manifest["completed_stages"].append("tts")
    save_json(folder / "job.json", manifest)
    queue._update(row["id"], status="rendering")
    manager.pool.pending.clear()  # Old process exited before draining the submitted callback.
    manager.recover()
    replacement = SubmissionQueue(config, manager, youtube)
    replacement.recover()
    monkeypatch.setattr(manager, "_prepare", lambda *_: pytest.fail("prepared source/script must be reused"))
    replacement.start()
    manager.pool.drain()
    assert replacement.get(row["id"])["job_id"] == job_id and renders == [job_id]
    assert json.loads((folder / "job.json").read_text())["completed_stages"] == ["script", "tts"]
    assert manifest["request"]["anchor_id"] == "headline60-brown-adult-01"


def test_connection_callback_during_drain_wakes_existing_auth_wait(system, monkeypatch):
    _, manager, youtube, queue, renders = system
    row = queue.create("https://example.org/a")
    original = youtube.upload
    calls = 0

    def upload(folder):
        nonlocal calls
        calls += 1
        if calls == 1:
            queue.start()  # An OAuth callback arrives while the current worker still owns the drain.
            raise YoutubeAuthError("old request had expired token")
        return original(folder)

    monkeypatch.setattr(youtube, "upload", upload)
    manager.pool.drain()
    assert queue.get(row["id"])["status"] == "completed" and len(renders) == 1 and calls == 2
