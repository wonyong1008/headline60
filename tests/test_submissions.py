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
from app.script.models import Scene, Script


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
    # The temporary root has no MLX runtime; rendering is mocked, so present the male voice as installed.
    from app.tts import profiles as voice_profiles
    real_listing = voice_profiles.listing
    monkeypatch.setattr(voice_profiles, "listing", lambda cfg: [{**p, "available": True} for p in real_listing(cfg)])
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


def test_queue_waits_for_a_briefly_busy_pipeline_lock(tmp_path):
    import fcntl

    from app.config import Settings
    from app.jobs.pipeline import LOCK_WAIT, exclusive
    config = Settings(root=tmp_path)
    config.data.mkdir(parents=True, exist_ok=True)
    holder = (config.data / "pipeline.lock").open("a")
    fcntl.flock(holder, fcntl.LOCK_EX)
    with pytest.raises(RuntimeError), exclusive(config):
        pass
    threading.Timer(0.5, lambda: fcntl.flock(holder, fcntl.LOCK_UN)).start()
    token = LOCK_WAIT.set(10.0)
    try:
        with exclusive(config):
            acquired = True
    finally:
        LOCK_WAIT.reset(token)
        holder.close()
    assert acquired


def test_rebuild_makes_a_new_job_and_new_upload_but_keeps_the_old_one(system):
    config, manager, youtube, queue, renders = system
    first = queue.create("https://example.org/a")
    manager.pool.drain()
    done = queue.get(first["id"])
    assert done["status"] == "completed" and len(youtube.calls) == 1
    with pytest.raises(ValueError):
        queue.retry(first["id"])
    queue.rebuild(first["id"])
    manager.pool.drain()
    again = queue.get(first["id"])
    assert again["status"] == "completed" and again["job_id"] != done["job_id"]
    assert youtube.calls == [done["job_id"], again["job_id"]] and len(renders) == 2
    assert (pipeline.job_folder(config, done["job_id"]) / "output.mp4").is_file()
    history = studio_store.get(config, again["draft_id"])["previous_outputs"]
    assert history[-1]["youtube_url"] == done["youtube_url"]


def test_daily_upload_limit_waits_and_retries_automatically_later(system, monkeypatch):
    from app.jobs import submissions as module
    _, manager, youtube, queue, renders = system
    monkeypatch.setattr(queue, "_schedule_wake", lambda: None)
    first = queue.create("https://example.org/a")
    youtube.error = RuntimeError("YouTube 요청 실패 (HTTP 400, uploadLimitExceeded)")
    manager.pool.drain()
    waiting = queue.get(first["id"])
    assert waiting["status"] == "upload_wait" and waiting["job_id"] and len(renders) == 1
    youtube.error = None
    queue.start()
    manager.pool.drain()
    assert queue.get(first["id"])["status"] == "upload_wait"  # too early: not retried yet
    monkeypatch.setattr(module, "UPLOAD_WAIT_SECONDS", 0)
    queue.start()
    manager.pool.drain()
    done = queue.get(first["id"])
    assert done["status"] == "completed" and len(renders) == 1  # same finished video, uploaded later


def test_new_and_rebuilt_submissions_target_one_minute(system):
    config, _manager, _youtube, queue, _renders = system
    first = queue.create("https://example.org/a")
    assert studio_store.get(config, queue.get(first["id"])["draft_id"])["seconds"] == 60


def test_long_internal_silences_are_shortened_before_the_quality_gate(tmp_path):
    import math
    import struct
    import wave

    from app.tts.engine import make_pcm
    raw = tmp_path / "raw.wav"
    rate = 24000
    def tone(seconds):
        return [int(9000 * math.sin(2 * math.pi * 220 * i / rate)) for i in range(int(rate * seconds))]
    samples = tone(1.5) + [0] * int(rate * 4.0) + tone(1.5)  # 4 s stall in the middle
    with wave.open(str(raw), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    result = make_pcm(raw, tmp_path / "out.wav", 1.0)
    assert result["before"]["longest_internal_silence"] < 1.5 and result["after"]["duration"] < 4.8  # 4 s stall gone


def test_submission_keeps_the_chosen_voice_and_falls_back_safely(system, monkeypatch):
    from app.jobs import submissions as module
    config, _manager, _youtube, queue, _renders = system
    monkeypatch.setattr(module.profiles, "listing", lambda cfg: [{"voice_profile_id": "reels-male-ryan", "available": True}])
    male = queue.create("https://example.org/m", voice_profile_id="reels-male-ryan")
    assert queue.get(male["id"])["voice_profile_id"] == "reels-male-ryan"
    assert studio_store.get(config, queue.get(male["id"])["draft_id"])["voice_profile_id"] == "reels-male-ryan"
    # No voice requested, and no runtime installed: still the male narrator (never a female fallback).
    monkeypatch.setattr(module.profiles, "listing", lambda cfg: [{"voice_profile_id": "reels-male-ryan", "available": False}])
    plain = queue.create("https://example.org/f")
    assert studio_store.get(config, queue.get(plain["id"])["draft_id"])["voice_profile_id"] == "reels-male-ryan"
