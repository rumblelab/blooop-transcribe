#!/usr/bin/env python3
"""
Bloop Flow — History Viewer
Standalone pywebview window. Launched as a subprocess by bloop.py.
"""

import json
import os
import re
import sqlite3
import sys
import threading
import time


def _app_base_dir():
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass and os.path.isdir(meipass):
            return meipass
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


APP_BASE_DIR = _app_base_dir()

def _state_dir():
    """Match the main app's resolved state dir (passed via BLOOOP_STATE_DIR)."""
    env_dir = os.environ.get("BLOOOP_STATE_DIR")
    if env_dir:
        return os.path.abspath(os.path.expanduser(env_dir))
    return os.path.expanduser("~/.bloop_flow")


STATE_DIR = _state_dir()
DB_PATH    = os.path.join(STATE_DIR, "history.db")
SETTINGS_PATH = os.path.join(STATE_DIR, "settings.json")
RUNTIME_STATUS_PATH = os.path.join(STATE_DIR, "runtime_status.json")
COMMAND_PATH = os.path.join(STATE_DIR, "history_command.json")
SETTINGS_FALLBACK = os.path.join(
    APP_BASE_DIR,
    ".bloop_flow",
    "settings.json",
)
LIMIT      = 40
HIDE_NOISE = True
_NOISE     = {"no_audio", "too_short"}
_PARENT_PID = os.environ.get("BLOOOP_PARENT_PID")
_SETTINGS_LOCK = threading.Lock()
_SETTINGS_ACTIVE_PATH = None

_SETTINGS_DEFAULTS = {
    "model": "mlx-community/whisper-small-mlx",
    "auto_paste": True,
    "latch_chunk_mode": True,
    "latch_chunk_seconds": 10.0,
    "silence_trim_preset": "normal",
    "mic_sensitivity": "normal",
    "hotkey": "right_cmd",
    "pill_window": True,
    "pill_style": "bubbles",
    "custom_vocab": ["Blooop"],
}
_SILENCE_PRESETS = {"off", "normal", "aggressive"}
_MIC_SENSITIVITIES = {"high", "normal", "low"}
_HOTKEYS = {"right_cmd", "right_option", "right_shift"}
_PILL_STYLES = {"bubbles", "spectrogram"}


def _set_process_program_name(name="Blooop History"):
    """Best-effort process name hint for macOS process surfaces."""
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

    try:
        from AppKit import NSProcessInfo

        NSProcessInfo.processInfo().setProcessName_(name)
    except Exception:
        pass


def _set_macos_accessory_app():
    """Best-effort: keep the history helper out of the Dock."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory
        )
    except Exception:
        pass


def _force_macos_ui_element():
    """Stronger Dock-hiding fallback for helper subprocesses."""
    if sys.platform != "darwin":
        return
    try:
        import ctypes

        class ProcessSerialNumber(ctypes.Structure):
            _fields_ = [
                ("highLongOfPSN", ctypes.c_uint32),
                ("lowLongOfPSN", ctypes.c_uint32),
            ]

        app_services = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        get_current = app_services.GetCurrentProcess
        transform = app_services.TransformProcessType
        get_current.argtypes = [ctypes.POINTER(ProcessSerialNumber)]
        get_current.restype = ctypes.c_int32
        transform.argtypes = [ctypes.POINTER(ProcessSerialNumber), ctypes.c_uint32]
        transform.restype = ctypes.c_int32

        psn = ProcessSerialNumber()
        if get_current(ctypes.byref(psn)) == 0:
            # kProcessTransformToUIElementApplication
            transform(ctypes.byref(psn), 4)
    except Exception:
        pass


def _watch_parent_and_exit():
    """Exit helper if the main Blooop process is gone."""
    if not _PARENT_PID:
        return
    try:
        ppid = int(_PARENT_PID)
    except Exception:
        return
    if ppid <= 1:
        return

    while True:
        try:
            os.kill(ppid, 0)
        except ProcessLookupError:
            os._exit(0)
        except PermissionError:
            # If we can't probe, keep running.
            pass
        except Exception:
            pass
        time.sleep(2.0)


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _list_rows():
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT id, created_at, status,
                       duration_sec AS duration,
                       app_bundle, text, error
                FROM history
                ORDER BY id DESC
                LIMIT ?
                """,
                (LIMIT,),
            ).fetchall()
        finally:
            conn.close()

        out = []
        for r in rows:
            if HIDE_NOISE and r["status"] in _NOISE:
                continue
            out.append({
                "id":         r["id"],
                "created_at": r["created_at"],
                "status":     r["status"],
                "duration":   r["duration"],
                "app_bundle": r["app_bundle"],
                "text":       r["text"] or "",
                "error":      r["error"] or "",
            })
        return out
    except Exception:
        return []


