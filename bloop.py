#!/usr/bin/env python3
"""
bloop_flow  –  local voice transcription for Mac Silicon

Keys
----
  Hold   Right Command*      push-to-talk  (release → transcribe & copy)
  Double-tap Right Command*  enable latch mode (hands-free)
  Tap    Right Command*      stop and unlatch
  Ctrl-C                     quit

* default; configurable from History settings panel

Config at the top of this file.
"""

import multiprocessing
import os
import re
import shutil
import json
import sys

# Must be called before any other code in a frozen PyInstaller build.
# Without this, multiprocessing child processes re-invoke the executable
# with internal arguments that the CLI parser can't handle.
multiprocessing.freeze_support()
import difflib
import errno
import signal
import sqlite3
import subprocess
import threading
import queue
import tempfile
import time
import math
import random
import traceback
from datetime import UTC, datetime, timedelta
from collections import deque

import numpy as np
import sounddevice as sd
import soundfile as sf
import pyperclip
from pynput import keyboard
from pynput.keyboard import Controller as KBController, Key

_INSTANCE_LOCK_FD = None


def _acquire_single_instance_lock():
    """Return True if this process owns the single-instance lock."""
    global _INSTANCE_LOCK_FD
    if _INSTANCE_LOCK_FD is not None:
        return True
    lock_path = INSTANCE_LOCK_PATH
    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    except Exception:
        return True
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
        except Exception:
            pass
        _INSTANCE_LOCK_FD = fd
        return True
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
        return False


def _release_single_instance_lock():
    global _INSTANCE_LOCK_FD
    if _INSTANCE_LOCK_FD is None:
        return
    try:
        import fcntl

        fcntl.flock(_INSTANCE_LOCK_FD, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(_INSTANCE_LOCK_FD)
    except Exception:
        pass
    _INSTANCE_LOCK_FD = None


def _early_set_process_program_name(name="Blooop"):
    """Set process name before any GUI objects are created (best-effort)."""
    if sys.platform != "darwin":
        return
    try:
        import ctypes

        libc = ctypes.CDLL("libc.dylib")
        setprogname = getattr(libc, "setprogname", None)
        if setprogname is not None:
            setprogname(name.encode("utf-8"))
    except Exception:
        pass


_early_set_process_program_name("Blooop")


def _is_subprocess():
    """Detect if we're a child/subprocess re-invocation (not the main app)."""
    argv = sys.argv[1:]
    # Multiprocessing resource tracker, probe, or Python -c invocations
    for a in argv:
        if a in ("--probe-mlx", "--history-ui-process"):
            return True
        if "multiprocessing" in a or "resource_tracker" in a:
            return True
    return False


def _setup_frozen_logging():
    """Redirect stdout/stderr to a log file when running as a standalone .app."""
    if not getattr(sys, "frozen", False):
        return
    # Don't let child processes truncate the main log.
    if _is_subprocess():
        return
    log_dir = os.path.expanduser("~/Library/Logs/Blooop")
    try:
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "standalone.log")
        # Rotate: keep previous run as .prev
        prev = log_path + ".prev"
        if os.path.exists(log_path):
            try:
                os.replace(log_path, prev)
            except Exception:
                pass
        log_fh = open(log_path, "w", encoding="utf-8", buffering=1)  # line-buffered
        sys.stdout = log_fh
        sys.stderr = log_fh
        print(f"[Blooop standalone] started {datetime.now().isoformat()}")
        print(f"  executable: {sys.executable}")
        print(f"  _MEIPASS: {getattr(sys, '_MEIPASS', None)}")
        print(f"  pid: {os.getpid()}")
        print(f"  argv: {sys.argv}")
    except Exception:
        pass  # can't log — best-effort


_setup_frozen_logging()


def _enable_crash_tracebacks():
    """Dump Python thread stacks on native crashes (SIGSEGV/SIGABRT/…).

    In frozen builds stderr already points at the standalone log, so an
    intermittent native crash leaves usable evidence behind instead of
    vanishing without a trace (macOS writes no .ips for some abort paths).
    """
    try:
        import faulthandler

        faulthandler.enable()
    except Exception:
        pass


_enable_crash_tracebacks()


def _ensure_macos_tool_paths():
    """Finder-launched .app processes often miss Homebrew PATH entries."""
    if sys.platform != "darwin":
        return
    base = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    cur = os.environ.get("PATH", "")
    parts = [p for p in cur.split(":") if p]
    seen = set(parts)
    merged = []
    for p in base:
        if p not in seen:
            merged.append(p)
    merged.extend(parts)
    os.environ["PATH"] = ":".join(merged)


def _resolve_ffmpeg():
    cand = shutil.which("ffmpeg")
    if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
        return cand
    for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


_ensure_macos_tool_paths()


def _app_base_dir():
    """Return the on-disk base directory for bundled/static assets."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass and os.path.isdir(meipass):
            return meipass
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


APP_BASE_DIR = _app_base_dir()


def _writable_dir(path):
    """Return True when `path` can be created and written."""
    if not path:
        return False
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, f".probe.{os.getpid()}")
        with open(probe, "w", encoding="utf-8"):
            pass
        os.unlink(probe)
        return True
    except Exception:
        return False


def _state_dir_candidates():
    out = []

    env_state_dir = os.environ.get("BLOOOP_STATE_DIR")
    if env_state_dir:
        out.append(os.path.abspath(os.path.expanduser(env_state_dir)))

    out.append(os.path.expanduser("~/.bloop_flow"))
    if sys.platform == "darwin":
        out.append(os.path.expanduser("~/Library/Application Support/Blooop"))

    # For source runs, keep a repo-local fallback.
    if not getattr(sys, "frozen", False):
        out.append(os.path.join(APP_BASE_DIR, ".bloop_flow"))

    try:
        out.append(os.path.join(os.getcwd(), ".bloop_flow"))
    except Exception:
        pass

    out.append(os.path.join(tempfile.gettempdir(), "bloop_flow"))

    deduped = []
    seen = set()
    for p in out:
        if p in seen:
            continue
        seen.add(p)
        deduped.append(p)
    return deduped


def _resolve_state_dir():
    for cand in _state_dir_candidates():
        if _writable_dir(cand):
            return cand
    # Final fallback: temp dir path even if writability probe failed.
    return os.path.join(tempfile.gettempdir(), "bloop_flow")

try:
    import tkinter as tk
    from tkinter import font as tkfont, ttk
    _TK = True
except ImportError:
    tkfont = None
    ttk = None
    _TK = False


def _set_macos_accessory_app():
    """Best-effort: hide helper process from Dock when not using tkinter."""
    # Important: if tkinter is active, it needs to create its own NSApplication
    # subclass. Calling NSApplication.sharedApplication() first can crash with
    # `-[NSApplication macOSVersion] unrecognized selector`.
    if sys.platform != "darwin" or _TK:
        return
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory
        )
    except Exception:
        pass


def _set_macos_regular_app():
    """Best-effort: show the main Blooop app in the Dock like a normal app."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyRegular

        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyRegular
        )
    except Exception:
        pass


def _set_process_program_name(name="Blooop"):
    """Best-effort program name hint for macOS process surfaces."""
    if sys.platform != "darwin":
        return
    try:
        import ctypes
        libc = ctypes.CDLL("libc.dylib")
        setprogname = getattr(libc, "setprogname", None)
        if setprogname is not None:
            setprogname(name.encode("utf-8"))
    except Exception:
        pass


