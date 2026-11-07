"""Optional experimental presenter, synthesized from this job's completed narration.

Importing the module never loads model weights. The pipeline's existing exclusive
lock serializes TTS, this isolated worker, and the renderer. No download is attempted.
"""
import json
import math
import time
from pathlib import Path
from uuid import uuid4

from PIL import Image

from app.config import Settings
from app.process import (
    JobCancelled,
    check_cancel,
    digest,
    file_digest,
    probe,
    restore_cached,
    run,
    save_json,
    store_cached,
)

ANCHOR_ID = "headline60-brown-adult-01"
VERSION = "experimental-presenter-v2-cutout-sway"
CROP_BOX = (228, 55, 495, 322)


def _approved(config: Settings, anchor_id: str) -> tuple[dict, Path]:
    if anchor_id != ANCHOR_ID:
        raise ValueError("지원하지 않는 AI 진행자입니다.")
    metadata = json.loads((config.data / "anchors/approved-face.json").read_text())
    if (metadata.get("anchor_id") != anchor_id or not metadata.get("fictional_adult")
            or not metadata.get("face_approved")
            or metadata.get("outfit_status") != "approved_for_lipsync_preview"):
        raise ValueError("승인된 가상 성인 진행자 이미지가 없습니다.")
    image = Path(metadata["outfit_candidate"]).resolve()
    if not image.is_relative_to((config.data / "anchors").resolve()) or not image.is_file():
        raise ValueError("승인된 진행자 이미지 경로를 확인하세요.")
    with Image.open(image) as asset:
        if asset.size != (720, 1280):
            raise ValueError("현재 실험 진행자는 승인된 720×1280 이미지가 필요합니다.")
    return metadata, image


def thumbnail_path(config: Settings, anchor_id: str) -> Path:
    """Return only the allowlisted approved still; API callers may serve this file."""
    return _approved(config, anchor_id)[1]


def profiles(config: Settings) -> list[dict]:
    try:
        _metadata, image = _approved(config, ANCHOR_ID)
        available = ((config.root / "runtime/lipsync/.venv/bin/python").is_file()
                     and (config.root / "runtime/lipsync/worker.py").is_file()
                     and (config.root / "runtime/lipsync/vendor/musetalk-mac/upstream/models").is_dir())
        return [{"anchor_id": ANCHOR_ID, "name": "AI 진행자 · 실험", "available": available,
                 "experimental": True, "production_ready": False,
                 "thumbnail_url": f"/api/anchors/{ANCHOR_ID}/thumbnail",
                 "thumbnail_path": str(image), "placement": "intro_outro",
                 "segment_seconds": 3, "fallback": "자료화면",
                 "description": "승인된 가상 성인 진행자를 장면 사진 위에 합성하고 영상마다 다른 의상을 입힙니다. 입 주변만 합성하며 몸 동작은 생성하지 않습니다."}]
    except (OSError, ValueError, KeyError):
        return []


def presenter_intervals(seconds: float) -> list[tuple[float, float]]:
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("유효한 영상 길이가 필요합니다.")
    length = min(3.0, math.floor(seconds / 2 * 25 + 1e-7) / 25)
    if length < .04:
        raise ValueError("진행자 구간이 너무 짧습니다.")
    # 25fps worker needs complete frames; closing placement remains exactly at
    # the original timeline's end. Narration itself is never shortened/retimed.
    return [(0.0, length), (seconds - length, seconds)]


def _attach(plan: dict, output: Path, intervals: list[tuple[float, float]]) -> None:
    matte = output.with_name("presenter-matte.mp4")
    cursor = 0.0
    for scene in plan["scenes"]:
        scene.pop("presenter_windows", None)
        duration = round(scene["duration"] * 30) / 30
        source_cursor = 0.0
        windows = []
        for start, end in intervals:
            left, right = max(cursor, start), min(cursor + duration, end)
            if right - left > 1e-6:
                windows.append({"path": str(output), "matte": str(matte) if matte.is_file() else None,
                                "start": round(left - cursor, 8), "end": round(right - cursor, 8),
                                "source_start": round(source_cursor + left - start, 8)})
            source_cursor += end - start
        if windows:
            scene["presenter_windows"] = windows
        cursor += duration


def _synthesize(config: Settings, audio: Path, image: Path, intervals, folder: Path) -> tuple[Path, dict]:
    runtime = config.root / "runtime/lipsync/.venv/bin/python"
    worker = config.root / "runtime/lipsync/worker.py"
    if not runtime.is_file() or not worker.is_file():
        raise RuntimeError("기존 로컬 립싱크 환경이 없습니다.")
    with Image.open(image) as source:
        source.crop(CROP_BOX).resize((512, 512), Image.Resampling.LANCZOS).save(folder / "portrait-crop.png")
    save_json(folder / "source.json", {"approved_image": str(image), "crop_box": CROP_BOX})
    clips = []
    for index, (start, end) in enumerate(intervals):
        target = folder / f"voice-{index}.wav"
        run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(audio), "-af",
             f"atrim=start={start:.8f}:end={end:.8f},asetpts=PTS-STARTPTS,apad,atrim=duration={end-start:.8f}",
             "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(target)])
        clips.append(target)
    from app.tts.engine import join_wavs
    join_wavs(clips, folder / "input.wav")
    seconds = round(sum(end - start for start, end in intervals), 8)
    request = {"reference_image": str(folder / "portrait-crop.png"),
               "audio_path": str(folder / "input.wav"), "output_path": str(folder / "mouth.mp4"),
               "metrics_path": str(folder / "lip-metrics.json"),
               "source_root": str(config.root / "runtime/lipsync/vendor/musetalk-mac/upstream"),
               "fps": 25, "target_seconds": seconds, "max_side": 512, "vae_fp16": True}
    save_json(folder / "request.json", request)
    run([str(runtime), str(worker), "--request", str(folder / "request.json")], timeout=600)
    metrics = json.loads((folder / "lip-metrics.json").read_text())
    if metrics.get("status") != "completed":
        raise RuntimeError("립싱크 합성이 완료되지 않았습니다.")
    run([str(runtime), str(config.root / "app/anchors/composite.py"),
         "--folder", str(folder), "--seconds", str(seconds), "--encoder", config.encoder], timeout=180)
    return folder / "presenter.mp4", metrics


