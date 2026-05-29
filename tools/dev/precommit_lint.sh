#!/usr/bin/env bash
# Black / isort / flake8 using the same env as CI: versions from pyproject.toml [project.optional-dependencies.dev].
# Prefer repo .venv if present (pip install -e ".[dev]"); otherwise install into the active Python (e.g. pre-commit.ci).
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
if [[ -x .venv/bin/python ]]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="python"
  "$PY" -m pip install -q -e ".[dev]"
fi
"$PY" -m black --check src tests
"$PY" -m isort --check-only src tests
"$PY" -m flake8 src tests
