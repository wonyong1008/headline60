import hashlib
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_cancel: ContextVar[threading.Event | None] = ContextVar("headline_cancel", default=None)


class JobCancelled(RuntimeError):
    pass


def check_cancel() -> None:
    event = _cancel.get()
    if event is not None and event.is_set():
        raise JobCancelled("사용자가 제작을 취소했습니다.")


@contextmanager
def cancellation(event: threading.Event):
    token = _cancel.set(event)
    try:
        yield
    finally:
        _cancel.reset(token)


def run(args: list[str], timeout: int = 180) -> str:
    """No shell interpolation; communicate() drains pipes and kills timed-out children."""
    try:
        check_cancel()
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        start = time.monotonic()
        try:
            while True:
                check_cancel()
                if time.monotonic() - start > timeout:
                    raise subprocess.TimeoutExpired(args, timeout)
                try:
                    stdout, stderr = process.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
    except FileNotFoundError as exc:
        raise RuntimeError(f"필수 도구가 없습니다: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"실행 시간 초과 ({timeout}초): {args[0]}") from exc
    if process.returncode:
        raise RuntimeError(f"{args[0]} 실패 ({process.returncode}): {stderr[-4000:]}")
    return stdout


def probe(path: Path) -> dict:
    return json.loads(run([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)
    ], timeout=30))


def duration(path: Path) -> float:
    return float(probe(path)["format"]["duration"])


def save_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def cache_path(root: Path, kind: str, key: str, suffix: str) -> Path:
    folder = root / "cache" / kind
    folder.mkdir(parents=True, exist_ok=True)
    return folder / (key + suffix)


def restore_cached(source: Path, target: Path) -> bool:
    check_cancel()
    if source.is_file() and source.stat().st_size > 0:
        shutil.copy2(source, target)
        return True
    return False


def store_cached(source: Path, target: Path) -> None:
    temp = target.with_suffix(target.suffix + ".tmp")
    shutil.copy2(source, temp)
    temp.replace(target)
