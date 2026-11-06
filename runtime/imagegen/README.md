# Isolated outfit-draft image editing

Separate from the app, MLX TTS and MuseTalk environments. Local only: no paid API or server.
One model is installed: FLUX.2 [klein] 4B (Apache-2.0) run by mflux 0.23.0 (MIT). See ../../docs/ASSETS.md.

```sh
uv sync --project runtime/imagegen --python .venv/bin/python
runtime/imagegen/.venv/bin/python runtime/imagegen/download_model.py   # ~16GB, fixed revision
runtime/imagegen/.venv/bin/python runtime/imagegen/outfit_draft.py --seed 7
runtime/lipsync/.venv/bin/python runtime/lipsync/face_compare.py \
  --reference data/anchors/benchmark-input/reference.png \
  --draft data/anchors/outfit-drafts/draft-…/draft.png --output data/anchors/outfit-drafts/draft-…/compare.png
```

Each draft gets a fresh `data/anchors/outfit-drafts/draft-<timestamp>/` folder (request, metrics, mflux log, image) and never overwrites earlier ones. A draft is an experiment, not a validated anchor/outfit profile. The face sheet is a visual aid, not biometric identity verification. Do not run it concurrently with TTS, Ollama or MuseTalk inference.

Cleanup when requested: remove `runtime/imagegen/.venv/` and `data/models/flux2-klein-4b/`; other environments are unaffected.
