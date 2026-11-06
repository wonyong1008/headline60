#!/bin/zsh
set -eu
cd "${0:A:h:h}"
if ! command -v uv >/dev/null; then
  print -u2 'uv가 없습니다. brew install uv 후 다시 실행하세요.'
  exit 1
fi
uv sync --frozen
exec uv run --frozen python -m app.serve
