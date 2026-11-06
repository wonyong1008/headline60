# Isolated MuseTalk smoke test

This runtime is separate from the application and MLX TTS environments. No network server or paid API is used. See ../../docs/PHASE3_VALIDATION.md for measured results and limitations.

Recreate only when needed:

```sh
.venv/bin/python runtime/lipsync/prepare_source.py
uv sync --project runtime/lipsync --frozen --python .venv/bin/python
runtime/lipsync/.venv/bin/python runtime/lipsync/download_models.py
```

The source preparer preserves existing directories. The downloader checks 12GiB free disk and fixed model sizes/SHA256. Re-downloading requires the user's authorization for a large model bundle.

Inference uses a JSON request and local files only. Always choose a fresh output/benchmark folder. Existing inputs and outputs cannot be overwritten by metrics paths.

```sh
runtime/lipsync/.venv/bin/python runtime/lipsync/worker.py --request PATH_TO_FRESH_REQUEST.json
```

Successful media decoding is not an endorsement of natural movement, phoneme alignment, or identity consistency. The current portrait experiment only changes the mouth region. Do not advertise it as a validated production presenter.

If the user later requests cleanup, the isolated .venv and vendor directories can be removed independently. Preserve experiment media, metrics/provenance and these reproducibility scripts as needed; do not remove other project environments or shared caches.
