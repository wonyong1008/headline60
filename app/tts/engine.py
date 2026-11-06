"""Two explicit engines, phrase-level audio boundaries, no silent voice substitution."""
import json
import shutil
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.process import (
    cache_path,
    check_cancel,
    digest,
    restore_cached,
    run,
    save_json,
    store_cached,
)
from app.script.models import Scene, Script
from app.tts.macos import TTSError, korean_voices
from app.tts.quality import inspect_audio
from app.tts.text import spoken_parts


def signature(profile: dict, config: Settings) -> dict:
    result = {key: profile[key] for key in ("voice_profile_id", "engine", "model", "speaker",
              "language", "style_prompt", "speed", "generation_parameters")}
    if profile["engine"] == "macos":
        result["tts_rate"] = config.tts_rate
    if profile["engine"] == "qwen3_mlx":
        record = config.data / "models/qwen3-tts-1.7b-8bit/headline60-model.json"
        if not record.is_file():
            raise TTSError("로컬 TTS 모델이 없습니다. 설치·검증 후 사용하거나 macOS 음성을 선택하세요.")
        result["model_revision"] = json.loads(record.read_text())["revision"]
    result["phrasing"] = profile.get("phrasing", "phrase")
    return result


def make_pcm(raw: Path, target: Path, speed: float = 1) -> dict:
    converted = target.with_suffix(".raw.wav")
    run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-ac", "1", "-ar", "48000",
         "-c:a", "pcm_s16le", str(converted)])
    quality = inspect_audio(converted)
    filters = (f"atrim=start={quality['trim_start']:.5f}:end={quality['trim_end']:.5f},"
               f"asetpts=PTS-STARTPTS,atempo={speed},loudnorm=I=-18:TP=-2:LRA=7,aresample=48000")
    run(["ffmpeg", "-v", "error", "-y", "-i", str(converted), "-af", filters,
         "-ac", "1", "-c:a", "pcm_s16le", str(target)])
    converted.unlink(missing_ok=True)
    return {"before": quality, "after": inspect_audio(target)}


def join_wavs(paths: list[Path], output: Path) -> float:
    frames = 0
    with wave.open(str(output), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(48000)
        for path in paths:
            with wave.open(str(path), "rb") as source:
                if source.getnchannels() != 1 or source.getsampwidth() != 2 or source.getframerate() != 48000:
                    raise TTSError("구절 음성 형식이 일치하지 않습니다.")
                target.writeframes(source.readframes(source.getnframes()))
                frames += source.getnframes()
    return frames / 48000


@dataclass
class SpeechDocument:
    scenes: list[Scene]


def synthesize(script: Script | SpeechDocument, folder: Path, config: Settings, profile: dict) -> list[float]:
    start = time.perf_counter()
    fingerprint = signature(profile, config)
    if profile["engine"] == "macos" and profile["speaker"] not in korean_voices():
        raise TTSError("선택한 macOS 한국어 음성이 설치되지 않았습니다.")
    chunks, pending, reused = [], [], 0
    for scene_index, scene in enumerate(script.scenes):
        parts = spoken_parts(scene.narration, scene.tts_text)
        if profile.get("phrasing") == "continuous":
            parts = [{"text": scene.narration, "spoken": " ".join(part["spoken"] for part in parts)}]
        for part_index, part in enumerate(parts):
            key = digest(["voice-phrase-v3", fingerprint, part["spoken"]])
            cached = cache_path(config.data, "voice-phrases", key, ".wav")
            target = folder / f"utterance-{scene_index:02}-{part_index:02}.wav"
            raw = folder / f"raw-{scene_index:02}-{part_index:02}.wav"
            item = {**part, "id": f"{scene_index}-{part_index}", "scene": scene_index,
                    "target": target, "raw": raw, "cache": cached}
            chunks.append(item)
            if restore_cached(cached, target):
                inspect_audio(target)
                reused += 1
            else:
                pending.append(item)
    worker_metrics = {}
    if pending and profile["engine"] == "qwen3_mlx":
        runtime = config.root / "runtime/tts/.venv/bin/python"
        if not runtime.is_file():
            raise TTSError("MLX TTS 별도 실행 환경이 없습니다. macOS 음성을 직접 선택해 전환하세요.")
        if shutil.disk_usage(config.root).free < 2 * 1024**3:
            raise TTSError("TTS 작업에 필요한 디스크 여유 공간이 부족합니다.")
        request = {"model_path": str(config.data / "models/qwen3-tts-1.7b-8bit"),
                   "speaker": profile["speaker"], "style_prompt": profile["style_prompt"],
                   **profile["generation_parameters"],
                   "items": [{"id": item["id"], "text": item["spoken"], "output": str(item["raw"])} for item in pending],
                   "metrics_path": str(folder / "mlx-metrics.json"), "max_tokens": 450}
        save_json(folder / "tts-request.json", request)
        try:
            output = run([str(runtime), str(config.root / "runtime/tts/worker.py"),
                          str(folder / "tts-request.json")], timeout=1200)
        except RuntimeError as exc:
            raise TTSError(f"선택한 MLX 음성 생성 실패: {exc}. 자동으로 다른 목소리로 바꾸지 않았습니다.") from exc
        (folder / "tts-worker.log").write_text(output)
        worker_metrics = json.loads((folder / "mlx-metrics.json").read_text())
    quality_records = []
    for item in pending:
        check_cancel()
        if profile["engine"] == "macos":
            text_file = item["raw"].with_suffix(".txt")
            text_file.write_text(item["spoken"], encoding="utf-8")
            run(["say", "-v", profile["speaker"], "-r", str(config.tts_rate),
                 "-f", str(text_file), "-o", str(item["raw"]), "--data-format=LEI16@48000"], timeout=90)
        quality = make_pcm(item["raw"], item["target"], profile["speed"])
        quality_records.append({"id": item["id"], **quality})
        store_cached(item["target"], item["cache"])
    lengths, utterances = [], []
    for scene_index, scene in enumerate(script.scenes):
        members = [item for item in chunks if item["scene"] == scene_index]
        joined = folder / f"speech-{scene_index:02}.wav"
        length = join_wavs([item["target"] for item in members], joined)
        # Keep the existing renderer's filename/contract, using actual AIFF PCM.
        run(["ffmpeg", "-v", "error", "-y", "-i", str(joined), "-c:a", "pcm_s16be",
             str(folder / f"speech-{scene_index:02}.aiff")])
        lengths.append(length)
        cursor = 0.0
        parts = []
        for item in members:
            with wave.open(str(item["target"]), "rb") as source:
                duration = source.getnframes() / source.getframerate()
            parts.append({"text": item["text"], "spoken": item["spoken"], "start": cursor,
                          "end": cursor + duration})
            cursor += duration
        utterances.append({"scene": scene_index, "duration": length, "parts": parts})
    save_json(folder / "utterances.json", utterances)
    save_json(folder / "spoken-script.json", {"profile": fingerprint, "scenes": utterances})
    save_json(folder / "audio-quality.json", quality_records)
    elapsed = time.perf_counter() - start
    save_json(folder / "tts-metrics.json", {"engine": profile["engine"], "profile": fingerprint,
        "elapsed_seconds": elapsed, "audio_seconds": sum(lengths), "rtf": elapsed / sum(lengths),
        "reused_phrases": reused, "total_phrases": len(chunks), "worker": worker_metrics})
    return lengths
