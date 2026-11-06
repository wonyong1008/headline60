import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.jobs import pipeline
from app.jobs.store import recent
from app.llm.ollama import review_script
from app.main import app
from app.renderer.ffmpeg import stamp, timeline, write_subtitles
from app.script.models import Scene, Script, VideoRequest, validate_explainer


def sample() -> Script:
    return Script(title="금리와 주택", description="일반 개념 설명이며 검토가 필요합니다.", scenes=[
        Scene(headline="금리의 영향", narration="금리가 오르면 대출의 이자 부담이 커질 수 있습니다.")
        for _ in range(6)
    ])


def test_timeline_and_subtitles_cover_exact_target(tmp_path):
    frames, speed = timeline([4.12, 6.35, 5.71, 4.23, 5.55, 4.01], 30)
    assert sum(frames) == 900
    assert 0.5 <= speed <= 2
    path = tmp_path / "subtitles.srt"
    write_subtitles(sample(), frames, path)
    text = path.read_text()
    assert "00:00:00,000 -->" in text
    assert "--> 00:00:30,000" in text
    assert stamp(59.9999) == "00:01:00,000"


@pytest.mark.parametrize("lengths", [[], [0, 5], [-1, 3], [float("nan")], [1000]])
def test_bad_audio_timing_fails(lengths):
    with pytest.raises(ValueError):
        timeline(lengths, 30)


def test_remote_llm_and_unsupported_video_rejected():
    with pytest.raises(ValidationError):
        Settings(ollama_url="https://api.example.com")
    with pytest.raises(ValidationError):
        VideoRequest(topic="일반 설명", seconds=45)
    with pytest.raises(ValueError):
        validate_explainer(VideoRequest(topic="오늘 금리 속보"))


def test_health_and_artifact_security():
    async def check():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            assert (await client.get("/health")).json()["status"] == "ok"
            assert (await client.get("/jobs/not-a-job/script.json")).status_code == 404
            assert (await client.get("/jobs/20261028T000000-12345678/.env")).status_code == 404
            response = await client.get("/health", headers={"host": "malicious.example"})
            assert response.status_code == 400

    asyncio.run(check())


def test_failure_resume_preserves_completed_stages(tmp_path, monkeypatch):
    config = Settings(root=tmp_path)
    calls = []

    def generate(*args):
        calls.append("llm")
        return sample(), {"response": "test"}

    monkeypatch.setattr(pipeline, "generate", generate)
    monkeypatch.setattr(pipeline, "synthesize", lambda *args: [5.0] * 6)

    def fail(*args):
        raise RuntimeError("카드 생성 테스트 오류")

    monkeypatch.setattr(pipeline, "create_cards", fail)
    with pytest.raises(RuntimeError, match="카드 생성"):
        pipeline.execute(config, VideoRequest(topic="금리와 부동산"))
    job = recent(config)[0]
    assert job["status"] == "failed" and job["stage"] == "cards"
    folder = pipeline.job_folder(config, job["id"])
    manifest = json.loads((folder / "job.json").read_text())
    assert manifest["completed_stages"] == ["script", "tts"]
    monkeypatch.setattr(pipeline, "create_cards", lambda *args: None)
    monkeypatch.setattr(pipeline, "render", lambda *args: None)
    monkeypatch.setattr(pipeline, "verify", lambda *args: {"format": {"duration": "30.0"}})
    assert pipeline.execute(config, resume=job["id"]) == folder
    assert calls == ["llm"]
    assert recent(config)[0]["status"] == "completed"
    with pytest.raises(ValueError, match="덮어쓰지"):
        pipeline.execute(config, resume=job["id"])


def test_cancel_is_distinct_from_failure(tmp_path, monkeypatch):
    config = Settings(root=tmp_path)

    def cancel(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(pipeline, "generate", cancel)
    with pytest.raises(KeyboardInterrupt):
        pipeline.execute(config, VideoRequest(topic="금리와 부동산"))
    assert recent(config)[0]["status"] == "cancelled"


def test_two_processes_cannot_run_pipeline_together(tmp_path):
    config = Settings(root=tmp_path)
    with (
        pipeline.exclusive(config),
        pytest.raises(RuntimeError, match="실행 중"),
        pipeline.exclusive(config),
    ):
        pytest.fail("중복 실행 잠금이 작동하지 않았습니다.")


def test_job_ids_cannot_escape_outputs(tmp_path):
    with pytest.raises(ValueError):
        pipeline.job_folder(Settings(root=tmp_path), "../../.env")


@pytest.mark.parametrize("text", [
    "중央은행은 시장에 영향을 줄 수 있습니다.",
    "통계적으로 부동산 가격은 반비례 관계입니다.",
    "금리가 5퍼센트로 변동하는 경우를 고려합니다.",
])
def test_unverified_statistics_and_mixed_language_rejected(text):
    script = sample()
    script.scenes[0].narration = text
    with pytest.raises(ValueError):
        review_script(script)


def test_explicit_limitation_is_allowed():
    script = sample()
    script.scenes[0].narration = "금리와 주택 가격은 단순한 반비례 관계로 단정할 수 없습니다."
    review_script(script)
