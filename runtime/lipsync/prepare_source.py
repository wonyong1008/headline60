"""Reproduce the reviewed MuseTalk port source without installing weights or starting servers."""
import hashlib
import json
import subprocess
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT_REVISION = '7fd019315127d7f31e2e7f9853547ddceabbeb6e'
TENCENT_REVISION = '0a89dec45a0192b824e3cf4daf96c239440c5ed8'
PORT_URL = 'https://github.com/barnent1/musetalk-mac.git'
TARGET = ROOT / 'vendor/musetalk-mac'


def main():
    if TARGET.exists():
        raise RuntimeError('소스 폴더가 이미 있습니다. 기존 파일을 덮어쓰지 않습니다.')
    import shutil
    with tempfile.TemporaryDirectory(prefix='headline60-musetalk-') as tmp:
        source = Path(tmp) / 'source'
        subprocess.run(['git', 'init', str(source)], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(source), 'remote', 'add', 'origin', PORT_URL], check=True)
        subprocess.run(['git', '-C', str(source), 'fetch', '--depth', '1', 'origin', PORT_REVISION], check=True)
        subprocess.run(['git', '-C', str(source), 'checkout', '--detach', 'FETCH_HEAD'], check=True)
        shutil.rmtree(source / '.git')
        module_root = source / 'upstream/musetalk/models'
        module_root.mkdir(parents=True, exist_ok=True)
        modules = []
        for name in ['vae.py', 'unet.py']:
            url = f'https://raw.githubusercontent.com/TMElyralab/MuseTalk/{TENCENT_REVISION}/musetalk/models/{name}'
            with urllib.request.urlopen(url, timeout=30) as response:
                content = response.read()
            (module_root / name).write_bytes(content)
            modules.append({'name': name, 'url': url, 'sha256': hashlib.sha256(content).hexdigest()})
        provenance = {'port': PORT_URL, 'port_revision': PORT_REVISION,
                      'missing_modules_source_revision': TENCENT_REVISION, 'restored_modules': modules,
                      'note': 'Models loaded on CPU first, explicitly moved to MPS by isolated worker; original CLI/server not used.'}
        (source / 'headline60-source.json').write_text(json.dumps(provenance, indent=2) + '\n')
        TARGET.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), TARGET)
    print(TARGET)


if __name__ == '__main__':
    main()