def _set_macos_app_identity(name="Blooop"):
    """Best-effort app name/icon for macOS Dock + app switcher."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSImage, NSProcessInfo
    except Exception:
        return

    try:
        NSProcessInfo.processInfo().setProcessName_(name)
    except Exception:
        pass

    # In frozen app builds, use the bundle icon (AppIcon.icns) to avoid
    # mismatched runtime icon scaling between helper and main processes.
    if getattr(sys, "frozen", False):
        return

    icon = None
    base = APP_BASE_DIR
    candidates = (
        os.path.join(base, "assets", "blooop-icon.png"),
        os.path.join(base, "assets", "icon.png"),
        os.path.join(base, "assets", "bloop-icon.png"),
        os.path.join(base, "assets", "bloop-icon2.png"),
        os.path.join(base, "assets", "bloop.png"),
        os.path.join(base, "bloop-icon.png"),
    )
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            img = NSImage.alloc().initWithContentsOfFile_(path)
            if img is not None and img.isValid():
                icon = img
                break
        except Exception:
            pass

    if icon is not None:
        try:
            NSApplication.sharedApplication().setApplicationIconImage_(icon)
        except Exception:
            pass


def _macos_app_is_active():
    if sys.platform != "darwin":
        return False
    try:
        from AppKit import NSApplication
        app = NSApplication.sharedApplication()
        return bool(app) and bool(app.isActive())
    except Exception:
        return False


def _macos_find_nswindow(root):
    if sys.platform != "darwin" or root is None:
        return None
    try:
        root.update_idletasks()
    except Exception:
        pass

    try:
        from AppKit import NSApp
    except Exception:
        return None

    try:
        app = NSApp()
        if not app:
            return None
        title_hint = ""
        try:
            title_hint = str(root.title() or "")
        except Exception:
            pass
        for win in list(app.windows() or []):
            try:
                if title_hint and str(win.title() or "") == title_hint:
                    return win
            except Exception:
                pass
    except Exception:
        return None
    return None


def _macos_set_window_collection_behavior(
    root,
    *,
    join_all_spaces=False,
    move_to_active_space=False,
    fullscreen_auxiliary=False,
    stationary=False,
):
    target = _macos_find_nswindow(root)
    if target is None:
        return False

    try:
        from AppKit import (
            NSWindowCollectionBehaviorCanJoinAllSpaces,
            NSWindowCollectionBehaviorFullScreenAuxiliary,
            NSWindowCollectionBehaviorMoveToActiveSpace,
            NSWindowCollectionBehaviorStationary,
        )
    except Exception:
        return False

    try:
        behavior = int(target.collectionBehavior())
        if join_all_spaces:
            behavior |= int(NSWindowCollectionBehaviorCanJoinAllSpaces)
        if move_to_active_space:
            behavior |= int(NSWindowCollectionBehaviorMoveToActiveSpace)
        if fullscreen_auxiliary:
            behavior |= int(NSWindowCollectionBehaviorFullScreenAuxiliary)
        if stationary:
            # Pin the window's Space membership so macOS doesn't treat it as
            # "follow the user" — needed alongside join_all_spaces for the
            # overlay to actually appear on fullscreen Spaces.
            behavior |= int(NSWindowCollectionBehaviorStationary)
        target.setCollectionBehavior_(behavior)
        return True
    except Exception:
        return False


def _macos_prepare_overlay_window(root, *, front=False):
    target = _macos_find_nswindow(root)
    if target is None:
        return False

    # NSScreenSaverWindowLevel (1000) is the canonical "above everything"
    # level on macOS: it appears over fullscreen-mode windows (Chrome,
    # green-button-maximized apps) which the lower NSPopUpMenuWindowLevel
    # (101) loses to. Tradeoff: while visible, the pill sits over the
    # menu bar and any notification in the same top-right corner. Since
    # the pill is only shown during recording/transcribing, this is the
    # right call for an "am I recording right now?" indicator.
    try:
        from AppKit import NSScreenSaverWindowLevel
        chosen_level = int(NSScreenSaverWindowLevel)
    except Exception:
        chosen_level = 1000

    try:
        target.setLevel_(chosen_level)
    except Exception:
        try:
            from AppKit import NSPopUpMenuWindowLevel
            target.setLevel_(int(NSPopUpMenuWindowLevel))
        except Exception:
            pass

    try:
        target.setIgnoresMouseEvents_(True)
    except Exception:
        pass

    if front:
        try:
            target.orderFrontRegardless()
        except Exception:
            pass
    return True


def _macos_configure_nonactivating_overlay(root):
    """Best-effort: keep overlay visible without activating Blooop on macOS."""
    if sys.platform != "darwin" or root is None:
        return

    try:
        # noActivates: clicks on the pill don't steal focus from the user's
        # current app. We intentionally omit `hideOnSuspend` here — with it,
        # the "help" panel auto-hides whenever Blooop isn't the active app,
        # which is *always* the case while the user is transcribing into
        # another app, so the pill would never be visible in practice.
        root.tk.call(
            "::tk::unsupported::MacWindowStyle",
            "style",
            root._w,
            "help",
            "noActivates",
        )
    except Exception:
        pass

    # Use join_all_spaces alone — combining it with move_to_active_space makes
    # macOS pick one behavior non-deterministically, which caused the overlay
    # to bind to whichever Space the root/history window lived on and vanish
    # everywhere else.
    _macos_set_window_collection_behavior(
        root,
        join_all_spaces=True,
        fullscreen_auxiliary=True,
        stationary=True,
    )
    _macos_prepare_overlay_window(root, front=False)


def _macos_configure_history_window(root):
    """Keep the history window in the active Space as a normal app window."""
    _macos_set_window_collection_behavior(
        root,
        move_to_active_space=True,
        fullscreen_auxiliary=True,
    )


def _indicator_state_label(state):
    return {
        "idle": "",
        "recording": "REC",
        "transcribing": "TXT",
    }.get(str(state or "").strip().lower(), "")


def _macos_set_dock_badge(label):
    if sys.platform != "darwin":
        return False
    try:
        from AppKit import NSApplication
        app = NSApplication.sharedApplication()
        if not app:
            return False
        dock_tile = app.dockTile()
        if not dock_tile:
            return False
        dock_tile.setBadgeLabel_(label or "")
        dock_tile.display()
        return True
    except Exception:
        return False


# ── Config ────────────────────────────────────────────────────────────────────

RUNTIME_BUILD = "2026-07-09-pill-rebuild-and-echo-filter"

# MLX Whisper model from HuggingFace (downloaded on first use, cached after).
# Faster / smaller  →  mlx-community/whisper-tiny-mlx   (~39 MB)
# Good balance      →  mlx-community/whisper-small-mlx  (~250 MB)  ← current
# Higher accuracy   →  mlx-community/whisper-medium-mlx (~770 MB)
# Best quality      →  mlx-community/whisper-large-v3-mlx (~3 GB)
MODEL = "mlx-community/whisper-small-mlx"

# Push-to-talk: hold right Command (avoids clobbering terminal Ctrl-C).
PTT_KEY = keyboard.Key.cmd_r

# Double-tap window to enter latch mode. Applies to both halves of the
# gesture: max held duration for a press to count as a tap, and max
# release-to-press gap between the two taps.
DOUBLE_TAP_MS = 450

# Delay start slightly to distinguish quick taps (for latch toggling) from
# intentional hold-to-talk recordings.
PTT_START_DELAY_MS = 110

# In latch mode, periodically transcribe partial chunks instead of waiting for
# one very long final clip.
LATCH_CHUNK_MODE = True
LATCH_CHUNK_SECONDS = 10.0

# After transcribing, paste the text into the focused app automatically.
AUTO_PASTE = True

# Minimum audio duration (seconds) to bother transcribing.
MIN_DURATION = 0.4

# Keep capturing this long after the stop hotkey lands before snapshotting the
# buffer. Covers both the CoreAudio input latency (the last ~50-150ms of real
# speech is still in flight when the flag flips) and the human habit of hitting
# the key while the final syllable is still coming out. The UI flips to
# "transcribing" immediately; only the buffer grab is deferred.
STOP_GRACE_TAIL_SEC = 0.30

# Decoding guardrails to reduce silence/noise hallucinations. The temperature
# LADDER matters: compression_ratio/logprob thresholds only mark a segment as
# "needs re-decode", and the re-decode happens at the next temperature in the
# tuple. A scalar 0.0 leaves nothing to fall back to, so repetitive (looping)
# segments were kept verbatim despite tripping the compression check.
WHISPER_TEMPERATURE = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
WHISPER_COMPRESSION_RATIO_THRESHOLD = 2.2
WHISPER_LOGPROB_THRESHOLD = -0.8
WHISPER_NO_SPEECH_THRESHOLD = 0.45
WHISPER_CONDITION_ON_PREVIOUS_TEXT = False
# Post-decode per-segment gate. Whisper's built-in no_speech gate keeps a
# "silent" segment whenever its text decodes confidently (avg_logprob above
# logprob_threshold) — which is exactly how fluent confabulations like
# initial-prompt echoes survive it. Re-screen each returned segment with a
# stricter combination: sounded like silence AND decoded worse than clean
# speech does. Clean dictation on the medium model sits well inside both
# bounds (no_speech_prob < ~0.3, avg_logprob > ~-0.4).
WHISPER_SEGMENT_NO_SPEECH_PROB = 0.6
WHISPER_SEGMENT_AVG_LOGPROB = -0.45

# Trim leading/trailing silence before transcription to reduce hallucinations
# from long pauses. Keeps chunking behavior intact by adding padding. The pad
# is asymmetric: trailing consonants (s/f/t, soft word endings) are low-energy
# and routinely fall below the voiced threshold, so the tail keeps more than
# the head — a tight head pad still guards against silence hallucinations.
SILENCE_TRIM_ENABLED = True
SILENCE_TRIM_DBFS = -42.0
SILENCE_TRIM_WINDOW_MS = 24
SILENCE_TRIM_HOP_MS = 12
SILENCE_TRIM_PAD_MS = 180
SILENCE_TRIM_PAD_TAIL_MS = 400
SILENCE_MIN_VOICED_SEC = 0.22
SILENCE_DYNAMIC_MULT = 2.8

# Whisper reliably drops or mangles the final word when the audio ends right
# at the word boundary — which is exactly what a hotkey stop + trailing trim
# produce. Append this much literal silence so decoding never ends mid-decay.
WHISPER_TAIL_PAD_SEC = 0.4


SAMPLE_RATE = 16_000   # Whisper expects 16 kHz
CHANNELS    = 1

# CoreAudio stream teardown normally completes well under a second. When
# Pa_StopStream wedges (HAL-vs-AudioUnit mutex lock inversion against
# CoreAudio's IO thread — thread sample in issues/20260702-deadlock-sample.txt)
# the HAL client is poisoned for the life of the process: every later
# Pa_OpenStream blocks on the same mutex, so the next hotkey press freezes the
# main thread and the app looks crashed (observed 2026-06-25 and 2026-07-02).
# The only recovery is a fresh process, so detect the wedge and self-relaunch.
AUDIO_TEARDOWN_WEDGE_SEC = 8.0
AUDIO_TEARDOWN_OPEN_WAIT_SEC = 3.0
AUDIO_WEDGE_DRAIN_TIMEOUT_SEC = 180.0

# History is text-only (no audio), pruned automatically.
HISTORY_ENABLED   = True
STATE_DIR = _resolve_state_dir()
HISTORY_DB_PATH   = os.path.join(STATE_DIR, "history.db")
HISTORY_DB_FALLBACK = os.path.join(APP_BASE_DIR, ".bloop_flow", "history.db")
HISTORY_MAX_ROWS  = 1000
HISTORY_MAX_DAYS  = 30
HISTORY_SHOW_MAX  = 200
HISTORY_SHOW_DEF  = 20
HISTORY_LIVE_FEED = True
HISTORY_LIVE_PREVIEW_CHARS = 96
HISTORY_UI_ENABLED = True
HISTORY_UI_ROWS = 40
HISTORY_UI_REFRESH_MS = 900
HISTORY_UI_PREVIEW_CHARS = 260
HISTORY_UI_SUMMARY_CHARS = 170
HISTORY_UI_CARD_HEIGHT = 114
# Hide low-signal noise rows (no_audio, too_short) from the history panel.
HISTORY_UI_HIDE_NOISE = True
# Switch the in-app history viewer from the Tk panel to the pywebview
# subprocess UI defined in history_ui.py. The Tk implementation is kept in
# place so this flag can flip back if the webview path regresses.
HISTORY_UI_USE_WEBVIEW = True
# Lives in STATE_DIR so the history subprocess (which inherits the resolved
# dir via BLOOOP_STATE_DIR) always polls the same file the main app writes.
HISTORY_COMMAND_FILE = os.path.join(STATE_DIR, "history_command.json")


def _env_flag(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


# Standalone bundles keep history UI on by default. Set
# BLOOOP_STANDALONE_HISTORY_UI=0 to suppress the history panel.
STANDALONE_HISTORY_UI = _env_flag("BLOOOP_STANDALONE_HISTORY_UI", default=True)
# Allow flipping the webview/Tk history viewer at runtime without editing code.
HISTORY_UI_USE_WEBVIEW = _env_flag(
    "BLOOOP_HISTORY_UI_USE_WEBVIEW",
    default=HISTORY_UI_USE_WEBVIEW,
)
BUNDLED_MODELS_DIR = os.path.join(APP_BASE_DIR, "bundled-models")

SETTINGS_PATH = os.path.join(STATE_DIR, "settings.json")
SETTINGS_FALLBACK = os.path.join(APP_BASE_DIR, ".bloop_flow", "settings.json")
RUNTIME_STATUS_PATH = os.path.join(STATE_DIR, "runtime_status.json")
ISSUES_LOG_PATH = os.path.join(STATE_DIR, "issues.log")
ISSUES_DIR = os.path.join(STATE_DIR, "issues")
INSTANCE_LOCK_PATH = os.path.join(STATE_DIR, "blooop.lock")

DEFAULT_MODEL = "mlx-community/whisper-small-mlx"
DEFAULT_HOTKEY = "right_cmd"
DEFAULT_SILENCE_PRESET = "normal"
# Native pill designs (chosen from the index.html design lab).
PILL_STYLES = ("bubbles", "spectrogram")
PILL_STYLE_DIMS = {
    "bubbles": (124, 30),
    "spectrogram": (188, 32),
}
DEFAULT_PILL_STYLE = "bubbles"
DEFAULT_CUSTOM_VOCAB = ["Blooop"]
MAX_CUSTOM_VOCAB_ITEMS = 64
MAX_CUSTOM_VOCAB_CHARS = 80

HOTKEY_OPTIONS = {
    "right_cmd": {
        "label": "Right Cmd",
        "pynput_key": keyboard.Key.cmd_r,
        "mac_key_code": 54,
        "mac_modifier": "command",
    },
    "right_option": {
        "label": "Right Option",
        "pynput_key": keyboard.Key.alt_r,
        "mac_key_code": 61,
        "mac_modifier": "option",
    },
    "right_shift": {
        "label": "Right Shift",
        "pynput_key": keyboard.Key.shift_r,
        "mac_key_code": 60,
        "mac_modifier": "shift",
    },
}

SILENCE_PRESETS = {
    "off": {
        "enabled": False,
        "dbfs": -42.0,
        "window_ms": 24,
        "hop_ms": 12,
        "pad_ms": 180,
        "pad_tail_ms": 400,
        "min_voiced_sec": 0.22,
        "dynamic_mult": 2.8,
    },
    "normal": {
        "enabled": True,
        "dbfs": -42.0,
        "window_ms": 24,
        "hop_ms": 12,
        "pad_ms": 180,
        "pad_tail_ms": 400,
        "min_voiced_sec": 0.22,
        "dynamic_mult": 2.8,
    },
    "aggressive": {
        "enabled": True,
        "dbfs": -37.0,
        "window_ms": 20,
        "hop_ms": 10,
        "pad_ms": 140,
        "pad_tail_ms": 320,
        "min_voiced_sec": 0.28,
        "dynamic_mult": 3.2,
    },
}

# ─────────────────────────────────────────────────────────────────────────────

_HISTORY_LOCK = threading.Lock()
_HISTORY_ACTIVE_PATH = None
_SETTINGS_LOCK = threading.Lock()
_SETTINGS_ACTIVE_PATH = None
_RUNTIME_STATUS_LOCK = threading.Lock()


def _settings_defaults():
    return {
        "model": DEFAULT_MODEL,
        "auto_paste": True,
        "latch_chunk_mode": True,
        "latch_chunk_seconds": 10.0,
        "silence_trim_preset": DEFAULT_SILENCE_PRESET,
        "hotkey": DEFAULT_HOTKEY,
        # Floating recording pill on the top-right of every Space. Most
        # reliable recording indicator since the menu-bar icon can be hidden
        # by macOS's menu-bar overflow and the Dock badge is invisible when
        # the Dock is auto-hidden.
        "pill_window": True,
        "pill_style": DEFAULT_PILL_STYLE,
        "custom_vocab": list(DEFAULT_CUSTOM_VOCAB),
    }


def _settings_candidates():
    out = [SETTINGS_PATH]
    if SETTINGS_FALLBACK not in out:
        out.append(SETTINGS_FALLBACK)
    return out


def _settings_normalize(raw):
    out = _settings_defaults()
    if not isinstance(raw, dict):
        return out

    model = raw.get("model")
    if isinstance(model, str):
        model = model.strip()
        if model:
            out["model"] = model

    auto_paste = raw.get("auto_paste")
    if isinstance(auto_paste, bool):
        out["auto_paste"] = auto_paste

    latch_chunk_mode = raw.get("latch_chunk_mode")
    if isinstance(latch_chunk_mode, bool):
        out["latch_chunk_mode"] = latch_chunk_mode

    chunk_sec = raw.get("latch_chunk_seconds")
    try:
        chunk_sec = float(chunk_sec)
        if not math.isfinite(chunk_sec):
            raise ValueError("non-finite")
        out["latch_chunk_seconds"] = min(60.0, max(2.0, chunk_sec))
    except Exception:
        pass

    silence = raw.get("silence_trim_preset")
    if isinstance(silence, str) and silence in SILENCE_PRESETS:
        out["silence_trim_preset"] = silence

    hotkey = raw.get("hotkey")
    if isinstance(hotkey, str) and hotkey in HOTKEY_OPTIONS:
        out["hotkey"] = hotkey

    pill = raw.get("pill_window")
    if isinstance(pill, bool):
        out["pill_window"] = pill

    pill_style = raw.get("pill_style")
    if isinstance(pill_style, str) and pill_style in PILL_STYLES:
        out["pill_style"] = pill_style

    if "custom_vocab" in raw:
        out["custom_vocab"] = _normalize_custom_vocab(raw.get("custom_vocab"))

    return out


def _normalize_custom_vocab(raw):
    if isinstance(raw, str):
        items = re.split(r"[\n,;]+", raw)
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        return list(DEFAULT_CUSTOM_VOCAB)

    out = []
    seen = set()
    for item in items:
        if not isinstance(item, str):
            continue
        text = re.sub(r"\s+", " ", item).strip()
        if not text:
            continue
        if len(text) > MAX_CUSTOM_VOCAB_CHARS:
            text = text[:MAX_CUSTOM_VOCAB_CHARS].rstrip()
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= MAX_CUSTOM_VOCAB_ITEMS:
            break

    return out or list(DEFAULT_CUSTOM_VOCAB)


# Split out so the prompt-echo filter can match either half on its own —
# Whisper usually parrots just the instruction tail, not the vocab list.
_VOCAB_PROMPT_HEAD = "Preferred spellings, product names, and proper nouns: "
_VOCAB_PROMPT_TAIL = "Use these exact spellings when they match the spoken audio."


def _custom_vocab_initial_prompt(items):
    vocab = _normalize_custom_vocab(items)
    if not vocab:
        return None

    prompt = _VOCAB_PROMPT_HEAD + ", ".join(vocab) + ". " + _VOCAB_PROMPT_TAIL
    if len(prompt) > 720:
        prompt = prompt[:717].rstrip(", ") + "..."
    return prompt


# Whisper is well-known to confabulate on silence/near-silence/background noise.
# Common outputs: "Thanks for watching!", "you", "Thank you.", or the same
# short word repeated several times ("help help help help"). These are not
# transcriptions of anything the user said — they're priors leaking through
# from caption-heavy training data.
# Pure-punctuation outputs, matched exactly.
_HALLUCINATION_BLACKLIST = {
    ".",
    "..",
    "...",
    "?",
    "!",
    "…",
}
# Whole-utterance confabulations, matched case-insensitively after stripping
# trailing punctuation so one entry covers "Thanks for watching",
# "thanks for watching!", "Thanks for watching!!", etc. Only ever compared
# against the ENTIRE cleaned text — real sentences that merely contain one of
# these phrases are unaffected.
_HALLUCINATION_BLACKLIST_BARE = {
    "you",
    "thank you",
    "thanks",
    "thanks for watching",
    "thank you for watching",
    "thanks for watching, and i'll see you in the next video",
    "thank you for watching, and i'll see you in the next video",
    "see you in the next video",
    "subtitles by the amara.org community",
    "please like and subscribe",
    "like and subscribe",
    "don't forget to like and subscribe",
    "don't forget to subscribe",
    "bye",
    "goodbye",
}


# Whisper's decoder can also get stuck in a repetition loop, emitting the same
# short phrase dozens of times ("the history panel made me feel like" x30).
# Unlike the whole-text confabulations above, these usually begin PARTWAY
# through an otherwise-valid long dictation, so the right fix is to cut the
# transcript at the start of the loop instead of discarding the whole chunk.
# Dictating the same short phrase two or three times on purpose is real
# speech; five consecutive copies is not. For long units (a looped clause or
# sentence) even four verbatim copies is implausible as dictation, so those
# trip one repeat sooner.
HALLUCINATION_LOOP_MIN_COUNT = 5
HALLUCINATION_LOOP_MAX_PHRASE_WORDS = 12
HALLUCINATION_LOOP_RELAXED_PHRASE_WORDS = 6
HALLUCINATION_LOOP_RELAXED_COUNT = 4


def _truncate_phrase_loops(
    text,
    min_count=HALLUCINATION_LOOP_MIN_COUNT,
    max_phrase_words=HALLUCINATION_LOOP_MAX_PHRASE_WORDS,
):
    """Cut ``text`` at the start of a consecutive phrase-repetition loop.

    Looks for an n-gram (2..max_phrase_words words, compared case- and
    punctuation-insensitively) repeated min_count or more times back to back
    (one fewer for units of RELAXED_PHRASE_WORDS+ words). Returns the text
    before the loop began — nothing after the loop's onset is kept, because
    once the decoder enters a loop the rest of that window's output is
    untrustworthy. Returns ``text`` unchanged when no loop is found and ""
    when the loop starts at the first word.
    """
    if not text:
        return text
    words = list(re.finditer(r"\S+", text))
    # Smallest possible loop: a 2-word phrase repeated min_count times.
    if len(words) < min_count * 2:
        return text
    norm = []
    for match in words:
        token = match.group(0).lower()
        norm.append(re.sub(r"[\W_]+", "", token) or token)
    total = len(norm)
    for start in range(total - min_count * 2 + 1):  # earliest loop start wins
        for n in range(2, max_phrase_words + 1):
            needed = (
                min(min_count, HALLUCINATION_LOOP_RELAXED_COUNT)
                if n >= HALLUCINATION_LOOP_RELAXED_PHRASE_WORDS
                else min_count
            )
            span = n * needed
            if start + span > total:
                # Not break: span is not monotonic in n (long units need
                # fewer repeats, so a larger n can need fewer tokens).
                continue
            # norm[j] == norm[j + n] across the window means the window is
            # n-periodic, i.e. the n-gram at `start` repeats `needed` times.
            if all(norm[j] == norm[j + n] for j in range(start, start + span - n)):
                return text[: words[start].start()].rstrip()
    return text


def _looks_like_hallucination(text):
    """Cheap post-filter for Whisper silence confabulations."""
    if text is None:
        return False
    cleaned = text.strip()
    if not cleaned:
        return False
    # Blacklist (case-insensitive, trailing punctuation ignored for worded
    # entries). Keeps us from pasting "Thanks for watching!" when the user
    # made no sound.
    lowered = cleaned.lower()
    if lowered in _HALLUCINATION_BLACKLIST:
        return True
    bare = lowered.rstrip(" .!?,…")
    if bare and bare in _HALLUCINATION_BLACKLIST_BARE:
        return True
    # Repeated single-word outputs: "help help help help", "yeah yeah yeah".
    # 3+ identical tokens (ignoring punctuation) is a telltale hallucination.
    tokens = [t for t in re.split(r"\W+", cleaned.lower()) if t]
    if len(tokens) >= 3 and len(set(tokens)) == 1:
        return True
    # Multi-word repetition loops that span the whole text ("the history panel
    # made me feel like" x30 with nothing real before it). Loops that follow a
    # legitimate prefix are handled by callers via _truncate_phrase_loops.
    if not _truncate_phrase_loops(cleaned):
        return True
    return False


def _echo_normalize(s):
    return re.sub(r"[\W_]+", " ", s.lower()).strip()


def _looks_like_prompt_echo(text, prompt):
    """True when the output is Whisper parroting the initial prompt back —
    the dominant silence confabulation once an initial_prompt is supplied
    (observed 2026-07-09: silent latch chunk -> "Use the exact spellings when
    they match the spoken audio."). Echoes are near-verbatim but not exact
    ("the" for "these"), so compare normalized token streams by similarity
    ratio, against the full prompt and against each fixed half on its own.
    """
    if not text or not prompt:
        return False
    t = _echo_normalize(text)
    if len(t.split()) < 3:
        # Too short to attribute to the prompt; vocab words alone ("Blooop")
        # are legitimate dictation and the blacklist covers the classics.
        return False
    references = (
        _echo_normalize(prompt),
        _echo_normalize(_VOCAB_PROMPT_HEAD),
        _echo_normalize(_VOCAB_PROMPT_TAIL),
    )
    for reference in references:
        if not reference or len(reference.split()) < 3:
            continue
        if t in reference:
            return True
        if difflib.SequenceMatcher(None, t, reference).ratio() >= 0.75:
            return True
    return False


def _filter_result_segments(result, prompt=None):
    """Re-screen Whisper's per-segment output for silence confabulations the
    built-in gates keep (see WHISPER_SEGMENT_* above): segments that sounded
    like silence and decoded poorly, plus initial-prompt echoes. Returns
    (text, dropped) where dropped is a list of short descriptions of the
    discarded segments; when nothing is dropped, text is the untouched
    whole-result text."""
    whole_text = (result.get("text") or "").strip()
    segments = result.get("segments") or []
    if not isinstance(segments, (list, tuple)):
        return whole_text, []
    kept, dropped = [], []
    for seg in segments:
        if not isinstance(seg, dict):
            return whole_text, []
        seg_text = (seg.get("text") or "").strip()
        if not seg_text:
            continue
        try:
            no_speech = float(seg.get("no_speech_prob", 0.0))
            avg_logprob = float(seg.get("avg_logprob", 0.0))
        except (TypeError, ValueError):
            kept.append(seg_text)
            continue
        if (
            no_speech > WHISPER_SEGMENT_NO_SPEECH_PROB
            and avg_logprob < WHISPER_SEGMENT_AVG_LOGPROB
        ):
            dropped.append(
                f"no_speech={no_speech:.2f} logprob={avg_logprob:.2f} {seg_text[:60]!r}"
            )
            continue
        if _looks_like_prompt_echo(seg_text, prompt):
            dropped.append(f"prompt_echo {seg_text[:60]!r}")
            continue
        kept.append(seg_text)
    if not dropped:
        return whole_text, []
    return " ".join(kept).strip(), dropped


def _settings_pick_write_path(candidates):
    global _SETTINGS_ACTIVE_PATH
    if _SETTINGS_ACTIVE_PATH:
        return _SETTINGS_ACTIVE_PATH
    for path in candidates:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            probe = f"{path}.probe.{os.getpid()}"
            with open(probe, "w", encoding="utf-8"):
                pass
            os.unlink(probe)
            _SETTINGS_ACTIVE_PATH = path
            return path
        except Exception:
            continue
    _SETTINGS_ACTIVE_PATH = SETTINGS_PATH
    return SETTINGS_PATH


def _settings_load():
    global _SETTINGS_ACTIVE_PATH
    defaults = _settings_defaults()
    candidates = _settings_candidates()
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            out = _settings_normalize(raw)
            _SETTINGS_ACTIVE_PATH = path
            try:
                mtime = os.path.getmtime(path)
            except Exception:
                mtime = None
            return out, path, mtime
        except Exception:
            continue
    path = _settings_pick_write_path(candidates)
    return defaults, path, None


def _settings_save(values):
    with _SETTINGS_LOCK:
        current, path, _ = _settings_load()
        merged = dict(current)
        if isinstance(values, dict):
            merged.update(values)
        out = _settings_normalize(merged)

        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(out, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, path)
        finally:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except Exception:
                pass
        try:
            mtime = os.path.getmtime(path)
        except Exception:
            mtime = None
        return out, path, mtime


def _runtime_status_update(values):
    if not isinstance(values, dict):
        return
    with _RUNTIME_STATUS_LOCK:
        cur = {}
        path = RUNTIME_STATUS_PATH
        try:
            with open(path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
                if isinstance(loaded, dict):
                    cur = loaded
        except Exception:
            cur = {}

        cur.update(values)
        cur["updated_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cur, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, path)
        except Exception:
            pass
        finally:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except Exception:
                pass


def _apply_silence_preset(name):
    global SILENCE_TRIM_ENABLED
    global SILENCE_TRIM_DBFS
    global SILENCE_TRIM_WINDOW_MS
    global SILENCE_TRIM_HOP_MS
    global SILENCE_TRIM_PAD_MS
    global SILENCE_TRIM_PAD_TAIL_MS
    global SILENCE_MIN_VOICED_SEC
    global SILENCE_DYNAMIC_MULT

    cfg = SILENCE_PRESETS.get(name, SILENCE_PRESETS[DEFAULT_SILENCE_PRESET])
    SILENCE_TRIM_ENABLED = bool(cfg["enabled"])
    SILENCE_TRIM_DBFS = float(cfg["dbfs"])
    SILENCE_TRIM_WINDOW_MS = int(cfg["window_ms"])
    SILENCE_TRIM_HOP_MS = int(cfg["hop_ms"])
    SILENCE_TRIM_PAD_MS = int(cfg["pad_ms"])
    SILENCE_TRIM_PAD_TAIL_MS = int(cfg["pad_tail_ms"])
    SILENCE_MIN_VOICED_SEC = float(cfg["min_voiced_sec"])
    SILENCE_DYNAMIC_MULT = float(cfg["dynamic_mult"])


def _apply_runtime_settings(settings, update_model=True):
    global MODEL
    global AUTO_PASTE
    global CUSTOM_VOCAB
    global LATCH_CHUNK_MODE
    global LATCH_CHUNK_SECONDS
    global PTT_KEY

    if update_model:
        MODEL = settings["model"]
    AUTO_PASTE = bool(settings["auto_paste"])
    CUSTOM_VOCAB = _normalize_custom_vocab(settings.get("custom_vocab"))
    LATCH_CHUNK_MODE = bool(settings["latch_chunk_mode"])
    LATCH_CHUNK_SECONDS = float(settings["latch_chunk_seconds"])
    _apply_silence_preset(settings["silence_trim_preset"])

    hotkey = HOTKEY_OPTIONS.get(settings["hotkey"], HOTKEY_OPTIONS[DEFAULT_HOTKEY])
    PTT_KEY = hotkey["pynput_key"]


try:
    _boot_settings, _, _ = _settings_load()
except Exception:
    _boot_settings = _settings_defaults()
_apply_runtime_settings(_boot_settings)


def _import_whisper():
    import sys
    import threading

    if "mlx.core" not in sys.modules:
        # Self-heal path: initialize mlx.core lazily if we're on main thread.
        if threading.current_thread() is threading.main_thread():
            try:
                import mlx          # noqa: F401
                import mlx.core     # noqa: F401
            except Exception as exc:
                _issues_append("mlx_import_failed", "mlx.core import failed", exc=exc)
                raise RuntimeError(f"MLX core failed to initialize: {exc}") from exc
        else:
            raise RuntimeError(
                "MLX core is not ready yet. Try again in a second."
            )
    import mlx_whisper
    return mlx_whisper


def _is_permission_like_error(exc):
    if isinstance(exc, OSError):
        return exc.errno in {errno.EACCES, errno.EPERM, errno.EROFS}
    msg = str(exc).lower()
    tokens = (
        "readonly",
        "read-only",
        "permission",
        "eacces",
        "operation not permitted",
    )
    return any(tok in msg for tok in tokens)


def _history_connect():
    global _HISTORY_ACTIVE_PATH

    if _HISTORY_ACTIVE_PATH:
        return sqlite3.connect(_HISTORY_ACTIVE_PATH, timeout=3)

    candidates = [HISTORY_DB_PATH]
    if HISTORY_DB_FALLBACK not in candidates:
        candidates.append(HISTORY_DB_FALLBACK)

    last_exc = None
    for path in candidates:
        conn = None
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            conn = sqlite3.connect(path, timeout=3)
            # Some environments allow opening a DB path but deny writes. Probe
            # writability once before selecting this path as active.
            row = conn.execute("PRAGMA user_version").fetchone()
            version = int(row[0]) if row else 0
            conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()
            _HISTORY_ACTIVE_PATH = path
            return conn
        except (OSError, sqlite3.Error) as exc:
            last_exc = exc
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            # Only fall through to alternates when this is truly a writable-path
            # issue. Database lock/corruption should surface directly.
            if not _is_permission_like_error(exc):
                break
    raise last_exc if last_exc is not None else OSError("No usable history path")


def _history_path():
    return _HISTORY_ACTIVE_PATH or HISTORY_DB_PATH


def _utc_now_iso():
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _issues_append(kind, message, exc=None):
    ts = _utc_now_iso()
    lines = [f"[{ts}] {kind}: {message}"]
    if exc is not None:
        lines.append(f"  exc_type: {type(exc).__name__}")
        lines.append(f"  exc: {exc}")
        tb = traceback.format_exc().strip()
        if tb:
            lines.append("  traceback:")
            lines.extend([f"    {ln}" for ln in tb.splitlines()])
    lines.append("")
    payload = "\n".join(lines)
    try:
        os.makedirs(os.path.dirname(ISSUES_LOG_PATH), exist_ok=True)
        with open(ISSUES_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(payload)
    except Exception:
        pass


def _issues_write_report(title, body):
    try:
        os.makedirs(ISSUES_DIR, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        safe_title = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(title)).strip("-") or "report"
        path = os.path.join(ISSUES_DIR, f"{stamp}-{safe_title}.log")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body.rstrip() + "\n")
        return path
    except Exception:
        return None


def _history_compact_message(text, error, max_chars=84):
    msg = (text or "").replace("\n", " ").strip()
    if not msg:
        msg = (error or "-").replace("\n", " ").strip()
    if len(msg) > max_chars:
        msg = msg[:max_chars] + "…"
    return msg


def _history_summary(text, error, max_chars=HISTORY_UI_SUMMARY_CHARS):
    raw = (text or error or "-").replace("\n", " ").strip()
    raw = re.sub(r"\s+", " ", raw)
    if not raw:
        return "-", False

    sentences = re.split(r"(?<=[.!?])\s+", raw)
    summary = " ".join(sentences[:2]).strip() if sentences else raw
    truncated = False

    if len(summary) > max_chars:
        summary = summary[:max_chars].rstrip()
        truncated = True
    if len(summary) < len(raw):
        truncated = True

    return summary, truncated


def _history_ensure_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL,
            duration_sec REAL,
            paste_ok INTEGER,
            app_bundle TEXT,
            text TEXT,
            error TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_history_created_at ON history(created_at DESC)"
    )


def _history_prune_locked(conn):
    if HISTORY_MAX_DAYS > 0:
        cutoff = (
            datetime.now(UTC) - timedelta(days=HISTORY_MAX_DAYS)
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        conn.execute("DELETE FROM history WHERE created_at < ?", (cutoff,))

    if HISTORY_MAX_ROWS > 0:
        conn.execute(
            """
            DELETE FROM history
            WHERE id NOT IN (
                SELECT id FROM history
                ORDER BY id DESC
                LIMIT ?
            )
            """,
            (HISTORY_MAX_ROWS,),
        )


def _history_init():
    if not HISTORY_ENABLED:
        return
    with _HISTORY_LOCK:
        conn = _history_connect()
        try:
            _history_ensure_schema(conn)
            _history_prune_locked(conn)
            conn.commit()
        finally:
            conn.close()


def _history_add(status, text=None, duration_sec=None, paste_ok=None, app_bundle=None, error=None):
    if not HISTORY_ENABLED:
        return None
    ts = _utc_now_iso()
    with _HISTORY_LOCK:
        conn = _history_connect()
        try:
            _history_ensure_schema(conn)
            cur = conn.execute(
                """
                INSERT INTO history (created_at, status, duration_sec, paste_ok, app_bundle, text, error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    status,
                    duration_sec,
                    None if paste_ok is None else int(bool(paste_ok)),
                    app_bundle,
                    text,
                    error,
                ),
            )
            row_id = cur.lastrowid
            _history_prune_locked(conn)
            conn.commit()
            return row_id
        finally:
            conn.close()


def _history_update(row_id, status=None, text=None, duration_sec=None, paste_ok=None, app_bundle=None, error=None):
    if not HISTORY_ENABLED or row_id is None:
        return False

    with _HISTORY_LOCK:
        conn = _history_connect()
        try:
            _history_ensure_schema(conn)
            row = conn.execute(
                """
                SELECT status, duration_sec, paste_ok, app_bundle, text, error
                FROM history
                WHERE id = ?
                """,
                (row_id,),
            ).fetchone()
            if not row:
                conn.commit()
                return False

            cur_status, cur_duration, cur_paste_ok, cur_app_bundle, cur_text, cur_error = row
            new_status = cur_status if status is None else status
            new_duration = cur_duration if duration_sec is None else duration_sec
            new_paste_ok = cur_paste_ok if paste_ok is None else int(bool(paste_ok))
            new_app_bundle = cur_app_bundle if app_bundle is None else app_bundle
            new_text = cur_text if text is None else text
            new_error = cur_error if error is None else error

            conn.execute(
                """
                UPDATE history
                SET status = ?, duration_sec = ?, paste_ok = ?,
                    app_bundle = ?, text = ?, error = ?
                WHERE id = ?
                """,
                (
                    new_status,
                    new_duration,
                    new_paste_ok,
                    new_app_bundle,
                    new_text,
                    new_error,
                    row_id,
                ),
            )
            conn.commit()
            return True
        finally:
            conn.close()


def _history_list(limit):
    if limit < 1:
        limit = HISTORY_SHOW_DEF
    limit = min(limit, HISTORY_SHOW_MAX)
    with _HISTORY_LOCK:
        conn = _history_connect()
        try:
            _history_ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT id, created_at, status, duration_sec, paste_ok, app_bundle, text, error
                FROM history
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()


