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


def test_wardrobe_rotates_outfits_and_caches_each_look(tmp_path, monkeypatch):
    from app.anchors import wardrobe
    config = Settings(root=tmp_path)
    approved = tmp_path / "approved.png"
    Image.new("RGB", (720, 1280), "white").save(approved)
    generated = []

    def fake_generate(config, look, approved_image):
        generated.append(look)
        folder = wardrobe.wardrobe_root(config) / look
        folder.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (720, 1280), "pink").save(folder / "presenter.png")
        return folder / "presenter.png"

    monkeypatch.setattr(wardrobe, "generate_look", fake_generate)
    picks = [wardrobe.outfit_for_video(config, approved)[0] for _ in range(len(wardrobe.LOOKS) + 1)]
    assert picks[:len(wardrobe.LOOKS)] == list(wardrobe.LOOKS) and picks[-1] == picks[0]  # rotation
    monkeypatch.setattr(wardrobe, "generate_look", lambda *a: (_ for _ in ()).throw(RuntimeError("no model")))
    look, image = wardrobe.outfit_for_video(config, approved)
    assert look == "approved-default" and image == approved  # failure keeps the video going
    assert (wardrobe.wardrobe_root(config) / "failures.jsonl").is_file()


def test_punch_style_adds_boxed_hook_centered_captions_and_progress_bar(tmp_path):
    config = Settings(root=tmp_path)
    scene = {"media_type": "image", "duration": 4, "frames": 120, "hook_scene": True, "visual_first": True,
             "hook_text": "국회, 특검법안 167-1 찬성", "lines": ["국회, 특검법안 167-1 찬성"], "fitted_lines": [("국회, 특검법안 167-1 찬성", 0)],
             "font_size": 72, "captions": [{"text": "특검법안이 167 대 1로 통과됐어요.", "start": 0, "end": 4}],
             "credit": "fixture", "emphasis_words": ["특검법안"], "progress_offset": 10}
    plan = {"template": "story_explainer", "edit_style": "reels_v3", "text_style": "punch", "subtitle_style": "dynamic",
            "scenes": [scene], "total_seconds": 40}
    _fixed, layers = reels.build_layers(scene, plan, tmp_path, 0, config)
    hook = next(layer for layer in layers if layer.get("stamp"))
    with Image.open(hook["path"]) as image:
        assert b"\xff\xe1\x4d\xff" in image.convert("RGBA").tobytes()  # yellow highlight box present
    assert abs(hook["x"] + hook["size"][0] / 2 - 360) < 2  # centred
    caption = next(layer for layer in layers if layer["caption"])
    assert abs(caption["x"] + caption["size"][0] / 2 - 360) < 2
    bar = next(layer for layer in layers if layer["animation"] == "progress")
    assert bar["progress_offset"] == 10 and bar["progress_total"] == 40
    _chain, x, _y, enable = reels.motion_filter(bar, 2, "m", 1)
    assert enable == "1" and "min(1,(10.0000+t)/40.0000)" in x


def test_outro_scene_gets_the_big_centred_cta_block_for_its_whole_length(tmp_path):
    config = Settings(root=tmp_path)
    scene = {"media_type": "image", "duration": 3, "frames": 90, "outro_scene": True, "visual_first": True,
             "hook_headline_text": "구독·좋아요 눌러주세요!", "lines": ["구독·좋아요 눌러주세요!"], "fitted_lines": [("구독·좋아요 눌러주세요!", 0)],
             "font_size": 72, "captions": [{"text": "도움 됐다면 구독과 좋아요, 꼭 눌러 주세요!", "start": 0, "end": 3}],
             "credit": "fixture", "emphasis_words": ["구독", "좋아요"]}
    plan = {"template": "story_explainer", "edit_style": "reels_v3", "text_style": "punch", "subtitle_style": "dynamic", "scenes": [scene]}
    _fixed, layers = reels.build_layers(scene, plan, tmp_path, 0, config)
    block = next(layer for layer in layers if layer.get("stamp"))
    assert block["end"] == 3 and abs(block["x"] + block["size"][0] / 2 - 360) < 2
