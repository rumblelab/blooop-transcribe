# Testing Guide

Use this as the pre-publish verification flow for `bloop_flow`.

## 1) Automated checks (required)

From repo root:

```bash
scripts/test_full.sh
```

This runs:
- shell syntax checks (`run.sh`, `setup.sh`, `scripts/*.sh`)
- Python compile checks (`bloop.py`, `history_ui.py`)
- unit tests (`tests/test_bloop_core.py`)
- CLI smoke tests (`--help`, `--history`)

If you only want static/unit checks (skip CLI smoke):

```bash
BLOOOP_SKIP_CLI_SMOKE=1 scripts/test_full.sh
```

## 2) Manual functional QA (required before release)

### Setup + launch

```bash
./setup.sh
blooop
```

Confirm:
- app starts without crash
- build + model line prints in startup banner
- history DB initializes

### Permissions

In System Settings > Privacy & Security:
- Microphone enabled for your terminal app
- Accessibility enabled for your terminal app

Then relaunch `blooop`.

### Hotkey + transcription flow

Check each behavior:
- hold configured hotkey, speak, release: transcript appears and pastes
- double-tap hotkey: latch mode starts
- tap hotkey in latch: session ends cleanly
- `Ctrl-C`: clean exit

### History UI

Check:
- rows appear for successful transcriptions
- copy button copies row text
- delete removes row
- model/hotkey/silence settings save and survive restart

### Model switch behavior

In History settings:
- switch model (for example `small` -> `tiny`)
- confirm runtime shows current model and queued next model
- relaunch app and confirm new model is active