def _history_last_text():
    with _HISTORY_LOCK:
        conn = _history_connect()
        try:
            _history_ensure_schema(conn)
            row = conn.execute(
                """
                SELECT text
                FROM history
                WHERE status = 'ok' AND text IS NOT NULL AND text <> ''
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            conn.commit()
            return row[0] if row else None
        finally:
            conn.close()


def _history_delete(row_id):
    with _HISTORY_LOCK:
        conn = _history_connect()
        try:
            _history_ensure_schema(conn)
            cur = conn.execute("DELETE FROM history WHERE id = ?", (row_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def _history_print(limit):
    rows = _history_list(limit)
    if not rows:
        print("○ No history yet.")
        return

    print(f"○ History ({len(rows)} newest):")
    for row in rows:
        hid, ts, status, dur, paste_ok, _app, text, err = row
        ts = ts.replace("T", " ").replace("Z", " UTC")
        dur_s = "-" if dur is None else f"{dur:.2f}s"
        if paste_ok is None:
            paste_s = "-"
        else:
            paste_s = "ok" if paste_ok else "fail"
        msg = _history_compact_message(text, err, max_chars=84)
        print(f"[{hid:>5}] {ts}  {status:<9} dur={dur_s:<7} paste={paste_s:<4} {msg}")


def _parse_cli(argv):
    show_history = False
    history_limit = HISTORY_SHOW_DEF
    recopy_last = False
    history_ui_process = False
    doctor = False
    probe_mlx = False

    i = 0
    while i < len(argv):
        arg = argv[i]
        if isinstance(arg, str) and (
            arg.startswith("-psn_")
            or (
                getattr(sys, "frozen", False)
                and len(arg) <= 3
                and arg.startswith("-")
                and not arg.startswith("--")
            )
        ):
            # Skip system-injected flags: -psn_ (macOS Finder process serial),
            # and short Python flags (-B, -S, -O, -OO, -s, -u, etc.) that
            # PyInstaller or macOS may pass to frozen .app bundles.
            i += 1
            continue
        if arg in ("-h", "--help"):
            return {
                "help": True,
                "show_history": False,
                "history_limit": history_limit,
                "recopy_last": False,
                "history_ui_process": False,
                "doctor": False,
                "probe_mlx": False,
            }
        if arg == "--history":
            show_history = True
            if i + 1 < len(argv):
                nxt = argv[i + 1]
                if nxt.isdigit():
                    history_limit = max(1, int(nxt))
                    i += 1
        elif arg == "--recopy-last":
            recopy_last = True
        elif arg == "--history-ui-process":
            history_ui_process = True
        elif arg == "--doctor":
            doctor = True
        elif arg == "--probe-mlx":
            probe_mlx = True
        else:
            raise ValueError(f"Unknown argument: {arg}")
        i += 1

    return {
        "help": False,
        "show_history": show_history,
        "history_limit": history_limit,
        "recopy_last": recopy_last,
        "history_ui_process": history_ui_process,
        "doctor": doctor,
        "probe_mlx": probe_mlx,
    }


def _print_usage():
    print("Usage:")
    print("  bloop.py                   # run push-to-talk")
    print("  bloop.py --history [N]     # show newest history rows")
    print("  bloop.py --recopy-last     # copy last successful transcript")
    print("  bloop.py --doctor          # runtime diagnostics + issue report")
    print("  bloop.py --help")


def _self_exec_command(extra_args):
    """Build a command that invokes this program with extra arguments."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *list(extra_args)]
    return [sys.executable, os.path.abspath(__file__), *list(extra_args)]


def _run_self_subprocess(extra_args, timeout=45):
    cmd = _self_exec_command(extra_args)
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return cmd, proc


def _summarize_probe_failure(proc):
    if proc is None:
        return "probe process did not start"
    if proc.returncode < 0:
        return f"probe terminated by signal {-proc.returncode}"

    stderr = (proc.stderr or "").strip()
    stdout = (proc.stdout or "").strip()
    lines = []
    if stderr:
        lines.extend(stderr.splitlines())
    if stdout:
        lines.extend(stdout.splitlines())
    tail = lines[-1].strip() if lines else ""
    if tail:
        tail = re.sub(r"\s+", " ", tail)
        if len(tail) > 180:
            tail = tail[:180] + "..."
        return f"probe exited {proc.returncode}: {tail}"
    return f"probe exited {proc.returncode}"


def _supports_subprocess_mlx_probe():
    """Return whether a child-process MLX probe is safe on this runtime."""
    # On frozen macOS app bundles, executing the app binary as a subprocess can
    # hit a different Metal/device-init path than the real app launch and abort
    # before Python can report a normal exception. Prefer in-process checks.
    return not (sys.platform == "darwin" and getattr(sys, "frozen", False))


def _capture_in_process_mlx_probe():
    """Run the MLX probe in-process and capture its text output."""
    import contextlib
    import io

    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = _run_mlx_probe()
    return rc, stdout.getvalue(), stderr.getvalue()


def _run_mlx_probe():
    print("== MLX probe ==")
    print(f"python: {sys.version.splitlines()[0]}")
    print(f"executable: {sys.executable}")
    print(f"frozen: {bool(getattr(sys, 'frozen', False))}")
    print(f"PATH: {os.environ.get('PATH', '')}")
    try:
        import mlx

        print(f"mlx: {getattr(mlx, '__file__', '-')}")
        try:
            print(f"mlx.__path__: {list(getattr(mlx, '__path__', []))}")
        except Exception:
            pass
    except Exception as exc:
        _issues_append("mlx_probe_import_mlx_failed", "doctor probe import mlx failed", exc=exc)
        print(f"ERR import mlx: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 1

    try:
        import mlx.core as mx

        print("mlx.core: ok")
        try:
            dev = mx.default_device()
            print(f"default_device: {dev}")
        except Exception as exc:
            print(f"default_device_err: {type(exc).__name__}: {exc}")
        try:
            sample = mx.array([1.0], dtype=mx.float32)
            mx.eval(sample)
            print("mx_eval: ok")
        except Exception as exc:
            print(f"mx_eval_err: {type(exc).__name__}: {exc}")
    except Exception as exc:
        _issues_append("mlx_probe_import_core_failed", "doctor probe import mlx.core failed", exc=exc)
        print(f"ERR import mlx.core: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 2

    try:
        import mlx_whisper  # noqa: F401

        print("mlx_whisper: ok")
    except Exception as exc:
        _issues_append("mlx_probe_import_whisper_failed", "doctor probe import mlx_whisper failed", exc=exc)
        print(f"ERR import mlx_whisper: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 3

    print("mlx_probe: ok")
    return 0


def _run_doctor():
    started = _utc_now_iso()
    lines = [
        "== Blooop doctor ==",
        f"started: {started}",
        f"build: {RUNTIME_BUILD}",
        f"executable: {sys.executable}",
        f"frozen: {bool(getattr(sys, 'frozen', False))}",
        f"python: {sys.version.splitlines()[0]}",
        f"app_base_dir: {APP_BASE_DIR}",
        f"state_dir: {STATE_DIR}",
        f"history_db: {HISTORY_DB_PATH}",
        f"issues_log: {ISSUES_LOG_PATH}",
        "",
        "-- running mlx probe --",
    ]
    try:
        if _supports_subprocess_mlx_probe():
            lines.append("probe_mode: subprocess")
            cmd, proc = _run_self_subprocess(["--probe-mlx"], timeout=45)
            lines.append(f"probe_cmd: {' '.join(cmd)}")
            probe_exit = proc.returncode
            probe_stdout = proc.stdout or ""
            probe_stderr = proc.stderr or ""
        else:
            lines.append("probe_mode: in-process")
            probe_exit, probe_stdout, probe_stderr = _capture_in_process_mlx_probe()

        lines.append(f"probe_exit: {probe_exit}")
        if probe_stdout:
            lines.append("probe_stdout:")
            lines.append(probe_stdout.rstrip())
        if probe_stderr:
            lines.append("probe_stderr:")
            lines.append(probe_stderr.rstrip())
        rc = 0 if probe_exit == 0 else 1
    except Exception as exc:
        _issues_append("doctor_subprocess_failed", "doctor mlx probe subprocess failed", exc=exc)
        lines.append(f"probe_error: {type(exc).__name__}: {exc}")
        rc = 2

    report = "\n".join(lines).rstrip() + "\n"
    report_path = _issues_write_report("doctor", report)
    if not report_path:
        try:
            fallback = os.path.join(
                tempfile.gettempdir(),
                f"blooop-doctor-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.log",
            )
            with open(fallback, "w", encoding="utf-8") as fh:
                fh.write(report)
            report_path = fallback
        except Exception:
            report_path = None
    if report_path:
        print(f"○ Doctor report: {report_path}")
    else:
        print("⚠ Doctor report could not be written.")
    print(report)
    if rc != 0:
        _issues_append("doctor_failed", "doctor detected mlx failure")
    return rc


# ── Native pill panel ─────────────────────────────────────────────────────────
# Tk can only create plain NSWindows, and scripts/overlay_probe.py proved those
# are never composited onto fullscreen Spaces, while borderless *nonactivating
# NSPanels* always are — at any window level, even from a regular Dock app.
# So the pill renders in a real NSPanel with a custom view; Tk keeps only the
# run loop, hotkey queue, and history plumbing.

try:
    from AppKit import NSView as _NSView

    class _BloopPillView(_NSView):
        """Content view for the native pill panel. Drawing only — state is
        precomputed each tick in WaveformVisualizer and attached as `_pill`."""

        def isFlipped(self):
            # Top-left origin so drawing matches the simulation coordinates.
            return True

        def drawRect_(self, rect):
            state = getattr(self, "_pill", None)
            if not isinstance(state, dict):
                return
            try:
                if state.get("style") == "spectrogram":
                    _draw_spectrogram_pill(state)
                else:
                    _draw_bubbles_pill(state)
            except Exception:
                pass
except Exception:
    _BloopPillView = None


def _pill_color(r, g, b, a=1.0):
    from AppKit import NSColor

    return NSColor.colorWithSRGBRed_green_blue_alpha_(r / 255.0, g / 255.0, b / 255.0, a)


def _create_pill_panel(width, height):
    """Borderless nonactivating NSPanel — the one window class macOS reliably
    composits onto fullscreen Spaces (empirically verified; see
    scripts/overlay_probe.py and docs/ISSUES.md #4)."""
    if _BloopPillView is None:
        raise RuntimeError("AppKit unavailable")
    from AppKit import (
        NSBackingStoreBuffered,
        NSColor,
        NSPanel,
        NSPopUpMenuWindowLevel,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorFullScreenAuxiliary,
        NSWindowCollectionBehaviorStationary,
        NSWindowStyleMaskBorderless,
        NSWindowStyleMaskNonactivatingPanel,
    )
    from Foundation import NSMakeRect

    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, width, height),
        NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
        NSBackingStoreBuffered,
        False,
    )
    # Probe result: level is irrelevant to fullscreen visibility for panels.
    # PopUpMenu level (101) floats over app windows but stays *below*
    # notification banners — better neighbor than the old screen-saver level.
    panel.setLevel_(int(NSPopUpMenuWindowLevel))
    panel.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces
        | NSWindowCollectionBehaviorFullScreenAuxiliary
        | NSWindowCollectionBehaviorStationary
    )
    panel.setIgnoresMouseEvents_(True)
    panel.setOpaque_(False)
    panel.setBackgroundColor_(NSColor.clearColor())
    panel.setHasShadow_(True)
    panel.setReleasedWhenClosed_(False)
    view = _BloopPillView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
    panel.setContentView_(view)
    return panel, view


def _cg_frontmost_window_bounds():
    """Bounds (CG coords: top-left origin, y down) of the frontmost regular
    window of any *other* app on the active Space, or None. CGWindowList
    returns front-to-back, so the first layer-0 hit is the focused window."""
    try:
        import Quartz

        info = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
        me = os.getpid()
        for win in info or []:
            try:
                if int(win.get("kCGWindowLayer", -1)) != 0:
                    continue
                if int(win.get("kCGWindowOwnerPID", -1)) == me:
                    continue
                if float(win.get("kCGWindowAlpha", 1.0)) <= 0.05:
                    continue
                b = win.get("kCGWindowBounds") or {}
                w, h = float(b.get("Width", 0.0)), float(b.get("Height", 0.0))
                if w < 64.0 or h < 64.0:
                    continue  # status-item slivers, palettes
                return (float(b.get("X", 0.0)), float(b.get("Y", 0.0)), w, h)
            except Exception:
                continue
    except Exception:
        pass
    return None


def _screen_containing_appkit_point(x, y, screens):
    for screen in screens:
        try:
            f = screen.frame()
            if (
                f.origin.x <= x < f.origin.x + f.size.width
                and f.origin.y <= y < f.origin.y + f.size.height
            ):
                return screen
        except Exception:
            continue
    return None


# The active-screen lookup walks the window list, so cache it between
# reposition ticks; force=True (used when the pill is shown) bypasses it.
_PILL_SCREEN_CACHE = {"t": 0.0, "screen": None}
_PILL_SCREEN_REFRESH_SEC = 0.5


def _pill_target_screen(force=False):
    """The screen the user is working on: prefer the screen hosting the
    focused (frontmost) window, then the one with the pointer, then primary.
    This keeps the pill on whichever display the user is actually looking at
    instead of hard-pinning it to the primary screen."""
    from AppKit import NSEvent, NSScreen

    screens = list(NSScreen.screens() or [])
    if not screens:
        return NSScreen.mainScreen()
    if len(screens) == 1:
        return screens[0]

    now = time.monotonic()
    cached = _PILL_SCREEN_CACHE["screen"]
    if (
        not force
        and cached is not None
        and (now - _PILL_SCREEN_CACHE["t"]) < _PILL_SCREEN_REFRESH_SEC
        and any(cached is s for s in screens)
    ):
        return cached

    chosen = None
    # CG coords have a top-left origin; AppKit a bottom-left one. Both are
    # anchored to the primary screen (screens[0], whose frame origin is 0,0).
    primary_h = screens[0].frame().size.height
    bounds = _cg_frontmost_window_bounds()
    if bounds is not None:
        cx = bounds[0] + bounds[2] / 2.0
        cy = primary_h - (bounds[1] + bounds[3] / 2.0)
        chosen = _screen_containing_appkit_point(cx, cy, screens)
    if chosen is None:
        try:
            loc = NSEvent.mouseLocation()
            chosen = _screen_containing_appkit_point(loc.x, loc.y, screens)
        except Exception:
            chosen = None
    if chosen is None:
        chosen = screens[0]

    _PILL_SCREEN_CACHE["t"] = now
    _PILL_SCREEN_CACHE["screen"] = chosen
    return chosen


def _pill_panel_is_composited(panel):
    """Ask the window server whether the panel is actually on the active
    Space. True/False when known, None when the check itself failed (never
    act on None). This is the ground truth `orderFrontRegardless` can't see:
    the panel can believe it is visible while the server has dropped it."""
    try:
        import Quartz

        num = int(panel.windowNumber())
        if num <= 0:
            return None
        info = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionIncludingWindow, num
        )
        if not info:
            return False
        return bool(info[0].get("kCGWindowIsOnscreen", False))
    except Exception:
        return None


def _position_pill_panel(panel, width, height, force=False):
    """Pin to the top-right of the active screen, under the menu bar."""
    try:
        screen = _pill_target_screen(force=force)
        if screen is None:
            return
        vf = screen.visibleFrame()  # excludes menu bar (and Dock)
        x = vf.origin.x + vf.size.width - width - 18
        y = vf.origin.y + vf.size.height - height - 12
        panel.setFrameOrigin_((x, y))
    except Exception:
        pass


def _draw_bubbles_pill(state):
    """Wordless deep-water capsule: a state dot + voice-driven bubbles."""
    from AppKit import NSBezierPath, NSGradient, NSGraphicsContext

    w, h = float(state["w"]), float(state["h"])
    radius = (h - 1.0) / 2.0
    capsule = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        ((0.5, 0.5), (w - 1.0, h - 1.0)), radius, radius
    )
    grad = NSGradient.alloc().initWithStartingColor_endingColor_(
        _pill_color(0x0B, 0x1D, 0x2A), _pill_color(0x06, 0x12, 0x1C)
    )
    grad.drawInBezierPath_angle_(capsule, 90.0)

    rec = state.get("mode") == "recording"
    phase = float(state.get("phase", 0.0))

    ctx = NSGraphicsContext.currentContext()
    ctx.saveGraphicsState()
    capsule.addClip()

    if rec:
        stroke = _pill_color(0x7F, 0xD8, 0xE8)
        fill = _pill_color(0x8C, 0xDC, 0xF0)
    else:
        stroke = _pill_color(0x9A, 0x86, 0xFF)
        fill = _pill_color(0x9A, 0x86, 0xFF)
    for b in state.get("bubbles", ()):
        if not b.get("alive"):
            continue
        alpha = min(1.0, (b["y"] / h) * 0.4 + 0.45)
        bx = b["x"] + math.sin(phase * 0.004 + b["wob"]) * 2.0
        r = b["r"]
        oval = NSBezierPath.bezierPathWithOvalInRect_(((bx - r, b["y"] - r), (r * 2, r * 2)))
        fill.colorWithAlphaComponent_(0.16 * alpha).set()
        oval.fill()
        stroke.colorWithAlphaComponent_(0.9 * alpha).set()
        oval.setLineWidth_(1.0)
        oval.stroke()

    # State dot: warm buoy-red while listening, violet while thinking. Driven
    # by the smoothed envelope so it breathes with speech instead of
    # dithering with per-block RMS.
    if rec:
        env = float(state.get("env", state.get("level", 0.0)))
        dot_r = 3.0 + 1.8 * min(1.0, env * 1.3)
        dot = _pill_color(0xFF, 0x6B, 0x5E)
    else:
        dot_r = 3.0 + 0.8 * (0.5 + 0.5 * math.sin(phase * 0.003))
        dot = _pill_color(0x9A, 0x86, 0xFF)
    cy = h / 2.0
    dot.set()
    NSBezierPath.bezierPathWithOvalInRect_(
        ((12.0 - dot_r, cy - dot_r), (dot_r * 2, dot_r * 2))
    ).fill()

    ctx.restoreGraphicsState()
    _pill_color(0x1C, 0x43, 0x56).set()
    capsule.setLineWidth_(1.0)
    capsule.stroke()


_HEAT_LUT = None


def _heat_lut():
    """Navy → teal → cyan → white colormap, 48 precomputed NSColors."""
    global _HEAT_LUT
    if _HEAT_LUT is not None:
        return _HEAT_LUT

    def mix(c1, c2, t):
        return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))

    stops = [(0x0A, 0x18, 0x30), (0x15, 0x5E, 0x7A), (0x3F, 0xD0, 0xE8), (0xE8, 0xFB, 0xFF)]
    cuts = [0.0, 0.33, 0.7, 1.0]
    lut = []
    for i in range(48):
        x = i / 47.0
        for k in range(3):
            if x <= cuts[k + 1]:
                t = (x - cuts[k]) / (cuts[k + 1] - cuts[k])
                r, g, b = mix(stops[k], stops[k + 1], t)
                break
        lut.append(_pill_color(r, g, b))
    _HEAT_LUT = lut
    return lut


def _draw_spectrogram_pill(state):
    """NOAA-chart heat trace of the voice, scrolling right-to-left."""
    from AppKit import (
        NSBezierPath,
        NSFont,
        NSFontAttributeName,
        NSForegroundColorAttributeName,
        NSGraphicsContext,
        NSRectFill,
    )

    w, h = float(state["w"]), float(state["h"])
    shell = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        ((0.5, 0.5), (w - 1.0, h - 1.0)), 9.0, 9.0
    )
    _pill_color(0x07, 0x0D, 0x16).set()
    shell.fill()

    ctx = NSGraphicsContext.currentContext()
    ctx.saveGraphicsState()
    shell.addClip()

    lut = _heat_lut()
    hist = state.get("hist", ())
    n = len(hist)
    colw = 3.0
    for i, q in enumerate(hist):
        x = w - (n - i) * colw - 4.0
        if x < 2.0:
            continue
        y = 2.0
        while y < h - 2.0:
            f = 1.0 - (y / h)
            e = q * (1.15 - f) - abs(f - q * 0.6) * 0.5 + math.sin(i * 0.7 + y) * 0.04
            e = max(0.0, min(1.0, e * 1.45))
            lut[int(e * 47)].set()
            NSRectFill(((x, y), (2.2, 1.8)))
            y += 2.1

    rec = state.get("mode") == "recording"
    if not rec:
        _pill_color(0x6F, 0xD8, 0xFF, 0.7).set()
        NSRectFill(((4.0 + float(state.get("scan", 0.0)), 2.0), (1.4, h - 4.0)))

    label = "REC" if rec else "TXT"
    attrs = {
        NSFontAttributeName: NSFont.boldSystemFontOfSize_(7.5),
        NSForegroundColorAttributeName: _pill_color(0x6F, 0xD8, 0xFF)
        if rec
        else _pill_color(0x4F, 0x7D, 0xA6),
    }
    try:
        from Foundation import NSString

        NSString.stringWithString_(label).drawAtPoint_withAttributes_((w - 27.0, 4.0), attrs)
    except Exception:
        pass

    ctx.restoreGraphicsState()
    _pill_color(0x1B, 0x2C, 0x44).set()
    shell.setLineWidth_(1.0)
    shell.stroke()


# ── Waveform visualizer ───────────────────────────────────────────────────────

