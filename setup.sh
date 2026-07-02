#!/usr/bin/env bash
# bloop_flow setup  --  creates an isolated venv to avoid Anaconda conflicts
set -euo pipefail

PRELOAD_MODEL=1
OPEN_SETTINGS=1

for arg in "$@"; do
    case "$arg" in
        --skip-model-preload|--no-preload)
            PRELOAD_MODEL=0
            ;;
        --skip-open-settings|--no-open-settings|--ci)
            OPEN_SETTINGS=0
            ;;
        --help|-h)
            cat <<'EOF'
Usage: ./setup.sh [options]

Options:
  --skip-model-preload         Skip Whisper model pre-download step
  --skip-open-settings         Do not open macOS Privacy settings panels
  --help                       Show this help
EOF
            exit 0
            ;;
        *)
            echo "✗ Unknown option: $arg"
            echo "  Run: ./setup.sh --help"
            exit 2
            ;;
    esac
done

DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$DIR/.venv"
VENV_PY="$VENV/bin/python"
LOCAL_BIN="$HOME/.local/bin"
BLOOOP_CMD="$LOCAL_BIN/blooop"
BLOOP_COMPAT_CMD="$LOCAL_BIN/bloop"
SHELL_NAME="$(basename "${SHELL:-zsh}")"
if [ "$SHELL_NAME" = "zsh" ]; then
    RC_FILE="$HOME/.zshrc"
elif [ "$SHELL_NAME" = "bash" ]; then
    RC_FILE="$HOME/.bashrc"
else
    RC_FILE="$HOME/.profile"
fi

if [ "$(uname -s)" != "Darwin" ]; then
    echo "✗ bloop_flow currently supports macOS only."
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "✗ python3 is required but was not found in PATH."
    exit 1
fi

echo "────────────────────────────────────────"
echo "  bloop_flow setup"
echo "────────────────────────────────────────"
echo

# ── Virtual environment ───────────────────────────────────────────────────────
if [ ! -d "$VENV" ]; then
    echo "→ Creating virtual environment at .venv …"
    python3 -m venv "$VENV"
else
    echo "→ Virtual environment already exists, reusing."
fi

# ── Dependencies ──────────────────────────────────────────────────────────────
echo "→ Installing Python dependencies…"
"$VENV_PY" -m pip install --upgrade pip -q
"$VENV_PY" -m pip install -r "$DIR/requirements.txt"

# Keep NumPy on the 1.x ABI for current mlx_whisper/numba compatibility.
"$VENV_PY" -m pip install --upgrade "numpy<2"

# ── CLI command (blooop) ──────────────────────────────────────────────────────
echo
echo "→ Installing 'blooop' command…"
mkdir -p "$LOCAL_BIN"
cat > "$BLOOOP_CMD" <<EOF
#!/usr/bin/env bash
unset PYTHONHOME PYTHONPATH
export PYTHONNOUSERSITE=1
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:\$PATH"
exec "$VENV_PY" "$DIR/bloop.py" "\$@"
EOF
chmod +x "$BLOOOP_CMD"

# Backward-compatible wrapper: existing `bloop` users still work.
cat > "$BLOOP_COMPAT_CMD" <<EOF
#!/usr/bin/env bash
exec "$BLOOOP_CMD" "\$@"
EOF
chmod +x "$BLOOP_COMPAT_CMD"

PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
if [ -f "$RC_FILE" ]; then
    if ! grep -Fqs "$PATH_LINE" "$RC_FILE"; then
        printf '\n%s\n' "$PATH_LINE" >> "$RC_FILE"
        PATH_UPDATED=1
    else
        PATH_UPDATED=0
    fi
else
    printf '%s\n' "$PATH_LINE" > "$RC_FILE"
    PATH_UPDATED=1
fi

case ":$PATH:" in
    *":$LOCAL_BIN:"*) ;;
    *) export PATH="$LOCAL_BIN:$PATH" ;;
esac

if [ "$PATH_UPDATED" -eq 1 ]; then
    echo "  Added $LOCAL_BIN to PATH in $RC_FILE"
    echo "  Run this once after setup: source \"$RC_FILE\""
else
    echo "  PATH already configured in $RC_FILE"
fi
echo "  Launcher: $BLOOOP_CMD"
echo "  Compat  : $BLOOP_COMPAT_CMD"
echo "  Python  : $VENV_PY"

# ── ffmpeg dependency ──────────────────────────────────────────────────────────
echo
echo "→ Checking ffmpeg…"
if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "✗ ffmpeg is required but was not found in PATH."
    if command -v brew >/dev/null 2>&1; then
        echo "  Install with:"
        echo "    brew install ffmpeg"
    else
        echo "  Install Homebrew first, then run:"
        echo "    brew install ffmpeg"
    fi
    exit 1
fi
echo "  ffmpeg: $(command -v ffmpeg)"

# ── Pre-download model ────────────────────────────────────────────────────────
if [ "$PRELOAD_MODEL" -eq 1 ]; then
echo
echo "→ Downloading Whisper model (cached after first download)…"
"$VENV_PY" - <<'EOF'
import tempfile, os
import numpy as np
import soundfile as sf
import mlx_whisper

MODEL = "mlx-community/whisper-small-mlx"
silent = np.zeros(8000, dtype="float32")
with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
    tmp = fh.name
try:
    sf.write(tmp, silent, 16000)
    mlx_whisper.transcribe(tmp, path_or_hf_repo=MODEL)
finally:
    try:
        os.unlink(tmp)
    except OSError:
        pass
print("  Model ready.")
EOF
fi

# ── Permissions ───────────────────────────────────────────────────────────────
if [ "$OPEN_SETTINGS" -eq 1 ]; then
echo
echo "────────────────────────────────────────"
echo "  IMPORTANT: grant two macOS permissions"
echo "────────────────────────────────────────"
echo
echo "  Blooop needs Microphone + Accessibility access."
echo "  Enable your terminal app in both settings."
echo "  Opening System Settings now…"
echo
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
sleep 1
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
echo
fi
echo "────────────────────────────────────────"
echo "  Done!  Run with:  blooop"
echo "────────────────────────────────────────"
