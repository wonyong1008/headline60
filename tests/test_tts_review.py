"""Pronunciation and cache regressions; no model, network or media processes."""
import json

import pytest

from app.config import Settings
from app.process import digest
from app.tts.engine import signature
from app.tts.profiles import VoiceProfile
from app.tts.text import normalize, read_number, spoken_parts


@pytest.mark.parametrize(("text", "spoken"), [
    ("금리는 0.25%p 올랐습니다.", "금리는 영점이오 퍼센트포인트 올랐습니다."),
    ("-0.25%p로 조정", "마이너스 영점이오 퍼센트포인트로 조정"),
    ("+0.25%P입니다.", "플러스 영점이오 퍼센트포인트입니다."),
    ("−0.05퍼센트포인트를 기록", "마이너스 영점영오 퍼센트포인트를 기록"),
    ("0.5퍼센트포인트보다", "영점오 퍼센트포인트보다"),
    ("평균 속도는 60km/h입니다.", "평균 속도는 시속 육십 킬로미터입니다."),
    ("80㎞/h로 달립니다.", "시속 팔십 킬로미터로 달립니다."),
    ("시속 60km/h를 유지", "시속 육십 킬로미터를 유지"),
    ("시속 60㎞/h보다", "시속 육십 킬로미터보다"),
    ("주행거리 1,200㎞입니다.", "주행거리 천이백 킬로미터입니다."),
    ("용량은 +2.4mg입니다.", "용량은 플러스 이점사 밀리그램입니다."),
    ("변동률은 −0.5%입니다.", "변동률은 마이너스 영점오 퍼센트입니다."),
])
def test_compound_units_signs_and_korean_particles(text, spoken):
    assert normalize(text) == spoken


@pytest.mark.parametrize("text", [
    "농도는 12mg/dL입니다.", "속도는 10km/hours입니다.",
    "하중은 20kgf입니다.", "표기는 5%point입니다.",
    "표기는 +0.25%point입니다.", "모델 QX2026km입니다.",
    "단위는 1.5km/hs입니다.", "코드는 20mg2입니다.",
    "면적은 10km²입니다.", "단위는 3kg·m입니다.",
    "가속도는 60km/h²입니다.",
])
def test_unknown_units_and_model_ids_are_not_partially_rewritten(text):
    assert normalize(text) == text


def test_compound_normalization_preserves_original_display_and_manual_override():
    original = "속도는 60km/h이고 금리는 0.25%p 올랐습니다."
    automatic = spoken_parts(original)
    assert " ".join(p["text"] for p in automatic) == original
    assert "시속 육십 킬로미터이고" in " ".join(p["spoken"] for p in automatic)
    overrides = "\n".join("사용자가 직접 검토한 발음입니다." for _ in automatic)
    manual = spoken_parts(original, overrides)
    assert [p["text"] for p in manual] == [p["text"] for p in automatic]
    assert all(p["spoken"] == "사용자가 직접 검토한 발음입니다." for p in manual)


def test_signed_number_reading():
    assert read_number("+1,250") == "플러스 천이백오십"
    assert read_number("−0.05") == "마이너스 영점영오"


def voice_profile(engine):
    return VoiceProfile(voice_profile_id="review-test", name="검증",
                        engine=engine, model="test", speaker="Yuna").model_dump()


def test_macos_rate_changes_phrase_cache_identity(tmp_path):
    profile = voice_profile("macos")
    slow = signature(profile, Settings(root=tmp_path, tts_rate=160))
    fast = signature(profile, Settings(root=tmp_path, tts_rate=220))
    assert slow["tts_rate"] == 160 and fast["tts_rate"] == 220
    assert digest(["voice-phrase-v3", slow, "같은 문장입니다."]) != digest(
        ["voice-phrase-v3", fast, "같은 문장입니다."])
    assert slow == signature(profile, Settings(root=tmp_path, tts_rate=160))


def test_mlx_cache_is_independent_of_unused_macos_rate(tmp_path):
    record = tmp_path / "data/models/qwen3-tts-1.7b-8bit/headline60-model.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"revision": "fixed-test-revision"}))
    profile = voice_profile("qwen3_mlx")
    slow = signature(profile, Settings(root=tmp_path, tts_rate=160))
    fast = signature(profile, Settings(root=tmp_path, tts_rate=220))
    assert slow == fast
    assert "tts_rate" not in slow
    assert slow["model_revision"] == "fixed-test-revision"
