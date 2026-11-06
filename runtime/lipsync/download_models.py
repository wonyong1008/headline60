"""Download only the approved MuseTalk inference bundle, checking fixed file sizes/hashes."""
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEST = ROOT / 'runtime/lipsync/vendor/musetalk-mac/upstream/models'
FILES = [
 ('musetalkV15/musetalk.json', 'TMElyralab/MuseTalk', '2bcb936e2fddb4d86db4c62fd45b387d0c061571', 'musetalkV15/musetalk.json', 748, None),
 ('musetalkV15/unet.pth', 'TMElyralab/MuseTalk', '2bcb936e2fddb4d86db4c62fd45b387d0c061571', 'musetalkV15/unet.pth', 3400074924, '7ebf6c98c181e20838e4c0054e96e944ac60d5d692cc01db42839fe11b787007'),
 ('sd-vae/config.json', 'stabilityai/sd-vae-ft-mse', '31f26fdeee1355a5c34592e401dd41e45d25a493', 'config.json', 547, None),
 ('sd-vae/diffusion_pytorch_model.bin', 'stabilityai/sd-vae-ft-mse', '31f26fdeee1355a5c34592e401dd41e45d25a493', 'diffusion_pytorch_model.bin', 334707217, '1b4889b6b1d4ce7ae320a02dedaeff1780ad77d415ea0d744b476155c6377ddc'),
 ('whisper/config.json', 'openai/whisper-tiny', '169d4a4341b33bc18d8881c4b69c2e104e1cc0af', 'config.json', 1983, None),
 ('whisper/preprocessor_config.json', 'openai/whisper-tiny', '169d4a4341b33bc18d8881c4b69c2e104e1cc0af', 'preprocessor_config.json', 184990, None),
 ('whisper/pytorch_model.bin', 'openai/whisper-tiny', '169d4a4341b33bc18d8881c4b69c2e104e1cc0af', 'pytorch_model.bin', 151095027, '9607f98a2b22d9e229ae43c52ecea79dcede9e0c5cfae67e8da6eda86d8aac1d'),
]


def sha(path):
    hasher = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            hasher.update(block)
    return hasher.hexdigest()


def main():
    if shutil.disk_usage(ROOT).free < 12 * 1024**3:
        raise RuntimeError('MuseTalk 설치에 필요한 12GiB 여유 공간이 없습니다.')
    records = []
    for relative, repo, revision, name, size, expected_sha in FILES:
        target = DEST / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        url = f'https://huggingface.co/{repo}/resolve/{revision}/{name}'
        if not target.is_file() or target.stat().st_size != size or (expected_sha and sha(target) != expected_sha):
            print(f'Download {relative}: {size:,} bytes', flush=True)
            part = target.with_suffix(target.suffix + '.partial')
            with urllib.request.urlopen(url, timeout=90) as response, part.open('wb') as handle:
                shutil.copyfileobj(response, handle, length=1024 * 1024)
            if part.stat().st_size != size or (expected_sha and sha(part) != expected_sha):
                raise RuntimeError(f'파일 크기 또는 SHA256 불일치: {relative}')
            part.replace(target)
        records.append({'path': relative, 'size_bytes': size, 'sha256': sha(target), 'url': url, 'revision': revision})
    for relative, url, drive, size, expected_sha in [
        ('face-parse-bisent/79999_iter.pth', 'https://drive.google.com/uc?id=154JgKpzCPW82qINcVieuPH3fZ2e0P812', True, 53289463, '468e13ca13a9b43cc0881a9f99083a430e9c0a38abd935431d1c28ee94b26567'),
        ('face-parse-bisent/resnet18-5c106cde.pth', 'https://download.pytorch.org/models/resnet18-5c106cde.pth', False, 46827520, '5c106cde386e87d4033832f2996f5493238eda96ccf559d1d62760c4de0613f8'),
    ]:
        target = DEST / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.stat().st_size != size or sha(target) != expected_sha:
            print(f'Download {relative}', flush=True)
            part = target.with_suffix(target.suffix + '.partial')
            if drive:
                subprocess.run([sys.executable, '-m', 'gdown', url, '-O', str(part)], check=True)
            else:
                with urllib.request.urlopen(url, timeout=90) as response, part.open('wb') as handle:
                    shutil.copyfileobj(response, handle, length=1024 * 1024)
            if part.stat().st_size != size or sha(part) != expected_sha:
                raise RuntimeError(f'모델 대신 작은 오류 페이지가 수신되었습니다: {relative}')
            part.replace(target)
        records.append({'path': relative, 'size_bytes': target.stat().st_size, 'sha256': sha(target), 'url': url})
    manifest = {'required': records, 'total_bytes': sum(r['size_bytes'] for r in records),
                'note': 'One approved MuseTalk inference bundle; not a lipsync quality validation.'}
    destination = ROOT / 'data/models/musetalk-v15-required.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'Complete: {manifest["total_bytes"]:,} bytes; {destination}', flush=True)


if __name__ == '__main__':
    main()