def _delete_row(hid):
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        try:
            conn.execute("DELETE FROM history WHERE id = ?", (int(hid),))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _clear_rows():
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        try:
            conn.execute("DELETE FROM history")
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _settings_candidates():
    out = [SETTINGS_PATH]
    if SETTINGS_FALLBACK not in out:
        out.append(SETTINGS_FALLBACK)
    return out


def _normalize_custom_vocab(raw):
    if isinstance(raw, str):
        items = re.split(r"[\n,;]+", raw)
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        return list(_SETTINGS_DEFAULTS["custom_vocab"])

    out = []
    seen = set()
    for item in items:
        if not isinstance(item, str):
            continue
        text = re.sub(r"\s+", " ", item).strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text[:80].rstrip())
        if len(out) >= 64:
            break
    return out or list(_SETTINGS_DEFAULTS["custom_vocab"])


def _settings_normalize(raw):
    out = dict(_SETTINGS_DEFAULTS)
    if not isinstance(raw, dict):
        return out

    model = raw.get("model")
    if isinstance(model, str):
        model = model.strip()
        if model:
            out["model"] = model

    if isinstance(raw.get("auto_paste"), bool):
        out["auto_paste"] = raw["auto_paste"]
    if isinstance(raw.get("latch_chunk_mode"), bool):
        out["latch_chunk_mode"] = raw["latch_chunk_mode"]
    if isinstance(raw.get("pill_window"), bool):
        out["pill_window"] = raw["pill_window"]

    pill_style = raw.get("pill_style")
    if isinstance(pill_style, str) and pill_style in _PILL_STYLES:
        out["pill_style"] = pill_style

    try:
        sec = float(raw.get("latch_chunk_seconds"))
        if sec == sec and sec not in (float("inf"), float("-inf")):
            out["latch_chunk_seconds"] = max(2.0, min(60.0, sec))
    except Exception:
        pass

    silence = raw.get("silence_trim_preset")
    if isinstance(silence, str) and silence in _SILENCE_PRESETS:
        out["silence_trim_preset"] = silence

    mic_sensitivity = raw.get("mic_sensitivity")
    if isinstance(mic_sensitivity, str) and mic_sensitivity in _MIC_SENSITIVITIES:
        out["mic_sensitivity"] = mic_sensitivity

    hotkey = raw.get("hotkey")
    if isinstance(hotkey, str) and hotkey in _HOTKEYS:
        out["hotkey"] = hotkey

    if "custom_vocab" in raw:
        out["custom_vocab"] = _normalize_custom_vocab(raw.get("custom_vocab"))

    return out


def _settings_pick_write_path():
    global _SETTINGS_ACTIVE_PATH
    if _SETTINGS_ACTIVE_PATH:
        return _SETTINGS_ACTIVE_PATH
    for path in _settings_candidates():
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
    for path in _settings_candidates():
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            _SETTINGS_ACTIVE_PATH = path
            return _settings_normalize(raw)
        except Exception:
            continue
    _settings_pick_write_path()
    return dict(_SETTINGS_DEFAULTS)


def _runtime_status_load():
    try:
        with open(RUNTIME_STATUS_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            return raw
    except Exception:
        pass
    return {}


def _command_state_load():
    try:
        with open(COMMAND_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            return {
                "raise_seq": int(raw.get("raise_seq") or 0),
                "settings_seq": int(raw.get("settings_seq") or 0),
            }
    except Exception:
        pass
    return {"raise_seq": 0, "settings_seq": 0}


def _settings_save(new_values):
    with _SETTINGS_LOCK:
        current = _settings_load()
        if isinstance(new_values, dict):
            current.update(new_values)
        out = _settings_normalize(current)
        path = _settings_pick_write_path()

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
        return out


# ── JS API ─────────────────────────────────────────────────────────────────────

class API:
    def get_history(self):
        return json.dumps(_list_rows())

    def get_settings(self):
        return json.dumps(_settings_load())

    def get_runtime_status(self):
        return json.dumps(_runtime_status_load())

    def get_command_state(self):
        return json.dumps(_command_state_load())

    def activate_window(self):
        """Bring this helper app to the front of macOS apps."""
        if sys.platform != "darwin":
            return
        try:
            from AppKit import NSApplication
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        except Exception:
            pass

    def copy_text(self, text):
        try:
            import pyperclip
            pyperclip.copy(text)
        except Exception:
            pass

    def delete_row(self, hid):
        _delete_row(hid)

    def clear_history(self):
        _clear_rows()

    def save_settings(self, raw):
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            payload = {}
        return json.dumps(_settings_save(payload))


# ── HTML ───────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bloop History</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  /* Deep-water palette — matches the bubbles pill, not a terminal. */
  --bg:        #0a121c;
  --surface:   #0f1b29;
  --surface-2: #142334;
  --border:    #1a2c3e;
  --border-2:  #27425a;
  --fg:        #dce9f2;
  --fg-muted:  #7e96a8;
  --fg-faint:  #46586a;
  --green:     #3ad6a5;
  --blue:      #6fd8ff;
  --yellow:    #c9b79b;
  --red:       #ff7e6b;
  --gray:      #5d7488;
}

html { height: 100%; }

body {
  min-height: 100%;
  background: var(--bg);
  color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text",
               "Helvetica Neue", Arial, sans-serif;
  font-size: 13px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}

/* scrollbar */
::-webkit-scrollbar { width: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border-2); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--fg-faint); }

