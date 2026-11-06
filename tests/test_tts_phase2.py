"""Phase 2 regression tests: no model loading, network, or AI inference."""
import array
import json
import math
import wave
from pathlib import Path

import pytest
from PIL import Image

from app.config import Settings
from app.renderer.captions import create_captions
from app.script.models import CaptionEdit, Scene, Script
from app.tts.quality import inspect_audio
from app.tts.text import normalize, read_number, spoken_parts


@pytest.mark.parametrize(("display", "spoken"), [
    ("2026년 10월 28일", "이천이십육 년 시월 이십팔 일"),
    ("2026년 6월 1일", "이천이십육 년 유월 일 일"),
    ("5억 3,000만 원", "오 억 삼천 만 원"),
    ("3.5%", "삼점오 퍼센트"),
    ("-0.5%", "마이너스 영점오 퍼센트"),
    ("1,250건", "천이백오십 건"),
    ("BMW EV6 SUV", "비엠더블유 이브이 식스 에스유브이"),
    ("AI GDP GLP-1", "에이아이 지디피 지엘피 원"),
])
def test_contextual_korean_pronunciation(display, spoken):
    assert normalize(display) == spoken


def test_decimal_zeroes_and_money_groups_are_not_dropped():
    assert read_number("0.05") == "영점영오"
    assert read_number("3,000") == "삼천"
    assert read_number("100,000,000") == "일억"


def test_display_script_and_unknown_names_are_preserved():
    text = "위고비와 마운자로, 서울 서초구 래미안원베일리를 확인합니다."
    scene = Scene(headline="표시용 원문", narration=text)
    before = scene.model_dump()
    result = spoken_parts(scene.narration)
    assert scene.model_dump() == before
    assert "위고비와 마운자로," in result[0]["text"]
    assert "위고비와 마운자로," in result[0]["spoken"]
    assert "래미안원베일리" in " ".join(p["spoken"] for p in result)
    # Bare numbers and unfamiliar model IDs have no guessed counter context.
    assert normalize("차량 QX2026, 코드 1250") == "차량 QX2026, 코드 1250"


def test_pronunciation_lexicon_can_correct_specific_names():
    original = "EV6와 래미안원베일리를 소개합니다."
    corrected = normalize(original, {"EV6": "이브이 식스", "래미안원베일리": "래미안 원 베일리"})
    assert corrected == "이브이 식스와 래미안 원 베일리를 소개합니다."
    assert original == "EV6와 래미안원베일리를 소개합니다."


def test_phrase_override_retains_original_captions():
    original = "금리는 3.5%입니다. 발표 자료를 확인합니다."
    parts = spoken_parts(original, "금리는 삼 점 오 퍼센트입니다.\n발표 자료를 정확히 확인합니다.")
    assert [p["text"] for p in parts] == ["금리는 3.5%입니다.", "발표 자료를 확인합니다."]
    assert parts[0]["spoken"] == "금리는 삼 점 오 퍼센트입니다."
    assert parts[1]["spoken"] == "발표 자료를 정확히 확인합니다."
    with pytest.raises(ValueError, match="2줄"):
        spoken_parts(original, "한 줄로만 입력한 발음입니다.")


def pcm(path: Path, samples: list[int], rate: int = 16000, channels: int = 1,
        width: int = 2) -> Path:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(width)
        audio.setframerate(rate)
        audio.writeframes(array.array("h", samples).tobytes() if width == 2 else bytes(samples))
    return path


