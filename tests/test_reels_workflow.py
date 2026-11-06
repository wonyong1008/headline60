"""Modern reel contracts: mocked AI, real short PCM/FFmpeg padding where stated."""
import array
import json
import math
import shutil
import wave
from datetime import date
from pathlib import Path

import pytest

from app import studio_store
from app.config import Settings
from app.jobs import pipeline, store
from app.jobs.manager import Manager
from app.renderer import reels, storyboard
from app.script.models import ChartData, Scene, Script, VideoRequest
from app.studio_models import Evidence, script_issues
from app.tts import engine
from app.tts.profiles import VoiceProfile


def evidence():
    return Evidence(id="aabbccddeeff", url="https://www.korea.kr/news/example",
        publisher="공식 자료", title="비교 자료의 기준과 수치", published_date=date(2026, 10, 28),
        accessed_date=date(2026, 10, 28), excerpt="확인한 비교 수치는 10과 20이며 기준일은 2026-10-28입니다.",
        fact="두 비교 항목은 10과 20으로 확인했습니다.", conditions="예시 자료의 동일 기준 비교입니다.", reviewed=True)


def script():
    scenes = [Scene(headline="확인한 뉴스의 핵심", narration="확인한 자료를 바탕으로 핵심 내용을 전달합니다.",
                    duration_seconds=duration, claim_ids=[evidence().id], scene_id=f"scene-{i}")
              for i, duration in enumerate((3, 9, 9, 9))]
    return Script(title="비교 자료를 읽는 방법", description="공식 출처와 기준일을 함께 확인합니다.",
                  hook=scenes[0].narration, scenes=scenes)


def pcm(path, seconds=.42):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        samples = array.array("h", (round(3000 * math.sin(2 * math.pi * 440 * i / 48000))
                                    for i in range(round(seconds * 48000))))
        wav.writeframes(samples.tobytes())


def test_unknown_animation_and_transition_use_supported_defaults():
    scene = Scene(headline="화면 동작 검증", narration="확인한 내용을 화면으로 차분하게 전달합니다.",
                  animation_type="rotate_and_glitter", transition_type="unknown_transition")
    assert scene.animation_type == "slide_up" and scene.transition_type == "cut"
    assert scene.visual_type == "card"


@pytest.mark.parametrize(("values", "as_of", "error"), [
    ([10, 20], "2026-10-28", None),
    ([10, 21], "2026-10-28", "차트 수치"),
    ([10, 20], "2026-10-29", "차트 기준일"),
])
def test_chart_numbers_and_as_of_require_linked_evidence(values, as_of, error):
    value = script()
    value.scenes[1].chart = ChartData(labels=["이전", "이후"], values=values, unit="건",
                                     as_of=as_of, source_refs=[evidence().id])
    issues = script_issues(value, [evidence()], 30)
    if error:
        assert any(error in issue for issue in issues)
    else:
        assert issues == []


def test_natural_timeline_preserves_speed_and_rounds_each_scene_up():
    lengths = [1.001, 2.05, .4]
    frames, speed = storyboard.natural_timeline(lengths)
    assert frames == [31, 62, 12] and speed == 1
    assert all(count / 30 >= length for count, length in zip(frames, lengths, strict=True))
    assert all(count / 30 - length < 1 / 30 for count, length in zip(frames, lengths, strict=True))


@pytest.mark.parametrize("lengths", [[], [0], [float("nan")], [91]])
def test_natural_timeline_rejects_invalid_or_unbounded_audio(lengths):
    with pytest.raises(ValueError):
        storyboard.natural_timeline(lengths)


def test_continuous_tts_uses_one_worker_item_and_reuses_audio_after_visual_edit(tmp_path, monkeypatch):
    """AI worker and PCM conversion are mocked; cache joining reads real tiny WAVs."""
    config = Settings(root=tmp_path)
    record = config.data / "models/qwen3-tts-1.7b-8bit/headline60-model.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"revision": "mock-revision"}))
    runtime = config.root / "runtime/tts/.venv/bin/python"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("not executed")
    profile = VoiceProfile(voice_profile_id="natural-test", name="검증용 자연 음성",
        engine="qwen3_mlx", model="mock-model", speaker="Sohee", phrasing="continuous", speed=1).model_dump()
    requests, speeds = [], []

    def mock_run(args, **kwargs):
        if args[0] == str(runtime):
            request = json.loads(Path(args[-1]).read_text())
            requests.append(request)
            for item in request["items"]:
                pcm(Path(item["output"]), .5)
            Path(request["metrics_path"]).write_text("{}")
        else:
            assert args[0] == "ffmpeg"
            Path(args[-1]).write_bytes(b"mock AIFF conversion")
        return "mocked subprocess"

    def mock_pcm(raw, target, speed):
        speeds.append(speed)
        shutil.copyfile(raw, target)
        return {"before": {}, "after": {}}

    monkeypatch.setattr(engine, "run", mock_run)
    monkeypatch.setattr(engine, "make_pcm", mock_pcm)
    monkeypatch.setattr(engine, "inspect_audio", lambda _: {})
    scene = Scene(headline="처음 화면", narration="확인한 자료입니다, 문장 사이 호흡을 유지하고 차분하게 내용을 전달합니다.")
    for label in ("first", "visual-edit"):
        folder = tmp_path / label
        folder.mkdir()
        document = engine.SpeechDocument([scene])
        assert engine.synthesize(document, folder, config, profile) == [.5]
        scene.headline = "시각 자료만 수정한 화면"
        scene.asset_id = "f" * 32
    assert len(requests) == 1 and len(requests[0]["items"]) == 1
    assert "자료입니다," in requests[0]["items"][0]["text"]
    assert speeds == [1]
    metrics = json.loads((tmp_path / "visual-edit/tts-metrics.json").read_text())
    assert metrics["reused_phrases"] == metrics["total_phrases"] == 1


