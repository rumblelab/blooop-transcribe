#!/usr/bin/env bash
DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PY="$DIR/.venv/bin/python"

if [ ! -x "$VENV_PY" ]; then
  echo "✗ Missing virtualenv Python at: $VENV_PY"
  echo "  Run: ./setup.sh"
  exit 1
fi

unset PYTHONHOME
unset PYTHONPATH
export PYTHONNOUSERSITE=1
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# Prefer an explicit process name so Dock hover shows Blooop instead of python.
exec -a "Blooop" "$VENV_PY" "$DIR/bloop.py" "$@"
