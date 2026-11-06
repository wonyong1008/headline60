"""No launch-at-login, sudo, port-based kills, or automatic process adoption."""
import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.config import settings

STATE = ROOT / ".run"
LOGS = ROOT / "logs"
RUNNER = ROOT / "scripts/service_runner.py"


def ps(pid: int) -> dict | None:
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=,command="],
                            capture_output=True, text=True, check=False)
    parts = result.stdout.strip().split(None, 5)
    if result.returncode or len(parts) < 6:
        return None
    return {"started": " ".join(parts[:5]), "command": parts[5]}


def owned(record: dict | None, role: str) -> bool:
    if not isinstance(record, dict) or type(record.get("pid")) is not int or record["pid"] <= 1:
        return False
    token = record.get("instance", "")
    if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token) or record.get("role") != role:
        return False
    actual = ps(record["pid"])
    command = f"{ROOT / '.venv/bin/python'} {RUNNER} {role} --instance {token}"
    return bool(actual and actual == record.get("identity") and actual["command"] == command)


def load(role: str) -> dict | None:
    try:
        record = json.loads((STATE / f"{role}.json").read_text())
        return record if isinstance(record, dict) else None
    except (OSError, ValueError):
        return None


def healthy(role: str) -> bool:
    import httpx
    url = settings.ollama_url + "/api/tags" if role == "ollama" else f"http://127.0.0.1:{settings.port}/health"
    try:
        response = httpx.get(url, timeout=2, trust_env=False)
        data = response.json()
        return response.status_code == 200 and isinstance(data, dict) and (
            isinstance(data.get("models"), list) if role == "ollama"
            else data.get("service") == "headline60" and data.get("status") == "ok")
    except (httpx.HTTPError, ValueError):
        return False


def status() -> dict:
    result = {}
    for role in ("ollama", "api"):
        record = load(role)
        is_owned = owned(record, role)
        result[role] = {"healthy": healthy(role), "owned": is_owned,
                        "pid": record["pid"] if is_owned else None,
                        "stale_record": bool(record and not is_owned)}
    return result


def wait_health(role: str, timeout: int = 40) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if healthy(role):
            return
        if not owned(load(role), role):
            break
        time.sleep(0.2)
    log = LOGS / f"{role}.log"
    if log.is_file():
        with log.open("rb") as stream:
            stream.seek(max(0, log.stat().st_size - 4000))
            tail = stream.read().decode(errors="replace")
    else:
        tail = "로그 파일이 없습니다."
    raise RuntimeError(f"{role} 기동 실패. {LOGS / (role + '.log')} 확인\n{tail}")


def start_one(role: str) -> bool:
    record = load(role)
    if healthy(role):
        print(f"{role}: 이미 정상 실행 중 ({'직접 관리' if owned(record, role) else '외부 서버 재사용·종료 제외'})")
        return False
    if owned(record, role):
        print(f"{role}: 기존 관리 프로세스 health 대기")
        wait_health(role)
        return False
    # Ignore stale or tampered ownership data; never signal that PID.
    token = uuid4().hex
    with (LOGS / f"{role}.log").open("a") as stream:
        stream.write(f"\n--- HEADLINE 60 start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        stream.flush()
        child = subprocess.Popen([str(ROOT / ".venv/bin/python"), str(RUNNER), role,
                                  "--instance", token], cwd=ROOT, stdin=subprocess.DEVNULL,
                                 stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
    identity = None
    for _ in range(20):
        identity = ps(child.pid)
        if identity and str(RUNNER) in identity["command"]:
            break
        time.sleep(0.05)
    record = {"pid": child.pid, "role": role, "instance": token, "identity": identity}
    if not owned(record, role):
        # A Popen handle is direct creation evidence; never use an unverified PID-file record.
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
        raise RuntimeError(f"{role} 프로세스의 실행 명령을 확인하지 못했습니다. 로그를 확인하세요.")
    temporary = STATE / f"{role}.tmp"
    try:
        temporary.write_text(json.dumps(record, indent=2))
        temporary.replace(STATE / f"{role}.json")
    except OSError:
        if child.poll() is None:
            child.terminate()
        raise
    try:
        wait_health(role)
    except (RuntimeError, OSError):
        stop_one(role)
        raise
    print(f"{role}: 시작 완료 PID {child.pid}")
    return True


def descendants(pid: int) -> list[tuple[int, dict]]:
    result = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, check=False)
    pairs = [tuple(map(int, line.split())) for line in result.stdout.splitlines() if len(line.split()) == 2]
    included = {pid}
    while True:
        previous = len(included)
        included |= {child for child, parent in pairs if parent in included}
        if len(included) == previous:
            break
    return [(child, identity) for child in included - {pid} if (identity := ps(child))]


def stop_one(role: str) -> None:
    record = load(role)
    path = STATE / f"{role}.json"
    if not owned(record, role):
        print(f"{role}: 관리 소유 프로세스 없음. 외부 프로세스는 보존합니다.")
        if path.exists():
            path.unlink()
        return
    pid = record["pid"]
    children = descendants(pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        path.unlink(missing_ok=True)
        return
    deadline = time.monotonic() + 20
    while owned(record, role) and time.monotonic() < deadline:
        time.sleep(0.2)
    # Children captured while ancestry was proven are only signalled if identity still matches.
    for child, identity in reversed(children):
        if ps(child) == identity:
            try:
                os.kill(child, signal.SIGTERM)
            except ProcessLookupError:
                pass
    child_deadline = time.monotonic() + 3
    while any(ps(child) == identity for child, identity in children) and time.monotonic() < child_deadline:
        time.sleep(0.1)
    for child, identity in children:
        if ps(child) == identity:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
    if owned(record, role):
        os.kill(pid, signal.SIGKILL)
    path.unlink(missing_ok=True)
    print(f"{role}: 종료 완료 (외부 서버는 보존)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["start", "stop", "restart", "status", "logs"])
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("--lines", type=int, default=50)
    args = parser.parse_args()
    STATE.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    try:
        if args.command == "logs":
            from collections import deque
            for role in ("api", "ollama"):
                path = LOGS / f"{role}.log"
                path.touch(exist_ok=True)
                print(f"\n[{role}] {path}", flush=True)
                with path.open(errors="replace") as stream:
                    print("".join(deque(stream, maxlen=max(1, args.lines))), end="")
            if args.follow:
                subprocess.run(["tail", "-n", "0", "-f", str(LOGS / "api.log"), str(LOGS / "ollama.log")], check=False)
            return 0
        if args.command == "status":
            state = status()
            print(json.dumps(state, ensure_ascii=False, indent=2))
            return 0 if all(item["healthy"] for item in state.values()) else 1
        with (STATE / "services.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if args.command in {"stop", "restart"}:
                stop_one("api")
                stop_one("ollama")
            if args.command in {"start", "restart"}:
                started = []
                try:
                    for role in ("ollama", "api"):
                        if start_one(role):
                            started.append(role)
                except (RuntimeError, OSError):
                    for role in reversed(started):
                        stop_one(role)
                    raise
                print(f"관리 화면: http://127.0.0.1:{settings.port}")
                if not args.no_browser:
                    subprocess.run(["open", f"http://127.0.0.1:{settings.port}"], check=False)
        return 0
    except (RuntimeError, OSError) as exc:
        print(f"서버 관리 오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