/* ── header ── */
.header {
  position: sticky;
  top: 0;
  z-index: 50;
  background: linear-gradient(180deg, rgba(10,18,28,0.98), rgba(10,18,28,0.9));
  backdrop-filter: blur(6px);
  border-bottom: 1px solid var(--border);
  padding: 12px 12px 10px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.header-left {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.header-right {
  display: flex;
  align-items: center;
  gap: 8px;
}
.header-title {
  font-size: 14px;
  font-weight: 600;
  letter-spacing: -0.2px;
}
.header-count {
  font-size: 11px;
  color: var(--fg-muted);
}

/* ── settings ── */
.settings-toggle {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  border: 1px solid var(--border-2);
  background: var(--surface-2);
  color: var(--fg-muted);
  cursor: pointer;
  font-size: 14px;
  line-height: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: transform 0.14s ease, color 0.12s, border-color 0.12s, background 0.12s;
}
.settings-toggle:hover {
  color: var(--fg);
  border-color: var(--blue);
}
.settings-toggle.is-open {
  background: #10283a;
  color: var(--blue);
  border-color: #2a5a74;
  transform: rotate(20deg);
}
.settings-live {
  min-width: 52px;
  text-align: right;
  font-size: 11px;
  color: var(--fg-faint);
}
.btn-clear {
  height: 28px;
  padding: 0 10px;
  border-radius: 8px;
  border: 1px solid var(--border-2);
  background: var(--surface-2);
  color: var(--fg-muted);
  cursor: pointer;
  font: inherit;
  font-size: 11px;
  line-height: 1;
  transition: color 0.12s, border-color 0.12s, background 0.12s;
}
.btn-clear:hover { color: var(--fg); }
.btn-clear.is-armed {
  color: var(--red);
  border-color: #6e3a2c;
  background: #2d1712;
}

.settings {
  margin: 8px 8px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
  padding: 10px;
  overflow: hidden;
  max-height: 560px;
  opacity: 1;
  transform: translateY(0);
  transition: max-height 0.2s ease, opacity 0.16s ease, transform 0.16s ease, margin 0.16s ease, padding 0.16s ease;
}
.settings.is-collapsed {
  max-height: 0;
  opacity: 0;
  transform: translateY(-4px);
  margin-top: 0;
  margin-bottom: 0;
  padding-top: 0;
  padding-bottom: 0;
  border-width: 0;
}
.settings-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 8px;
}
.settings-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--fg);
}
.settings-status {
  font-size: 11px;
  color: var(--fg-muted);
}
.settings-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 9px;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.field.full { grid-column: 1 / span 2; }
.field label {
  font-size: 11px;
  color: var(--fg-muted);
}
.field input[type="number"],
.field textarea,
.field select {
  width: 100%;
  border: 1px solid var(--border-2);
  border-radius: 6px;
  padding: 6px 8px;
  background: var(--surface-2);
  color: var(--fg);
  font: inherit;
}
.field textarea {
  resize: vertical;
  min-height: 64px;
  font-size: 12px;
  line-height: 1.5;
}
.field select option { color: var(--fg); }
.field input[type="checkbox"] {
  width: 14px;
  height: 14px;
}
.check-row {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--fg);
}
.settings-hint {
  margin-top: 8px;
  font-size: 11px;
  color: var(--fg-muted);
}
.settings-runtime {
  margin-top: 6px;
  font-size: 11px;
  color: var(--fg-muted);
  display: flex;
  align-items: center;
  gap: 6px;
  min-height: 15px;
}
.settings-runtime.is-error {
  color: var(--red);
}
.runtime-spinner {
  width: 11px;
  height: 11px;
  border: 2px solid var(--border-2);
  border-top-color: var(--blue);
  border-radius: 50%;
  opacity: 0;
  flex: 0 0 auto;
  animation: runtime-spin 0.8s linear infinite;
}
.runtime-spinner.is-active {
  opacity: 1;
}
@keyframes runtime-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

