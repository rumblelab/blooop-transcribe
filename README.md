# blooop

**Local push-to-talk voice for AI. Mac only. Apple Silicon. Free. Open source.**

Hold right-Command, talk, release. Your words get transcribed locally via [MLX Whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper) and pasted into whatever app is focused. Claude Code, ChatGPT, Cursor, your terminal, a Google Doc -- doesn't matter. If you can paste into it, blooop works with it.

No cloud. No subscription. No account. Your voice never leaves your machine.

**[blooop.lol](https://blooop.lol)**

---

## Install

Download **[Blooop-1.0.0.zip](https://blooop.lol)** (~160 MB), unzip, and drag **Blooop** into your **Applications** folder. Then open it -- on the first open, click "Open" on the macOS "downloaded from the internet" dialog, and that's it.

A short setup wizard walks you through the two macOS permissions, then you pick a speech model to download (one time) -- Medium (1.4 GB) is recommended; Small (459 MB) if you're short on disk or bandwidth. After that everything runs offline.

Prefer to run from source? See [Running from source](#running-from-source) below.

## How to use

1. Click into any text field, in any app.
2. Hold `Right Command (⌘)` and talk.
3. Release. Your words paste right at your cursor.

| Action | Hotkey |
|---|---|
| **Push-to-talk** | Hold `Right Command (⌘)` -> speak -> release |
| **Hands-free mode** | Double-tap `Right Command (⌘)` (keeps recording) |
| **Stop hands-free** | Tap `Right Command (⌘)` once |
| **Quit** | Menu bar icon -> Quit Blooop |

Text gets copied to your clipboard and auto-pasted into the focused app. Some Electron apps block programmatic paste -- just hit `Cmd-V` manually.

## Requirements

- macOS 13+ (Ventura or later)
- Apple Silicon (M1 / M2 / M3 / M4)

Running from source also needs Python 3.10+.

## Permissions

Blooop needs two macOS permissions, both granted to **Blooop** itself (the setup wizard requests them and opens the right System Settings panes):

| Permission | Why |
|---|---|
| **Microphone** | To hear you (only while you hold the hotkey) |
| **Accessibility** | To notice the hotkey while you're in other apps, and to paste the text for you |

Running from source instead? Grant both to your terminal app -- see [Running from source](#running-from-source).

## Privacy

No cloud. No account. Audio never leaves your Mac -- transcription runs entirely on your machine, and nothing touches the network after the one-time model download.

## Settings

Click the Blooop icon in the menu bar -> Show Settings. You can switch models (Tiny ~80 MB fastest · Small ~460 MB · Medium ~1.5 GB recommended · Large v3 ~3 GB most accurate), change the push-to-talk key, toggle auto-paste, adjust mic sensitivity, and add custom vocabulary -- no config files.

## Works with

Anything you can paste into:

- Claude Code / Claude.ai
- ChatGPT
- Cursor / Windsurf / VS Code
- Terminal (for shell commands)
- Notion, Google Docs, Slack, email
- Literally anything with a text field

## How it works

```
Hold key  ->  record audio (sounddevice)
Release   ->  mlx-whisper transcribes on the Apple GPU
          ->  copy text to clipboard
          ->  osascript pastes into the focused app
```

Everything runs locally. No API keys, no internet after the model downloads.

## Why not Wispr?

Sure, you could download Wispr Flow and spend $15/month sending your voice to someone else's servers. But you won't. Because you're smart. And cheap. And you don't trust the cloud with your half-formed thoughts about refactoring the auth layer.

blooop is the open-source alternative. Same idea, zero dollars, fully private.

## Troubleshooting

**Nothing happens when I hold Right Command**
- Accessibility permission is missing. System Settings -> Privacy & Security -> Accessibility -> turn on Blooop, then relaunch Blooop. (In Settings inside the app, click "Run setup again" to re-run the wizard.)

**No text appears**
- Check System Settings -> Privacy & Security -> Microphone -> turn on Blooop.

**Auto-paste doesn't work in my app**
- The text is always on your clipboard. Just `Cmd-V`.

**First transcription is slow**
- The model loads once per launch. Subsequent ones are fast.

## Known issues

- If a model download fails (say, you were offline), Blooop retries when you press the hotkey or click Retry in setup -- it does not retry on its own when your connection returns.
- Model downloads can't be cancelled mid-transfer yet; switching models mid-download lets the old download finish in the background.

## Running from source

```bash
git clone https://github.com/rumblelab/blooop-transcribe
cd blooop-transcribe
./setup.sh
source ~/.zshrc   # or open a new terminal tab
blooop
```

`setup.sh` creates an isolated venv, installs deps, downloads the Whisper model (~460 MB), drops a `blooop` command in your PATH, and opens the macOS permission panels you need to approve. When running from source, grant Microphone and Accessibility to your **terminal app** (Terminal, iTerm, Kitty, etc.) instead of Blooop.

**`command not found: blooop`** -- run `source ~/.zshrc` (or open a new terminal tab), then `type -a blooop` to verify.

Defaults live at the top of `bloop.py`, but the in-app Settings panel covers the common ones.

## Companion tools

- **[blooop ding](https://github.com/rumblelab/blooop-ding)** -- terminal notifier that bloops when your AI agent needs attention (planned)
- **[blooop.lol](https://blooop.lol)** -- the website, such as it is

## Star this repo

If blooop saved you from mass-backspacing a typo in Claude Code, mass-backspacing a typo in ChatGPT, or mass-backspacing a typo in a commit message you were dictating to fix a typo... you know what to do.

PRs welcome. Issues welcome. Feature requests welcome. Honestly just any engagement with another human being is welcome at this point.

## License

MIT
