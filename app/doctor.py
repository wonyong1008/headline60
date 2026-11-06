import platform
import shutil
import sys

import httpx

from app.config import Settings
from app.process import run
from app.tts.macos import korean_voices


def diagnose(config: Settings) -> dict:
    checks = {}
    for binary in ("uv", "ffmpeg", "ffprobe", "git", "ollama"):
        path = shutil.which(binary)
        try:
            flag = "-version" if binary in {"ffmpeg", "ffprobe"} else "--version"
            checks[binary] = {"ok": bool(path), "path": path,
                              "version": run([binary, flag], timeout=10).splitlines()[0]}
        except RuntimeError as exc:
            checks[binary] = {"ok": False, "error": str(exc)}
    try:
        encoders = run(["ffmpeg", "-hide_banner", "-encoders"], timeout=10)
        checks["encoder"] = {"ok": config.encoder in encoders, "selected": config.encoder,
                              "videotoolbox_available": "h264_videotoolbox" in encoders}
    except RuntimeError as exc:
        checks["encoder"] = {"ok": False, "error": str(exc)}
    try:
        voices = korean_voices()
        checks["tts"] = {"ok": config.voice in voices, "selected": config.voice, "voices": voices}
    except RuntimeError as exc:
        checks["tts"] = {"ok": False, "error": str(exc)}
    try:
        response = httpx.get(config.ollama_url + "/api/tags", timeout=5, trust_env=False)
        response.raise_for_status()
        models = [entry["name"] for entry in response.json()["models"]]
        checks["ollama_api"] = {"ok": True, "url": config.ollama_url}
        checks["model"] = {"ok": config.model in models, "selected": config.model, "installed": models}
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        checks["ollama_api"] = {"ok": False, "error": str(exc), "fix": "./scripts/ollama.sh"}
        checks["model"] = {"ok": False, "fix": f"ollama pull {config.model}"}
    checks["font"] = {"ok": config.font.is_file(), "path": str(config.font)}
    return {
        "ready": all(c["ok"] for c in checks.values()), "checks": checks,
        "platform": platform.platform(), "architecture": platform.machine(),
        "python": sys.version.split()[0], "root": str(config.root),
        "free_disk_gib": round(shutil.disk_usage(config.root).free / 1024**3, 1),
    }