/* ── card list ── */
.cards {
  padding: 0 8px 60px;
  display: flex;
  flex-direction: column;
  gap: 5px;
}

/* ── card shell ── */
.card {
  display: flex;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 11px;
  overflow: hidden;
  transition: border-color 0.18s;
}
.card:hover { border-color: var(--border-2); }
.card[data-st="recording"] .status-dot {
  animation: dotpulse 1.6s ease-in-out infinite;
}
@keyframes dotpulse {
  0%, 100% { transform: scale(0.85); opacity: 0.6; }
  50% { transform: scale(1.2); opacity: 1; }
}

/* left accent strip */
.card-accent {
  width: 3px;
  flex-shrink: 0;
}

/* card content */
.card-body {
  flex: 1;
  min-width: 0;
  padding: 10px 12px 0;
}

/* ── meta row ── */
.card-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 7px;
  gap: 8px;
}
.status-badge {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.1px;
  flex-shrink: 0;
}
.status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}
.card-chips {
  font-size: 11px;
  color: var(--fg-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ── body text ── */
.card-text {
  font-size: 13px;
  color: var(--fg);
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.55;
}
.card-text.collapsed {
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.card-text.is-muted {
  color: var(--fg-muted);
  font-style: italic;
}
.card-text.is-error { color: var(--red); }

/* show more link */
.show-toggle {
  display: inline-block;
  color: var(--fg-muted);
  font-size: 11px;
  margin-top: 5px;
  cursor: pointer;
  user-select: none;
  transition: color 0.1s;
}
.show-toggle:hover { color: var(--fg); }

/* ── separator ── */
.card-sep {
  height: 1px;
  background: var(--border);
  margin: 9px -12px 0;
}

/* ── actions ── */
.card-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 5px 0 7px;
}
.actions-left { display: flex; align-items: center; gap: 6px; }

.btn {
  font-family: inherit;
  font-size: 11px;
  border-radius: 5px;
  border: none;
  cursor: pointer;
  padding: 3px 10px;
  transition: background 0.1s, color 0.1s, border-color 0.1s;
  line-height: 1.6;
}
.btn-copy {
  background: var(--surface-2);
  color: var(--fg);
  border: 1px solid var(--border-2);
}
.btn-copy:hover {
  background: var(--border-2);
}
.btn-copy.copied {
  background: #0e3a30;
  color: var(--green);
  border-color: #1d6a55;
  pointer-events: none;
}
.btn-delete {
  background: none;
  color: var(--fg-muted);
  padding: 3px 6px;
}
.btn-delete:hover { color: var(--red); }

/* ── empty state ── */
.empty {
  text-align: center;
  color: var(--fg-muted);
  padding: 60px 24px;
  font-size: 13px;
}
.empty-icon {
  font-size: 28px;
  margin-bottom: 10px;
  opacity: 0.4;
}
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <span class="header-title">Bloop History</span>
    <span class="header-count" id="count"></span>
  </div>
  <div class="header-right">
    <span class="settings-live" id="settings-live"></span>
    <button class="btn-clear" id="clear-all" title="Delete all history rows">Clear all</button>
    <button class="settings-toggle" id="settings-toggle" title="Settings" aria-expanded="false">⚙</button>
  </div>
</div>

<div class="settings is-collapsed" id="settings-panel">
  <div class="settings-head">
    <span class="settings-title">Settings</span>
    <span class="settings-status" id="settings-status"></span>
  </div>
  <div class="settings-grid">
    <div class="field full">
      <label for="s-model">Model</label>
      <select id="s-model">
        <option value="mlx-community/whisper-tiny-mlx">Tiny - fastest, lowest accuracy</option>
        <option value="mlx-community/whisper-small-mlx">Small - fast, good accuracy</option>
        <option value="mlx-community/whisper-medium-mlx">Medium - slower, better accuracy</option>
        <option value="mlx-community/whisper-large-v3-mlx">Large v3 - slowest, best accuracy</option>
      </select>
    </div>

    <div class="field">
      <label for="s-hotkey">Push-to-talk key</label>
      <select id="s-hotkey">
        <option value="right_cmd">Right Cmd</option>
        <option value="right_option">Right Option</option>
        <option value="right_shift">Right Shift</option>
      </select>
    </div>

    <div class="field">
      <label for="s-silence">Silence trim</label>
      <select id="s-silence">
        <option value="off">Off</option>
        <option value="normal">Normal</option>
        <option value="aggressive">Aggressive</option>
      </select>
    </div>

    <div class="field">
      <label for="s-sensitivity">Mic sensitivity</label>
      <select id="s-sensitivity">
        <option value="high">High (picks up quiet speech)</option>
        <option value="normal">Normal</option>
        <option value="low">Low (noisy rooms)</option>
      </select>
    </div>

    <div class="field">
      <label>Auto-paste</label>
      <div class="check-row">
        <input type="checkbox" id="s-auto-paste">
        <span>Paste after transcript</span>
      </div>
    </div>

    <div class="field">
      <label>Chunking (latch mode)</label>
      <div class="check-row">
        <input type="checkbox" id="s-latch-mode">
        <span>Enable chunking</span>
      </div>
    </div>

    <div class="field full">
      <label for="s-chunk-sec">Chunk seconds</label>
      <input id="s-chunk-sec" type="number" min="2" max="60" step="1" value="10">
    </div>

    <div class="field">
      <label>Recording pill</label>
      <div class="check-row">
        <input type="checkbox" id="s-pill">
        <span>Show pill (relaunch to apply)</span>
      </div>
    </div>

    <div class="field">
      <label for="s-pillstyle">Pill style (relaunch to apply)</label>
      <select id="s-pillstyle">
        <option value="bubbles">Bubbles &mdash; the namesake</option>
        <option value="spectrogram">Spectrogram &mdash; NOAA trace</option>
      </select>
    </div>

    <div class="field full">
      <label for="s-vocab">Custom vocabulary &mdash; one word or phrase per line</label>
      <textarea id="s-vocab" rows="4" spellcheck="false" placeholder="Blooop&#10;Acme Widget"></textarea>
    </div>
  </div>
  <div class="settings-hint">Hotkey changes apply live. Model changes download in background and apply after relaunch.</div>
  <div class="settings-runtime" id="settings-runtime-wrap">
    <span class="runtime-spinner" id="runtime-spinner"></span>
    <span id="settings-runtime"></span>
  </div>
</div>

<div class="cards" id="cards"></div>

<script>
const COLORS = {
  ok:        '#3ad6a5',
  recording: '#6fd8ff',
  no_speech: '#c9b79b',
  no_audio:  '#5d7488',
  too_short: '#5d7488',
  error:     '#ff7e6b',
};
const LABELS = {
  ok:        'ok',
  recording: 'recording',
  no_speech: 'no speech',
  no_audio:  'no audio',
  too_short: 'too short',
  error:     'error',
};

const expanded = new Set();
let settingsLoaded = false;
let settingsSaveTimer = null;

function setSettingsStatus(msg, isError=false) {
  const el = document.getElementById('settings-status');
  const live = document.getElementById('settings-live');
  if (!el) return;
  el.textContent = msg || '';
  if (live) live.textContent = msg || '';
  el.style.color = isError ? 'var(--red)' : 'var(--fg-muted)';
  if (live) live.style.color = isError ? 'var(--red)' : 'var(--fg-faint)';
}

function setRuntimeStatus(msg, isError=false, busy=false) {
  const wrap = document.getElementById('settings-runtime-wrap');
  const el = document.getElementById('settings-runtime');
  const spin = document.getElementById('runtime-spinner');
  if (!el || !wrap || !spin) return;
  el.textContent = msg || '';
  wrap.classList.toggle('is-error', !!isError);
  spin.classList.toggle('is-active', !!busy);
}

function summarizeRuntimeStatus(s) {
  if (!s || typeof s !== 'object') return { msg: '', isError: false, busy: false };
  const runtime = (s.runtime_model || '').trim();
  const requested = (s.requested_model || runtime).trim();
  const state = (s.model_download_state || 'idle').trim();
  const target = (s.model_download_target || requested || '').trim();
  const error = (s.model_download_error || '').trim();

  if (!runtime) return { msg: '', isError: false, busy: false };
  if (state === 'downloading' && target) {
    return { msg: `Downloading model: ${target} (using ${runtime})`, isError: false, busy: true };
  }
  if (state === 'queued' && target) {
    return { msg: `Queued model download: ${target} (using ${runtime})`, isError: false, busy: true };
  }
  if (state === 'downloaded' && target) {
    return { msg: `Model downloaded: ${target}. Relaunch Blooop to switch.`, isError: false, busy: false };
  }
  if (state === 'failed') {
    const detail = error || target || 'unknown error';
    return { msg: `Model download failed: ${detail}`, isError: true, busy: false };
  }
  if (requested && requested !== runtime) {
    return { msg: `Using ${runtime}. Next after relaunch: ${requested}`, isError: false, busy: false };
  }
  return { msg: `Using model: ${runtime}`, isError: false, busy: false };
}

async function refreshRuntimeStatus() {
  try {
    const raw = await window.pywebview.api.get_runtime_status();
    const info = summarizeRuntimeStatus(JSON.parse(raw || '{}'));
    setRuntimeStatus(info.msg, info.isError, info.busy);
  } catch (_) { /* bridge not ready yet */ }
}

function setSettingsOpen(open) {
  const panel = document.getElementById('settings-panel');
  const btn = document.getElementById('settings-toggle');
  if (!panel || !btn) return;
  panel.classList.toggle('is-collapsed', !open);
  btn.classList.toggle('is-open', !!open);
  btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  try {
    localStorage.setItem('bloop_history_settings_open', open ? '1' : '0');
  } catch (_) {}
}

function toggleSettingsPanel() {
  const panel = document.getElementById('settings-panel');
  if (!panel) return;
  setSettingsOpen(panel.classList.contains('is-collapsed'));
}

function restoreSettingsOpenState() {
  let open = false;
  try {
    open = localStorage.getItem('bloop_history_settings_open') === '1';
  } catch (_) {}
  setSettingsOpen(open);
}

function updateChunkControlState() {
  const enabled = document.getElementById('s-latch-mode').checked;
  document.getElementById('s-chunk-sec').disabled = !enabled;
}

function readSettingsForm() {
  let sec = parseFloat(document.getElementById('s-chunk-sec').value || '10');
  if (!Number.isFinite(sec)) sec = 10;
  sec = Math.max(2, Math.min(60, sec));
  return {
    model: document.getElementById('s-model').value,
    hotkey: document.getElementById('s-hotkey').value,
    silence_trim_preset: document.getElementById('s-silence').value,
    mic_sensitivity: document.getElementById('s-sensitivity').value,
    auto_paste: !!document.getElementById('s-auto-paste').checked,
    latch_chunk_mode: !!document.getElementById('s-latch-mode').checked,
    latch_chunk_seconds: sec,
    pill_window: !!document.getElementById('s-pill').checked,
    pill_style: document.getElementById('s-pillstyle').value,
    custom_vocab: document.getElementById('s-vocab').value,
  };
}

function applySettingsForm(s) {
  if (!s) return;
  const modelSel = document.getElementById('s-model');
  if ([...modelSel.options].some(o => o.value === s.model)) {
    modelSel.value = s.model;
  }
  const hotSel = document.getElementById('s-hotkey');
  if ([...hotSel.options].some(o => o.value === s.hotkey)) {
    hotSel.value = s.hotkey;
  }
  const silSel = document.getElementById('s-silence');
  if ([...silSel.options].some(o => o.value === s.silence_trim_preset)) {
    silSel.value = s.silence_trim_preset;
  }
  const sensSel = document.getElementById('s-sensitivity');
  if ([...sensSel.options].some(o => o.value === s.mic_sensitivity)) {
    sensSel.value = s.mic_sensitivity;
  }
  document.getElementById('s-auto-paste').checked = !!s.auto_paste;
  document.getElementById('s-latch-mode').checked = !!s.latch_chunk_mode;
  if (s.latch_chunk_seconds != null) {
    document.getElementById('s-chunk-sec').value = String(Math.round(s.latch_chunk_seconds));
  }
  document.getElementById('s-pill').checked = s.pill_window !== false;
  const styleSel = document.getElementById('s-pillstyle');
  if ([...styleSel.options].some(o => o.value === s.pill_style)) {
    styleSel.value = s.pill_style;
  }
  const vocabEl = document.getElementById('s-vocab');
  // Don't rewrite the textarea mid-typing — the normalized form would yank
  // the cursor. It refreshes on next panel load / focus elsewhere.
  if (vocabEl && document.activeElement !== vocabEl) {
    vocabEl.value = Array.isArray(s.custom_vocab)
      ? s.custom_vocab.join('\\n')
      : String(s.custom_vocab || '');
  }
  updateChunkControlState();
}

async function saveSettingsNow() {
  if (!settingsLoaded) return;
  const payload = readSettingsForm();
  setSettingsStatus('Saving…');
  try {
    const raw = await window.pywebview.api.save_settings(JSON.stringify(payload));
    const saved = JSON.parse(raw);
    applySettingsForm(saved);
    setSettingsStatus('Saved');
    setTimeout(() => setSettingsStatus(''), 1200);
  } catch (_) {
    setSettingsStatus('Save failed', true);
  }
}

function scheduleSettingsSave() {
  if (!settingsLoaded) return;
  updateChunkControlState();
  setSettingsStatus('Pending…');
  if (settingsSaveTimer) clearTimeout(settingsSaveTimer);
  settingsSaveTimer = setTimeout(saveSettingsNow, 220);
}

async function loadSettings() {
  try {
    const raw = await window.pywebview.api.get_settings();
    applySettingsForm(JSON.parse(raw));
    settingsLoaded = true;
    setSettingsStatus('');
  } catch (_) {
    setSettingsStatus('Settings unavailable', true);
  }
}

function fmtTs(iso) {
  const d = new Date(iso.endsWith('Z') ? iso : iso + 'Z');
  const s = Math.floor((Date.now() - d) / 1000);
  if (s < 5)    return 'just now';
  if (s < 60)   return s + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}

function fmtApp(bundle) {
  if (!bundle) return null;
  const parts = bundle.split('.');
  const raw = parts[parts.length - 1];
  return raw.charAt(0).toUpperCase() + raw.slice(1);
}

function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function buildCard(r) {
  const col    = COLORS[r.status]  || '#6e7681';
  const label  = LABELS[r.status]  || r.status;
  const text   = (r.text || '').trim();
  const isExp  = expanded.has(r.id);
  const lines  = text.split('\\n').length;
  const hasMore = lines > 3 || text.length > 220;

  // chips
  const chips = [fmtTs(r.created_at)];
  const app = fmtApp(r.app_bundle);
  if (app) chips.push(app);
  if (r.duration != null) chips.push(r.duration.toFixed(1) + 's');

  // body html
  let bodyHtml = '';
  if (text) {
    const cls = 'card-text' + (!isExp && hasMore ? ' collapsed' : '');
    bodyHtml = `<div class="${cls}" id="t-${r.id}">${esc(text)}</div>`;
    if (hasMore) {
      bodyHtml += `<span class="show-toggle" onclick="toggleExpand(${r.id})">`
               + (isExp ? '&#8593; show less' : '&#8595; show more')
               + '</span>';
    }
  } else if (r.status === 'recording') {
    bodyHtml = '<div class="card-text is-muted">Recording\u2026</div>';
  } else if (r.status === 'no_speech') {
    bodyHtml = '<div class="card-text is-muted">No speech detected.</div>';
  } else if (r.error) {
    bodyHtml = `<div class="card-text is-error">${esc(r.error)}</div>`;
  }

  const copyBtn = text
    ? `<button class="btn btn-copy" id="cp-${r.id}" onclick="copyText(${r.id})">Copy</button>`
    : '';

  return `
<div class="card" id="card-${r.id}" data-st="${r.status}">
  <div class="card-accent" style="background:${col}"></div>
  <div class="card-body">
    <div class="card-meta">
      <div class="status-badge">
        <div class="status-dot" style="background:${col}"></div>
        <span style="color:${col}">${label}</span>
      </div>
      <div class="card-chips">${chips.join('&thinsp;&middot;&thinsp;')}</div>
    </div>
    ${bodyHtml}
    <div class="card-sep"></div>
    <div class="card-actions">
      <div class="actions-left">${copyBtn}</div>
      <button class="btn btn-delete" onclick="deleteRow(${r.id})">Delete</button>
    </div>
  </div>
</div>`;
}

let lastSig = null;

function renderAll(rows) {
  const cards   = document.getElementById('cards');
  const countEl = document.getElementById('count');

  if (!rows || !rows.length) {
    cards.innerHTML = `
      <div class="empty">
        <div class="empty-icon">🎙</div>
        No transcriptions yet.
      </div>`;
    countEl.textContent = '';
    return;
  }

  // Include a 30s time bucket so relative timestamps ("2m ago") keep moving
  // even when the rows themselves haven't changed.
  const sig = rows.map(r => `${r.id}:${r.status}:${r.text.length}`).join('|')
            + '@' + Math.floor(Date.now() / 30000);
  if (sig === lastSig) return;
  lastSig = sig;

  countEl.textContent = rows.length + (rows.length === 1 ? ' item' : ' items');
  cards.innerHTML = rows.map(buildCard).join('');
}

function toggleExpand(id) {
  if (expanded.has(id)) expanded.delete(id); else expanded.add(id);
  lastSig = null;
  refresh();
}

function copyText(id) {
  const el  = document.getElementById('t-' + id);
  const btn = document.getElementById('cp-' + id);
  if (!el) return;
  window.pywebview.api.copy_text(el.innerText).then(() => {
    if (!btn) return;
    btn.textContent = 'Copied!';
    btn.classList.add('copied');
    setTimeout(() => {
      btn.textContent = 'Copy';
      btn.classList.remove('copied');
    }, 1500);
  });
}

function deleteRow(id) {
  const card = document.getElementById('card-' + id);
  if (card) { card.style.opacity = '0.3'; card.style.pointerEvents = 'none'; }
  window.pywebview.api.delete_row(id).then(() => {
    lastSig = null;
    refresh();
  });
}

let clearArmTimer = null;
function setupClearAll() {
  const btn = document.getElementById('clear-all');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    if (!btn.classList.contains('is-armed')) {
      // Two-step confirm: first click arms, second click (within 2.6s) clears.
      btn.classList.add('is-armed');
      btn.textContent = 'Really clear?';
      clearArmTimer = setTimeout(() => {
        btn.classList.remove('is-armed');
        btn.textContent = 'Clear all';
      }, 2600);
      return;
    }
    if (clearArmTimer) clearTimeout(clearArmTimer);
    btn.classList.remove('is-armed');
    btn.textContent = 'Clear all';
    try { await window.pywebview.api.clear_history(); } catch (_) {}
    expanded.clear();
    lastSig = null;
    refresh();
  });
}

