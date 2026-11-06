#!/bin/zsh
set -eu
cd "${0:A:h:h}"
uv run --frozen ruff check .
uv run --frozen pytest -q
uv run --frozen python -m app.cli doctor
