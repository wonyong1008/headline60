import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.config import Settings
from app.jobs.store import connect

MODEL = "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit"


class VoiceProfile(BaseModel):
    voice_profile_id: str = Field(pattern=r"^[a-z0-9-]{3,60}$")
    name: str
    engine: Literal["macos", "qwen3_mlx"]
    model: str
    speaker: str
    language: str = "Korean"
    style_prompt: str = ""
    previous_style_prompt: str | None = None  # plainer instruction the TTS worker retries with
    speed: float = Field(default=1, ge=0.8, le=1.2)
    generation_parameters: dict = Field(default_factory=lambda: {"seed": 42, "temperature": 0.9, "top_p": 1, "top_k": 50})
    phrasing: Literal["phrase", "continuous"] = "phrase"
    validated: bool = False
    sample_path: str | None = None
    note: str = "실제 자연스러움은 샘플을 듣고 확인하세요."


def seed(config: Settings) -> None:
    presets = [VoiceProfile(voice_profile_id="macos-yuna-news", name="macOS Yuna · 기본 뉴스",
        engine="macos", model="macOS say", speaker="Yuna", validated=True)]
    for key, name, style in [
        ("female-news", "Female News Anchor · Mature Sohee", "Speak as a mature adult female Korean news anchor. Use a composed, warm, authoritative tone in a slightly lower natural register. Use a measured cadence and stable sentence endings. Avoid youthful brightness, cuteness, breathiness, and exaggerated emotion."),
        ("calm-economy", "Calm Economic News · Sohee", "Read in Korean with a calm measured economic news delivery. Use clear diction and steady sentence endings."),
        ("bright-lifestyle", "Bright Lifestyle News · Sohee", "Read in Korean with a warm bright and clear lifestyle news delivery, without exaggeration."),
        ("energetic-trending", "Energetic Trending News · Sohee", "Read in Korean with a lively but controlled news delivery. Keep clear articulation and natural breathing."),
    ]:
        presets.append(VoiceProfile(voice_profile_id=key, name=name, engine="qwen3_mlx",
                                   model=MODEL, speaker="Sohee", style_prompt=style, speed=.96 if key == "female-news" else 1))
    presets.append(VoiceProfile(
        voice_profile_id="reels-male-ryan", name="릴스 · 남성 내레이터 Ryan", engine="qwen3_mlx", model=MODEL, speaker="ryan",
        style_prompt=("A charismatic Korean male narrator with crisp, clear diction and a confident, stylish delivery. "
                      "Warm mid-low voice, natural rhythm, energetic but never shouting. Speak like a popular short-form "
                      "creator telling a friend something surprising."),
        previous_style_prompt="Read in Korean clearly with natural rhythm and clean sentence endings.",
        generation_parameters={"seed": 42, "temperature": 0.65, "top_p": 0.9, "top_k": 50},
        phrasing="continuous", validated=True,
        note="2026-11-07 남성 화자 비교(ryan·aiden·uncle_fu) 후 등록. data/voice_samples/male-compare-20261107."))
    approved = config.data / "reels-voice.json"
    if approved.is_file():
        record = json.loads(approved.read_text())
        if record.get("status") == "user_approved_sample":
            value = {**record["profile"], "phrasing": "continuous"}
            presets.append(VoiceProfile.model_validate(value))
    with connect(config) as db:
        db.execute("CREATE TABLE IF NOT EXISTS voice_profiles (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS voice_samples (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        for profile in presets:
            db.execute("INSERT OR IGNORE INTO voice_profiles VALUES (?, ?)",
                       (profile.voice_profile_id, profile.model_dump_json()))
        if True:
            # Code-defined reels profiles follow their current definition (style prompt edits apply).
            for profile in presets:
                if profile.voice_profile_id in {"reels-natural-sohee", "reels-male-ryan"} and (
                        profile.voice_profile_id != "reels-natural-sohee" or approved.is_file()):
                    db.execute("UPDATE voice_profiles SET payload=? WHERE id=?",
                               (profile.model_dump_json(), profile.voice_profile_id))


def listing(config: Settings) -> list[dict]:
    seed(config)
    with connect(config) as db:
        profiles = [json.loads(row[0]) for row in db.execute("SELECT payload FROM voice_profiles")]
    runtime = config.root / "runtime/tts/.venv/bin/python"
    weights = config.root / "data/models/qwen3-tts-1.7b-8bit/model.safetensors"
    for profile in profiles:
        profile["available"] = profile["validated"] and (profile["engine"] == "macos" or (runtime.is_file() and weights.is_file()))
    return profiles


def get(config: Settings, key: str, require_available: bool = True) -> dict:
    match = next((p for p in listing(config) if p["voice_profile_id"] == key), None)
    if match is None or (require_available and not match["available"]):
        raise ValueError("선택한 음성 프로필이 설치·검증되지 않았습니다. macOS 음성을 직접 선택해 전환할 수 있습니다.")
    return VoiceProfile.model_validate({k: v for k, v in match.items() if k != "available"}).model_dump()


def mark_validated(config: Settings, key: str, sample_path: Path) -> None:
    profile = get(config, key, require_available=False)
    profile.update(validated=True, sample_path=str(sample_path))
    with connect(config) as db:
        db.execute("UPDATE voice_profiles SET payload=? WHERE id=?", (json.dumps(profile, ensure_ascii=False), key))


def sample_record(config: Settings, key: str, payload: dict | None = None) -> dict:
    seed(config)
    with connect(config) as db:
        if payload is not None:
            db.execute("INSERT INTO voice_samples VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                       (key, json.dumps(payload, ensure_ascii=False)))
        row = db.execute("SELECT payload FROM voice_samples WHERE id=?", (key,)).fetchone()
    if not row:
        raise KeyError("음성 샘플을 찾을 수 없습니다.")
    return json.loads(row[0])


def recover_samples(config: Settings) -> None:
    seed(config)
    with connect(config) as db:
        for row in db.execute("SELECT id, payload FROM voice_samples").fetchall():
            record = json.loads(row[1])
            if record["status"] in {"queued", "running"}:
                record.update(status="failed", error="서버 재시작으로 샘플 생성이 중단되었습니다. 다시 생성하세요.")
                db.execute("UPDATE voice_samples SET payload=? WHERE id=?",
                           (json.dumps(record, ensure_ascii=False), row[0]))