async function refresh() {
  try {
    const raw  = await window.pywebview.api.get_history();
    const rows = JSON.parse(raw);
    renderAll(rows);
  } catch (_) { /* bridge not ready yet */ }
}

let lastRaiseSeq = null;
let lastSettingsSeq = null;
async function pollCommands() {
  try {
    const raw = await window.pywebview.api.get_command_state();
    const cmd = JSON.parse(raw || '{}');
    const r = Number(cmd.raise_seq || 0);
    const s = Number(cmd.settings_seq || 0);
    if (lastRaiseSeq === null) lastRaiseSeq = r;
    if (lastSettingsSeq === null) lastSettingsSeq = s;
    if (r > lastRaiseSeq) {
      lastRaiseSeq = r;
      try { window.pywebview.api.activate_window(); } catch (_) {}
      try { window.focus(); } catch (_) {}
    }
    if (s > lastSettingsSeq) {
      lastSettingsSeq = s;
      setSettingsOpen(true);
    }
  } catch (_) { /* bridge not ready yet */ }
}

async function tick() {
  await refresh();
  await refreshRuntimeStatus();
}

function initSettingsBindings() {
  const btn = document.getElementById('settings-toggle');
  if (btn) btn.addEventListener('click', toggleSettingsPanel);

  const ids = ['s-model', 's-hotkey', 's-silence', 's-sensitivity', 's-auto-paste',
               's-latch-mode', 's-chunk-sec', 's-pill', 's-pillstyle', 's-vocab'];
  ids.forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    const evt = (id === 's-chunk-sec' || id === 's-vocab') ? 'input' : 'change';
    el.addEventListener(evt, scheduleSettingsSave);
  });

  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') setSettingsOpen(false);
  });
}