def test_build_plan_without_available_media_keeps_audio_and_ends_captions_on_timeline(tmp_path, monkeypatch):
    """Only real short WAV creation and FFmpeg padding; no AI inference or video render."""
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg unavailable")
    from app.media import library
    monkeypatch.setattr(library, "listing", lambda _: [])
    config = Settings(root=tmp_path)
    value = script()
    for scene in value.scenes:
        scene.visual_type = "stock_image"
        scene.visual_query = "검색 결과만 있고 사용 허가된 자료는 없음"
    lengths = [.42] * 4
    frames, speed = storyboard.natural_timeline(lengths)
    for i in range(4):
        pcm(tmp_path / f"speech-{i:02}.wav", lengths[i])
    (tmp_path / "timing.json").write_text(json.dumps({"audio_seconds": lengths, "frames": frames, "speed": speed}))
    request = VideoRequest(topic=value.title, template="headline_news")
    plan = storyboard.build_plan(value, tmp_path, request, config)
    assert all(s["media_type"] == "graphic" and s["asset_path"] is None for s in plan["scenes"])
    assert plan["music_path"] is None
    cues = json.loads((tmp_path / "captions.json").read_text())
    assert "estimated" in cues["alignment"]
    end = sum(frames) / 30
    assert cues["cues"][-1]["end"] == pytest.approx(end, abs=.0001)
    with wave.open(plan["audio_path"], "rb") as wav:
        actual = wav.getnframes() / wav.getframerate()
    assert actual == pytest.approx(end, abs=.0001)
    assert actual >= sum(lengths)
    assert json.loads((tmp_path / "media-sources.json").read_text()) == []


def test_modern_template_requires_approval_and_same_revision_does_not_duplicate(tmp_path, monkeypatch):
    config = Settings(root=tmp_path)
    service = Manager(config)
    service.pool.shutdown(wait=True)
    draft = studio_store.create(config, {"category": "tech", "evidence": [evidence().model_dump(mode="json")],
        "articles": [], "seconds": 30, "resolution": 720, "voice": "Yuna", "voice_profile_id": None,
        "style": "뉴스 해설", "template": "story_explainer", "subtitle_style": "boxed",
        "music_asset_id": None, "script": script().model_dump()})
    with pytest.raises(ValueError, match="검토 승인"):
        service.render(draft["id"], 1)
    monkeypatch.setattr("app.jobs.manager.korean_voices", lambda: ["Yuna"])
    scheduled = []
    monkeypatch.setattr(service, "_schedule", lambda *args: scheduled.append(args))
    service.approve(draft["id"], 1)
    first = service.render(draft["id"], 1)
    second = service.render(draft["id"], 1)
    assert first["id"] == second["id"] and len(scheduled) == 1
    manifest = json.loads((pipeline.job_folder(config, first["id"]) / "job.json").read_text())
    assert manifest["request"]["template"] == "story_explainer"
    assert manifest["request"]["subtitle_style"] == "boxed"


def test_modern_pipeline_dispatches_plan_renderer_and_verifies_natural_duration(tmp_path, monkeypatch):
    """All inference/media stages mocked; checks actual job orchestration and persisted policy."""
    config = Settings(root=tmp_path)
    value = script()
    context = {"id": "d" * 32, "revision": 2, "approved_revision": 2, "category": "tech",
               "evidence": [evidence().model_dump(mode="json")]}
    request = VideoRequest(topic=value.title, template="headline_news", subtitle_style="boxed")
    job = pipeline.prepare_news(config, request, value, context)
    observed = {}
    monkeypatch.setattr(pipeline, "synthesize", lambda *args: [3.01] * 4)

    def plan_builder(script, folder, request, config, news):
        observed["template"] = request.template
        (folder / "reels-plan.json").write_text(json.dumps({"template": request.template}))
        (folder / "media-sources.json").write_text("[]")

    def render(plan, folder, config):
        observed["rendered"] = plan["template"]
        (folder / "output.mp4").write_bytes(b"mock media")

    def verify(path, resolution, seconds):
        observed["verified_seconds"] = seconds
        return {"format": {"duration": str(seconds)}}

    monkeypatch.setattr(pipeline, "build_plan", plan_builder)
    monkeypatch.setattr(reels, "render_reel", render)
    monkeypatch.setattr(pipeline, "verify", verify)
    monkeypatch.setattr("app.process.run", lambda *args, **kwargs: "mock poster extraction")
    folder = pipeline.execute(config, resume=job)
    assert observed == {"template": "headline_news", "rendered": "headline_news", "verified_seconds": 364 / 30}
    assert store.get(config, job)["status"] == "completed"
    timing = json.loads((folder / "timing.json").read_text())
    assert timing["speed"] == 1 and timing["frames"] == [91] * 4
    metadata = json.loads((folder / "metadata.json").read_text())
    assert metadata["timing_policy"] == "natural speech, no time stretching"
    assert metadata["target_seconds"] == 30
    assert metadata["duration_seconds"] == pytest.approx(364 / 30)