class WaveformVisualizer:
    """Small floating corner chip with animated bars for recording/transcribing."""

    BAR_COUNT = 7
    BAR_W     = 7       # thickness of each bar
    BAR_GAP   = 8       # gap between bars
    PAD_X     = 14      # horizontal padding inside chip
    PAD_Y     = 8       # vertical padding inside chip
    H         = 38      # chip height
    STATUS_W  = 38      # space reserved for asterisk + status label
    STATUS_GAP = 10
    EDGE_MARGIN_X = 18
    EDGE_MARGIN_Y = 38
    FPS       = 36
    IDLE_FPS  = 20      # hidden/idle tick rate; hotkey queue still drains each tick
    # Fable palette: warm charcoal card, coral accent, cream text. The window
    # itself stays opaque (transparent Tk windows regressed in the packaged
    # app), so the square corners are painted a near-black matte that reads
    # as shadow against any wallpaper.
    BG_OUTER  = "#161514"   # canvas/window matte behind the card
    BG_INNER  = "#262624"   # card fill
    SHADOW    = "#0c0b0a"
    BORDER    = "#453f38"
    TEXT      = "#f0eee5"
    TEXT_MUTED = "#b3aca0"
    CORAL     = "#d97757"
    CORAL_SOFT = "#c98a6e"
    CORAL_LIGHT = "#f0a987"
    BAR_LOW   = "#564e46"
    SAND      = "#c9b79b"
    # Slightly uneven ray lengths keep the asterisk organic instead of gear-like.
    RAY_PATTERN = (1.0, 0.78, 0.94, 0.78, 1.0, 0.78, 0.94, 0.78)
    VISIBLE_ALPHA = 0.98
    HIDDEN_ALPHA  = 0.0
    REPOSITION_INTERVAL_SEC = 0.16
    FRONT_ASSERT_INTERVAL_SEC = 0.5
    # Not-composited escalation: after this many consecutive failed checks
    # the orderOut/orderFront recycle has demonstrably not re-composited the
    # panel (observed 2026-07-07..09: recycle never recovered once the server
    # dropped the window), so rebuild it under a fresh window number. Capped
    # per visibility episode so a hostile window-server state can't make us
    # churn panels twice a second forever.
    PILL_REBUILD_AFTER_DROPS = 2
    PILL_MAX_REBUILDS_PER_EPISODE = 3

    def __init__(self, enabled=True, show_window=True, pill_style="bubbles"):
        self._enabled = bool(enabled)
        # When False, the pill window is suppressed and state is surfaced
        # only via the menu bar icon. The Tk root still has to exist — it
        # drives the main loop and hosts the history panel as a child window.
        self._show_window = bool(show_window)
        self._pill_style = pill_style if pill_style in PILL_STYLES else PILL_STYLES[0]
        # Derive width from bar layout
        bars_w = self.BAR_COUNT * self.BAR_W + (self.BAR_COUNT - 1) * self.BAR_GAP
        self.W = self.PAD_X * 2 + self.STATUS_W + self.STATUS_GAP + bars_w

        self.root = tk.Tk()
        _set_macos_app_identity("Blooop")
        try:
            self.root.title("Blooop")
        except Exception:
            pass
        self.root.withdraw()
        self._overlay = None
        self._native_panel = None
        self._native_view = None
        self._window = self.root
        if self._show_window and sys.platform == "darwin":
            # Preferred: real NSPanel — the only window class composited onto
            # fullscreen Spaces (scripts/overlay_probe.py).
            try:
                pw, ph = PILL_STYLE_DIMS[self._pill_style]
                self._native_panel, self._native_view = _create_pill_panel(pw, ph)
                self.W, self.H = pw, ph
            except Exception as exc:
                print(f"⚠ Native pill panel unavailable ({exc}); using Tk overlay fallback.")
                self._native_panel = None
                self._native_view = None
        if self._show_window and self._native_panel is None:
            try:
                self._overlay = tk.Toplevel(self.root)
                self._overlay.withdraw()
                self._overlay.title("Blooop Overlay")
                self._window = self._overlay
            except Exception:
                self._overlay = None
                self._window = self.root
        if self._enabled and self._overlay is not None:
            self._window.overrideredirect(True)
            self._window.attributes("-topmost", False)
        # Prefer an opaque surface here. Tk's transparent-window modes have
        # been unreliable in the packaged app and can make the chip disappear
        # entirely even while recording is active.
        self._canvas_bg = self.BG_OUTER
        try:
            self._window.configure(bg=self.BG_OUTER)
        except Exception:
            pass

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x  = max(self.EDGE_MARGIN_X, sw - self.W - self.EDGE_MARGIN_X)
        y  = min(
            max(self.EDGE_MARGIN_Y, 0),
            max(self.EDGE_MARGIN_Y, sh - self.H - 72),
        )
        self._window.geometry(f"{self.W}x{self.H}+{x}+{y}")
        if self._show_window and self._overlay is not None:
            _macos_configure_nonactivating_overlay(self._window)

        self.canvas = tk.Canvas(
            self._window, width=self.W, height=self.H,
            bg=self._canvas_bg, highlightthickness=0,
        )
        self.canvas.pack()

        self._levels   = deque([0.0] * self.BAR_COUNT, maxlen=self.BAR_COUNT)
        self._smooth   = [0.0] * self.BAR_COUNT
        self._visible  = False
        self._show_req = False
        self._hide_req = False
        self._mode     = "idle"
        self._phase    = 0.0
        self._on_activate = None
        self._on_active_change = None
        self._on_mode_change = None
        self._on_level_change = None
        self._last_app_active = _macos_app_is_active()
        self._ui_callbacks = queue.Queue()
        self._alpha_supported = False
        self._last_overlay_reposition = 0.0
        self._last_front_assert = 0.0
        # One breadcrumb per visibility episode when the window server drops
        # the pill from the active Space (see _pill_panel_is_composited).
        self._pill_drop_logged = False
        self._pill_drop_streak = 0
        self._pill_rebuilds = 0
        # Native pill simulations (advanced in _draw_native).
        self._bubble_pool = [
            dict(alive=False, x=0.0, y=0.0, r=1.0, vy=0.0, wob=random.random() * 6.0)
            for _ in range(28)
        ]
        self._bubble_last_ms = 0.0
        # Fast-attack / slow-decay loudness envelope: keeps the bubble stream
        # and the state dot alive through natural syllable gaps instead of
        # flickering with the instantaneous RMS.
        self._bubble_env = 0.0
        self._spec_hist = deque(maxlen=max(8, int((self.W - 8) / 3)))
        self._spec_scan = 0.0
        if self._show_window:
            self._prime_overlay_window()

    # ── Thread-safe API ───────────────────────────────────────────────────────

    def show(self):
        if not self._enabled:
            return
        self._mode = "recording"
        self._show_req = True
        self._hide_req = False
        self._notify_mode("recording")

    def show_transcribing(self):
        if not self._enabled:
            return
        self._mode = "transcribing"
        self._show_req = True
        self._hide_req = False
        self._notify_mode("transcribing")

    def hide(self):
        if not self._enabled:
            return
        self._hide_req = True
        self._show_req = False
        self._notify_mode("idle")

    def push_level(self, rms: float):
        self._levels.append(min(rms, 1.0))

    def set_on_activate(self, callback):
        self._on_activate = callback

    def set_on_active_change(self, callback):
        self._on_active_change = callback

    def set_on_mode_change(self, callback):
        """Fires with 'recording' | 'transcribing' | 'idle' on state change."""
        self._on_mode_change = callback

    def set_on_level_change(self, callback):
        """Fires from the UI tick with the latest RMS (0.0..1.0)."""
        self._on_level_change = callback

    def post_to_ui(self, callback):
        if callback is None:
            return
        try:
            self._ui_callbacks.put_nowait(callback)
        except Exception:
            pass

    def _notify_mode(self, mode):
        cb = self._on_mode_change
        if cb is None:
            return
        self.post_to_ui(lambda: cb(mode))

    def _drain_ui_callbacks(self):
        while True:
            try:
                cb = self._ui_callbacks.get_nowait()
            except queue.Empty:
                return
            try:
                cb()
            except Exception:
                pass

    def _prime_overlay_window(self):
        if not self._show_window or self._native_panel is not None:
            return
        try:
            self._window.attributes("-alpha", self.HIDDEN_ALPHA)
            self._alpha_supported = True
        except Exception:
            self._alpha_supported = False
        try:
            self._window.deiconify()
        except Exception:
            pass
        if not self._alpha_supported:
            try:
                self._window.withdraw()
            except Exception:
                pass

    def _rebuild_native_panel(self):
        """Replace the pill NSPanel with a freshly created one. A new panel
        gets a new window number, which re-registers it with the window
        server — the recovery of last resort once the server has dropped the
        old window's record from the active Space and order recycling no
        longer takes. Create-then-swap so a failed create leaves the old
        panel (and the recycle fallback) in place."""
        try:
            pw, ph = PILL_STYLE_DIMS[self._pill_style]
            panel, view = _create_pill_panel(pw, ph)
        except Exception as exc:
            _issues_append("pill_rebuild_failed", "pill panel recreate failed", exc=exc)
            return False
        old = self._native_panel
        self._native_panel = panel
        self._native_view = view
        try:
            _position_pill_panel(panel, self.W, self.H, force=True)
            panel.orderFrontRegardless()
        except Exception:
            pass
        if old is not None:
            try:
                old.orderOut_(None)
                old.close()
            except Exception:
                pass
        _issues_append(
            "pill_panel_rebuilt",
            f"pill panel recreated after failed recycle "
            f"(rebuild {self._pill_rebuilds}/{self.PILL_MAX_REBUILDS_PER_EPISODE} this episode)",
        )
        return True

    def _set_overlay_visible(self, visible):
        if not self._show_window:
            return
        if self._native_panel is not None:
            try:
                if visible:
                    self._pill_drop_logged = False
                    self._pill_drop_streak = 0
                    self._pill_rebuilds = 0
                    _position_pill_panel(self._native_panel, self.W, self.H, force=True)
                    self._native_panel.orderFrontRegardless()
                else:
                    self._native_panel.orderOut_(None)
            except Exception:
                pass
            return
        try:
            _macos_configure_nonactivating_overlay(self._window)
        except Exception:
            pass
        appkit_managed = False
        if visible:
            try:
                appkit_managed = bool(
                    _macos_prepare_overlay_window(self._window, front=True)
                )
            except Exception:
                appkit_managed = False
            self._reposition_overlay(force=True)
        if not appkit_managed:
            # Fallback only (non-macOS or the NSWindow lookup failed). Tk's
            # -topmost resets the NSWindow level to a tier that loses to
            # fullscreen Spaces, so it must never run after the AppKit path
            # has set NSScreenSaverWindowLevel.
            try:
                self._window.attributes("-topmost", bool(visible))
            except Exception:
                pass
        if self._alpha_supported:
            try:
                alpha = self.VISIBLE_ALPHA if visible else self.HIDDEN_ALPHA
                self._window.attributes("-alpha", alpha)
                if visible:
                    try:
                        self._window.deiconify()
                    except Exception:
                        pass
                return
            except Exception:
                self._alpha_supported = False
        if visible:
            try:
                self._window.deiconify()
            except Exception:
                pass
        else:
            try:
                self._window.withdraw()
            except Exception:
                pass

    def _reposition_overlay(self, force=False):
        if not self._show_window:
            return
        now = time.monotonic()
        if not force and (now - self._last_overlay_reposition) < self.REPOSITION_INTERVAL_SEC:
            return
        self._last_overlay_reposition = now
        if self._native_panel is not None:
            _position_pill_panel(self._native_panel, self.W, self.H)
            return
        # Anchor to the top-right corner of the primary screen, below the
        # menu bar / notch. We deliberately do NOT track the cursor: a pill
        # that jumps with pointer movement reads as broken, and on macOS it
        # interacts badly with Spaces (the overlay appears to "leak" across
        # displays as the user drags the mouse).
        try:
            sw = int(self.root.winfo_screenwidth())
            sh = int(self.root.winfo_screenheight())
        except Exception:
            return
        x = max(self.EDGE_MARGIN_X, sw - self.W - self.EDGE_MARGIN_X)
        y = min(
            max(self.EDGE_MARGIN_Y, 0),
            max(self.EDGE_MARGIN_Y, sh - self.H - 72),
        )
        try:
            self._window.geometry(f"{self.W}x{self.H}+{int(x)}+{int(y)}")
        except Exception:
            pass

    # ── Main-thread animation loop ────────────────────────────────────────────

    def _tick(self):
        self._drain_ui_callbacks()
        if not self._enabled:
            self.root.after(self._tick_interval_ms(), self._tick)
            return
        active = _macos_app_is_active()
        if active != self._last_app_active and self._on_active_change is not None:
            try:
                self._on_active_change(active)
            except Exception:
                pass
        if active and not self._last_app_active and self._on_activate is not None:
            try:
                self._on_activate()
            except Exception:
                pass
        self._last_app_active = active

        if self._show_req:
            self._show_req = False
            self._visible  = True
            if self._show_window:
                self._set_overlay_visible(True)
        if self._hide_req:
            self._hide_req = False
            self._visible  = False
            if self._show_window:
                self._set_overlay_visible(False)

        if self._visible and self._show_window:
            if self._native_panel is not None:
                # NSPanels stay composited across Space switches (probe-
                # verified); a cheap periodic re-front covers pathological
                # window-server reshuffles.
                now = time.monotonic()
                if (now - self._last_front_assert) >= self.FRONT_ASSERT_INTERVAL_SEC:
                    self._last_front_assert = now
                    try:
                        if _pill_panel_is_composited(self._native_panel) is False:
                            # The server dropped us from the active Space even
                            # though the panel thinks it is visible. Cycle
                            # out+front first; breadcrumb once per visibility
                            # episode so issues.log records the invisible-
                            # while-recording windows. If the drop persists
                            # across checks the recycle isn't taking — the
                            # panel's window-server record is stuck — so
                            # escalate to a full rebuild (fresh window
                            # number), bounded per episode.
                            self._pill_drop_streak += 1
                            if not self._pill_drop_logged:
                                self._pill_drop_logged = True
                                _issues_append(
                                    "pill_not_composited",
                                    "pill panel missing from active Space while visible; recycling window order",
                                )
                            if (
                                self._pill_drop_streak >= self.PILL_REBUILD_AFTER_DROPS
                                and self._pill_rebuilds < self.PILL_MAX_REBUILDS_PER_EPISODE
                            ):
                                self._pill_drop_streak = 0
                                self._pill_rebuilds += 1
                                self._rebuild_native_panel()
                            else:
                                self._native_panel.orderOut_(None)
                                self._native_panel.orderFrontRegardless()
                        else:
                            self._pill_drop_streak = 0
                            self._native_panel.orderFrontRegardless()
                    except Exception:
                        pass
                self._reposition_overlay()
                self._draw_native()
            else:
                # Tk fallback. Periodically reassert window level + front
                # ordering via AppKit. Never use Tk's -topmost first: it
                # stomps the NSWindow level back below fullscreen windows.
                now = time.monotonic()
                if (now - self._last_front_assert) >= self.FRONT_ASSERT_INTERVAL_SEC:
                    self._last_front_assert = now
                    fronted = False
                    try:
                        fronted = bool(
                            _macos_prepare_overlay_window(self._window, front=True)
                        )
                    except Exception:
                        fronted = False
                    if not fronted:
                        try:
                            self._window.attributes("-topmost", True)
                        except Exception:
                            pass
                self._reposition_overlay()
                self._draw()

        # Feed the latest RMS to subscribers (menu-bar pulse, etc.) while
        # recording so the indicator reflects live mic activity.
        if self._mode == "recording" and self._on_level_change is not None:
            try:
                latest = float(self._levels[-1]) if len(self._levels) else 0.0
            except Exception:
                latest = 0.0
            try:
                self._on_level_change(latest)
            except Exception:
                pass

        self.root.after(self._tick_interval_ms(), self._tick)

    def _tick_interval_ms(self):
        # Full frame rate only while the pill is visible or a session is live;
        # idle slower otherwise to cut steady-state CPU over long uptimes.
        # The hotkey queue drains every tick, so the worst-case extra press
        # latency at IDLE_FPS is ~50ms.
        if self._visible or self._show_req or self._mode in ("recording", "transcribing"):
            return 1000 // self.FPS
        return 1000 // self.IDLE_FPS

    def _mix_color(self, c1: str, c2: str, t: float) -> str:
        t = max(0.0, min(1.0, t))
        r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
        r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
        r = int(r1 + (r2 - r1) * t)
        g = int(g1 + (g2 - g1) * t)
        b = int(b1 + (b2 - b1) * t)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _bar_color(self, s: float, transcribing: bool = False) -> str:
        s = max(0.0, min(1.0, s))
        if transcribing:
            return self._mix_color(self.BAR_LOW, self.SAND, s)
        # Quiet bars sit in warm umber; louder ones bloom into coral.
        if s < 0.6:
            return self._mix_color(self.BAR_LOW, self.CORAL, s / 0.6)
        return self._mix_color(self.CORAL, self.CORAL_LIGHT, (s - 0.6) / 0.4)

    def _draw(self):
        c   = self.canvas
        c.delete("all")

        self._phase += 0.22

        # Card: warm charcoal capsule, hairline border, soft bottom shadow.
        # Flat and calm — no gloss.
        r = (self.H - 2) // 2
        self._rrect(c, 1, 2, self.W - 1, self.H, r, self.SHADOW)
        self._rrect(c, 1, 0, self.W - 1, self.H - 2, r, self.BG_INNER)
        self._rrect_outline(c, 1, 0, self.W - 1, self.H - 2, r, self.BORDER, 1)

        max_h = (self.H - 2) - self.PAD_Y * 2
        cy    = (self.H - 2) // 2

        try:
            latest = float(self._levels[-1]) if len(self._levels) else 0.0
        except Exception:
            latest = 0.0

        transcribing = self._mode == "transcribing"
        if transcribing:
            status_label = "TXT"
            label_fill = self.TEXT_MUTED
            # Thinking: the asterisk spins slowly at a steady size.
            spin = self._phase * 0.30
            scale = 0.88 + 0.05 * math.sin(self._phase * 0.8)
            ast_color = self.CORAL_SOFT
        else:
            status_label = "REC"
            label_fill = self.TEXT
            # Listening: the asterisk breathes with the live mic level.
            spin = 0.0
            breath = 0.5 + 0.5 * math.sin(self._phase * 0.5)
            scale = min(0.74 + 0.12 * breath + 0.5 * min(latest * 1.6, 1.0), 1.3)
            ast_color = self.CORAL

        self._draw_asterisk(c, self.PAD_X + 7, cy, 6.8 * scale, ast_color, spin=spin)
        c.create_text(
            self.PAD_X + 17,
            cy + 1,
            text=status_label,
            anchor="w",
            fill=label_fill,
            font=("TkDefaultFont", 9, "bold"),
        )
        # x-center of first bar
        x0 = self.PAD_X + self.STATUS_W + self.STATUS_GAP + self.BAR_W // 2

        for i, lvl in enumerate(list(self._levels)):
            # One warm theme in both states; transcribing only changes the
            # motion profile and mutes the palette toward sand.
            if transcribing:
                self._smooth[i] *= 0.93
                pulse = 0.18 + 0.16 * (0.5 + 0.5 * math.sin(self._phase * 0.9 + i * 0.8))
                s = max(self._smooth[i], pulse)
            else:
                self._smooth[i] += (lvl - self._smooth[i]) * 0.36
                idle = 0.05 + 0.04 * (0.5 + 0.5 * math.sin(self._phase * 0.7 + i * 0.8))
                s = max(min(self._smooth[i] * 1.45, 1.0), idle)

            color = self._bar_color(s, transcribing)
            h     = max(self.BAR_W + 1, int((0.06 + s * 0.94) * max_h))
            cx    = x0 + i * (self.BAR_W + self.BAR_GAP)
            glow  = self._mix_color(color, self.BG_INNER, 0.66)

            # Round-capped bars read better than hard rectangles at this size.
            c.create_line(
                cx, cy - h // 2,
                cx, cy + h // 2,
                width=self.BAR_W + 3, fill=glow, capstyle=tk.ROUND,
            )
            c.create_line(
                cx, cy - h // 2,
                cx, cy + h // 2,
                width=self.BAR_W, fill=color, capstyle=tk.ROUND,
            )

    def _draw_asterisk(self, c, cx, cy, radius, color, spin=0.0):
        """Eight-ray Fable asterisk drawn from short inner stubs outward."""
        for i, frac in enumerate(self.RAY_PATTERN):
            ang = spin + i * (math.pi / 4.0)
            x1 = cx + math.cos(ang) * radius * 0.22
            y1 = cy + math.sin(ang) * radius * 0.22
            x2 = cx + math.cos(ang) * radius * frac
            y2 = cy + math.sin(ang) * radius * frac
            c.create_line(x1, y1, x2, y2, width=2, fill=color, capstyle=tk.ROUND)

    # ── Native pill rendering ────────────────────────────────────────────────

    def _update_bubbles(self, level, mode, now_ms):
        dt = min(40.0, now_ms - self._bubble_last_ms) if self._bubble_last_ms else 16.0
        self._bubble_last_ms = now_ms
        recording = mode == "recording"
        # Envelope: jump up instantly with the voice, bleed off over ~0.3s.
        self._bubble_env = max(float(level), self._bubble_env * 0.94)
        env = self._bubble_env
        # Linear in loudness — quiet conversational speech (~0.2) should still
        # read as a clear stream (~6 bubbles/s at 36fps), loud speech a boil.
        rate = min(0.85, 0.05 + env * 0.65) if recording else 0.05
        if random.random() < rate:
            for b in self._bubble_pool:
                if not b["alive"]:
                    b["alive"] = True
                    b["x"] = 24.0 + random.random() * (self.W - 36.0)
                    b["y"] = self.H + 3.0
                    if recording:
                        b["r"] = 1.2 + env * 3.2 * (0.5 + random.random() * 0.7)
                        b["vy"] = 0.3 + random.random() * 0.35 + env * 0.45
                    else:
                        b["r"] = 1.0 + random.random() * 0.8
                        b["vy"] = 0.3 + random.random() * 0.35
                    break
        for b in self._bubble_pool:
            if b["alive"]:
                b["y"] -= b["vy"] * (dt / 16.0)
                if b["y"] < -3.0:
                    b["alive"] = False

    def _draw_native(self):
        view = self._native_view
        if view is None:
            return
        try:
            latest = float(self._levels[-1]) if len(self._levels) else 0.0
        except Exception:
            latest = 0.0
        now_ms = time.monotonic() * 1000.0
        if self._pill_style == "spectrogram":
            if self._mode == "recording":
                self._spec_hist.append(latest)
                self._spec_scan = 0.0
            else:
                self._spec_scan = (self._spec_scan + 1.4) % max(1.0, self.W - 8.0)
        else:
            self._update_bubbles(latest, self._mode, now_ms)
        view._pill = {
            "style": self._pill_style,
            "mode": self._mode,
            "level": latest,
            "env": self._bubble_env,
            "w": self.W,
            "h": self.H,
            "bubbles": self._bubble_pool,
            "hist": list(self._spec_hist),
            "scan": self._spec_scan,
            "phase": now_ms,
        }
        try:
            view.display()
        except Exception:
            pass

    def _rrect(self, c, x1, y1, x2, y2, r, fill):
        # Compose the pill from two circles + center rect to avoid seam lines
        # that can appear when layering multiple arc segments.
        c.create_oval(x1, y1, x1 + 2 * r, y2, fill=fill, outline="")
        c.create_oval(x2 - 2 * r, y1, x2, y2, fill=fill, outline="")
        c.create_rectangle(x1 + r, y1, x2 - r, y2, fill=fill, outline="")

    def _rrect_outline(self, c, x1, y1, x2, y2, r, outline, width=1):
        c.create_arc(x1, y1, x1 + 2 * r, y1 + 2 * r, start=90, extent=90,
                     style=tk.ARC, outline=outline, width=width)
        c.create_arc(x2 - 2 * r, y1, x2, y1 + 2 * r, start=0, extent=90,
                     style=tk.ARC, outline=outline, width=width)
        c.create_arc(x2 - 2 * r, y2 - 2 * r, x2, y2, start=270, extent=90,
                     style=tk.ARC, outline=outline, width=width)
        c.create_arc(x1, y2 - 2 * r, x1 + 2 * r, y2, start=180, extent=90,
                     style=tk.ARC, outline=outline, width=width)
        c.create_line(x1 + r, y1, x2 - r, y1, fill=outline, width=width)
        c.create_line(x2, y1 + r, x2, y2 - r, fill=outline, width=width)
        c.create_line(x1 + r, y2, x2 - r, y2, fill=outline, width=width)
        c.create_line(x1, y1 + r, x1, y2 - r, fill=outline, width=width)

    def start(self):
        self._tick()
        self.root.mainloop()

    def stop(self):
        if self._native_panel is not None:
            try:
                self._native_panel.orderOut_(None)
            except Exception:
                pass
        try:
            self.root.quit()
        except Exception:
            pass


# ── History panel ──────────────────────────────────────────────────────────────


class WebviewHistoryPanel:
    """Proxy that hosts the history UI in a pywebview subprocess.

    Keeps the public surface of HistoryPanel (start/show/stop/refresh_if_visible/
    on_app_active_change/_is_visible/_settings_visible/_toggle_settings_panel)
    so callers in App don't need to know which implementation is active.
    """

    def __init__(self, _root=None):
        self._proc = None
        self._lock = threading.Lock()
        self._settings_visible = False
        self._raise_seq = 0
        self._settings_seq = 0

    def _proc_alive(self):
        return self._proc is not None and self._proc.poll() is None

    def _write_command(self):
        try:
            os.makedirs(os.path.dirname(HISTORY_COMMAND_FILE), exist_ok=True)
            payload = {
                "raise_seq": self._raise_seq,
                "settings_seq": self._settings_seq,
            }
            tmp = f"{HISTORY_COMMAND_FILE}.tmp.{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, HISTORY_COMMAND_FILE)
        except Exception:
            pass

    def _spawn(self):
        if self._proc_alive():
            return True
        cmd = _self_exec_command(["--history-ui-process"])
        env = dict(os.environ)
        env["BLOOOP_PARENT_PID"] = str(os.getpid())
        # The helper must read/write the same state dir the main app resolved
        # (history.db, settings.json, command file) even when that fell back
        # to a non-default location.
        env["BLOOOP_STATE_DIR"] = STATE_DIR
        try:
            self._proc = subprocess.Popen(
                cmd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            return True
        except Exception as exc:
            print(f"⚠ History window failed to launch: {exc}")
            self._proc = None
            return False

    def start(self):
        # Match HistoryPanel.start(): build the helper but do not force focus.
        with self._lock:
            self._spawn()

    def show(self):
        with self._lock:
            self._spawn()
            self._raise_seq += 1
            self._write_command()

    def refresh_if_visible(self):
        # The subprocess polls SQLite on its own (~2s), so nothing to push.
        return

    def on_app_active_change(self, active):
        return

    def _is_visible(self):
        return self._proc_alive()

    def _toggle_settings_panel(self):
        # The webview UI treats settings_seq bumps as "force open" (idempotent).
        # We always set _settings_visible=False here so App's _menu_show_settings
        # — which only bumps when _settings_visible is False — fires every time.
        with self._lock:
            self._settings_visible = False
            self._settings_seq += 1
            self._write_command()

    def stop(self):
        with self._lock:
            if not self._proc_alive():
                self._proc = None
                return
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2.0)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
            except Exception:
                pass
            self._proc = None


