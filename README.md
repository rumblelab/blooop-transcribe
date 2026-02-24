# bloop_flow

`bloop_flow` is the first tool in the broader **Bloop** toolkit.

Planned sibling tools:
- `bloop_flow`: local voice-to-text + auto-paste (this project)
- `bloop_ding`: terminal completion/attention notifier (planned)
- `bloop.lol`: product/landing site that links all tools

Repository scaffolding for those siblings lives in:
- `apps/bloop-ding/`
- `web/bloop-lol/`

Local, free, offline voice-to-text for Apple Silicon Macs. No subscription, no cloud, no data leaving your machine.

Transcribes using [mlx-whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper) — Whisper running natively on the M-series GPU via Apple's MLX framework. Pastes the result wherever your cursor is.

---

## Requirements

- **Mac with Apple Silicon** (M1 / M2 / M3 or later)
- **macOS 13 Ventura or later**
- **Python 3.10+** — check with `python3 --version`

---

## Install

**With Claude Code (easiest):**
```bash
git clone https://github.com/yourname/bloop_flow
cd bloop_flow
claude
```
Then ask: *"Set this up and get it running for me"* — it'll handle the rest.

**Manual:**
```bash
git clone https://github.com/yourname/bloop_flow
cd bloop_flow
./setup.sh
```

`setup.sh` will:
- Create an isolated Python venv
- Install all dependencies
- Download the Whisper model (~250 MB, cached after first download)
- Open System Settings so you can grant the two required permissions

---

## Permissions

bloop needs two permissions granted for your terminal app (Terminal, iTerm2, Warp, etc.):

| Permission | Why |
|---|---|
| **Microphone** | To record your voice |
| **Accessibility** | To listen for the global hotkey from any app |

`setup.sh` opens both settings panels automatically. If you skipped that step:

- **System Settings → Privacy & Security → Microphone**
- **System Settings → Privacy & Security → Accessibility**

---

## Run

```bash
./run.sh
```

Or if you set up the alias during install:

```bash
bloop
```

---

## Usage

| Action | Hotkey |
|---|---|
| **Push-to-talk** | Hold `Right ⌘` → speak → release |
| **Latch mode on** | Double-tap `Right ⌘` (hands-free, keeps recording) |
| **Latch mode off** | Tap `Right ⌘` once |
| **Quit** | `Ctrl-C` in the terminal |

After transcription, the text is automatically copied to your clipboard and pasted into whatever app was focused when you started speaking.

A small **waveform pill** appears near the Dock while recording.
A **history window** shows your recent transcriptions — you can copy or delete individual entries.

---

## Config

Open `bloop.py` and edit the values at the top:

```python
# Model — trade speed for accuracy
MODEL = "mlx-community/whisper-small-mlx"   # default (~250 MB)
# Faster / smaller  →  mlx-community/whisper-tiny-mlx   (~39 MB)
# Higher accuracy   →  mlx-community/whisper-medium-mlx (~770 MB)
# Best quality      →  mlx-community/whisper-large-v3-mlx (~3 GB)

# Paste automatically after transcription
AUTO_PASTE = True

# Latch mode: transcribe in rolling chunks (avoids one huge clip)
LATCH_CHUNK_SECONDS = 10.0
```

---

## Troubleshooting

**Nothing happens when I hold Right ⌘**
→ Accessibility permission is missing or wasn't granted to the right terminal app. Check System Settings → Privacy & Security → Accessibility.

**Microphone permission prompt never appeared**
→ Go to System Settings → Privacy & Security → Microphone and enable your terminal manually.

**Auto-paste doesn't work in my app**
→ Some apps (browsers, Electron apps) block programmatic paste. The text is always on your clipboard — just paste manually with `⌘V`.

**First transcription is slow**
→ The model loads on first use and stays warm. Subsequent transcriptions are fast.

**I want a different model**
→ Edit `MODEL` in `bloop.py`. Run `./setup.sh` again to pre-download the new model.

---

## How it works

```
Hold key → record audio (sounddevice)
Release  → write WAV to temp file
         → mlx-whisper transcribes on M-series GPU
         → copy text to clipboard
         → osascript pastes into focused app
```

Everything runs locally. No API keys, no internet required after the model is downloaded.

---

## License

MIT
