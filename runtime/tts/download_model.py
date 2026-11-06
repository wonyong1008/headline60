import json
import shutil
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download

root=Path(__file__).resolve().parents[2]
repo='mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit'
info=HfApi().model_info(repo, files_metadata=True, token=False)
size=sum(file.size or 0 for file in info.siblings)
free=shutil.disk_usage(root).free
if free < size + 5 * 1024**3:
    raise SystemExit(f'저장 공간 부족: 모델 {size} bytes + 여유 5GiB 필요, 현재 {free}')
folder=root/'data/models/qwen3-tts-1.7b-8bit'
print(json.dumps({'repository':repo,'revision':info.sha,'download_bytes':size,'free_bytes':free}),flush=True)
snapshot_download(repo, revision=info.sha, local_dir=folder, token=False)
record={'repository':repo,'revision':info.sha,'download_bytes':size,'local_path':str(folder),
        'license':'apache-2.0','model_variant':'CustomVoice 1.7B 8bit'}
(folder/'headline60-model.json').write_text(json.dumps(record,indent=2))
print(json.dumps(record,indent=2))
