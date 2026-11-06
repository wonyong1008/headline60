"""Optional RSS sampler; sums observed processes, not physical/unified GPU memory."""
import argparse
import json
import sqlite3
import subprocess
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--pid', type=int, required=True)
parser.add_argument('--root', type=Path, required=True)
parser.add_argument('--stop', type=Path, required=True)
parser.add_argument('--seconds', type=int, default=600)
args = parser.parse_args()
started = time.monotonic()
peaks: dict[str, float] = {}
counts: dict[str, int] = {}
while time.monotonic() - started < args.seconds and not args.stop.exists():
    process = subprocess.run(['ps', '-axo', 'pid=,ppid=,rss=,comm='], capture_output=True,
                             text=True, check=False, timeout=5)
    rows = []
    for line in process.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4:
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3]))
    included = {args.pid} | {pid for pid, _, _, name in rows if 'ollama' in name.lower()}
    while True:
        previous = len(included)
        included |= {pid for pid, parent, _, _ in rows if parent in included}
        if len(included) == previous:
            break
    phase = 'idle'
    with sqlite3.connect(f'file:{args.root / "data/jobs.sqlite3"}?mode=ro', uri=True) as db:
        draft = db.execute('SELECT status FROM drafts ORDER BY updated_at DESC LIMIT 1').fetchone()
        if draft and draft[0] == 'generating':
            phase = 'llm'
        else:
            job = db.execute("SELECT stage FROM jobs WHERE status='running' ORDER BY updated_at DESC LIMIT 1").fetchone()
            if job:
                phase = job[0]
    rss_mb = sum(rss for pid, _, rss, _ in rows if pid in included) / 1024
    peaks[phase] = round(max(peaks.get(phase, 0), rss_mb), 1)
    counts[phase] = counts.get(phase, 0) + 1
    time.sleep(0.3)
result = {'elapsed_seconds': round(time.monotonic() - started, 2),
          'observed_rss_peak_mb': peaks, 'sample_count': counts,
          'method': 'ps RSS sum for app PID descendants plus Ollama processes, sampled ~0.3s',
          'limitations': 'Shared pages may be double-counted; this is not physical unified/Metal memory. Short stages may be missed.'}
folder = args.root / 'data/performance'
folder.mkdir(parents=True, exist_ok=True)
path = folder / ('resources-' + time.strftime('%Y%m%dT%H%M%S') + '.json')
path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
print(path)
print(json.dumps(result, indent=2, ensure_ascii=False))