class HistoryPanel:
    """In-process history/settings window (single app + single Dock icon)."""

    MODEL_CHOICES = [
        "mlx-community/whisper-tiny-mlx",
        "mlx-community/whisper-small-mlx",
        "mlx-community/whisper-medium-mlx",
        "mlx-community/whisper-large-v3-mlx",
    ]
    STATUS_LABELS = {
        "ok": "Saved",
        "recording": "Recording",
        "no_speech": "No speech",
        "no_audio": "No audio",
        "too_short": "Too short",
        "error": "Error",
    }
    STATUS_COLORS = {
        "ok": "#64d98b",
        "recording": "#70b7ff",
        "no_speech": "#f4bd61",
        "no_audio": "#8e9db6",
        "too_short": "#8e9db6",
        "error": "#ff8d8d",
    }
    BG = "#0f1724"
    SURFACE = "#172133"
    SURFACE_ALT = "#1d2940"
    BORDER = "#2a3956"
    TEXT = "#eef4ff"
    MUTED = "#97a7c1"

    def __init__(self, root):
        self._root = root
        self._win = None
        self._tree = None
        self._runtime_label = None
        self._msg_var = tk.StringVar(value="")
        self._count_var = tk.StringVar(value="")
        self._detail_title_var = tk.StringVar(value="Select a transcript")
        self._detail_meta_var = tk.StringVar(
            value="Click any row to preview it here and copy it immediately."
        )
        self._rows_by_id = {}
        self._refresh_job = None
        self._detail_text = None
        self._custom_vocab_text = None
        self._style_ready = False
        self._restore_on_activate = False
        self._settings_frame = None
        self._settings_toggle_btn = None
        self._save_btn = None
        self._saved_settings_snapshot = None
        self._settings_dirty = False
        self._settings_visible = False
        self._suspend_settings_watch = False
        self._settings_status_var = tk.StringVar(value="")

        self._model_var = tk.StringVar(value=DEFAULT_MODEL)
        self._hotkey_var = tk.StringVar(value=DEFAULT_HOTKEY)
        self._silence_var = tk.StringVar(value=DEFAULT_SILENCE_PRESET)
        self._auto_paste_var = tk.BooleanVar(value=True)
        self._latch_mode_var = tk.BooleanVar(value=True)
        self._pill_window_var = tk.BooleanVar(value=True)
        self._chunk_sec_var = tk.StringVar(value="10.0")

    def _configure_styles(self):
        if self._style_ready or ttk is None:
            return

        style = ttk.Style(self._root)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure("History.TFrame", background=self.BG)
        style.configure(
            "History.Title.TLabel",
            background=self.BG,
            foreground=self.TEXT,
            font=("SF Pro Text", 18, "bold"),
        )
        style.configure(
            "History.Subtle.TLabel",
            background=self.BG,
            foreground=self.MUTED,
            font=("SF Pro Text", 11),
        )
        style.configure(
            "History.Section.TLabel",
            background=self.BG,
            foreground=self.TEXT,
            font=("SF Pro Text", 12, "bold"),
        )
        style.configure(
            "History.Meta.TLabel",
            background=self.SURFACE,
            foreground=self.MUTED,
            font=("SF Pro Text", 11),
        )
        style.configure(
            "History.TNotebook",
            background=self.BG,
            borderwidth=0,
            tabmargins=(0, 0, 0, 0),
        )
        style.configure(
            "History.TNotebook.Tab",
            background=self.SURFACE_ALT,
            foreground=self.MUTED,
            padding=(12, 8),
            borderwidth=0,
        )
        style.map(
            "History.TNotebook.Tab",
            background=[("selected", self.SURFACE), ("active", self.SURFACE_ALT)],
            foreground=[("selected", self.TEXT), ("active", self.TEXT)],
        )
        style.configure(
            "History.Treeview",
            background="#10192a",
            fieldbackground="#10192a",
            foreground=self.TEXT,
            rowheight=30,
            borderwidth=0,
            relief="flat",
        )
        style.map(
            "History.Treeview",
            background=[("selected", "#263754")],
            foreground=[("selected", "#ffffff")],
        )
        style.configure(
            "History.Treeview.Heading",
            background=self.SURFACE_ALT,
            foreground=self.TEXT,
            padding=(10, 8),
            borderwidth=0,
            relief="flat",
        )
        style.map(
            "History.Treeview.Heading",
            background=[("active", "#24314e")],
        )
        style.configure(
            "History.TButton",
            background="#263754",
            foreground=self.TEXT,
            padding=(12, 7),
            borderwidth=0,
            relief="flat",
        )
        style.map(
            "History.TButton",
            background=[("active", "#31466a")],
            foreground=[("active", "#ffffff")],
        )
        style.configure(
            "History.Secondary.TButton",
            background=self.SURFACE_ALT,
            foreground=self.TEXT,
            padding=(12, 7),
            borderwidth=0,
            relief="flat",
        )
        style.map(
            "History.Secondary.TButton",
            background=[("active", "#263754")],
        )
        style.configure(
            "History.Danger.TButton",
            background="#4a2330",
            foreground="#ffe6ea",
            padding=(12, 7),
            borderwidth=0,
            relief="flat",
        )
        style.map(
            "History.Danger.TButton",
            background=[("active", "#623040")],
        )
        style.configure(
            "History.TLabelframe",
            background=self.BG,
            bordercolor=self.BORDER,
            relief="solid",
        )
        style.configure(
            "History.TLabelframe.Label",
            background=self.BG,
            foreground=self.TEXT,
            font=("SF Pro Text", 11, "bold"),
        )
        style.configure(
            "History.TCheckbutton",
            background=self.BG,
            foreground=self.TEXT,
        )
        self._style_ready = True

    def _set_msg(self, msg):
        try:
            self._msg_var.set(msg)
        except Exception:
            pass

    def _current_settings_snapshot(self):
        hotkey = self._hotkey_var.get().strip()
        silence = self._silence_var.get().strip()
        chunk_raw = self._chunk_sec_var.get().strip()

        try:
            chunk = float(chunk_raw)
            if not math.isfinite(chunk):
                raise ValueError("non-finite")
            chunk_value = min(60.0, max(2.0, chunk))
        except Exception:
            chunk_value = f"invalid:{chunk_raw}"

        if self._custom_vocab_text is not None:
            custom_vocab = _normalize_custom_vocab(self._custom_vocab_text.get("1.0", "end"))
        else:
            custom_vocab = list(DEFAULT_CUSTOM_VOCAB)

        return {
            "model": self._model_var.get().strip() or DEFAULT_MODEL,
            "hotkey": hotkey if hotkey in HOTKEY_OPTIONS else f"invalid:{hotkey}",
            "silence_trim_preset": (
                silence if silence in SILENCE_PRESETS else f"invalid:{silence}"
            ),
            "auto_paste": bool(self._auto_paste_var.get()),
            "latch_chunk_mode": bool(self._latch_mode_var.get()),
            "latch_chunk_seconds": chunk_value,
            "pill_window": bool(self._pill_window_var.get()),
            "custom_vocab": custom_vocab,
        }

    def _update_settings_controls(self):
        if self._settings_toggle_btn is not None:
            toggle_text = "Hide Settings" if self._settings_visible else "Show Settings"
            if self._settings_dirty:
                toggle_text += " *"
            self._settings_toggle_btn.config(text=toggle_text)

        if self._save_btn is not None:
            self._save_btn.config(text="Save Changes" if self._settings_dirty else "No Changes")
            try:
                if self._settings_dirty:
                    self._save_btn.state(["!disabled"])
                else:
                    self._save_btn.state(["disabled"])
            except Exception:
                pass

        if self._settings_status_var is not None:
            self._settings_status_var.set("Unsaved changes." if self._settings_dirty else "")

    def _update_settings_dirty(self):
        self._settings_dirty = (
            self._saved_settings_snapshot is not None
            and self._current_settings_snapshot() != self._saved_settings_snapshot
        )
        self._update_settings_controls()

    def _toggle_settings_panel(self):
        if self._settings_frame is None:
            return
        self._settings_visible = not self._settings_visible
        try:
            if self._settings_visible:
                self._settings_frame.grid()
            else:
                self._settings_frame.grid_remove()
        except Exception:
            pass
        self._update_settings_controls()

    def _on_settings_field_changed(self, *_args):
        if self._suspend_settings_watch:
            return
        self._update_settings_dirty()

    def _on_custom_vocab_modified(self, _event=None):
        if self._custom_vocab_text is not None:
            try:
                self._custom_vocab_text.edit_modified(False)
            except Exception:
                pass
        if self._suspend_settings_watch:
            return
        self._update_settings_dirty()

    def _load_settings_into_vars(self):
        try:
            cur, _path, _mtime = _settings_load()
        except Exception:
            cur = _settings_defaults()

        self._suspend_settings_watch = True
        try:
            model = cur.get("model", DEFAULT_MODEL)
            if model and model not in self.MODEL_CHOICES:
                choices = [model] + [m for m in self.MODEL_CHOICES if m != model]
            else:
                choices = list(self.MODEL_CHOICES)

            if hasattr(self, "_model_combo") and self._model_combo is not None:
                self._model_combo["values"] = choices

            self._model_var.set(model)
            self._hotkey_var.set(cur.get("hotkey", DEFAULT_HOTKEY))
            self._silence_var.set(cur.get("silence_trim_preset", DEFAULT_SILENCE_PRESET))
            self._auto_paste_var.set(bool(cur.get("auto_paste", True)))
            self._latch_mode_var.set(bool(cur.get("latch_chunk_mode", True)))
            self._pill_window_var.set(bool(cur.get("pill_window", True)))
            try:
                chunk = float(cur.get("latch_chunk_seconds", 10.0))
            except Exception:
                chunk = 10.0
            self._chunk_sec_var.set(f"{chunk:.1f}")
            if self._custom_vocab_text is not None:
                self._set_text_widget(
                    self._custom_vocab_text,
                    "\n".join(cur.get("custom_vocab", list(DEFAULT_CUSTOM_VOCAB))),
                )

            self._saved_settings_snapshot = self._current_settings_snapshot()
            self._settings_dirty = False
        finally:
            self._suspend_settings_watch = False
        self._update_settings_controls()

    def _save_settings_from_vars(self):
        model = self._model_var.get().strip() or DEFAULT_MODEL
        hotkey = self._hotkey_var.get().strip()
        silence = self._silence_var.get().strip()

        if hotkey not in HOTKEY_OPTIONS:
            hotkey = DEFAULT_HOTKEY
        if silence not in SILENCE_PRESETS:
            silence = DEFAULT_SILENCE_PRESET

        try:
            chunk = float(self._chunk_sec_var.get().strip())
            if not math.isfinite(chunk):
                raise ValueError("non-finite")
        except Exception:
            self._set_msg("Chunk seconds must be a number (2-60).")
            return

        custom_vocab = list(DEFAULT_CUSTOM_VOCAB)
        if self._custom_vocab_text is not None:
            custom_vocab = _normalize_custom_vocab(
                self._custom_vocab_text.get("1.0", "end")
            )

        out = {
            "model": model,
            "hotkey": hotkey,
            "silence_trim_preset": silence,
            "auto_paste": bool(self._auto_paste_var.get()),
            "latch_chunk_mode": bool(self._latch_mode_var.get()),
            "latch_chunk_seconds": min(60.0, max(2.0, chunk)),
            "pill_window": bool(self._pill_window_var.get()),
            "custom_vocab": custom_vocab,
        }
        try:
            saved, _path, _mtime = _settings_save(out)
            self._model_var.set(saved["model"])
            self._hotkey_var.set(saved["hotkey"])
            self._silence_var.set(saved["silence_trim_preset"])
            self._auto_paste_var.set(bool(saved["auto_paste"]))
            self._latch_mode_var.set(bool(saved["latch_chunk_mode"]))
            self._pill_window_var.set(bool(saved.get("pill_window", True)))
            self._chunk_sec_var.set(f"{float(saved['latch_chunk_seconds']):.1f}")
            if self._custom_vocab_text is not None:
                self._set_text_widget(
                    self._custom_vocab_text,
                    "\n".join(saved.get("custom_vocab", list(DEFAULT_CUSTOM_VOCAB))),
                )
            self._saved_settings_snapshot = self._current_settings_snapshot()
            self._settings_dirty = False
            self._update_settings_controls()
            self._set_msg("Settings saved.")
        except Exception as exc:
            self._set_msg(f"Save failed: {exc}")

    def _selected_row(self):
        if self._tree is None:
            return None
        sel = self._tree.selection()
        if not sel:
            return None
        try:
            hid = int(sel[0])
        except Exception:
            return None
        return self._rows_by_id.get(hid)

    def _set_text_widget(self, widget, text):
        if widget is None:
            return
        try:
            widget.config(state="normal")
            widget.delete("1.0", "end")
            if text:
                widget.insert("1.0", text)
            if hasattr(widget, "edit_modified"):
                widget.edit_modified(False)
            if widget is self._detail_text:
                widget.config(state="disabled")
        except Exception:
            pass

    def _format_row_meta(self, row):
        if row is None:
            return ""
        _hid, ts, status, dur, paste_ok, app_bundle, _text, _err = row
        parts = []
        if ts:
            parts.append(str(ts).replace("T", " ").replace("Z", " UTC"))
        if app_bundle:
            parts.append(app_bundle)
        if dur is not None:
            try:
                parts.append(f"{float(dur):.2f}s")
            except Exception:
                pass
        if paste_ok is not None:
            parts.append("pasted" if paste_ok else "copy only")
        label = self.STATUS_LABELS.get(status, str(status).replace("_", " ").title())
        if label:
            parts.insert(0, label)
        return "  •  ".join(parts)

    def _show_row_details(self, row):
        if row is None:
            self._detail_title_var.set("Select a transcript")
            self._detail_meta_var.set(
                "Click any row to preview it here and copy it immediately."
            )
            self._set_text_widget(self._detail_text, "")
            return

        _hid, _ts, status, _dur, _paste_ok, _app_bundle, text, err = row
        title = self.STATUS_LABELS.get(status, str(status).replace("_", " ").title())
        self._detail_title_var.set(title)
        self._detail_meta_var.set(self._format_row_meta(row))

        body = (text or "").strip()
        if not body:
            if status == "recording":
                body = "Recording in progress..."
            elif status == "no_speech":
                body = "No speech detected for this capture."
            elif err:
                body = str(err).strip()
            else:
                body = "-"
        self._set_text_widget(self._detail_text, body)

    def _copy_row(self, row, auto=False):
        if row is None:
            if not auto:
                self._set_msg("Select a row first.")
            return False
        text = (row[6] or "").strip()
        if not text:
            if not auto:
                self._set_msg("Selected row has no transcript text.")
            return False
        try:
            pyperclip.copy(text)
            self._set_msg("Copied transcript to clipboard.")
            return True
        except Exception as exc:
            self._set_msg(f"Clipboard failed: {exc}")
            return False

    def _copy_selected(self):
        return self._copy_row(self._selected_row(), auto=False)

    def _delete_selected(self):
        row = self._selected_row()
        if row is None:
            self._set_msg("Select a row first.")
            return
        try:
            hid = int(row[0])
        except Exception:
            self._set_msg("Invalid history row.")
            return
        ok = _history_delete(hid)
        if not ok:
            self._set_msg("Delete failed.")
            return
        self._set_msg("Deleted.")
        self._refresh_rows()

    def _on_tree_select(self, _event=None):
        self._show_row_details(self._selected_row())

    def _on_tree_click_copy(self, _event=None):
        if self._root is None:
            return
        try:
            self._root.after_idle(lambda: self._copy_row(self._selected_row(), auto=True))
        except Exception:
            pass

    def _refresh_rows(self):
        if self._tree is None:
            return

        selected = None
        sel = self._tree.selection()
        if sel:
            selected = sel[0]

        rows = _history_list(HISTORY_UI_ROWS)
        if HISTORY_UI_HIDE_NOISE:
            rows = [r for r in rows if r[2] not in {"no_audio", "too_short"}]

        self._rows_by_id = {}
        for iid in self._tree.get_children():
            self._tree.delete(iid)

        for row in rows:
            hid, ts, status, dur, _paste_ok, _app, text, err = row
            self._rows_by_id[int(hid)] = row
            ts_s = str(ts or "").replace("T", " ").replace("Z", " UTC")
            dur_s = "-" if dur is None else f"{float(dur):.2f}s"
            preview, _truncated = _history_summary(
                text, err, max_chars=HISTORY_UI_PREVIEW_CHARS
            )
            self._tree.insert(
                "",
                "end",
                iid=str(hid),
                values=(ts_s, status, dur_s, preview),
            )

        if selected and self._tree.exists(selected):
            self._tree.selection_set(selected)

    def _refresh_runtime_label(self):
        runtime = None
        requested = None
        dl_state = None
        try:
            with open(RUNTIME_STATUS_PATH, "r", encoding="utf-8") as fh:
                st = json.load(fh)
                if isinstance(st, dict):
                    runtime = st.get("runtime_model")
                    requested = st.get("requested_model")
                    dl_state = st.get("model_download_state")
        except Exception:
            pass

        if not runtime:
            runtime = MODEL
        msg = f"Runtime model: {runtime}"
        if requested and requested != runtime:
            msg += f" (next: {requested})"
        if dl_state:
            msg += f"   download: {dl_state}"
        if self._runtime_label is not None:
            self._runtime_label.config(text=msg)

    def _schedule_refresh(self):
        self._cancel_refresh()
        if self._root is None:
            return
        try:
            self._refresh_job = self._root.after(HISTORY_UI_REFRESH_MS, self._refresh_tick)
        except Exception:
            self._refresh_job = None

    def _cancel_refresh(self):
        if self._refresh_job is None:
            return
        try:
            self._root.after_cancel(self._refresh_job)
        except Exception:
            pass
        self._refresh_job = None

    def _refresh_tick(self):
        self._refresh_job = None
        self._refresh_rows()
        self._refresh_runtime_label()
        self._schedule_refresh()

    def _is_visible(self):
        if self._win is None:
            return False
        try:
            return str(self._win.state()) != "withdrawn"
        except Exception:
            return False

    def _on_close(self):
        self._restore_on_activate = False
        if self._win is not None:
            try:
                self._win.withdraw()
            except Exception:
                pass

    def on_app_active_change(self, active):
        if self._win is None:
            return
        if active:
            self.refresh_if_visible()

    def _build_window(self):
        if not _TK or ttk is None:
            return

        win = tk.Toplevel(self._root)
        win.title("Blooop History")
        win.geometry("900x620")
        win.minsize(640, 420)
        win.protocol("WM_DELETE_WINDOW", self._on_close)
        _macos_configure_history_window(win)

        outer = ttk.Frame(win, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        topbar = ttk.Frame(outer)
        topbar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        topbar.columnconfigure(0, weight=1)
        topbar.columnconfigure(1, weight=0)
        topbar.columnconfigure(2, weight=0)
        self._runtime_label = ttk.Label(topbar, text="Runtime model: -")
        self._runtime_label.grid(row=0, column=0, sticky="w")
        self._settings_toggle_btn = ttk.Button(
            topbar,
            text="Show Settings",
            command=self._toggle_settings_panel,
        )
        self._settings_toggle_btn.grid(row=0, column=1, sticky="e", padx=(8, 0))
        ttk.Label(topbar, text="Click a row to copy it.").grid(row=0, column=2, sticky="e")

        tree_host = ttk.Frame(outer)
        tree_host.grid(row=1, column=0, sticky="nsew")
        tree_host.columnconfigure(0, weight=1)
        tree_host.rowconfigure(0, weight=1)

        tree = ttk.Treeview(
            tree_host,
            columns=("created", "status", "duration", "preview"),
            show="headings",
            selectmode="browse",
        )
        tree.heading("created", text="Created")
        tree.heading("status", text="Status")
        tree.heading("duration", text="Duration")
        tree.heading("preview", text="Preview")
        tree.column("created", width=190, minwidth=150, anchor="w")
        tree.column("status", width=90, minwidth=80, anchor="w")
        tree.column("duration", width=90, minwidth=80, anchor="e")
        tree.column("preview", width=520, minwidth=220, anchor="w")

        yscroll = ttk.Scrollbar(tree_host, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=yscroll.set)

        tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        self._tree = tree
        self._tree.bind("<ButtonRelease-1>", self._on_tree_click_copy, add="+")
        self._tree.bind(
            "<Return>",
            lambda _event: self._copy_row(self._selected_row(), auto=False),
            add="+",
        )

        actions = ttk.Frame(outer)
        actions.grid(row=2, column=0, sticky="ew", pady=(8, 6))
        ttk.Button(actions, text="Refresh", command=self._refresh_rows).pack(side="left")
        ttk.Button(actions, text="Copy Text", command=self._copy_selected).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(actions, text="Delete", command=self._delete_selected).pack(
            side="left", padx=(6, 0)
        )
        ttk.Label(actions, textvariable=self._settings_status_var).pack(side="right")
        self._save_btn = ttk.Button(
            actions,
            text="No Changes",
            command=self._save_settings_from_vars,
        )
        self._save_btn.pack(side="right", padx=(0, 6))

        settings = ttk.Frame(outer)
        settings.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        self._settings_frame = settings
        for idx in range(6):
            settings.columnconfigure(idx, weight=1 if idx in (1, 3, 5) else 0)

        ttk.Label(settings, text="Model").grid(row=0, column=0, sticky="w")
        self._model_combo = ttk.Combobox(
            settings,
            textvariable=self._model_var,
            values=self.MODEL_CHOICES,
            state="readonly",
        )
        self._model_combo.grid(row=0, column=1, sticky="ew", padx=(6, 12))

        ttk.Label(settings, text="Hotkey").grid(row=0, column=2, sticky="w")
        ttk.Combobox(
            settings,
            textvariable=self._hotkey_var,
            values=list(HOTKEY_OPTIONS.keys()),
            state="readonly",
        ).grid(row=0, column=3, sticky="ew", padx=(6, 12))

        ttk.Label(settings, text="Silence").grid(row=0, column=4, sticky="w")
        ttk.Combobox(
            settings,
            textvariable=self._silence_var,
            values=list(SILENCE_PRESETS.keys()),
            state="readonly",
        ).grid(row=0, column=5, sticky="ew", padx=(6, 0))

        ttk.Checkbutton(
            settings,
            text="Auto paste",
            variable=self._auto_paste_var,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(
            settings,
            text="Latch chunk mode",
            variable=self._latch_mode_var,
        ).grid(row=1, column=2, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(settings, text="Chunk sec").grid(row=1, column=4, sticky="w", pady=(8, 0))
        ttk.Entry(settings, textvariable=self._chunk_sec_var, width=8).grid(
            row=1, column=5, sticky="w", padx=(6, 0), pady=(8, 0)
        )

        ttk.Checkbutton(
            settings,
            text="Show recording pill (restart to apply)",
            variable=self._pill_window_var,
        ).grid(row=2, column=0, columnspan=6, sticky="w", pady=(8, 0))

        ttk.Label(settings, text="Custom Vocabulary").grid(
            row=3, column=0, sticky="nw", pady=(10, 0)
        )
        vocab_host = ttk.Frame(settings)
        vocab_host.grid(
            row=3, column=1, columnspan=5, sticky="ew", padx=(6, 0), pady=(10, 0)
        )
        vocab_host.columnconfigure(0, weight=1)
        vocab_scroll = ttk.Scrollbar(vocab_host, orient="vertical")
        vocab_text = tk.Text(
            vocab_host,
            height=5,
            wrap="word",
            yscrollcommand=vocab_scroll.set,
        )
        vocab_scroll.config(command=vocab_text.yview)
        vocab_text.grid(row=0, column=0, sticky="nsew")
        vocab_scroll.grid(row=0, column=1, sticky="ns")
        self._custom_vocab_text = vocab_text

        ttk.Label(
            settings,
            text="One preferred word or phrase per line. These are passed to Whisper as a spelling prompt.",
        ).grid(row=4, column=1, columnspan=5, sticky="w", pady=(6, 0))

        ttk.Label(
            outer,
            textvariable=self._msg_var,
        ).grid(row=4, column=0, sticky="w", pady=(8, 0))

        for var in (
            self._model_var,
            self._hotkey_var,
            self._silence_var,
            self._auto_paste_var,
            self._latch_mode_var,
            self._pill_window_var,
            self._chunk_sec_var,
        ):
            var.trace_add("write", self._on_settings_field_changed)
        self._custom_vocab_text.bind("<<Modified>>", self._on_custom_vocab_modified, add="+")

        self._win = win
        self._load_settings_into_vars()
        self._settings_frame.grid_remove()
        self._settings_visible = False
        self._update_settings_controls()
        self._refresh_rows()
        self._refresh_runtime_label()

    def start(self):
        if self._win is None or not self._win.winfo_exists():
            self._build_window()
        self._schedule_refresh()
        self._on_close()

    def show(self):
        self.start()
        if self._win is None:
            return
        try:
            self._restore_on_activate = True
            if not self._settings_dirty:
                self._load_settings_into_vars()
            self._refresh_rows()
            self._refresh_runtime_label()
            _macos_configure_history_window(self._win)
            self._win.deiconify()
            # Do not raise/focus; this helper panel should never steal the
            # active app while recording/transcribing.
        except Exception:
            pass

    def refresh_if_visible(self):
        if self._win is None:
            return
        try:
            if str(self._win.state()) == "withdrawn":
                return
            if not self._settings_dirty:
                self._load_settings_into_vars()
            self._refresh_rows()
            self._refresh_runtime_label()
        except Exception:
            pass

    def stop(self):
        self._cancel_refresh()
        if self._win is None:
            return
        try:
            self._win.destroy()
        except Exception:
            pass
        self._win = None
        self._tree = None


class MacCommandHotkeyMonitor:
    """Global modifier-key monitor using AppKit (avoids pynput SIGTRAP in .app)."""

    def __init__(self, on_press, on_release, key_code=54, modifier="command",
                 schedule=None):
        self._on_press = on_press
        self._on_release = on_release
        # Optional scheduler: called with a zero-arg callable, expected to
        # invoke it off the NSEvent dispatch stack (e.g. Tk's root.after).
        # This avoids re-entering CoreAudio (sd.InputStream) from inside an
        # AppKit event handler, which SIGSEGVs after long uptime as the
        # cached AudioComponentInstance goes stale.
        self._schedule = schedule
        self._key_code = int(key_code)
        self._modifier = str(modifier)
        self._pressed = False
        self._global = None
        self._local = None
        self._handler = None
        self._nsevent = None
        self._mod_flags = {}
        self._mod_flag = None

    def configure(self, key_code=None, modifier=None):
        if key_code is not None:
            self._key_code = int(key_code)
        if modifier is not None:
            mod = str(modifier)
            if mod in self._mod_flags:
                self._modifier = mod
                self._mod_flag = int(self._mod_flags[mod])
        self._pressed = False

    def start(self):
        if sys.platform != "darwin":
            return False
        try:
            from AppKit import (
                NSEvent,
                NSEventModifierFlagControl,
                NSEventMaskFlagsChanged,
                NSEventModifierFlagCommand,
                NSEventModifierFlagOption,
                NSEventModifierFlagShift,
            )
        except Exception:
            return False

        self._nsevent = NSEvent
        self._mod_flags = {
            "command": int(NSEventModifierFlagCommand),
            "option": int(NSEventModifierFlagOption),
            "shift": int(NSEventModifierFlagShift),
            "control": int(NSEventModifierFlagControl),
        }
        self._mod_flag = int(self._mod_flags.get(self._modifier, self._mod_flags["command"]))

        def _dispatch(cb):
            sched = self._schedule
            if sched is not None:
                try:
                    sched(cb)
                    return
                except Exception:
                    pass
            cb()

        def _handle(event):
            try:
                key_code = int(event.keyCode())
                is_down = bool(int(event.modifierFlags()) & int(self._mod_flag))

                # Emit explicit edges for our configured modifier key.
                if key_code == self._key_code:
                    if is_down:
                        if not self._pressed:
                            self._pressed = True
                            _dispatch(self._on_press)
                    else:
                        if self._pressed:
                            self._pressed = False
                            _dispatch(self._on_release)
                        # Ignore release when not pressed — duplicate release
                        # events from macOS confuse the latch state machine.
                    return event

                # Fallback: if modifier state drops via a non-target event,
                # release any latched internal pressed state.
                if (not is_down) and self._pressed:
                    self._pressed = False
                    _dispatch(self._on_release)
            except Exception:
                pass
            return event

        self._handler = _handle
        try:
            self._global = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                NSEventMaskFlagsChanged, _handle
            )
        except Exception:
            self._global = None
        if self._global is None:
            print("⚠  NSEvent global monitor returned nil — Accessibility permission "
                  "is likely not granted for this app.")
            print("   System Settings → Privacy & Security → Accessibility → enable Blooop")
        try:
            self._local = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                NSEventMaskFlagsChanged, _handle
            )
        except Exception:
            self._local = None
        return self._global is not None or self._local is not None

    def stop(self):
        if self._nsevent is None:
            return
        if self._global is not None:
            try:
                self._nsevent.removeMonitor_(self._global)
            except Exception:
                pass
            self._global = None
        if self._local is not None:
            try:
                self._nsevent.removeMonitor_(self._local)
            except Exception:
                pass
            self._local = None
        self._handler = None


# ── Menu bar status item ──────────────────────────────────────────────────────
# Optional menu bar shortcut for Show History / Show Settings / Quit.
# The main app now also appears in the Dock, but this remains a convenient
# always-available shortcut while Blooop is running.

try:
    from Foundation import NSObject as _NSObject

    class _BloopMenuBarController(_NSObject):
        # pyobjc selector names end in "_" per arg; these map to
        # Objective-C's "showHistory:", "showSettings:", "quitBlooop:".
        # Note: we intentionally do NOT define a custom initWithCallbacks_.
        # PyObjC treats any method starting with `init` as an NSObject
        # ownership-transfer initializer and has been unreliable about
        # registering the selector's arity, which caused the menu bar icon
        # to silently fail to install ("Need 0 arguments, got 1"). Using
        # plain init() + a separate configuration call sidesteps that.

        def setCallbacks_(self, callbacks):
            self._bloop_callbacks = callbacks
            self._bloop_status_item = None
            self._bloop_state = "idle"
            self._bloop_level = 0.0
            self._bloop_level_bucket = None
            self._bloop_image_cache = {}

        def showHistory_(self, sender):
            cb = self._bloop_callbacks.get("show_history")
            if cb is not None:
                try:
                    cb()
                except Exception:
                    pass

        def showSettings_(self, sender):
            cb = self._bloop_callbacks.get("show_settings")
            if cb is not None:
                try:
                    cb()
                except Exception:
                    pass

        def quitBlooop_(self, sender):
            cb = self._bloop_callbacks.get("quit")
            if cb is not None:
                try:
                    cb()
                except Exception:
                    pass

        def set_state(self, state):
            """idle | recording | transcribing — repaint status icon."""
            if state == self._bloop_state:
                return
            self._bloop_state = state
            self._bloop_level_bucket = None
            self._apply_visuals()

        def set_level(self, level):
            """0.0..1.0 — while recording, drives the oscillating red dot."""
            try:
                lvl = max(0.0, min(1.0, float(level)))
            except Exception:
                lvl = 0.0
            self._bloop_level = lvl
            if self._bloop_state != "recording":
                return
            # Quantize so the 36Hz level feed repaints only when the dot's
            # size actually changes, instead of rendering a fresh NSImage
            # every tick for the whole recording.
            bucket = int(round(lvl * 11))
            if bucket == self._bloop_level_bucket:
                return
            self._bloop_level_bucket = bucket
            self._apply_visuals()

        def _apply_visuals(self):
            state = self._bloop_state
            item = self._bloop_status_item
            if item is None:
                return
            button = item.button()
            if button is None:
                return
            try:
                from AppKit import NSColor, NSImage, NSImageLeft, NSImageOnly
            except Exception:
                return
            if state == "recording":
                image = self._cachedRecordingImage()
                if image is not None:
                    try:
                        button.setImage_(image)
                    except Exception:
                        pass
            else:
                sym_map = {
                    "idle": "waveform",
                    "transcribing": "waveform.and.mic",
                }
                sym = sym_map.get(state, "waveform")
                try:
                    image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                        sym, "Blooop"
                    )
                except Exception:
                    image = None
                if image is not None:
                    try:
                        image.setTemplate_(True)
                    except Exception:
                        pass
                    try:
                        button.setImage_(image)
                    except Exception:
                        pass
            label = _indicator_state_label(state)
            try:
                button.setImagePosition_(NSImageLeft if label else NSImageOnly)
            except Exception:
                pass
            try:
                button.setTitle_(f" {label}" if label else "")
            except Exception:
                pass
            try:
                # Custom-drawn recording image already carries its own color,
                # so we clear tint for it too — tint only applies to templates.
                button.setContentTintColor_(None)
            except Exception:
                pass

        def _cachedRecordingImage(self):
            bucket = self._bloop_level_bucket
            if bucket is None:
                bucket = int(round(max(0.0, min(1.0, self._bloop_level)) * 11))
                self._bloop_level_bucket = bucket
            image = self._bloop_image_cache.get(bucket)
            if image is None:
                image = self._renderRecordingImage_(bucket / 11.0)
                if image is not None:
                    self._bloop_image_cache[bucket] = image
            return image

        def _renderRecordingImage_(self, level):
            # 18pt canvas with a red dot whose radius scales with the live
            # audio RMS. Gives an unambiguous "recording + mic is hearing you"
            # indicator that's legible at menu-bar size, where tiny bars or
            # waveforms would be indistinguishable from static icons.
            try:
                from AppKit import NSImage, NSBezierPath, NSColor
                from Foundation import NSMakeSize, NSMakeRect
            except Exception:
                return None
            size = NSMakeSize(18.0, 18.0)
            try:
                image = NSImage.alloc().initWithSize_(size)
            except Exception:
                return None
            base_r = 2.8
            max_r = 7.8
            r = base_r + (max_r - base_r) * max(0.0, min(1.0, level))
            try:
                image.lockFocus()
                try:
                    try:
                        # Fable coral (#d97757) — matches the overlay pill.
                        NSColor.colorWithSRGBRed_green_blue_alpha_(
                            0.851, 0.467, 0.341, 1.0
                        ).set()
                    except Exception:
                        NSColor.systemRedColor().set()
                    NSBezierPath.bezierPathWithOvalInRect_(
                        NSMakeRect(9.0 - r, 9.0 - r, r * 2.0, r * 2.0)
                    ).fill()
                finally:
                    image.unlockFocus()
            except Exception:
                return None
            try:
                image.setTemplate_(False)
            except Exception:
                pass
            return image