window.addEventListener('pywebviewready', async () => {
  restoreSettingsOpenState();
  initSettingsBindings();
  setupClearAll();
  await loadSettings();
  await tick();
  await pollCommands();
});
setInterval(tick, 2000);
// Commands (raise window / open settings from the menu bar) poll faster than
// the data tick so menu clicks feel immediate instead of up-to-2s laggy.
setInterval(pollCommands, 500);
</script>
</body>
</html>"""


# ── Entry point ────────────────────────────────────────────────────────────────

def run_history_ui():
    _set_process_program_name("Blooop History")
    _force_macos_ui_element()
    _set_macos_accessory_app()
    threading.Thread(target=_watch_parent_and_exit, daemon=True).start()
    try:
        import webview
    except ImportError:
        print("pywebview is not installed. Run:  pip install pywebview", flush=True)
        raise SystemExit(1)

    api    = API()
    window = webview.create_window(
        "Bloop History",
        html=HTML,
        width=460,
        height=720,
        resizable=True,
        min_size=(360, 400),
        background_color="#0a121c",
        js_api=api,
    )

    def _on_start():
        # Some GUI backends can reset activation policy during startup.
        _set_process_program_name("Blooop History")
        _force_macos_ui_element()
        _set_macos_accessory_app()

    webview.start(_on_start)


if __name__ == "__main__":
    run_history_ui()
