from pathlib import Path

"""Optional presenters must follow current speech and preserve the footage fallback."""
import json
from copy import deepcopy

import pytest
from PIL import Image

from app.anchors import production
from app.config import Settings
from app.process import JobCancelled
from app.renderer import reels, storyboard
from app.script.models import Scene, Script, VideoRequest
from app.studio_models import ReelOptions


@pytest.fixture
def approved(tmp_path):
    config = Settings(root=tmp_path)
    folder = config.data / "anchors"
    folder.mkdir(parents=True)
    image = folder / "approved.png"
    Image.new("RGB", (720, 1280), "green").save(image)
    (folder / "approved-face.json").write_text(json.dumps({
        "anchor_id": production.ANCHOR_ID, "fictional_adult": True, "face_approved": True,
        "outfit_status": "approved_for_lipsync_preview", "outfit_candidate": str(image)}))
    for name in ("runtime/lipsync/.venv/bin/python", "runtime/lipsync/worker.py", "app/anchors/composite.py"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("local existing runtime")
    (tmp_path / "runtime/lipsync/vendor/musetalk-mac/upstream/models").mkdir(parents=True)
    return config


def plan(folder, audio=b"new narration"):
    folder.mkdir(exist_ok=True)
    narration = folder / "narration.wav"
    narration.write_bytes(audio)
    return {"audio_path": str(narration), "scenes": [
        {"duration": 4, "media_type": "graphic", "captions": [{"text": "현재 내레이션"}]},
        {"duration": 4, "media_type": "graphic", "chart": {"values": [1, 2]}}]}


def test_presenter_selection_is_optional_and_explicit():
    assert ReelOptions().anchor_id is None
    assert VideoRequest(topic="금리 해설").anchor_id is None
    assert ReelOptions(anchor_id=production.ANCHOR_ID).anchor_id == production.ANCHOR_ID
    with pytest.raises(ValueError):
        ReelOptions(anchor_id="unknown")


def test_profiles_only_expose_approved_adult_assets(approved):
    profile = production.profiles(approved)[0]
    assert profile["name"] == "AI 진행자 · 실험" and profile["available"]
    assert profile["experimental"] and not profile["production_ready"]
    assert production.thumbnail_path(approved, profile["anchor_id"]).name == "approved.png"
    with pytest.raises(ValueError):
        production.thumbnail_path(approved, "../secret")
    path = approved.data / "anchors/approved-face.json"
    metadata = json.loads(path.read_text())
    metadata["fictional_adult"] = False
    path.write_text(json.dumps(metadata))
    assert production.profiles(approved) == []


def test_real_job_audio_controls_cache_and_windows(approved, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(production, "_verify", lambda *_: None)

    def synthesize(config, audio, image, intervals, folder):
        calls.append((audio.read_bytes(), intervals))
        output = folder / "presenter.mp4"
        output.write_bytes(b"fresh mouth frames")
        output.with_name("presenter-matte.mp4").write_bytes(b"fresh matte frames")
        return output, {"status": "completed", "source_audio": str(audio)}

    monkeypatch.setattr(production, "_synthesize", synthesize)
    first = tmp_path / "first"
    selected = plan(first)
    original = deepcopy(selected)
    result = production.prepare_presenter(selected, first, approved, production.ANCHOR_ID)
    assert result["status"] == "completed" and not result["cached"]
    assert calls == [(b"new narration", [(0, 3), (5, 8)])]
    assert selected["audio_path"] == original["audio_path"]
    for scene, before in zip(selected["scenes"], original["scenes"], strict=True):
        assert {k: v for k, v in scene.items() if k != "presenter_windows"} == before
    intro = selected["scenes"][0]["presenter_windows"][0]
    assert Path(intro["matte"]).read_bytes() == b"fresh matte frames"  # cut-out matte travels with the clip
    outro = selected["scenes"][1]["presenter_windows"][0]
    assert (intro["start"], intro["end"], intro["source_start"]) == (0, 3, 0)
    assert (outro["start"], outro["end"], outro["source_start"]) == (1, 4, 3)
    second = tmp_path / "second"
    assert production.prepare_presenter(plan(second), second, approved, production.ANCHOR_ID)["cached"]
    third = tmp_path / "third"
    assert not production.prepare_presenter(plan(third, b"different job audio"), third, approved,
                                           production.ANCHOR_ID)["cached"]
    assert len(calls) == 2


def test_worker_failure_falls_back_but_cancellation_propagates(approved, tmp_path, monkeypatch):
    folder = tmp_path / "failed"
    selected = plan(folder)
    original = deepcopy(selected)
    def failed(*_):
        raise RuntimeError("local worker failed")
    monkeypatch.setattr(production, "_synthesize", failed)
    result = production.prepare_presenter(selected, folder, approved, production.ANCHOR_ID)
    assert result["status"] == "fallback" and "local worker failed" in result["error"]
    assert selected["scenes"] == original["scenes"]
    def cancelled(*_):
        raise JobCancelled("stop")
    monkeypatch.setattr(production, "_synthesize", cancelled)
    with pytest.raises(JobCancelled):
        production.prepare_presenter(selected, folder, approved, production.ANCHOR_ID)
    assert json.loads((folder / "anchor-status.json").read_text())["status"] == "cancelled"


def test_charts_select_relevant_approved_photos(approved, monkeypatch):
    from app.media import library
    assets = [{"asset_id": "bank", "usage_status": "approved", "media_type": "image", "category": "economy",
                   "name": "은행 창구", "tags": ["bank"], "license_type": "permission-granted"},
              {"asset_id": "car", "usage_status": "approved", "media_type": "image", "category": "economy",
                   "name": "자동차", "tags": ["automobile"]}]
    monkeypatch.setattr(library, "listing", lambda _: assets)
    monkeypatch.setattr(library, "resolve_asset", lambda _, key: next(a for a in assets if a["asset_id"] == key))
    scene = Scene(headline="기준금리 비교", narration="은행 기준금리의 변화를 확인합니다.",
                  visual_query="은행 창구", visual_type="data_chart")
    script = Script(title="금리 비교", description="근거를 확인한 금리 비교입니다.", scenes=[scene] * 4)
    chosen = storyboard.resolve_visuals(approved, script, "economy")
    assert [a["asset_id"] if a else None for a in chosen] == ["bank", "bank", None, None]
    assert all(not a or a["asset_id"] != "car" for a in chosen)


def test_chart_panel_is_translucent_with_readable_immutable_values(tmp_path):
    config = Settings(root=tmp_path)
    chart = {"labels": ["이전", "이후"], "values": [2.5, 2.75], "unit": "%",
             "source_label": "한국은행", "as_of": "2025-01-16", "source_refs": ["verified"]}
    original = deepcopy(chart)
    scene = {"media_type": "image", "duration": 3, "frames": 90, "chart": chart,
             "fitted_lines": [("기준금리 비교", 0)], "font_size": 72, "lines": ["기준금리 비교"],
             "captions": [{"text": "근거 확인", "start": 0, "end": 3}], "credit": "approved source"}
    for template in ("data_comparison", "story_explainer", "headline_news"):
        plan = {"template": template, "edit_style": "reels_v3", "scenes": [scene]}
        fixed, layers = reels.build_layers(scene, plan, tmp_path, 0, config)
        with Image.open(fixed) as overlay:
            assert overlay.getpixel((10, 600))[3] < 200
        chart_layer = next(layer for layer in layers if layer["animation"] == "chart_reveal")
        with Image.open(chart_layer["path"]) as image:
            assert image.getpixel((580, 340)) == (16, 27, 45, 178)
        assert chart == original


def test_presenter_background_is_limited_to_selected_window_and_tail_stays_pure(tmp_path):
    import subprocess
    import wave

    from app.process import run

    still = tmp_path / "footage.png"
    Image.new("RGB", (720, 1280), "red").save(still)
    presenter = tmp_path / "generated-presenter.mp4"
    run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
         "color=c=blue:s=720x1280:r=30:d=2", "-an", "-c:v", "libx264", "-preset", "ultrafast", str(presenter)])
    audio = tmp_path / "current-narration.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(48000)
        stream.writeframes(b"\0\0" * 48000 * 4)
    plan = {"audio_path": str(audio), "edit_style": "reels_v3", "scenes": [
        {"media_type": "image", "asset_path": str(still), "duration": 4, "lines": ["현재 음성 사용"],
         "visual_first": True, "credit": "synthetic test fixture",
         "captions": [{"text": "현재 음성과 자막 보존", "start": 0, "end": 4}],
         "presenter_windows": [{"path": str(presenter), "start": 1, "end": 3, "source_start": 0}]}]}
    output = reels.render_reel(plan, tmp_path / "render", Settings(root=tmp_path, encoder="libx264"))
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(output), "-vf",
        "select='eq(n,15)+eq(n,45)+eq(n,105)',crop=2:2:10:600", "-frames:v", "3", "-fps_mode", "vfr",
        "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], check=True, capture_output=True, timeout=15).stdout
    assert len(raw) == 36
    before, during, after = [tuple(raw[i * 12:i * 12 + 3]) for i in range(3)]
    assert before[0] > 100 and before[2] < 20
    assert during[0] < 20 and during[2] > 100
    assert after[0] > 100 and after[2] < 20
    with Image.open(output.parent / "media-tail-00.png") as tail:
        assert tail.getpixel((10, 600))[0] > 240


def test_chart_and_panel_never_cover_the_presenter(tmp_path):
    config = Settings(root=tmp_path)
    chart = {"labels": ["이전", "이후"], "values": [2.5, 2.75], "unit": "%",
             "source_label": "한국은행", "as_of": "2025-01-16", "source_refs": ["verified"]}
    scene = {"media_type": "image", "duration": 3, "frames": 90, "chart": chart,
             "fitted_lines": [("기준금리 비교", 0)], "font_size": 72, "lines": ["기준금리 비교"],
             "captions": [{"text": "근거 확인", "start": 0, "end": 3}], "credit": "approved source",
             "presenter_windows": [{"path": "p.mp4", "start": 0, "end": 2, "source_start": 0}]}
    plan = {"template": "story_explainer", "edit_style": "reels_v3", "scenes": [scene]}
    fixed, layers = reels.build_layers(scene, plan, tmp_path, 0, config)
    with Image.open(fixed) as overlay:
        assert overlay.getpixel((300, 400))[3] < 120  # no dark panel over the presenter's body
    assert next(layer for layer in layers if layer["animation"] == "chart_reveal")["hide_with_presenter"]
