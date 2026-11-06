"""Read-only projections of local lip-sync benchmarks; never production profiles."""
import json
import math
import re
from datetime import datetime
from pathlib import Path

from app.config import Settings

EXPERIMENT_ID = re.compile(r"lipsync-(5s|10s|15s)-(\d{8}T\d{6})\Z")


def experiment_directory(config: Settings, key: str) -> Path:
    match = EXPERIMENT_ID.fullmatch(key)
    if not match:
        raise ValueError("잘못된 진행자 실험 ID입니다.")
    try:
        datetime.strptime(match[2], "%Y%m%dT%H%M%S")
    except ValueError:
        raise ValueError("잘못된 진행자 실험 날짜입니다.") from None
    data = config.root / "data"
    base = data / "benchmarks"
    folder = base / key
    if data.is_symlink() or base.is_symlink() or folder.is_symlink():
        raise ValueError("실험 파일의 심볼릭 링크는 제공하지 않습니다.")
    if not folder.resolve().is_relative_to(base.resolve()):
        raise ValueError("허용되지 않은 실험 경로입니다.")
    return folder


def video_path(config: Settings, key: str) -> Path:
    folder = experiment_directory(config, key)
    path = folder / "output.mp4"
    if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
        raise ValueError("허용되지 않은 실험 영상입니다.")
    if not path.is_file():
        raise FileNotFoundError("실험 영상이 없습니다.")
    return path


def number(data: dict, key: str) -> float | None:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def listing(config: Settings) -> list[dict]:
    base = config.root / "data/benchmarks"
    if not base.is_dir() or base.is_symlink() or base.parent.is_symlink():
        return []
    result = []
    for entry in sorted(base.iterdir(), reverse=True):
        if not EXPERIMENT_ID.fullmatch(entry.name):
            continue
        try:
            folder = experiment_directory(config, entry.name)
            metadata = folder / "metrics.json"
            if metadata.is_symlink() or not metadata.is_file() or metadata.stat().st_size > 1_000_000:
                continue
            data = json.loads(metadata.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            try:
                output = video_path(config, entry.name)
            except (ValueError, FileNotFoundError):
                output = None
            status = data.get("status")
            result.append({
                "id": entry.name,
                "status": status if status in {"completed", "running", "failed", "cancelled"} else "unknown",
                "seconds": number(data, "target_seconds"),
                "width": number(data, "width"), "height": number(data, "height"),
                "fps": number(data, "fps"), "frames": number(data, "rendered_frames"),
                "total_seconds": number(data, "total_seconds"),
                "model_load_seconds": number(data, "model_load_seconds"),
                "inference_seconds": number(data, "inference_seconds"),
                "render_seconds": number(data, "render_seconds"),
                "peak_rss_bytes": number(data, "peak_rss_bytes"),
                "mps_observed_driver_bytes": number(data, "mps_max_observed_driver_bytes"),
                "video_url": f"/anchor-experiments/{entry.name}/output.mp4" if output else None,
                "static_portrait_mouth_only": data.get("static_portrait_mouth_only") is True,
                "body_motion_generated": data.get("body_motion_generated") is True,
                "production_ready": False,
            })
        except (ValueError, OSError, UnicodeError):
            continue
        if len(result) == 50:
            break
    return result
