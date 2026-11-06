#!/bin/zsh
set -eu
if ! command -v ollama >/dev/null; then
  print -u2 'Ollama가 없습니다. brew install ollama 후 다시 실행하세요.'
  exit 1
fi
export OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}"
export OLLAMA_NUM_PARALLEL=1
export OLLAMA_MAX_LOADED_MODELS=1
export OLLAMA_CONTEXT_LENGTH=4096
export OLLAMA_KEEP_ALIVE=0
export OLLAMA_NO_CLOUD=1
exec ollama serve
