#!/bin/zsh
set -eu
cd "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  print -u2 "프로젝트 가상환경이 없습니다. uv sync --frozen을 먼저 실행하세요."
  exit 1
fi
exec .venv/bin/python scripts/services.py stop "$@"
