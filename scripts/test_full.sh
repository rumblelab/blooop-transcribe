#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PY="$ROOT_DIR/.venv/bin/python"

if [[ ! -x "$VENV_PY" ]]; then
  echo "✗ Missing venv Python: $VENV_PY"
  echo "  Run: ./setup.sh"
  exit 1
fi

echo "→ Shell syntax checks"
bash -n "$ROOT_DIR/run.sh"
bash -n "$ROOT_DIR/setup.sh"
bash -n "$ROOT_DIR/build_app.sh"
for script in "$ROOT_DIR"/scripts/*.sh; do
  bash -n "$script"
done

echo "→ Python compile checks"
"$VENV_PY" -m py_compile "$ROOT_DIR/bloop.py" "$ROOT_DIR/history_ui.py"

echo "→ Unit tests"
"$VENV_PY" -m unittest discover -s "$ROOT_DIR/tests" -p "test_*.py" -v

if [[ "${BLOOOP_SKIP_CLI_SMOKE:-0}" != "1" ]]; then
  echo "→ CLI smoke tests"
  "$VENV_PY" "$ROOT_DIR/bloop.py" --help >/dev/null
  "$VENV_PY" "$ROOT_DIR/bloop.py" --history 1 >/dev/null
fi

echo "✓ All checks passed"
