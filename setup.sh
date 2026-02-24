#!/usr/bin/env bash
# bloop_flow setup  –  creates an isolated venv to avoid Anaconda conflicts
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$DIR/.venv"

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

source "$VENV/bin/activate"

# ── Dependencies ──────────────────────────────────────────────────────────────
echo "→ Installing Python dependencies…"
pip install --upgrade pip -q
pip install -r "$DIR/requirements.txt"

# ── Pre-download model ────────────────────────────────────────────────────────
echo
echo "→ Downloading Whisper model (cached after first download)…"
python - <<'EOF'
import tempfile, os
import numpy as np
import soundfile as sf
import mlx_whisper

MODEL = "mlx-community/whisper-small-mlx"
silent = np.zeros(8000, dtype="float32")
tmp = tempfile.mktemp(suffix=".wav")
sf.write(tmp, silent, 16000)
mlx_whisper.transcribe(tmp, path_or_hf_repo=MODEL)
os.unlink(tmp)
print("  Model ready.")
EOF

# ── Permissions ───────────────────────────────────────────────────────────────
echo
echo "────────────────────────────────────────"
echo "  IMPORTANT: grant two macOS permissions"
echo "────────────────────────────────────────"
echo
echo "  bloop needs Microphone + Accessibility access for your terminal app."
echo "  Opening System Settings now — enable both for Terminal / iTerm2 / Warp."
echo
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
sleep 1
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
echo
echo "────────────────────────────────────────"
echo "  Done!  Run with:  ./run.sh"
echo "────────────────────────────────────────"
