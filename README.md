# blooop

**Local push-to-talk voice for AI. Mac only. Apple Silicon. Open source.**

Hold right-Command, talk, release. Your words get transcribed locally via [MLX Whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper) and pasted into whatever app is focused. Claude Code, ChatGPT, Cursor, your terminal, a Google Doc -- doesn't matter. If you can paste into it, blooop works with it.

No cloud. No subscription. No account. Your voice never leaves your machine.

**[blooop.lol](https://blooop.lol)**

---

## What it looks like

```
$ blooop

────────────────────────────────────────
  blooop  v0.1  |  model: whisper-small-mlx
────────────────────────────────────────

  Hold Right Cmd     push-to-talk
  Double-tap         latch mode (hands-free)
  Tap                stop + unlatch
  Ctrl-C             quit

  Listening…

  [Recording]  ██████████████░░  3.2s
  Transcribing…
  "Can you refactor the auth middleware to use JWT instead of sessions"
  Pasted.
```

## Install

```bash
git clone https://github.com/rumblelab/blooop
cd blooop
./setup.sh
source ~/.zshrc   # or open a new terminal tab
blooop
```

That's it. `setup.sh` creates an isolated venv, installs deps, downloads the Whisper model (~250 MB), drops a `blooop` command in your PATH, and opens the macOS permission panels you need to approve.

## Requirements

- macOS 13+ (Ventura or later)
- Apple Silicon (M1 / M2 / M3 / M4)
- Python 3.10+
- ffmpeg (`brew install ffmpeg`)

## Permissions

Blooop needs two macOS permissions, both granted to your **terminal app** (Terminal, iTerm, Kitty, etc.):

| Permission | Why |
|---|---|
| **Microphone** | To record your voice |
| **Accessibility** | To listen for the global hotkey from any app |

System Settings -> Privacy & Security -> Microphone + Accessibility. `setup.sh` opens both panels for you.

## Usage

| Action | Hotkey |
|---|---|
| **Push-to-talk** | Hold `Right Cmd` -> speak -> release |
| **Latch mode** | Double-tap `Right Cmd` (hands-free, keeps recording) |
| **Stop latch** | Tap `Right Cmd` once |
| **Quit** | `Ctrl-C` |

Text gets copied to your clipboard and auto-pasted into the focused app. Some Electron apps block programmatic paste -- just hit `Cmd-V` manually.

## Works with

Anything you can paste into:

- Claude Code / Claude.ai
- ChatGPT
- Cursor / Windsurf / VS Code
- Terminal (for shell commands)
- Notion, Google Docs, Slack, email
- Literally anything with a text field

## Config

Edit the top of `bloop.py`:

```python
MODEL = "mlx-community/whisper-small-mlx"   # default (~250 MB)
# Faster  -> mlx-community/whisper-tiny-mlx   (~39 MB)
# Better  -> mlx-community/whisper-medium-mlx (~770 MB)
# Best    -> mlx-community/whisper-large-v3-mlx (~3 GB)

AUTO_PASTE = True
LATCH_CHUNK_SECONDS = 10.0
```

## How it works

```
Hold key  ->  record audio (sounddevice)
Release   ->  write WAV to temp file
          ->  mlx-whisper transcribes on Apple GPU
          ->  copy text to clipboard
          ->  osascript pastes into focused app
```

Everything runs locally. No API keys, no internet after the model downloads.

## Why not Whispr?

Sure, you could download Whispr Flow and spend $15/month sending your voice to someone else's servers. But you won't. Because you're smart. And cheap. And you don't trust the cloud with your half-formed thoughts about refactoring the auth layer.

blooop is the open-source alternative. Same idea, zero dollars, fully private.

## Troubleshooting

**Nothing happens when I hold Right Cmd**
- Accessibility permission is missing. Check System Settings -> Privacy & Security -> Accessibility. Enable your terminal app.

**Microphone permission prompt never appeared**
- Launch blooop and try recording once, then check System Settings -> Privacy & Security -> Microphone.

**`command not found: blooop`**
- Run `source ~/.zshrc` (or open a new terminal tab). Then `type -a blooop` to verify.

**Auto-paste doesn't work in my app**
- The text is always on your clipboard. Just `Cmd-V`.

**First transcription is slow**
- The model loads on first use. Subsequent ones are fast.

**ffmpeg not found**
- `brew install ffmpeg`

## Companion tools

- **[blooop ding](https://github.com/rumblelab/blooop-ding)** -- terminal notifier that bloops when your AI agent needs attention (planned)
- **[blooop.lol](https://blooop.lol)** -- the website, such as it is

## npm wrapper

If you prefer npm:

```bash
npm install -g blooop
```

This is a thin launcher that delegates to the Python CLI. You still need `setup.sh` for the actual runtime.

## Star this repo

If blooop saved you from mass-backspacing a typo in Claude Code, mass-backspacing a typo in ChatGPT, or mass-backspacing a typo in a commit message you were dictating to fix a typo... you know what to do.

PRs welcome. Issues welcome. Feature requests welcome. Honestly just any engagement with another human being is welcome at this point.

## License

MIT