def _verify(path: Path, seconds: float) -> None:
    info = probe(path)
    video = next(s for s in info["streams"] if s.get("codec_type") == "video")
    if ((video.get("width"), video.get("height"), video.get("avg_frame_rate")) != (720, 1280, "30/1")
            or abs(float(info["format"]["duration"]) - seconds) > .07):
        raise RuntimeError("진행자 클립 형식 검증에 실패했습니다.")
    run(["ffmpeg", "-v", "error", "-xerror", "-nostdin", "-i", str(path), "-f", "null", "-"])


def prepare_presenter(plan: dict, folder: Path, config: Settings, anchor_id: str | None) -> dict:
    """Mutate scene background windows, preserving all captions/chart values/audio.

    Caller must hold pipeline.exclusive(config), just as for TTS. Failure is
    recorded and returns the original footage plan. Cancellation always propagates.
    """
    for scene in plan["scenes"]:
        scene.pop("presenter_windows", None)
    status_path = folder / "anchor-status.json"
    status = {"anchor_id": anchor_id, "status": "disabled", "experimental": True, "version": VERSION}
    if anchor_id is None:
        plan["presenter"] = status
        save_json(status_path, status)
        return status
    started = time.perf_counter()
    try:
        check_cancel()
        _metadata, approved_image = _approved(config, anchor_id)
        from app.anchors.wardrobe import outfit_for_video
        look, image = outfit_for_video(config, approved_image)  # a different outfit per video
        status.update(look=look, outfit_image=str(image))
        audio = Path(plan["audio_path"])
        total = sum(round(scene["duration"] * 30) for scene in plan["scenes"]) / 30
        intervals = presenter_intervals(total)
        seconds = round(sum(end - start for start, end in intervals), 8)
        manifest = config.data / "models/musetalk-v15-required.json"
        model_manifest = file_digest(manifest) if manifest.is_file() else None
        key = digest([VERSION, anchor_id, file_digest(image), file_digest(audio), intervals, model_manifest,
                      file_digest(config.root / "runtime/lipsync/worker.py"),
                      file_digest(config.root / "app/anchors/composite.py"), config.encoder])
        cache = config.data / "cache/presenter" / key
        cache.mkdir(parents=True, exist_ok=True)
        target, target_matte = folder / "presenter.mp4", folder / "presenter-matte.mp4"
        cached = restore_cached(cache / "presenter.mp4", target) and restore_cached(cache / "presenter-matte.mp4", target_matte)
        metrics = {}
        if cached:
            try:
                _verify(target, seconds)
                _verify(target_matte, seconds)
                metrics = json.loads((cache / "metrics.json").read_text())
            except (OSError, ValueError, RuntimeError, StopIteration):
                cached = False
        status.update(status="running", cache_key=key)
        save_json(status_path, status)
        if not cached:
            attempt = folder / ("anchor-attempt-" + uuid4().hex[:8])
            attempt.mkdir()
            status["attempt_path"] = str(attempt)
            generated, metrics = _synthesize(config, audio, image, intervals, attempt)
            _verify(generated, seconds)
            _verify(generated.with_name("presenter-matte.mp4"), seconds)
            store_cached(generated, cache / "presenter.mp4")
            store_cached(generated.with_name("presenter-matte.mp4"), cache / "presenter-matte.mp4")
            save_json(cache / "metrics.json", metrics)
            restore_cached(cache / "presenter.mp4", target)
            restore_cached(cache / "presenter-matte.mp4", target_matte)
        _attach(plan, target, intervals)
        status.update(status="completed", cached=cached, intervals=intervals, metrics=metrics,
                      duration=seconds, body_motion_generated=False, static_portrait_mouth_only=True,
                      output_path=str(target), quality_review="awaiting_user_review")
    except JobCancelled:
        status.update(status="cancelled")
        raise
    except (OSError, ValueError, RuntimeError, KeyError, StopIteration) as exc:
        status.update(status="fallback", error=str(exc), fallback="자료화면")
        if status.get("attempt_path"):
            metrics_path = Path(status["attempt_path"]) / "lip-metrics.json"
            if metrics_path.is_file():
                try:
                    status["metrics"] = json.loads(metrics_path.read_text())
                except (OSError, ValueError):
                    pass
    finally:
        status["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        plan["presenter"] = status
        save_json(status_path, status)
    return status
