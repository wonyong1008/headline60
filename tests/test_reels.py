import importlib.util
from pathlib import Path

import pytest
from PIL import Image

from app.config import Settings
from app.media.cards import load_font

spec = importlib.util.spec_from_file_location(
    "reel_prototype", Path(__file__).parents[1] / "app/renderer/reels.py")
reels = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reels)


@pytest.fixture
def reel_plan(tmp_path):
    asset = tmp_path / "licensed-photo.png"
    Image.new("RGB", (400, 300), "gray").save(asset)
    audio = tmp_path / "prepared.wav"
    audio.write_bytes(b"fixture: renderer validates audio with FFmpeg during composition")
    return {"width": 720, "fps": 30, "audio_path": str(audio), "scenes": [
        {"asset_path": str(asset), "duration": 3, "lines": ["호가만 보고", "판단할까?"],
         "accent_line": 1, "credit": "Author / CC BY 4.0"} for _ in range(4)]}


def test_headline_fits_mobile_safe_area_and_three_lines():
    config = Settings()
    lines, size = reels.layout_lines(["호가만 보고", "판단할까?"], config.font)
    font = load_font(config.font, size, True)
    assert size >= 62 and len(lines) == 2
    assert all(reels.LEFT + font.getlength(text) <= 638 for text, _ in lines)
    assert 364 + (len(lines) - 1) * 106 + size <= 750


def test_long_headline_fails_instead_of_becoming_tiny():
    with pytest.raises(ValueError, match="안전영역"):
        reels.layout_lines(["대한민국 아파트 가격을 확인할 때 반드시 비교해야 하는 조건 " * 2], Settings().font)


def test_twelve_second_plan_has_360_frames(reel_plan, tmp_path):
    scenes = reels.validate_plan(reel_plan, Settings(root=tmp_path))
    assert sum(scene["frames"] for scene in scenes) == 360


@pytest.mark.parametrize("field,value,message", [
    ("asset_path", "/missing/photo.jpg", "자료사진"),
    ("duration", float("nan"), "장면 길이"),
    ("credit", "", "크레딧"),
    ("accent_line", 5, "강조 줄"),
])
def test_invalid_media_or_timing_is_rejected(reel_plan, tmp_path, field, value, message):
    reel_plan["scenes"][0][field] = value
    with pytest.raises(ValueError, match=message):
        reels.validate_plan(reel_plan, Settings(root=tmp_path))


def test_existing_output_is_preserved(reel_plan, tmp_path):
    target = tmp_path / "output.mp4"
    target.write_bytes(b"existing result")
    with pytest.raises(ValueError, match="덮어쓰지"):
        reels.render_reel(reel_plan, tmp_path, Settings(root=tmp_path))
    assert target.read_bytes() == b"existing result"
