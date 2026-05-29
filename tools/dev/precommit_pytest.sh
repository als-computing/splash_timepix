#!/usr/bin/env bash
# Default pytest (pyproject [tool.pytest.ini_options] only). Tooling from pyproject.toml [dev] + [test].
# Prefer repo .venv; otherwise pip install -e ".[dev,test]" (e.g. pre-commit.ci).
#
# Local pre-commit skips the @pytest.mark.slow suite (CPS x TDC burstiness matrix,
# end-to-end workflow integration, long simulator streams) so commits stay snappy.
# CI runs the full collection via `pytest -m ""` — see .pre-commit-config.yaml.
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
if [[ -x .venv/bin/python ]]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="python"
  "$PY" -m pip install -q -e ".[dev,test]"
fi
exec "$PY" -m pytest -m "not slow"