def tone(seconds: float, rate: int = 16000) -> list[int]:
    return [round(6000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(round(seconds * rate))]


def test_normal_pcm_reports_real_sample_duration_and_trim(tmp_path):
    samples = [0] * 6400 + tone(1) + [0] * 9600
    result = inspect_audio(pcm(tmp_path / "normal.wav", samples))
    assert result["duration"] == pytest.approx(2.0)
    assert result["peak_dbfs"] < -10
    assert result["rms_dbfs"] > -30
    assert result["speech_fraction"] == pytest.approx(.5, abs=.03)
    assert result["trim_start"] == pytest.approx(.3, abs=.02)
    assert result["trim_end"] == pytest.approx(1.56, abs=.02)
    assert result["clipping_ratio"] == 0


@pytest.mark.parametrize("samples", [[0] * 16000, [4] * 16000, []])
def test_empty_or_silent_audio_is_rejected(tmp_path, samples):
    with pytest.raises(ValueError, match="무음|빈 음성"):
        inspect_audio(pcm(tmp_path / "silent.wav", samples))


def test_clipped_audio_is_not_success(tmp_path):
    with pytest.raises(ValueError, match="클리핑"):
        inspect_audio(pcm(tmp_path / "clipped.wav", [32767] * 16000))


def test_excessive_internal_silence_is_rejected(tmp_path):
    samples = tone(.4) + [0] * 51200 + tone(.4)
    with pytest.raises(ValueError, match="침묵"):
        inspect_audio(pcm(tmp_path / "long-gap.wav", samples))


@pytest.mark.parametrize(("channels", "width", "samples"), [
    (2, 2, [1000, 1000] * 8000),
    (1, 1, [128] * 16000),
])
def test_audio_format_must_be_mono_pcm16(tmp_path, channels, width, samples):
    with pytest.raises(ValueError, match="모노 PCM 16bit"):
        inspect_audio(pcm(tmp_path / "unsupported.wav", samples, channels=channels, width=width))


def caption_fixture(tmp_path):
    config = Settings(root=tmp_path)
    if not config.font.is_file():
        pytest.skip("Korean font fixture requires the configured macOS font")
    script = Script(title="실제 구절 길이 검증", description="고정 음성 길이로 자막 시간 계산을 확인합니다.",
                    scenes=[Scene(headline="검증한 장면", narration="공식 자료로 확인한 내용을 전달합니다.")
                            for _ in range(4)])
    utterances = [{"scene": index, "duration": 6, "parts": [
        {"text": "금리 3.5% 확인", "spoken": "금리 삼점오 퍼센트 확인", "start": 0, "end": 2},
        {"text": "발표 자료를 확인합니다.", "spoken": "발표 자료를 확인합니다.", "start": 2, "end": 6},
    ]} for index in range(4)]
    (tmp_path / "utterances.json").write_text(json.dumps(utterances, ensure_ascii=False))
    return config, script


def test_caption_timing_uses_measured_phrase_audio_not_text_lengths(tmp_path):
    config, script = caption_fixture(tmp_path)
    cues = create_captions(script, tmp_path, [90] * 4, config, 720)
    assert len(cues) == 8
    # Raw 2s + 4s phrases are scaled to the actual 3s scene: 1s + 2s.
    assert [(cue["start"], cue["end"]) for cue in cues[:2]] == [(0, 1), (1, 3)]
    assert cues[2]["scene"] == 1 and cues[2]["scene_start"] == 3
    assert (cues[2]["start"], cues[2]["end"]) == (3, 4)
    assert cues[-1]["end"] == 12
    saved = json.loads((tmp_path / "captions.json").read_text())
    assert "not word alignment" in saved["alignment"]
    assert "00:00:12,000" in (tmp_path / "subtitles.srt").read_text()


def test_caption_png_highlights_numbers_inside_safe_area(tmp_path):
    config, script = caption_fixture(tmp_path)
    cues = create_captions(script, tmp_path, [90] * 4, config, 720)
    assert "3.5%" in cues[0]["highlight"]
    with Image.open(tmp_path / cues[0]["image"]) as image:
        assert image.mode == "RGBA" and image.size == (720, 1280)
        bounds = image.getchannel("A").getbbox()
        assert bounds[1] >= 920 and bounds[3] <= 1085
        assert image.getpixel((20, 1200))[3] == 0
        colors = image.getcolors(maxcolors=720 * 1280)
        assert any(color == (234, 255, 114, 255) for _, color in colors)


def test_manual_caption_timing_and_highlight_are_preserved(tmp_path):
    config, script = caption_fixture(tmp_path)
    script.scenes[0].caption_overrides = [CaptionEdit(text="주요 사실 확인", start=.5, end=1.5,
                                                    highlight=["사실"])]
    cues = create_captions(script, tmp_path, [90] * 4, config, 720)
    assert cues[0]["text"] == "주요 사실 확인"
    assert (cues[0]["start"], cues[0]["end"]) == (.5, 1.5)
    assert cues[0]["highlight"] == ["사실"]


def test_overlong_caption_requires_shorter_phrase(tmp_path):
    config, script = caption_fixture(tmp_path)
    script.scenes[0].caption_overrides = [CaptionEdit(text="가" * 80, start=0, end=3)]
    with pytest.raises(ValueError, match="두 줄"):
        create_captions(script, tmp_path, [90] * 4, config, 720)


@pytest.mark.parametrize(("start", "end"), [(0, 3.2), (2, 1)])
def test_caption_must_stay_within_actual_scene_duration(tmp_path, start, end):
    config, script = caption_fixture(tmp_path)
    script.scenes[0].caption_overrides = [CaptionEdit(text="시간 범위 확인", start=start, end=end)]
    with pytest.raises(ValueError, match="범위"):
        create_captions(script, tmp_path, [90] * 4, config, 720)
