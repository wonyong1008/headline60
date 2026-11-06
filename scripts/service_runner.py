"""Unique command marker remains in the process argv for safe ownership checks."""
import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
parser = argparse.ArgumentParser()
parser.add_argument("role", choices=["api", "ollama"])
parser.add_argument("--instance", required=True)
args = parser.parse_args()
os.chdir(ROOT)
signal.signal(signal.SIGHUP, signal.SIG_IGN)
if args.role == "api":
    from app.serve import main
    main()
else:
    from app.config import settings
    env = {**os.environ, "OLLAMA_HOST": settings.ollama_url.removeprefix("http://")}
    result = subprocess.run([str(ROOT / "scripts/ollama.sh")], env=env, check=False)
    raise SystemExit(result.returncode)
