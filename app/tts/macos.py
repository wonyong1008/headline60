import re
from pathlib import Path

from app.config import Settings
from app.process import cache_path, digest, duration, restore_cached, run, save_json, store_cached
from app.script.models import Script


class TTSError(RuntimeError):
    pass


def korean_voices() -> list[str]:
    lines = run(["say", "-v", "?"], timeout=30).splitlines()
    return [line.split("ko_KR")[0].strip() for line in lines if re.search(r"\bko_KR\b", line)]


def synthesize(script: Script, folder: Path, config: Settings) -> list[float]:
    if config.voice not in korean_voices():
        raise TTSError(
            f"한국어 음성 '{config.voice}'가 없습니다. 시스템 설정 → 손쉬운 사용 → "
            "읽기 및 말하기 → 시스템 음성에서 한국어 음성을 추가한 뒤 say -v '?'로 확인하세요."
        )
    lengths = []
    reused = 0
    for index, scene in enumerate(script.scenes):
        text_file = folder / f"speech-{index:02}.txt"
        audio = folder / f"speech-{index:02}.aiff"
        text_file.write_text(scene.narration, encoding="utf-8")
        key = digest(["say-v1", scene.narration, config.voice, config.tts_rate])
        cached = cache_path(config.data, "tts", key, ".aiff")
        if restore_cached(cached, audio):
            reused += 1
        else:
            run(["say", "-v", config.voice, "-r", str(config.tts_rate),
                 "-f", str(text_file), "-o", str(audio)], timeout=90)
        seconds = duration(audio)
        if seconds < 0.1:
            raise TTSError(f"음성 생성 결과가 비어 있습니다: {audio}")
        lengths.append(seconds)
        store_cached(audio, cached)
    save_json(folder / "tts-cache.json", {"reused": reused, "total": len(lengths)})
    return lengths
