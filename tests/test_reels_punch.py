"""Reels rendering: chart panels and the 'punch' short-form text style."""
from copy import deepcopy

from PIL import Image

from app.config import Settings
from app.renderer import reels, storyboard
from app.script.models import Scene, Script


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


def test_charts_select_relevant_approved_photos(tmp_path, monkeypatch):
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
    chosen = storyboard.resolve_visuals(Settings(root=tmp_path), script, "economy")
    assert [a["asset_id"] if a else None for a in chosen] == ["bank", "bank", None, None]
    assert all(not a or a["asset_id"] != "car" for a in chosen)


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