except Exception:
    _BloopMenuBarController = None


def _install_menu_bar_icon(callbacks):
    """Create an NSStatusItem with a menu. Returns the controller (keep alive) or None."""
    if sys.platform != "darwin" or _BloopMenuBarController is None:
        return None
    try:
        from AppKit import NSStatusBar, NSMenu, NSMenuItem, NSImage
    except Exception:
        return None

    try:
        controller = _BloopMenuBarController.alloc().init()
        controller.setCallbacks_(callbacks)
    except Exception:
        return None

    status_bar = NSStatusBar.systemStatusBar()
    item = status_bar.statusItemWithLength_(-1.0)  # NSVariableStatusItemLength

    button = item.button()
    image = None
    try:
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "waveform", "Blooop"
        )
    except Exception:
        image = None
    if image is not None:
        try:
            image.setTemplate_(True)
        except Exception:
            pass
        button.setImage_(image)
    else:
        button.setTitle_("B")

    menu = NSMenu.alloc().init()
    entries = [
        ("Show History", "showHistory:"),
        ("Show Settings…", "showSettings:"),
        (None, None),
        ("Quit Blooop", "quitBlooop:"),
    ]
    for title, action in entries:
        if title is None:
            menu.addItem_(NSMenuItem.separatorItem())
            continue
        mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
        mi.setTarget_(controller)
        menu.addItem_(mi)

    item.setMenu_(menu)
    controller._bloop_status_item = item
    return controller


# ── Main app ──────────────────────────────────────────────────────────────────

