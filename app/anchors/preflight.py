"""Read-only prerequisites for a standalone five-second lip-sync experiment."""
import json
import math
import os
import platform
import shutil
from pathlib import Path

from PIL import Image

from app.config import Settings
from app.process import probe, run

MIN_FREE_BYTES = 12 * 1024**3
VENDOR = Path("runtime/lipsync/vendor/musetalk-mac/upstream")
MANIFEST = Path("data/models/musetalk-v15-required.json")


def diagnose(config: Settings, *, base_video: Path | None = None,
             reference_image: Path | None = None, manifest: Path | None = None) -> dict:
    """Inspect files and media headers only; never install, download or import a runtime."""
    root = config.root.resolve()
    checks = {}

    def check(key, ok, **details):
        checks[key] = {"ok": bool(ok), **details}

    check("platform", platform.system() == "Darwin" and platform.machine() == "arm64",
          system=platform.system(), architecture=platform.machine(),
          message="Apple Silicon macOS 환경이 필요합니다.")
    try:
        free = shutil.disk_usage(root).free
        check("disk", free >= MIN_FREE_BYTES, free_bytes=free, minimum_bytes=MIN_FREE_BYTES,
              message="최소 12GiB 여유 공간이 필요합니다.")
    except OSError as exc:
        check("disk", False, message=str(exc))
    for binary in ("ffmpeg", "ffprobe"):
        path = shutil.which(binary)
        try:
            version = run([path, "-version"], timeout=10).splitlines()[0] if path else None
            check(binary, bool(version), path=path, version=version,
                  message=f"실행 가능한 {binary}가 필요합니다.")
        except (RuntimeError, OSError, IndexError) as exc:
            check(binary, False, path=path, message=str(exc))
    runtime = root / "runtime/lipsync/.venv/bin/python"
    check("runtime_python", runtime.is_file() and os.access(runtime, os.X_OK),
          path=str(runtime), message="립싱크 전용 Python 실행 환경이 없습니다.")
    for name in ("vae.py", "unet.py"):
        path = root / VENDOR / "musetalk/models" / name
        check("source_" + name[:-3], path.is_file(), path=str(path),
              message=f"필수 upstream 소스 {name}가 없습니다.")
    manifest_path = Path(manifest) if manifest is not None else root / MANIFEST
    model_root = (root / VENDOR / "models").resolve()
    models = []
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        required = payload["required"]
        if not isinstance(required, list) or not required:
            raise ValueError("manifest.required는 비어 있지 않은 배열이어야 합니다.")
        for entry in required:
            item = {"path": entry} if isinstance(entry, str) else entry
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"]:
                raise ValueError("필수 모델 항목에는 상대 path가 필요합니다.")
            relative = Path(item["path"])
            path = (model_root / relative).resolve()
            if relative.is_absolute() or not path.is_relative_to(model_root):
                raise ValueError("필수 모델 경로가 upstream/models 밖을 가리킵니다.")
            expected = item.get("size_bytes")
            if expected is not None and (type(expected) is not int or expected <= 0):
                raise ValueError("size_bytes는 양의 정수여야 합니다.")
            size = path.stat().st_size if path.is_file() else None
            ok = size is not None and size > 0 and (expected is None or size == expected)
            models.append({"path": str(path), "relative_path": str(relative), "ok": ok,
                           "size_bytes": size, "expected_size_bytes": expected})
        check("model_manifest", True, path=str(manifest_path),
              message="필수 모델 목록을 읽었습니다. 가중치를 로드하지 않았습니다.")
        for index, model in enumerate(models):
            check(f"model_{index}", model["ok"], **{k: v for k, v in model.items() if k != "ok"},
                  message="필수 모델 파일이 없거나 크기가 일치하지 않습니다.")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        check("model_manifest", False, path=str(manifest_path), message=str(exc))
    if reference_image is None:
        check("reference_image", False, supplied=False,
              message="사용자가 지정한 가상 성인 진행자 참조 이미지가 없습니다.")
    else:
        try:
            path = Path(reference_image).resolve()
            if not path.is_file():
                raise ValueError("참조 이미지 파일이 없거나 일반 파일이 아닙니다.")
            with Image.open(path) as image:
                image.verify()
                image_format, dimensions = image.format, image.size
            check("reference_image", image_format in {"PNG", "JPEG", "WEBP"}, supplied=True,
                  path=str(path), format=image_format, dimensions=list(dimensions),
                  message="PNG/JPEG/WEBP 참조 이미지가 필요합니다. 인물·권리는 사람이 확인해야 합니다.")
        except (OSError, ValueError, RuntimeError, SyntaxError, Image.DecompressionBombError) as exc:
            check("reference_image", False, supplied=True, path=str(reference_image), message=str(exc))
    if base_video is None:
        check("base_video", False, supplied=False,
              message="사용자가 지정한 움직임이 있는 기본 인물 영상이 없습니다.")
    else:
        try:
            path = Path(base_video).resolve()
            if not path.is_file():
                raise ValueError("기본 영상 파일이 없거나 일반 파일이 아닙니다.")
            if not checks["ffprobe"]["ok"]:
                raise ValueError("ffprobe가 없어 기본 영상 형식을 검사할 수 없습니다.")
            info = probe(path)
            video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
            seconds = float(video.get("duration") or info.get("format", {}).get("duration", 0))
            width, height = int(video.get("width", 0)), int(video.get("height", 0))
            check("base_video", bool(video) and math.isfinite(seconds) and seconds >= 5
                  and width > 0 and height > 0,
                  supplied=True, path=str(path), duration_seconds=seconds, width=width, height=height,
                  codec=video.get("codec_name"),
                  message="5초 이상의 실제 인물 동작 영상이 필요합니다. 움직임·얼굴 품질은 사람이 확인합니다.")
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            check("base_video", False, supplied=True, path=str(base_video), message=str(exc))
    missing = [f"{key}: {value['message']}" for key, value in checks.items() if not value["ok"]]
    return {"ready": not missing, "root": str(root), "target_seconds": 5,
            "checks": checks, "models": models, "missing": missing,
            "limitations": "파일·미디어 헤더 사전 진단입니다. 의존 패키지 import·MPS 추론·얼굴/의상/립싱크 품질은 미검증입니다."}
