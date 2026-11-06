import hashlib
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from huggingface_hub import HfApi, hf_hub_url

root=Path(__file__).resolve().parents[2]
repo='black-forest-labs/FLUX.2-klein-4B'
revision='e7b7dc27f91deacad38e78976d1f2b499d76a294'
# Diffusers layout only: the root single-file checkpoint duplicates transformer/ (7.75GB).
patterns=['model_index.json','LICENSE.md','README.md','scheduler/*','text_encoder/*',
          'tokenizer/*','transformer/*','vae/*']
info=HfApi().model_info(repo, revision=revision, files_metadata=True, token=False)
wanted=[file for file in info.siblings
        if file.rfilename in patterns[:3] or file.rfilename.split('/')[0]+'/*' in patterns]
size=sum(file.size or 0 for file in wanted)
folder=root/'data/models/flux2-klein-4b'
missing=sum(file.size or 0 for file in wanted
            if not (folder/file.rfilename).is_file()
            or (folder/file.rfilename).stat().st_size != file.size)
free=shutil.disk_usage(root).free
if free < missing + 10 * 1024**3:
    raise SystemExit(f'저장 공간 부족: 남은 {missing} bytes + 여유 10GiB 필요, 현재 {free}')
print(json.dumps({'repository':repo,'revision':revision,'download_bytes':size,
                  'remaining_bytes':missing,'free_bytes':free}),flush=True)


def fetch(file) -> str:
    # The hub client stalled twice without resuming (random temp names per attempt).
    # curl appends to its own .part file, aborts below 1MB/s for 20s, and resumes.
    target=folder/file.rfilename
    if target.is_file() and target.stat().st_size == file.size:
        return f'{file.rfilename}: present'
    target.parent.mkdir(parents=True, exist_ok=True)
    part=target.with_name(target.name+'.part')
    url=hf_hub_url(repo, file.rfilename, revision=revision)
    for attempt in range(1, 41):
        subprocess.run(['curl','-sfL','-C','-','--speed-limit','1000000','--speed-time','20',
                        '--connect-timeout','20','-o',str(part),url], check=False)
        if part.is_file() and part.stat().st_size == file.size:
            part.replace(target)
            return f'{file.rfilename}: downloaded in {attempt} attempt(s)'
        if part.is_file() and part.stat().st_size > file.size:
            part.unlink()  # corrupt overrun of our own partial file; start that file again
    raise SystemExit(f'다운로드 실패: {file.rfilename}')


with ThreadPoolExecutor(max_workers=3) as pool:
    for line in pool.map(fetch, wanted):
        print(line, flush=True)
# Resumed partial files must match the published LFS hashes before any use.
for file in wanted:
    if file.lfs is None:
        continue
    digest=hashlib.sha256()
    with (folder/file.rfilename).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 24), b''):
            digest.update(block)
    if digest.hexdigest() != file.lfs.sha256:
        raise SystemExit(f'SHA256 불일치: {file.rfilename}')
record={'repository':repo,'revision':revision,'download_bytes':size,'local_path':str(folder),
        'license':'apache-2.0','model_variant':'FLUX.2 [klein] 4B distilled, diffusers layout',
        'excluded':'flux-2-klein-4b.safetensors (duplicate single-file checkpoint), sample jpgs',
        'sha256_verified':True}
(folder/'headline60-model.json').write_text(json.dumps(record,indent=2))
print(json.dumps(record,indent=2))