class BloopFlow:
    def __init__(self):
        _set_process_program_name("Blooop")
        self.recording  = False
        self.latched    = False
        self.frames     = []
        self._lock      = threading.Lock()
        self._stream      = None
        self._stream_rate = SAMPLE_RATE
        self._stream_name = None
        self._stream_close_lock = threading.Lock()
        self._teardown_state_lock = threading.Lock()
        self._teardown_inflight = 0
        self._teardown_idle = threading.Event()
        self._teardown_idle.set()
        self._audio_wedged = False
        self._quitting = False
        self._stop_pending = False
        self._stop_grace_timer = None
        self._stop_finalize_lock = threading.Lock()
        self._key_held  = False
        self._press_time = 0.0
        self._last_release = 0.0
        self._last_was_tap = False
        self._ignore_release_once = False
        self._start_timer = None
        self._chunk_stop = threading.Event()
        self._chunk_thread = None
        self._whisper   = None
        self._wlock     = threading.Lock()
        self._tx_lock   = threading.Lock()
        self._runtime_model = MODEL
        self._requested_model = MODEL
        self._model_dl_lock = threading.Lock()
        self._model_dl_requested = None
        self._model_dl_thread = None
        self._model_dl_state = "idle"
        self._model_dl_target = None
        self._model_dl_error = None
        self._kb          = KBController()
        self._hotkey_monitor = None
        self._menu_bar = None
        self._settings_lock = threading.Lock()
        self._settings = _settings_defaults()
        self._settings_path = None
        self._settings_mtime = None
        self._settings_thread = None
        self._settings_stop = threading.Event()
        self._hotkey_name = DEFAULT_HOTKEY
        self._ptt_key = HOTKEY_OPTIONS[DEFAULT_HOTKEY]["pynput_key"]
        self._mic_silent_warned = False
        self._last_mic_announced = None
        # Enable the overlay (recording pill + waveform) by default in all modes.
        # The original focus-stealing issue was fixed by removing forced lift().
        overlay_default = True
        self._overlay_enabled = _env_flag(
            "BLOOOP_OVERLAY_ENABLED",
            default=overlay_default,
        )
        # Floating pill window is the primary recording indicator. The default
        # comes from the user's saved setting (settings.json -> pill_window);
        # BLOOOP_PILL_WINDOW env var still wins if explicitly set, for power
        # users / debugging.
        self._pill_window_enabled = _env_flag(
            "BLOOOP_PILL_WINDOW",
            default=bool(_boot_settings.get("pill_window", True)),
        )
        self._mlx_probe_checked = False
        self._mlx_runtime_ok = True
        self._mlx_runtime_error = None
        self._mlx_unavailable_notified = False
        self._viz         = WaveformVisualizer(
            enabled=self._overlay_enabled,
            show_window=self._pill_window_enabled,
            pill_style=str(_boot_settings.get("pill_style", DEFAULT_PILL_STYLE)),
        ) if _TK else None
        # Now that Tk has created its NSApplication subclass, switch the main
        # app to a normal Dock app so it behaves like a standard macOS app and
        # stays discoverable after the user clicks away from the history window.
        if self._viz and sys.platform == "darwin":
            try:
                _set_macos_regular_app()
            except Exception:
                pass
        # Transcription runs on a single long-lived worker thread fed by this
        # queue. MLX is primed on the main thread at startup (see
        # _prepare_whisper_runtime); the worker only uses an already-loaded
        # instance, which avoids the cold-start SIGABRT seen with per-job
        # threads.
        self._transcribe_queue = queue.Queue()
        self._transcribe_worker_thread = None
        self._history_ui_enabled_runtime = bool(
            HISTORY_UI_ENABLED
            and (not getattr(sys, "frozen", False) or STANDALONE_HISTORY_UI)
        )
        self._history_panel = None
        if self._viz and HISTORY_ENABLED and self._history_ui_enabled_runtime:
            try:
                if HISTORY_UI_USE_WEBVIEW:
                    self._history_panel = WebviewHistoryPanel(self._viz.root)
                else:
                    self._history_panel = HistoryPanel(self._viz.root)
            except Exception as exc:
                print(f"⚠ History panel unavailable: {exc}")
        if self._viz:
            self._viz.set_on_activate(self._on_app_activate)
            self._viz.set_on_active_change(self._on_app_active_change)
        # Bundle ID of the app that was frontmost when the current recording
        # started. Copied into each transcription job on stop.
        self._active_paste_target = None
        self._session_lock = threading.Lock()
        self._latch_session_seq = 0
        self._latch_session_id = None
        self._latch_session_row_id = None
        self._latch_session_text = ""
        self._latch_session_duration = 0.0
        self._latch_session_app = None
        self._reload_settings(force=True, announce=False)

    def _hotkey_info(self):
        return HOTKEY_OPTIONS.get(self._hotkey_name, HOTKEY_OPTIONS[DEFAULT_HOTKEY])

    def _bundled_model_path(self, model_name):
        if not model_name:
            return None
        safe = str(model_name).replace("/", "--")
        cand = os.path.join(BUNDLED_MODELS_DIR, safe)
        if not os.path.isdir(cand):
            return None
        required = ("config.json", "tokenizer.json")
        for fn in required:
            if not os.path.exists(os.path.join(cand, fn)):
                return None
        return cand

    def _model_source(self, model_name):
        bundled = self._bundled_model_path(model_name)
        return bundled if bundled else model_name

    def _publish_runtime_status(self, extra=None):
        payload = {
            "build": RUNTIME_BUILD,
            "pid": os.getpid(),
            "stopped": False,
            "runtime_model": self._runtime_model,
            "requested_model": self._requested_model,
            "model_download_state": self._model_dl_state,
            "model_download_target": self._model_dl_target,
            "model_download_error": self._model_dl_error,
        }
        if isinstance(extra, dict):
            payload.update(extra)
        _runtime_status_update(payload)

    def _reload_settings(self, force=False, announce=False):
        try:
            loaded, path, mtime = _settings_load()
        except Exception:
            return False

        with self._settings_lock:
            changed = (
                force
                or self._settings != loaded
                or self._settings_path != path
                or self._settings_mtime != mtime
            )
            if not changed:
                return False

            prev = dict(self._settings)
            requested_model = loaded.get("model", MODEL)
            model_changed = prev.get("model") != requested_model
            self._settings = dict(loaded)
            self._settings_path = path
            self._settings_mtime = mtime
            self._requested_model = requested_model

            # Keep the current model active during runtime; switch model at next
            # relaunch to avoid disruptive live model swaps.
            apply_model_now = bool(force)
            _apply_runtime_settings(loaded, update_model=apply_model_now)
            if apply_model_now:
                self._runtime_model = requested_model
                self._model_dl_state = "idle"
                self._model_dl_target = None
                self._model_dl_error = None

            prev_hotkey = self._hotkey_name
            self._hotkey_name = loaded["hotkey"]
            self._ptt_key = self._hotkey_info()["pynput_key"]

            if self._hotkey_monitor is not None and prev_hotkey != self._hotkey_name:
                info = self._hotkey_info()
                self._hotkey_monitor.configure(
                    key_code=info["mac_key_code"],
                    modifier=info["mac_modifier"],
                )

            if not LATCH_CHUNK_MODE:
                self._stop_chunk_worker()
            elif self.latched and self.recording:
                self._start_chunk_worker_if_needed()

            if model_changed and not apply_model_now:
                if requested_model == self._runtime_model:
                    self._print(f"○ Model unchanged at runtime: {self._runtime_model}")
                    self._model_dl_state = "idle"
                    self._model_dl_target = None
                    self._model_dl_error = None
                else:
                    self._print(
                        "○ Model change saved. Keeping current model until relaunch."
                    )
                    self._print(
                        f"○ Current: {self._runtime_model}  Next: {requested_model}"
                    )
                    self._model_dl_state = "queued"
                    self._model_dl_target = requested_model
                    self._model_dl_error = None
                    self._queue_model_download(requested_model)

            if announce:
                changes = []
                for k in (
                    "model",
                    "auto_paste",
                    "latch_chunk_mode",
                    "latch_chunk_seconds",
                    "silence_trim_preset",
                    "hotkey",
                    "pill_window",
                    "pill_style",
                    "custom_vocab",
                ):
                    if prev.get(k) != loaded.get(k):
                        changes.append(k)
                if changes:
                    self._print(f"○ Settings updated: {', '.join(changes)}")
                if prev.get("pill_window") != loaded.get("pill_window"):
                    self._print("  (pill_window change takes effect after restart)")
                if prev.get("pill_style") != loaded.get("pill_style"):
                    self._print("  (pill_style change takes effect after restart)")
            self._publish_runtime_status()
            return True

    def _settings_watch_loop(self):
        while not self._settings_stop.wait(1.0):
            self._reload_settings(force=False, announce=True)

    def _start_settings_watch(self):
        if self._settings_thread is not None and self._settings_thread.is_alive():
            return
        self._settings_stop.clear()
        self._settings_thread = threading.Thread(
            target=self._settings_watch_loop, daemon=True
        )
        self._settings_thread.start()

    def _stop_settings_watch(self):
        self._settings_stop.set()
        if self._settings_thread is not None:
            self._settings_thread.join(timeout=0.2)
        self._settings_thread = None

    def _queue_model_download(self, model_name):
        model_name = (model_name or "").strip()
        if not model_name:
            return
        started = False
        with self._model_dl_lock:
            self._model_dl_requested = model_name
            self._model_dl_state = "queued"
            self._model_dl_target = model_name
            self._model_dl_error = None
            if self._model_dl_thread is not None and self._model_dl_thread.is_alive():
                self._publish_runtime_status()
                return
            self._model_dl_thread = threading.Thread(
                target=self._model_download_loop, daemon=True
            )
            started = True
        self._publish_runtime_status()
        if started:
            self._model_dl_thread.start()

    def _model_download_loop(self):
        while True:
            with self._model_dl_lock:
                target = self._model_dl_requested
                self._model_dl_requested = None
            if not target:
                break
            if self._bundled_model_path(target):
                self._model_dl_state = "downloaded"
                self._model_dl_target = target
                self._model_dl_error = None
                self._print(
                    f"○ Model available in bundled assets: {target}. Relaunch Blooop to switch."
                )
                self._publish_runtime_status()
                threading.Thread(
                    target=self._offer_model_relaunch,
                    args=(target,),
                    daemon=True,
                    name="bloop-model-relaunch-offer",
                ).start()
                continue
            if target == self._runtime_model:
                self._model_dl_state = "idle"
                self._model_dl_target = None
                self._model_dl_error = None
                self._publish_runtime_status()
                break

            self._model_dl_state = "downloading"
            self._model_dl_target = target
            self._model_dl_error = None
            self._publish_runtime_status()
            self._print(f"○ Downloading model in background: {target}")
            try:
                # In-process download. The previous implementation shelled out to
                # sys.executable -c "...", which silently fails inside a
                # PyInstaller .app because sys.executable is the bootloader, not
                # a Python interpreter.
                from huggingface_hub import snapshot_download
                snapshot_download(repo_id=target)
                self._model_dl_state = "downloaded"
                self._model_dl_target = target
                self._model_dl_error = None
                self._print(
                    f"○ Model downloaded: {target}. Relaunch Blooop to switch."
                )
                self._publish_runtime_status()
                # Console fine print isn't enough — surface the required
                # relaunch as a native dialog. Own thread so a long-lived
                # dialog doesn't block this download worker slot.
                threading.Thread(
                    target=self._offer_model_relaunch,
                    args=(target,),
                    daemon=True,
                    name="bloop-model-relaunch-offer",
                ).start()
            except Exception as exc:
                self._model_dl_state = "failed"
                self._model_dl_target = target
                self._model_dl_error = str(exc)
                self._print(f"⚠ Model download failed ({target}): {exc}")
                self._publish_runtime_status()

        with self._model_dl_lock:
            self._model_dl_thread = None

    def _offer_model_relaunch(self, target):
        """Native dialog offering to relaunch now that the new model is ready."""
        short = target.rsplit("/", 1)[-1]
        script = (
            f'display dialog "New Whisper model ready: {short}. '
            'Blooop switches models on relaunch — relaunch now?" '
            'with title "Blooop" buttons {"Later", "Relaunch Now"} '
            'default button "Relaunch Now" giving up after 120'
        )
        try:
            r = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=150,
            )
        except Exception:
            return
        # "giving up after" reports an empty button name, which lands in the
        # Later branch — silence must never trigger a surprise relaunch.
        if "Relaunch Now" not in (r.stdout or ""):
            self._print("○ OK — the new model applies on the next relaunch.")
            return
        self._print("○ Relaunching to switch model…")
        if not self._drain_transcription(600):
            self._print(
                "⚠ Still recording/transcribing after 10 min — "
                "model switch deferred to the next relaunch."
            )
            return
        self._relaunch_self(f"model switch to {target}")

    # ── Audio ─────────────────────────────────────────────────────────────────

    def _audio_callback(self, indata, n_frames, t, status):
        if self.recording:
            with self._lock:
                self.frames.append(indata.copy())
            if self._viz:
                rms = float(np.sqrt(np.mean(indata ** 2)))
                self._viz.push_level(min(rms * 18, 1.0))

    def _close_stream(self):
        stream, self._stream = self._stream, None
        self._stream_rate = SAMPLE_RATE
        self._stream_name = None
        if stream is None:
            return

        # CoreAudio/PortAudio teardown is the only unguarded native call on
        # the hotkey stop path and has both crashed natively and wedged
        # (Pa_StopStream blocks until the callback drains) in the wild — a
        # 2026-06-10 first-tap death cut the standalone log exactly here,
        # with no .ips and no faulthandler dump. Run it off the main thread
        # so a wedge can't freeze the UI into a force-quit, and leave
        # durable breadcrumbs so an artifact-free native death here is
        # attributable post-mortem ("begin" without "end" = teardown died).
        done = threading.Event()
        with self._teardown_state_lock:
            self._teardown_inflight += 1
            self._teardown_idle.clear()

        def _teardown():
            _issues_append("audio_stream_close_begin", f"stream=0x{id(stream):x}")
            try:
                with self._stream_close_lock:
                    try:
                        stream.stop()
                    except Exception:
                        pass
                    try:
                        stream.close()
                    except Exception:
                        pass
            finally:
                done.set()
                with self._teardown_state_lock:
                    self._teardown_inflight -= 1
                    if self._teardown_inflight == 0:
                        self._teardown_idle.set()
            _issues_append("audio_stream_close_end", f"stream=0x{id(stream):x}")

        def _watchdog():
            if not done.wait(AUDIO_TEARDOWN_WEDGE_SEC):
                self._declare_audio_wedge(f"stream=0x{id(stream):x}")

        threading.Thread(
            target=_teardown, daemon=True, name="bloop-audio-close"
        ).start()
        threading.Thread(
            target=_watchdog, daemon=True, name="bloop-audio-close-watchdog"
        ).start()

    def _open_input_stream(self):
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            callback=self._audio_callback,
        )
        stream.start()
        return stream

    def _declare_audio_wedge(self, detail=""):
        with self._teardown_state_lock:
            if self._audio_wedged or self._quitting:
                return
            self._audio_wedged = True
        _issues_append(
            "audio_teardown_wedged",
            f"Pa_StopStream never returned ({detail}); "
            "CoreAudio HAL deadlocked — self-relaunching",
        )
        self._print("✗ Audio system deadlocked (CoreAudio). Blooop will relaunch itself…")
        threading.Thread(
            target=self._relaunch_after_wedge, daemon=True, name="bloop-wedge-relaunch"
        ).start()

    def _drain_transcription(self, timeout_sec):
        """Wait until no recording is active and the transcribe queue is idle.

        Two consecutive idle polls guard the gap between queue.get() and the
        worker taking _tx_lock. Returns True once idle, False on timeout.
        """
        deadline = time.monotonic() + timeout_sec
        idle_streak = 0
        while idle_streak < 2:
            if time.monotonic() >= deadline:
                return False
            idle = (
                self._transcribe_queue.empty()
                and not self._tx_lock.locked()
                and not self.recording
            )
            idle_streak = idle_streak + 1 if idle else 0
            time.sleep(1.0)
        return True

    def _relaunch_self(self, reason):
        """Spawn a replacement instance and exit this process abruptly.

        os._exit because the callers' contexts (wedged native threads, or a
        background dialog thread with Tk still live) can't do a clean
        interpreter shutdown. The history-ui child watches our pid and exits
        on its own.
        """
        _issues_append("self_relaunch", reason)
        _runtime_status_update({"pid": None, "stopped": True})
        _release_single_instance_lock()
        try:
            if getattr(sys, "frozen", False) and ".app/" in sys.executable:
                bundle = os.path.dirname(
                    os.path.dirname(os.path.dirname(sys.executable))
                )
                cmd = ["open", "-n", "-g", "-a", bundle]
            else:
                cmd = [sys.executable, os.path.abspath(sys.argv[0])] + sys.argv[1:]
            subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except Exception as exc:
            _issues_append(
                "self_relaunch_failed", "could not spawn replacement", exc=exc
            )
        os._exit(0)

    def _relaunch_after_wedge(self):
        # Let queued/in-flight transcription finish first so the recording
        # whose teardown wedged still pastes — transcription only needs
        # Metal/MLX, not the wedged audio stack. Relaunch even on drain
        # timeout: a poisoned HAL client has no other way out.
        self._drain_transcription(AUDIO_WEDGE_DRAIN_TIMEOUT_SEC)
        _issues_append("audio_wedge_relaunch", "spawning replacement instance")
        self._relaunch_self("audio wedge")

    def _reset_portaudio(self):
        """Re-initialize PortAudio so it rescans CoreAudio devices.

        PortAudio snapshots the device table once at init; after sleep/wake or
        AirPods-style device swaps the cached entries go stale, and opening
        against them fails (or, historically, crashed natively).
        """
        # Don't yank PortAudio out from under an async stream teardown
        # (bloop-audio-close thread). Bounded wait: if teardown is wedged,
        # proceeding after the timeout is no worse than the old behavior.
        got_lock = self._stream_close_lock.acquire(timeout=2.0)
        try:
            try:
                sd._terminate()
            except Exception:
                pass
            try:
                sd._initialize()
            except Exception as exc:
                _issues_append("portaudio_reinit_failed", "PortAudio re-init failed", exc=exc)
        finally:
            if got_lock:
                self._stream_close_lock.release()

    def _ensure_stream(self):
        """(Re)open the input stream on the current macOS default mic.

        Always closes any existing stream first so we pick up device changes
        (plug/unplug) cleanly. device=None tells CoreAudio to use whatever the
        system default input is right now — no stale index caching.
        """
        if self._audio_wedged:
            raise RuntimeError("audio system deadlocked; Blooop is relaunching itself")
        self._close_stream()
        # A wedged Pa_StopStream holds the CoreAudio HAL mutex forever, and
        # Pa_OpenStream needs that same mutex — opening now would drag the main
        # thread into the deadlock and freeze the whole app. Never open while
        # a teardown is still in flight.
        if not self._teardown_idle.wait(timeout=AUDIO_TEARDOWN_OPEN_WAIT_SEC):
            self._declare_audio_wedge("open blocked by unfinished teardown")
            raise RuntimeError("previous audio stream teardown never finished")
        try:
            self._stream = self._open_input_stream()
        except Exception as exc:
            # Stale device table (sleep/wake, headphones switched). Rescan
            # CoreAudio devices and retry once before giving up.
            _issues_append(
                "audio_open_retry",
                "input stream open failed; reinitializing PortAudio",
                exc=exc,
            )
            self._reset_portaudio()
            self._stream = self._open_input_stream()
        self._stream_rate = SAMPLE_RATE

        # Best-effort: show which mic is active (only when it changes).
        try:
            info = sd.query_devices(kind="input")
            name = (info or {}).get("name") or "default mic"
        except Exception:
            name = "default mic"
        self._stream_name = name
        if name != self._last_mic_announced:
            self._last_mic_announced = name
            self._print(f"○ Mic: {name}")

    def _snapshot_paste_target(self):
        """Background thread: populate _active_paste_target without blocking."""
        bid = self._get_frontmost_bundle_id()
        # Only write back if we haven't already been given a value by the time
        # this returns (e.g. latch mode already set one).
        if self._active_paste_target is None:
            self._active_paste_target = bid

    def _resolve_paste_target(self, fallback=None):
        target = fallback or self._active_paste_target
        if target:
            return target
        target = self._get_frontmost_bundle_id()
        if target:
            self._active_paste_target = target
        return target

    def _get_frontmost_bundle_id(self):
        """Best-effort bundle ID of the app currently at the front."""
        for _ in range(2):
            try:
                r = subprocess.run(
                    ['osascript', '-e',
                     'tell app "System Events" to get bundle identifier of '
                     'first process whose frontmost is true'],
                    # Keep this snappy to avoid delaying recording start.
                    capture_output=True, text=True, timeout=0.35,
                )
                if r.returncode == 0:
                    bid = r.stdout.strip()
                    if bid:
                        return bid
            except Exception:
                pass
            time.sleep(0.02)
        return None

    def _clear_latch_session_state(self):
        with self._session_lock:
            self._latch_session_id = None
            self._latch_session_row_id = None
            self._latch_session_text = ""
            self._latch_session_duration = 0.0
            self._latch_session_app = None

    def _begin_latch_session(self):
        if not LATCH_CHUNK_MODE:
            self._clear_latch_session_state()
            return None
        with self._session_lock:
            self._latch_session_seq += 1
            sid = self._latch_session_seq
            self._latch_session_id = sid
            self._latch_session_row_id = None
            self._latch_session_text = ""
            self._latch_session_duration = 0.0
            self._latch_session_app = self._active_paste_target
        # Insert a "recording" placeholder row so the panel shows the active
        # session immediately — updated in-place as chunks arrive.
        if HISTORY_ENABLED:
            try:
                row_id = _history_add(
                    status="recording",
                    app_bundle=self._active_paste_target,
                )
                self._latch_session_bind_row(sid, row_id)
            except Exception:
                pass
        return sid

    def _active_latch_session_id(self):
        with self._session_lock:
            return self._latch_session_id

    def _latch_session_touch(self, session_id, duration_sec=0.0, text_piece=None, app_bundle=None):
        with self._session_lock:
            if session_id is None or session_id != self._latch_session_id:
                return None

            if duration_sec and duration_sec > 0:
                self._latch_session_duration += float(duration_sec)

            if app_bundle and not self._latch_session_app:
                self._latch_session_app = app_bundle

            piece = (text_piece or "").strip()
            if piece:
                if self._latch_session_text:
                    self._latch_session_text += " "
                self._latch_session_text += piece

            return {
                "session_id": self._latch_session_id,
                "row_id": self._latch_session_row_id,
                "text": self._latch_session_text,
                "duration_sec": self._latch_session_duration,
                "app_bundle": self._latch_session_app,
            }

    def _latch_session_bind_row(self, session_id, row_id):
        with self._session_lock:
            if (
                row_id is None
                or session_id is None
                or session_id != self._latch_session_id
            ):
                return False
            self._latch_session_row_id = row_id
            return True

    def _latch_session_close(self, session_id):
        with self._session_lock:
            if session_id is None or session_id != self._latch_session_id:
                return None
            snapshot = {
                "session_id": self._latch_session_id,
                "row_id": self._latch_session_row_id,
                "text": self._latch_session_text,
                "duration_sec": self._latch_session_duration,
                "app_bundle": self._latch_session_app,
            }
            self._latch_session_id = None
            self._latch_session_row_id = None
            self._latch_session_text = ""
            self._latch_session_duration = 0.0
            self._latch_session_app = None
            return snapshot

    def _cancel_start_timer(self):
        timer_state, self._start_timer = self._start_timer, None
        if timer_state is None:
            return
        kind, token = timer_state
        if kind == "tk" and self._viz is not None:
            try:
                self._viz.root.after_cancel(token)
            except Exception:
                pass
            return
        if kind == "thread":
            try:
                token.cancel()
            except Exception:
                pass

    def _schedule_start_timer(self):
        self._cancel_start_timer()
        if self._viz is not None:
            try:
                token = self._viz.root.after(
                    PTT_START_DELAY_MS,
                    self._delayed_start_if_held,
                )
                self._start_timer = ("tk", token)
                return
            except Exception:
                pass
        timer = threading.Timer(
            PTT_START_DELAY_MS / 1000.0,
            self._delayed_start_if_held,
        )
        timer.daemon = True
        self._start_timer = ("thread", timer)
        timer.start()

    def _delayed_start_if_held(self):
        self._start_timer = None
        # A pending grace-tail stop still reads as recording=True; treat it as
        # stopped so a quick stop→talk-again doesn't swallow the new take
        # (start_recording flushes the previous one first).
        if self._key_held and not self.latched and (
                self._stop_pending or not self.recording):
            self.start_recording()

    def _start_chunk_worker_if_needed(self):
        if not (LATCH_CHUNK_MODE and self.latched and self.recording):
            return
        if self._chunk_thread is not None and self._chunk_thread.is_alive():
            return
        self._chunk_stop.clear()
        self._chunk_thread = threading.Thread(target=self._chunk_worker, daemon=True)
        self._chunk_thread.start()

    def _stop_chunk_worker(self):
        self._chunk_stop.set()

    def _queue_transcribe(
        self,
        frames,
        paste_target,
        auto_paste,
        session_id,
        final_chunk,
        capture_rate,
    ):
        self._transcribe_queue.put((
            frames,
            paste_target,
            auto_paste,
            session_id,
            final_chunk,
            capture_rate,
        ))

    def _start_transcribe_worker(self):
        """Spin up the dedicated transcribe worker.

        Must only be called after _prepare_whisper_runtime has imported
        mlx.core on the main thread — otherwise the worker may be the first
        thread to touch nanobind/Metal, which abort()s inside a .app bundle.
        """
        if self._transcribe_worker_thread is not None:
            return
        t = threading.Thread(
            target=self._transcribe_worker,
            daemon=True,
            name="bloop-transcribe",
        )
        t.start()
        self._transcribe_worker_thread = t

    def _transcribe_worker(self):
        while True:
            job = self._transcribe_queue.get()
            if job is None:
                # Shutdown sentinel pushed from _quit.
                return
            try:
                self._transcribe(*job)
            except Exception as exc:
                _issues_append(
                    "transcribe_worker_unhandled",
                    "transcribe worker crashed on a job",
                    exc=exc,
                )
            finally:
                self._release_transcribe_memory()

    def _release_transcribe_memory(self):
        # MLX holds onto Metal buffer pools across calls; on Apple Silicon the
        # resident set climbs every session until macOS jetsams the process.
        # Clearing the cache + a gc pass keeps RSS flat across long use.
        try:
            import mlx.core as mx
            # mx.metal.clear_cache() is deprecated; if it were removed and we
            # silently swallowed the AttributeError, RSS would climb every
            # session until macOS kills the app.
            clear = getattr(mx, "clear_cache", None) or mx.metal.clear_cache
            clear()
        except Exception:
            pass
        import gc
        gc.collect()

    def _stop_transcribe_worker(self):
        if self._transcribe_worker_thread is None:
            return
        self._transcribe_queue.put(None)
        self._transcribe_worker_thread = None

    def _flush_chunk(self, final=False):
        with self._lock:
            if not self.recording and not final:
                return False
            frames = self.frames[:]
            self.frames = []
            paste_target = self._active_paste_target
            capture_rate = self._stream_rate
        if final:
            paste_target = self._resolve_paste_target(paste_target)
        session_id = self._active_latch_session_id()

        if not frames and not (final and session_id is not None):
            return False

        self._print("⟳ Transcribing…" if final else "⟳ Transcribing chunk…")
        self._queue_transcribe(
            frames=frames,
            paste_target=paste_target,
            auto_paste=final,
            session_id=session_id,
            final_chunk=final,
            capture_rate=capture_rate,
        )
        return True

    def _chunk_worker(self):
        while not self._chunk_stop.wait(LATCH_CHUNK_SECONDS):
            with self._lock:
                if not (self.recording and self.latched):
                    return
            self._flush_chunk(final=False)

    def _on_app_activate(self):
        if self.recording or self._tx_lock.locked():
            return
        if self._history_panel:
            if self._history_panel._is_visible():
                self._history_panel.refresh_if_visible()
            else:
                self._history_panel.show()

    def _on_app_active_change(self, active):
        if self._history_panel:
            if active and (self.recording or self._tx_lock.locked()):
                return
            self._history_panel.on_app_active_change(bool(active))

    def start_recording(self):
        if self._audio_wedged:
            self._print("✗ Audio deadlocked — relaunch in progress, one moment…")
            return
        if not self._mlx_runtime_ok:
            self._notify_mlx_unavailable_once()
            return
        # A restart during the stop grace tail must not be swallowed by the
        # still-true recording flag: cut the grace short (or wait out an
        # in-flight finalize), flush the previous take, then start fresh.
        self._finalize_stop()
        with self._lock:
            if self.recording:
                return
        try:
            self._ensure_stream()
        except Exception as exc:
            _issues_append("audio_open_failed", "input stream open failed after retry", exc=exc)
            self._print(f"✗ Microphone unavailable: {exc}")
            self._print("  Check the input device, then press the hotkey again.")
            return
        with self._lock:
            if self.recording:
                return
            self.recording = True
            self.frames    = []
        if self.latched and LATCH_CHUNK_MODE:
            self._begin_latch_session()
        else:
            self._clear_latch_session_state()
        if self._viz:
            self._viz.show()
        key_hint = self._hotkey_info()["label"].lower()
        hint = (
            f"tap {key_hint} to stop"
            if self.latched
            else f"release {key_hint} to transcribe"
        )
        self._print(f"● Recording…  ({hint})")
        self._start_chunk_worker_if_needed()

    def stop_recording(self):
        self._cancel_start_timer()
        self._stop_chunk_worker()
        with self._lock:
            if not self.recording or self._stop_pending:
                return
            self._stop_pending = True
        # Don't snapshot the buffer yet: the hotkey lands while the last words
        # are still in the CoreAudio input buffer (or still being spoken).
        # Keep the callback appending for a short grace tail and defer only
        # the buffer grab; the UI flips to "transcribing" right away so the
        # stop still feels instant.
        if self._viz:
            self._viz.show_transcribing()
        timer = threading.Timer(STOP_GRACE_TAIL_SEC, self._finalize_stop)
        timer.daemon = True
        self._stop_grace_timer = timer
        timer.start()

    def _finalize_stop(self):
        # Serialized: the grace timer and a restart cutting the grace short
        # can race, and the loser must not proceed until the winner's
        # _close_stream is done — otherwise it could tear down a stream a
        # fresh start_recording just opened.
        with self._stop_finalize_lock:
            self._finalize_stop_locked()

    def _finalize_stop_locked(self):
        timer, self._stop_grace_timer = self._stop_grace_timer, None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
        with self._lock:
            if not self._stop_pending:
                return
            self._stop_pending = False
            if not self.recording:
                return
            self.recording = False
            frames = self.frames[:]
            self.frames    = []
            paste_target = self._active_paste_target
            capture_rate = self._stream_rate
            self._active_paste_target = None
        # Release the mic between sessions: the orange "mic in use" indicator
        # turns off, and no long-lived CoreAudio stream is left to go stale
        # across sleep/wake — the historical long-uptime native-crash path
        # (the next open already rebuilds the stream from scratch anyway).
        self._close_stream()
        paste_target = self._resolve_paste_target(paste_target)
        session_id = self._active_latch_session_id()
        if not frames and session_id is None:
            if self._viz:
                self._viz.hide()
            self._print("○ Stopped.")
            return
        if self._viz:
            self._viz.show_transcribing()
        self._print("⟳ Finalizing…" if not frames else "⟳ Transcribing…")
        self._queue_transcribe(
            frames=frames,
            paste_target=paste_target,
            auto_paste=True,
            session_id=session_id,
            final_chunk=True,
            capture_rate=capture_rate,
        )

    # ── Transcription ─────────────────────────────────────────────────────────

    def _paste_clipboard(self, paste_target):
        paste_ok = False
        time.sleep(0.05)
        # Build a single osascript call: optionally re-activate the source app,
        # then paste.
        script = []
        if paste_target:
            script += ['-e', f'tell application id "{paste_target}" to activate',
                       '-e', 'delay 0.08']
        script += ['-e', 'tell application "System Events" to '
                          'keystroke "v" using {command down}']
        try:
            res = subprocess.run(
                ['osascript'] + script,
                capture_output=True,
                timeout=3,
            )
            if res.returncode != 0:
                raise RuntimeError("osascript paste failed")
            paste_ok = True
        except Exception:
            try:
                with self._kb.pressed(Key.cmd):
                    self._kb.press('v')
                    self._kb.release('v')
                paste_ok = True
            except Exception:
                paste_ok = False
        return paste_ok

    def _upsert_history_row(self, row_id, status, text=None, duration_sec=None, paste_ok=None, app_bundle=None, error=None):
        if row_id is None:
            return _history_add(
                status=status,
                text=text,
                duration_sec=duration_sec,
                paste_ok=paste_ok,
                app_bundle=app_bundle,
                error=error,
            )

        ok = _history_update(
            row_id=row_id,
            status=status,
            text=text,
            duration_sec=duration_sec,
            paste_ok=paste_ok,
            app_bundle=app_bundle,
            error=error,
        )
        if ok:
            return row_id
        return _history_add(
            status=status,
            text=text,
            duration_sec=duration_sec,
            paste_ok=paste_ok,
            app_bundle=app_bundle,
            error=error,
        )

    def _resample_audio(self, audio, src_rate, dst_rate):
        if src_rate == dst_rate:
            return audio.astype("float32", copy=False)
        if len(audio) == 0:
            return audio.astype("float32", copy=False)
        if src_rate <= 0 or dst_rate <= 0:
            return audio.astype("float32", copy=False)

        # Lightweight linear interpolation resample keeps dependencies minimal.
        src_n = len(audio)
        dst_n = max(1, int(round(src_n * (float(dst_rate) / float(src_rate)))))
        x_old = np.arange(src_n, dtype=np.float64)
        x_new = np.linspace(0.0, src_n - 1, num=dst_n, dtype=np.float64)
        out = np.interp(x_new, x_old, audio).astype("float32")
        return out

    def _trim_silence(self, audio, src_rate):
        if not SILENCE_TRIM_ENABLED:
            return audio.astype("float32", copy=False), len(audio) / float(src_rate)
        if len(audio) == 0 or src_rate <= 0:
            return np.array([], dtype="float32"), 0.0

        frame = max(1, int(src_rate * (SILENCE_TRIM_WINDOW_MS / 1000.0)))
        hop = max(1, int(src_rate * (SILENCE_TRIM_HOP_MS / 1000.0)))
        if len(audio) < frame:
            return audio.astype("float32", copy=False), len(audio) / float(src_rate)

        n_frames = 1 + (len(audio) - frame) // hop
        rms = np.empty(n_frames, dtype=np.float32)
        for i in range(n_frames):
            st = i * hop
            seg = audio[st:st + frame]
            rms[i] = float(np.sqrt(np.mean(seg * seg)))

        static_thr = 10 ** (SILENCE_TRIM_DBFS / 20.0)
        if n_frames >= 4:
            noise_floor = float(np.percentile(rms, 25))
        else:
            noise_floor = float(np.min(rms))
        threshold = max(static_thr, noise_floor * SILENCE_DYNAMIC_MULT)
        voiced = rms >= threshold

        if not np.any(voiced):
            return np.array([], dtype="float32"), 0.0

        first = int(np.argmax(voiced))
        last = int(len(voiced) - 1 - np.argmax(voiced[::-1]))
        pad = int(src_rate * (SILENCE_TRIM_PAD_MS / 1000.0))
        pad_tail = int(src_rate * (SILENCE_TRIM_PAD_TAIL_MS / 1000.0))
        start = max(0, first * hop - pad)
        end = min(len(audio), last * hop + frame + pad_tail)
        if end <= start:
            return np.array([], dtype="float32"), 0.0

        trimmed = audio[start:end].astype("float32", copy=False)
        return trimmed, len(trimmed) / float(src_rate)

    def _transcribe_audio(self, frames, capture_rate=SAMPLE_RATE):
        duration = None
        if not self._mlx_runtime_ok:
            return "error", "", duration, "mlx_runtime_unavailable"
        if not frames:
            return "no_audio", "", duration, None

        audio = np.concatenate(frames).flatten()
        src_rate = int(capture_rate) if capture_rate else SAMPLE_RATE
        if src_rate <= 0:
            src_rate = SAMPLE_RATE
        duration = len(audio) / float(src_rate)
        raw_rms = float(np.sqrt(np.mean(audio * audio))) if len(audio) else 0.0
        raw_peak = float(np.max(np.abs(audio))) if len(audio) else 0.0

        if duration < MIN_DURATION:
            return "too_short", "", duration, f"{duration:.2f}s"

        work_audio = audio
        if SILENCE_TRIM_ENABLED:
            work_audio, voiced_duration = self._trim_silence(audio, src_rate)
            if voiced_duration < SILENCE_MIN_VOICED_SEC:
                if raw_rms < 0.003:
                    return (
                        "no_speech",
                        "",
                        duration,
                        f"silent_input:rms={raw_rms:.6f}:peak={raw_peak:.6f}",
                    )
                return "no_speech", "", duration, None

        model_audio = self._resample_audio(work_audio, src_rate, SAMPLE_RATE)
        if WHISPER_TAIL_PAD_SEC > 0 and len(model_audio):
            model_audio = np.concatenate([
                model_audio,
                np.zeros(int(SAMPLE_RATE * WHISPER_TAIL_PAD_SEC), dtype="float32"),
            ])

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
            tmp = fh.name
        try:
            sf.write(tmp, model_audio, SAMPLE_RATE)
            w = self._get_whisper()
            initial_prompt = _custom_vocab_initial_prompt(CUSTOM_VOCAB)
            result = w.transcribe(
                tmp,
                path_or_hf_repo=self._model_source(self._runtime_model),
                temperature=WHISPER_TEMPERATURE,
                compression_ratio_threshold=WHISPER_COMPRESSION_RATIO_THRESHOLD,
                logprob_threshold=WHISPER_LOGPROB_THRESHOLD,
                no_speech_threshold=WHISPER_NO_SPEECH_THRESHOLD,
                condition_on_previous_text=WHISPER_CONDITION_ON_PREVIOUS_TEXT,
                initial_prompt=initial_prompt,
            )
            text, dropped_segments = _filter_result_segments(result, initial_prompt)
            if dropped_segments:
                _issues_append(
                    "segments_filtered",
                    f"dropped {len(dropped_segments)} confabulated segment(s): "
                    + "; ".join(dropped_segments)[:300],
                )
        except Exception as exc:
            _issues_append("transcribe_failed", "transcribe call failed", exc=exc)
            return "error", "", duration, str(exc)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

        if not text:
            if raw_rms < 0.003:
                return (
                    "no_speech",
                    "",
                    duration,
                    f"silent_input:rms={raw_rms:.6f}:peak={raw_peak:.6f}",
                )
            return "no_speech", "", duration, None
        if _looks_like_prompt_echo(text, initial_prompt):
            return (
                "no_speech",
                "",
                duration,
                f"prompt_echo_filtered:{text!r}:rms={raw_rms:.6f}",
            )
        if _looks_like_hallucination(text):
            return (
                "no_speech",
                "",
                duration,
                f"hallucination_filtered:{text!r}:rms={raw_rms:.6f}",
            )
        stripped = _truncate_phrase_loops(text)
        if stripped != text:
            # Repetition loop at the tail of real dictation: keep the prefix,
            # drop the loop. _looks_like_hallucination already caught the
            # loop-only case, so stripped is non-empty here.
            _issues_append(
                "hallucination_loop_trimmed",
                f"chunk trimmed {len(text)}->{len(stripped)} chars; "
                f"loop tail: {text[len(stripped):][:120]!r}",
            )
            if _looks_like_hallucination(stripped):
                return (
                    "no_speech",
                    "",
                    duration,
                    f"hallucination_filtered:{stripped!r}:rms={raw_rms:.6f}",
                )
            text = stripped
        return "ok", text, duration, None

    def _transcribe(
        self,
        frames,
        paste_target,
        auto_paste=True,
        session_id=None,
        final_chunk=True,
        capture_rate=SAMPLE_RATE,
    ):
        try:
            with self._tx_lock:
                self._transcribe_locked(
                    frames=frames,
                    paste_target=paste_target,
                    auto_paste=auto_paste,
                    session_id=session_id,
                    final_chunk=final_chunk,
                    capture_rate=capture_rate,
                )
        finally:
            if final_chunk and self._viz and not self.recording:
                self._viz.hide()

    def _transcribe_locked(
        self,
        frames,
        paste_target,
        auto_paste=True,
        session_id=None,
        final_chunk=True,
        capture_rate=SAMPLE_RATE,
    ):
        status, text, duration, error = self._transcribe_audio(
            frames, capture_rate=capture_rate
        )
        in_latch_session = session_id is not None

        if not in_latch_session:
            if status == "no_audio":
                self._print("○ No audio captured.")
                self._record_history("no_audio", duration_sec=duration, app_bundle=paste_target)
                return
            if status == "too_short":
                self._print(f"○ Too short ({duration:.2f}s) – skipped.")
                self._record_history(
                    "too_short",
                    duration_sec=duration,
                    app_bundle=paste_target,
                    error=error,
                )
                return
            if status == "error":
                self._print(f"✗ Error: {error}")
                self._record_history(
                    "error",
                    duration_sec=duration,
                    app_bundle=paste_target,
                    error=error,
                )
                return
            if status == "no_speech":
                if error and str(error).startswith("silent_input"):
                    self._print(
                        "○ No speech detected (input was near-silent). Check Microphone permission/input."
                    )
                    if getattr(sys, "frozen", False):
                        self._warn_silent_microphone_input(open_settings=True)
                else:
                    self._print("○ No speech detected.")
                self._record_history(
                    "no_speech",
                    duration_sec=duration,
                    app_bundle=paste_target,
                    error=error,
                )
                return

            try:
                pyperclip.copy(text)
            except Exception as exc:
                self._print(f"✗ Clipboard error: {exc}")
                self._record_history(
                    "error",
                    text=text,
                    duration_sec=duration,
                    app_bundle=paste_target,
                    error=f"clipboard: {exc}",
                )
                return

            preview = text[:72] + ("…" if len(text) > 72 else "")
            self._print(f"✓ {preview}")

            paste_ok = None
            if AUTO_PASTE and auto_paste:
                paste_ok = self._paste_clipboard(paste_target)
                if paste_ok is False:
                    self._print("⚠ Auto-paste failed; text is on clipboard (⌘V).")

            self._record_history(
                "ok",
                text=text,
                duration_sec=duration,
                paste_ok=paste_ok,
                app_bundle=paste_target,
            )
            return

        session = self._latch_session_touch(
            session_id=session_id,
            duration_sec=0.0 if duration is None else duration,
            text_piece=text if status == "ok" else None,
            app_bundle=paste_target,
        )
        if session is None:
            return

        if status == "error":
            self._print(f"✗ Error: {error}")
        elif status == "ok" and not final_chunk:
            preview = text[:72] + ("…" if len(text) > 72 else "")
            self._print(f"✓ {preview}")

        # For partial chunks: update the placeholder row in-place with
        # accumulated text but keep status="recording" until the session ends.
        if not final_chunk:
            row_id = session.get("row_id")
            if row_id and session.get("text"):
                try:
                    self._upsert_history_row(
                        row_id=row_id,
                        status="recording",
                        text=session["text"],
                        duration_sec=session["duration_sec"],
                        paste_ok=None,
                        app_bundle=session["app_bundle"],
                        error=None,
                    )
                except Exception as exc:
                    self._print(f"⚠ History update failed: {exc}")
            return

        closed = self._latch_session_close(session_id)
        if not closed:
            return

        final_text = (closed["text"] or "").strip()
        if final_text:
            stripped_final = _truncate_phrase_loops(final_text)
            if stripped_final != final_text:
                # Safety net for loops split across chunk boundaries: each
                # chunk stayed under the consecutive-repeat threshold, but the
                # assembled session text crosses it.
                _issues_append(
                    "hallucination_loop_trimmed",
                    f"assembled transcript trimmed {len(final_text)}->"
                    f"{len(stripped_final)} chars",
                )
                if not stripped_final and status == "ok":
                    status = "no_speech"
                    error = "hallucination_loop_filtered:assembled"
                final_text = stripped_final
        final_duration = closed["duration_sec"]
        final_app = paste_target or closed["app_bundle"] or self._resolve_paste_target()
        row_id = closed["row_id"]

        if final_text:
            try:
                pyperclip.copy(final_text)
            except Exception as exc:
                self._print(f"✗ Clipboard error: {exc}")
                try:
                    row_id = self._upsert_history_row(
                        row_id=row_id,
                        status="error",
                        text=final_text,
                        duration_sec=final_duration,
                        paste_ok=None,
                        app_bundle=final_app,
                        error=f"clipboard: {exc}",
                    )
                    self._print_history_live(
                        row_id=row_id,
                        status="error",
                        text=final_text,
                        duration_sec=final_duration,
                        paste_ok=None,
                        error=f"clipboard: {exc}",
                    )
                except Exception as write_exc:
                    self._print(f"⚠ History write failed: {write_exc}")
                return

            final_preview = final_text[:72] + ("…" if len(final_text) > 72 else "")
            self._print(f"✓ {final_preview}")

            paste_ok = None
            if AUTO_PASTE and auto_paste:
                paste_ok = self._paste_clipboard(final_app)
                if paste_ok is False:
                    self._print("⚠ Auto-paste failed; text is on clipboard (⌘V).")

            try:
                row_id = self._upsert_history_row(
                    row_id=row_id,
                    status="ok",
                    text=final_text,
                    duration_sec=final_duration,
                    paste_ok=paste_ok,
                    app_bundle=final_app,
                    error=None,
                )
                self._print_history_live(
                    row_id=row_id,
                    status="ok",
                    text=final_text,
                    duration_sec=final_duration,
                    paste_ok=paste_ok,
                    error=None,
                )
            except Exception as exc:
                self._print(f"⚠ History write failed: {exc}")
            return

        # Avoid noise rows when unlatching with no meaningful captured audio.
        effective_duration = final_duration if final_duration and final_duration > 0 else duration
        if status in ("no_audio", "too_short") and (
            effective_duration is None or effective_duration < MIN_DURATION
        ):
            self._print("○ Stopped.")
            return

        if status == "too_short" and effective_duration is not None:
            self._print(f"○ Too short ({effective_duration:.2f}s) – skipped.")
        elif status == "no_speech":
            if error and str(error).startswith("silent_input"):
                self._print(
                    "○ No speech detected (input was near-silent). Check Microphone permission/input."
                )
                if getattr(sys, "frozen", False):
                    self._warn_silent_microphone_input(open_settings=True)
            else:
                self._print("○ No speech detected.")
        elif status == "no_audio":
            self._print("○ No audio captured.")

        try:
            row_id = self._upsert_history_row(
                row_id=row_id,
                status=status,
                text=None,
                duration_sec=effective_duration,
                paste_ok=None,
                app_bundle=final_app,
                error=error
                if status in ("error", "too_short", "no_speech")
                else None,
            )
            self._print_history_live(
                row_id=row_id,
                status=status,
                text=None,
                duration_sec=effective_duration,
                paste_ok=None,
                error=error
                if status in ("error", "too_short", "no_speech")
                else None,
            )
        except Exception as exc:
            self._print(f"⚠ History write failed: {exc}")

    def _get_whisper(self):
        with self._wlock:
            if self._whisper is None:
                self._whisper = _import_whisper()
            return self._whisper

    # ── Hotkeys ───────────────────────────────────────────────────────────────

    def _handle_ptt_press(self):
        now = time.monotonic()
        print(f"[hotkey] PRESS  held={self._key_held} rec={self.recording} "
              f"latch={self.latched} last_tap={self._last_was_tap}")
        # Recover from a missed key-up. If we're not recording, stale held state
        # should never block a fresh press.
        if self._key_held:
            if not self.recording:
                self._key_held = False
                self._ignore_release_once = False
            elif (now - self._press_time) < max(0.8, (DOUBLE_TAP_MS / 1000.0) * 2.0):
                return
            else:
                self._key_held = False
                self._ignore_release_once = False
        self._key_held = True
        self._press_time = now
        # Kick off the bundle-ID lookup immediately on key-down so the 110ms
        # start-delay gives it time to complete before recording begins.
        if not (self.latched and self.recording):
            self._active_paste_target = None
            threading.Thread(
                target=self._snapshot_paste_target, daemon=True
            ).start()

        # Double-tap configured hotkey enters latch mode.
        if (not self.latched and self._last_was_tap and
                (now - self._last_release) <= (DOUBLE_TAP_MS / 1000)):
            self.latched = True
            # Ignore this release so latch doesn't immediately turn off.
            self._ignore_release_once = True
            self._last_was_tap = False
            self._cancel_start_timer()
            if self._stop_pending or not self.recording:
                self.start_recording()
            else:
                key_hint = self._hotkey_info()["label"].lower()
                self._print(f"● Recording…  (latched; tap {key_hint} to stop)")
            return

        if self.latched:
            # Tap toggles latch off immediately on key-down. This avoids getting
            # stuck if a key-up event is dropped by the global monitor.
            self.latched = False
            self._ignore_release_once = True
            self._last_was_tap = False
            if self.recording:
                self.stop_recording()
            else:
                self._print("○ Stopped.")
            return

        # Non-latched PTT: defer start slightly so quick taps for double-tap
        # latch don't create empty recordings/history rows.
        if PTT_START_DELAY_MS <= 0:
            self.start_recording()
        else:
            self._schedule_start_timer()

    def _handle_ptt_release(self):
        now = time.monotonic()
        held = now - self._press_time if self._key_held else -1
        print(f"[hotkey] RELEASE  held_flag={self._key_held} held_dur={held:.3f}s "
              f"rec={self.recording} latch={self.latched} ignore={self._ignore_release_once}")
        if not self._key_held:
            # Recovery for dropped key-downs while recording in push-to-talk mode.
            if self.recording and not self.latched:
                self._cancel_start_timer()
                self.stop_recording()
                self._last_release = now
                self._last_was_tap = False
                return
            # Fallback for dropped key-down events: allow release-only unlatch.
            if self.latched and (now - self._last_release) > 0.08:
                self.latched = False
                self._ignore_release_once = False
                self._last_release = now
                self._last_was_tap = False
                if self.recording:
                    self.stop_recording()
                else:
                    self._print("○ Stopped.")
            return
        self._key_held = False
        self._cancel_start_timer()
        if self._ignore_release_once:
            self._ignore_release_once = False
            self._last_release = now
            self._last_was_tap = False
            return
        held = now - self._press_time
        self._last_release = now
        # Count as a "tap" (for double-tap latch) purely by held duration.
        # Gating on `not self.recording` made the gesture a coin flip: real
        # taps land right at the PTT_START_DELAY_MS boundary (observed
        # 0.103–0.187s), so whenever the deferred start fired mid-tap the
        # first tap was voided and the double-tap silently failed. A finished
        # push-to-talk hold is far longer than DOUBLE_TAP_MS, so duration
        # alone still prevents accidental latch when one PTT quickly follows
        # another; the sub-window recording a tap may have started is shorter
        # than MIN_DURATION and gets discarded anyway.
        self._last_was_tap = held <= (DOUBLE_TAP_MS / 1000)

        if self.latched:
            self.latched = False
            if self.recording:
                self.stop_recording()
            return

        if self.recording:
            self.stop_recording()

    def _is_ptt_key(self, key):
        return key == self._ptt_key

    def _on_press(self, key):
        if self._is_ptt_key(key):
            if self._viz is not None:
                self._viz.post_to_ui(self._handle_ptt_press)
            else:
                self._handle_ptt_press()
            return

    def _on_release(self, key):
        if not self._is_ptt_key(key):
            return

        if self._viz is not None:
            self._viz.post_to_ui(self._handle_ptt_release)
        else:
            self._handle_ptt_release()

    # ── Utility ───────────────────────────────────────────────────────────────

    def _print_history_live(self, row_id, status, text=None, duration_sec=None, paste_ok=None, error=None):
        if not HISTORY_LIVE_FEED:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        dur_s = "-" if duration_sec is None else f"{duration_sec:.2f}s"
        if paste_ok is None:
            paste_s = "-"
        else:
            paste_s = "ok" if paste_ok else "fail"
        msg = _history_compact_message(text, error, max_chars=HISTORY_LIVE_PREVIEW_CHARS)
        rid = "?" if row_id is None else str(row_id)
        print(f"[H{rid}] {ts}  {status:<9} dur={dur_s:<7} paste={paste_s:<4} {msg}", flush=True)

    def _record_history(self, status, text=None, duration_sec=None, paste_ok=None, app_bundle=None, error=None):
        row_id = None
        try:
            row_id = _history_add(
                status=status,
                text=text,
                duration_sec=duration_sec,
                paste_ok=paste_ok,
                app_bundle=app_bundle,
                error=error,
            )
        except Exception as exc:
            self._print(f"⚠ History write failed: {exc}")
        else:
            self._print_history_live(
                row_id=row_id,
                status=status,
                text=text,
                duration_sec=duration_sec,
                paste_ok=paste_ok,
                error=error,
            )

    def _print(self, msg):
        print(f"\r{msg:<80}", flush=True)

    def _ensure_mlx_runtime(self):
        if self._mlx_probe_checked:
            return self._mlx_runtime_ok

        self._mlx_probe_checked = True
        if not _supports_subprocess_mlx_probe():
            # Standalone macOS bundles initialize MLX more reliably through the
            # real app process on the main thread than through an extra child
            # process launched only for probing.
            self._mlx_runtime_ok = True
            self._mlx_runtime_error = None
            return True

        try:
            cmd, proc = _run_self_subprocess(["--probe-mlx"], timeout=25)
        except Exception as exc:
            self._mlx_runtime_ok = False
            self._mlx_runtime_error = f"probe failed to start: {type(exc).__name__}: {exc}"
            _issues_append("mlx_probe_subprocess_failed", "mlx probe subprocess failed", exc=exc)
            return False

        if proc.returncode == 0:
            self._mlx_runtime_ok = True
            self._mlx_runtime_error = None
            return True

        self._mlx_runtime_ok = False
        self._mlx_runtime_error = _summarize_probe_failure(proc)
        detail = [
            "== MLX startup probe failed ==",
            f"time: {_utc_now_iso()}",
            f"cmd: {' '.join(cmd)}",
            f"returncode: {proc.returncode}",
            "",
            "stdout:",
            (proc.stdout or "").rstrip() or "(empty)",
            "",
            "stderr:",
            (proc.stderr or "").rstrip() or "(empty)",
        ]
        _issues_append(
            "mlx_probe_failed",
            f"startup probe failed ({self._mlx_runtime_error})",
        )
        _issues_write_report("mlx-probe-failed", "\n".join(detail))
        return False

    def _notify_mlx_unavailable_once(self):
        if self._mlx_unavailable_notified:
            return
        self._mlx_unavailable_notified = True
        self._print("✗ Transcription unavailable: MLX runtime failed to initialize.")
        if self._mlx_runtime_error:
            print(f"  Detail: {self._mlx_runtime_error}")
        print("  Run `blooop --doctor` for diagnostics.")
        print()

    def _prewarm(self):
        if not self._tx_lock.acquire(timeout=0.05):
            return
        tmp = None
        try:
            self._get_whisper()
            silent = np.zeros(SAMPLE_RATE // 4, dtype="float32")
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
                tmp = fh.name
            sf.write(tmp, silent, SAMPLE_RATE)
            self._whisper.transcribe(
                tmp,
                path_or_hf_repo=self._model_source(self._runtime_model),
                temperature=WHISPER_TEMPERATURE,
                compression_ratio_threshold=WHISPER_COMPRESSION_RATIO_THRESHOLD,
                logprob_threshold=WHISPER_LOGPROB_THRESHOLD,
                no_speech_threshold=WHISPER_NO_SPEECH_THRESHOLD,
                condition_on_previous_text=WHISPER_CONDITION_ON_PREVIOUS_TEXT,
            )
            self._print(f"○ Model ready: {self._runtime_model}")
        except Exception as exc:
            self._print(f"⚠ Prewarm failed ({exc}) – will load on first use.")
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            try:
                self._tx_lock.release()
            except Exception:
                pass

    def _prepare_whisper_runtime(self):
        """Initialize MLX Whisper + first transcribe on main thread.

        Some MLX builds can abort when `mlx.core` first initializes from a
        background Python thread. Force the full initialization path here so
        later worker-thread transcriptions run against an already-initialized
        runtime.
        """
        # Step 1: import mlx.core on the main thread.  This MUST succeed before
        # any background transcription threads run – nanobind/Metal will abort()
        # if mlx.core is first imported on a non-main thread inside a .app.
        try:
            import mlx          # noqa: F401
            import mlx.core     # noqa: F401
        except Exception as exc:
            self._mlx_runtime_ok = False
            self._mlx_runtime_error = f"mlx.core init failed: {exc}"
            _issues_append("mlx_runtime_init_failed", "startup mlx.core import failed", exc=exc)
            self._print(f"✗ MLX core failed to initialize: {exc}")
            self._print("  Transcription will be unavailable until Blooop is restarted.")
            return False

        # mlx.core is now in sys.modules — safe to spin up the worker thread.
        # We start it before warmup so any job enqueued during warmup is
        # processed in order.
        self._start_transcribe_worker()

        # Step 2: warmup – load model weights + run one dummy inference so the
        # first real transcription is fast.  Failure here is non-fatal because
        # mlx.core is already in sys.modules (background threads are safe).
        # Hold the transcribe lock for the whole warmup: a recording that
        # finishes during warmup is queued to the worker thread, and two
        # threads inside MLX/Metal at once is an intermittent-abort path.
        self._tx_lock.acquire()
        try:
            w = self._get_whisper()
            silent = np.zeros(SAMPLE_RATE // 8, dtype="float32")
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
                tmp = fh.name
            try:
                sf.write(tmp, silent, SAMPLE_RATE)
                w.transcribe(
                    tmp,
                    path_or_hf_repo=self._model_source(self._runtime_model),
                    temperature=WHISPER_TEMPERATURE,
                    compression_ratio_threshold=WHISPER_COMPRESSION_RATIO_THRESHOLD,
                    logprob_threshold=WHISPER_LOGPROB_THRESHOLD,
                    no_speech_threshold=WHISPER_NO_SPEECH_THRESHOLD,
                    condition_on_previous_text=WHISPER_CONDITION_ON_PREVIOUS_TEXT,
                )
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            self._mlx_runtime_ok = True
            self._mlx_runtime_error = None
            self._print("○ Whisper runtime initialized.")
            return True
        except Exception as exc:
            _issues_append("whisper_warmup_failed", "startup warmup failed", exc=exc)
            self._print(
                f"⚠ Whisper startup warmup failed ({exc}) – retrying on first use."
            )
            return False
        finally:
            self._tx_lock.release()

    # ── Run ───────────────────────────────────────────────────────────────────

    def _quit(self):
        with self._teardown_state_lock:
            self._quitting = True
        self._cancel_start_timer()
        self._stop_chunk_worker()
        self._stop_transcribe_worker()
        self._stop_settings_watch()
        self._close_stream()
        _runtime_status_update({"pid": None, "stopped": True})
        _release_single_instance_lock()
        if self._hotkey_monitor:
            self._hotkey_monitor.stop()
            self._hotkey_monitor = None
        if self._menu_bar is not None:
            # Dropping the ref releases the NSStatusItem; the system removes it.
            self._menu_bar = None
        if self._history_panel:
            self._history_panel.stop()
        if self._viz:
            self._viz.stop()

    def _open_privacy_settings(self, open_microphone=False, open_accessibility=False):
        urls = []
        if open_microphone:
            urls.append(
                "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
            )
        if open_accessibility:
            urls.append(
                "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
            )
        for url in urls:
            try:
                subprocess.Popen(
                    ["open", url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass
            time.sleep(0.2)

    def _notify_permission_requirements(self, mic_ok, accessibility_ok):
        missing = []
        if not mic_ok:
            missing.append("Microphone")
        if not accessibility_ok:
            missing.append("Accessibility")
        if not missing:
            return

        print("⚠  Missing macOS permissions:", ", ".join(missing))
        if not mic_ok:
            print("   - Privacy & Security → Microphone → enable Blooop")
        if not accessibility_ok:
            print("   - Privacy & Security → Accessibility → enable Blooop")
        print("   Relaunch Blooop after enabling permissions.")
        print()

        label = " and ".join(missing)
        msg = (
            f"Enable {label} for Blooop in System Settings > Privacy & Security, "
            "then relaunch Blooop."
        )
        msg = msg.replace('"', '\\"')
        try:
            subprocess.Popen(
                [
                    "osascript",
                    "-e",
                    (
                        'display alert "Blooop needs permissions" '
                        f'message "{msg}" '
                        'buttons {"OK"} default button "OK"'
                    ),
                ]
                ,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def _check_accessibility(self):
        """Return whether Accessibility is currently granted."""
        try:
            from ApplicationServices import (
                AXIsProcessTrusted,
                AXIsProcessTrustedWithOptions,
                kAXTrustedCheckOptionPrompt,
            )
        except Exception as exc:
            if getattr(sys, "frozen", False):
                # In a standalone .app, import failure likely means a bundling
                # issue — do NOT assume granted; fall through to the prompt.
                print(f"⚠  ApplicationServices import failed in frozen app: {exc}")
                self._open_privacy_settings(open_accessibility=True)
                return False
            return True

        try:
            if AXIsProcessTrusted():
                return True
        except Exception:
            return True

        # Ask macOS to show the native trust prompt for this app/bundle.
        try:
            AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
            # Give the prompt a moment to settle before we continue.
            time.sleep(0.1)
            if AXIsProcessTrusted():
                return True
        except Exception:
            pass

        print("⚠  Accessibility permission not granted.")
        print("   Blooop needs Accessibility permission to listen for the global hotkey.")
        print()
        print("System Settings → Privacy & Security → Accessibility")
        print("→ enable Blooop (or your terminal app).")
        print()
        print("Then restart Blooop.")
        print()
        return False

    def _check_microphone_permission(self):
        """Request mic permission via AVFoundation, with audio-probe fallback."""
        try:
            from AVFoundation import AVCaptureDevice, AVMediaTypeAudio
            status = int(AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio))
            # 0=notDetermined  1=restricted  2=denied  3=authorized
            if status == 3:
                return True
            if status in (1, 2):
                print("⚠  Microphone permission denied.")
                print("   System Settings → Privacy & Security → Microphone → enable Blooop.")
                print()
                return False
            # status == 0: not determined — show native TCC dialog and wait
            print("○ Requesting Microphone permission…")
            _granted = [None]
            _done = threading.Event()

            def _cb(granted):
                _granted[0] = bool(granted)
                _done.set()

            AVCaptureDevice.requestAccessForMediaType_completionHandler_(AVMediaTypeAudio, _cb)
            _done.wait(timeout=15.0)
            if _granted[0]:
                return True
            if _granted[0] is None:
                print("⚠  Microphone permission request timed out.")
                print("   Falling back to direct audio probe…")
                print()
                return self._probe_microphone_input()
            else:
                print("⚠  Microphone permission denied.")
            print("   System Settings → Privacy & Security → Microphone → enable Blooop.")
            print()
            return False
        except Exception as exc:
            if getattr(sys, "frozen", False):
                print(f"⚠  AVFoundation import failed in frozen app: {exc}")
                print("   Cannot verify Microphone permission; opening System Settings.")
                self._open_privacy_settings(open_microphone=True)
            else:
                print(f"⚠  AVFoundation mic permission check unavailable ({exc}).")
            print("   Falling back to direct audio probe…")
            print()
            return self._probe_microphone_input()

    def _probe_microphone_input(self):
        # Fallback for CLI / limited AVFoundation environments.
        probe = None
        try:
            captured = []

            def _probe_cb(indata, _n_frames, _t, _status):
                try:
                    captured.append(indata.copy())
                except Exception:
                    pass

            probe = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="float32",
                callback=_probe_cb,
            )
            probe.start()
            time.sleep(0.5)
            probe.stop()
            if not captured:
                self._warn_silent_microphone_input()
                return False
            audio = np.concatenate(captured).flatten()
            if len(audio) == 0:
                self._warn_silent_microphone_input()
                return False
            peak = float(np.max(np.abs(audio)))
            if peak <= 1e-7:
                self._warn_silent_microphone_input()
                return False
            return True
        except Exception:
            print("⚠  Microphone permission not granted yet.")
            print("   System Settings → Privacy & Security → Microphone → enable Blooop.")
            print()
            return False
        finally:
            if probe is not None:
                try:
                    probe.close()
                except Exception:
                    pass

    def _warn_silent_microphone_input(self, open_settings=False):
        if self._mic_silent_warned:
            return
        self._mic_silent_warned = True
        print("⚠  Microphone input appears silent (all-zero samples).")
        print("   This usually means permission/input selection is not ready for Blooop.")
        print()
        print("System Settings → Privacy & Security → Microphone")
        print("→ enable Blooop and verify the active input device.")
        print()
        if open_settings:
            self._open_privacy_settings(open_microphone=True)

    def _check_ffmpeg_dependency(self):
        ffmpeg = _resolve_ffmpeg()
        if ffmpeg:
            os.environ["FFMPEG_BINARY"] = ffmpeg
            return True

        print("✗ Missing dependency: ffmpeg")
        print("  Install with: brew install ffmpeg")
        print("  Then relaunch Blooop.")
        print()
        try:
            subprocess.Popen(
                [
                    "osascript",
                    "-e",
                    (
                        'display alert "Blooop needs ffmpeg" '
                        'message "Install ffmpeg in Terminal: brew install ffmpeg. '
                        'Then relaunch Blooop." '
                        'buttons {"OK"} default button "OK"'
                    ),
                ]
                ,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
        return False

    def run(self):
        if not _acquire_single_instance_lock():
            print("⚠ Blooop is already running. Quit the existing instance first.")
            try:
                subprocess.Popen(
                    [
                        "osascript",
                        "-e",
                        (
                            'display alert "Blooop is already running" '
                            'message "Quit the existing Blooop instance from the Dock, then reopen." '
                            'buttons {"OK"} default button "OK"'
                        ),
                    ]
                    ,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass
            return

        self._reload_settings(force=True, announce=False)
        self._start_settings_watch()

        hotkey_label = self._hotkey_info()["label"]
        model_label = self._runtime_model
        if self._requested_model != self._runtime_model:
            model_label = f"{self._runtime_model} (next: {self._requested_model})"
        model_label = model_label[:40]
        print(f"○ Build: {RUNTIME_BUILD}  pid={os.getpid()}")
        print(f"○ Executable: {sys.executable}")
        print("┌─────────────────────────────────────────────────────┐")
        print("│  bloop_flow  –  local whisper on Apple Silicon      │")
        print("├─────────────────────────────────────────────────────┤")
        print(f"│  model    : {model_label:<40}│")
        print(f"│  PTT      : hold {hotkey_label:<10} → release to transcribe  │")
        print(f"│  latch    : dbl-tap {hotkey_label:<10} on, tap to turn off    │")
        chunk_desc = f"{int(LATCH_CHUNK_SECONDS)}s chunks" if LATCH_CHUNK_MODE else "off"
        print(f"│  chunks   : {chunk_desc:<37}│")
        print(f"│  paste    : {'auto-paste' if AUTO_PASTE else 'copy only':<38}│")
        print( "│  quit     : Ctrl-C                                  │")
        print("└─────────────────────────────────────────────────────┘")
        print()

        if not _TK:
            print("⚠ tkinter not found – waveform overlay disabled.")
            print("  Install with: brew install python-tk@3.x")
            if HISTORY_ENABLED and self._history_ui_enabled_runtime:
                print("  History UI disabled (tkinter unavailable).")
            print()
        elif not self._overlay_enabled:
            print("○ Waveform overlay: off (focus-safe mode)")
            print("  Set BLOOOP_OVERLAY_ENABLED=1 to re-enable.")
            print()
        elif self._pill_window_enabled:
            print("○ Status indicator: pointer-follow overlay + menu bar/Dock")
            print("  Shows REC while recording and TXT while whisper finishes.")
            print()

        if HISTORY_ENABLED:
            try:
                _history_init()
                print(f"○ History enabled: {_history_path()}")
                print(f"  Retention: max {HISTORY_MAX_ROWS} rows, {HISTORY_MAX_DAYS} days")
                print(f"  Live feed: {'on' if HISTORY_LIVE_FEED else 'off'}")
                print(f"  Issues log: {ISSUES_LOG_PATH}")
                if self._history_ui_enabled_runtime:
                    print(f"  UI panel: {'on' if self._history_panel else 'off'}")
                elif HISTORY_UI_ENABLED and getattr(sys, "frozen", False):
                    print("  UI panel: off (standalone single-Dock mode)")
            except Exception as exc:
                print(f"⚠ History init failed: {exc}")

        if not self._check_ffmpeg_dependency():
            self._stop_settings_watch()
            _release_single_instance_lock()
            return

        print("⚙ Initializing runtime…")
        if self._ensure_mlx_runtime():
            if self._viz:
                # Defer warmup until Tk mainloop is active; some macOS/MLX builds
                # are more stable once the app event loop is running. The
                # transcribe worker is started from inside _prepare_whisper_runtime
                # once mlx.core has been imported on the main thread.
                self._viz.root.after(120, self._prepare_whisper_runtime)
                print("○ Whisper warmup: deferred until UI loop starts")
            else:
                self._prepare_whisper_runtime()
            print("○ Transcribe dispatch: worker-thread")
        else:
            self._notify_mlx_unavailable_once()

        mic_ok = self._check_microphone_permission()
        if not mic_ok:
            self._print(
                "⚠ Microphone not granted. Recording disabled until permission is enabled."
            )

        accessibility_ok = self._check_accessibility()
        if not accessibility_ok:
            self._print(
                "⚠ Accessibility not granted. Hotkeys disabled until permission is enabled."
            )

        if not mic_ok or not accessibility_ok:
            if not mic_ok:
                self._open_privacy_settings(open_microphone=True)
            self._notify_permission_requirements(
                mic_ok=mic_ok,
                accessibility_ok=accessibility_ok,
            )

        if self._history_panel:
            self._history_panel.start()
            self._history_panel.show()

        if self._viz:
            # In .app mode, AppKit monitors are more stable than pynput threads.
            hotkey_started = False
            if accessibility_ok:
                hk = self._hotkey_info()
                # Funnel hotkey callbacks through the Tk-owned callback queue so
                # Tk and PortAudio state are touched only from the main thread.
                mon = MacCommandHotkeyMonitor(
                    on_press=self._handle_ptt_press,
                    on_release=self._handle_ptt_release,
                    key_code=hk["mac_key_code"],
                    modifier=hk["mac_modifier"],
                    schedule=self._viz.post_to_ui,
                )
                if mon.start():
                    self._hotkey_monitor = mon
                    hotkey_started = True
                    print("○ Hotkey monitor: appkit")
                else:
                    print("⚠ AppKit hotkey monitor unavailable; falling back to pynput.")

            if accessibility_ok and not hotkey_started:
                # Fallback for environments where AppKit global monitor fails.
                def _listen():
                    with keyboard.Listener(
                        on_press=self._on_press,
                        on_release=self._on_release,
                    ) as lst:
                        lst.join()

                threading.Thread(target=_listen, daemon=True).start()
                print("○ Hotkey monitor: pynput")
            signal.signal(signal.SIGINT, lambda s, f: self._quit())

            # Menu bar icon (Show History / Show Settings / Quit). The Dock
            # icon is back, but this is still a useful secondary shortcut and
            # recording-state indicator when the pill window is suppressed.
            def _sched_menu(cb):
                self._viz.post_to_ui(cb)

            def _menu_show_history():
                if self._history_panel is not None:
                    self._history_panel.show()

            def _menu_show_settings():
                if self._history_panel is None:
                    return
                self._history_panel.show()
                if not self._history_panel._settings_visible:
                    try:
                        self._history_panel._toggle_settings_panel()
                    except Exception:
                        pass

            self._menu_bar = _install_menu_bar_icon({
                "show_history": lambda: _sched_menu(_menu_show_history),
                "show_settings": lambda: _sched_menu(_menu_show_settings),
                "quit": lambda: _sched_menu(self._quit),
            })
            if self._menu_bar is not None:
                print("○ Menu bar icon: on")
            # Route viz mode changes to the menu bar icon and Dock badge so
            # recording state remains visible even when the overlay is missed.
            menu_bar = self._menu_bar
            def _on_viz_mode(mode):
                try:
                    _macos_set_dock_badge(_indicator_state_label(mode))
                except Exception:
                    pass
                if menu_bar is not None:
                    try:
                        menu_bar.set_state(mode)
                    except Exception:
                        pass
            def _on_viz_level(level):
                if menu_bar is not None:
                    try:
                        menu_bar.set_level(level)
                    except Exception:
                        pass
            if self._viz is not None:
                self._viz.set_on_mode_change(_on_viz_mode)
                self._viz.set_on_level_change(_on_viz_level)
                _on_viz_mode("idle")

            try:
                self._viz.start()           # blocks on main thread (macOS req)
            finally:
                self._close_stream()
        else:
            # Fallback: no visualizer, run listener on main thread as before
            with keyboard.Listener(
                on_press=self._on_press,
                on_release=self._on_release,
            ) as lst:
                try:
                    lst.join()
                except KeyboardInterrupt:
                    pass
            self._close_stream()

        self._stop_settings_watch()
        _release_single_instance_lock()
        print("\n  bye 👋")


if __name__ == "__main__":
    try:
        opts = _parse_cli(sys.argv[1:])
    except ValueError as exc:
        print(f"✗ {exc}")
        _print_usage()
        sys.exit(2)

    if opts.get("history_ui_process"):
        from history_ui import run_history_ui

        run_history_ui()
        sys.exit(0)

    if opts.get("probe_mlx"):
        sys.exit(_run_mlx_probe())

    if opts.get("doctor"):
        sys.exit(_run_doctor())

    if opts["help"]:
        _print_usage()
        sys.exit(0)

    if opts["show_history"] or opts["recopy_last"]:
        if not HISTORY_ENABLED:
            print("○ History is disabled in config.")
            sys.exit(0)

        try:
            _history_init()
        except Exception as exc:
            print(f"✗ History init failed: {exc}")
            sys.exit(1)

        if opts["show_history"]:
            _history_print(opts["history_limit"])

        if opts["recopy_last"]:
            text = _history_last_text()
            if not text:
                print("○ No successful transcript in history yet.")
            else:
                pyperclip.copy(text)
                preview = text[:72] + ("…" if len(text) > 72 else "")
                print(f"✓ Re-copied last transcript: {preview}")
        sys.exit(0)

    BloopFlow().run()
