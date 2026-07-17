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
# Webview -> main command channel (onboarding wizard buttons).
UI_COMMAND_PATH = os.path.join(STATE_DIR, "ui_command.json")
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
_UI_COMMAND_LOCK = threading.Lock()
# ui_command.json keeps this many recent {seq, command} entries so clicks
# faster than the main process's 0.5s poll aren't dropped.
_UI_COMMAND_PENDING_MAX = 16

# Must stay in sync with ONBOARDING_COMMANDS in bloop.py; the main process
# validates again, this just refuses obvious garbage before it hits disk.
_UI_COMMANDS = frozenset({
    "request_mic",
    "request_ax",
    "open_privacy_mic",
    "open_privacy_ax",
    "choose_model",
    "retry_model_download",
    "finish_onboarding",
    "restart_onboarding",
    "relaunch_app",
})

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
    # Silent latch chunking is unconditional now — no toggle, not even a hidden
    # settings key. Coerce a stored false (an unchecked old toggle) back to True
    # so those users get chunking back. latch_chunk_seconds stays file-tunable.
    out["latch_chunk_mode"] = True
    if isinstance(raw.get("pill_window"), bool):
        out["pill_window"] = raw["pill_window"]

    # Bubbles is the only pill style now. Coerce any stored value (e.g. an old
    # "spectrogram") back to "bubbles" so legacy settings files render bubbles.
    out["pill_style"] = "bubbles"

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
                "onboarding_seq": int(raw.get("onboarding_seq") or 0),
            }
    except Exception:
        pass
    return {"raise_seq": 0, "settings_seq": 0, "onboarding_seq": 0}


def _ui_command_send(name, arg=None):
    """Write {"seq": n, "command": name, "arg": optional, "pending": [...]}
    for the main process to pick up.

    "pending" is a short queue of recent {seq, command, arg} entries, not a
    single slot: the main process polls at 0.5s, and two buttons clicked
    within one poll window must both go through (a single slot silently
    dropped the first click). The seq continues from whatever is on disk so
    the main process (which dedupes by seq) sees every write as new, even
    across helper restarts. Returns the written seq, or 0 when the command
    isn't allowlisted.

    "arg" (e.g. choose_model's repo id) rides along as an opaque string —
    the main process validates it against its own fixed list; here it's only
    length-capped so garbage can't bloat the file.
    """
    name = str(name or "").strip()
    if name not in _UI_COMMANDS:
        return 0
    arg = str(arg or "").strip()[:128]
    with _UI_COMMAND_LOCK:
        seq = 0
        pending = []
        try:
            with open(UI_COMMAND_PATH, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                seq = int(raw.get("seq") or 0)
                prev = raw.get("pending")
                if isinstance(prev, list):
                    pending = [
                        {
                            "seq": int(item.get("seq") or 0),
                            "command": str(item.get("command") or ""),
                            "arg": str(item.get("arg") or ""),
                        }
                        for item in prev
                        if isinstance(item, dict)
                    ]
                elif raw.get("command"):
                    pending = [{
                        "seq": seq,
                        "command": str(raw.get("command")),
                        "arg": str(raw.get("arg") or ""),
                    }]
        except Exception:
            pass
        seq += 1
        pending.append({"seq": seq, "command": name, "arg": arg})
        pending = pending[-_UI_COMMAND_PENDING_MAX:]
        os.makedirs(os.path.dirname(UI_COMMAND_PATH), exist_ok=True)
        tmp = f"{UI_COMMAND_PATH}.tmp.{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(
                    {"seq": seq, "command": name, "arg": arg, "pending": pending},
                    fh,
                )
            os.replace(tmp, UI_COMMAND_PATH)
        except Exception:
            return 0
        finally:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except Exception:
                pass
        return seq


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

    def send_command(self, name, arg=None):
        """Webview -> main command channel (onboarding wizard buttons)."""
        return json.dumps({"seq": _ui_command_send(name, arg)})

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
<title>Blooop</title>
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
  --coral:     #d97757;
  --violet:    #8b7bd8;
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
  padding: 10px 10px 18px;
  overflow-y: auto;
  overflow-x: hidden;
  max-height: 640px;
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
  align-items: flex-start;
  gap: 6px;
  color: var(--fg);
}
.check-row input[type="checkbox"] {
  margin-top: 1px;
  flex: 0 0 auto;
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
.runtime-progress {
  margin-top: 6px;
  height: 4px;
  border-radius: 2px;
  background: var(--border-2);
  overflow: hidden;
  display: none;
}
.runtime-progress.is-active { display: block; }
.runtime-progress-fill {
  height: 100%;
  width: 0%;
  border-radius: 2px;
  background: linear-gradient(90deg, var(--blue), var(--green));
  transition: width 0.4s ease;
}
.runtime-progress.is-indeterminate .runtime-progress-fill {
  width: 30%;
  animation: runtime-progress-slide 1.4s ease-in-out infinite;
}
@keyframes runtime-progress-slide {
  from { margin-left: -30%; }
  to { margin-left: 100%; }
}
.settings-rerun {
  margin-top: 10px;
  background: none;
  border: none;
  padding: 0;
  font: inherit;
  font-size: 11px;
  color: var(--fg-faint);
  cursor: pointer;
}
.settings-rerun:hover { color: var(--blue); text-decoration: underline; }

/* Boot: hide the normal UI until the first onboarding poll decides wizard
   vs. app, so fresh installs never flash the history view for the ~1s the
   js bridge takes to come up. The wizard overlay is deliberately excluded;
   a bounded failsafe in JS reveals the UI even if status never loads. */
body.is-booting > .header,
body.is-booting > .settings,
body.is-booting > .cards { visibility: hidden; }

/* ── onboarding wizard ── */
.onboarding {
  position: fixed;
  inset: 0;
  z-index: 200;
  display: none;
  align-items: center;
  justify-content: center;
  padding: 28px 20px;
  overflow-y: auto;
  background:
    radial-gradient(120% 90% at 85% -10%, rgba(139,123,216,0.16), transparent 60%),
    radial-gradient(100% 80% at 10% 110%, rgba(111,216,255,0.10), transparent 55%),
    var(--bg);
}
.onboarding.is-visible { display: flex; }
.ob-card {
  width: 100%;
  max-width: 340px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 26px 24px 22px;
  box-shadow: 0 18px 50px rgba(0,0,0,0.45);
}
.ob-dots {
  display: flex;
  gap: 7px;
  justify-content: center;
  margin-bottom: 20px;
}
.ob-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--border-2);
  transition: background 0.2s, transform 0.2s;
}
.ob-dot.is-on { background: var(--coral); transform: scale(1.25); }
.ob-dot.is-done { background: var(--violet); }
.ob-step { display: none; }
.ob-step.is-current { display: block; animation: ob-fade 0.25s ease; }
@keyframes ob-fade {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: none; }
}
.ob-kicker {
  font-size: 11px;
  letter-spacing: 1.4px;
  text-transform: uppercase;
  color: var(--violet);
  margin-bottom: 8px;
}
.ob-title {
  font-size: 19px;
  font-weight: 700;
  letter-spacing: -0.3px;
  margin-bottom: 8px;
}
.ob-copy {
  font-size: 13px;
  color: var(--fg-muted);
  line-height: 1.6;
  margin-bottom: 18px;
}
.ob-state {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--fg-muted);
  min-height: 18px;
  margin-bottom: 14px;
}
.ob-state.is-ok { color: var(--green); }
.ob-state.is-bad { color: var(--red); }
.ob-actions { display: flex; flex-direction: column; gap: 8px; }
.ob-btn {
  width: 100%;
  padding: 9px 14px;
  border-radius: 9px;
  border: 1px solid transparent;
  font: inherit;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  background: var(--coral);
  color: #14100d;
  transition: filter 0.12s, border-color 0.12s;
}
.ob-btn:hover { filter: brightness(1.08); }
.ob-btn.ghost {
  background: var(--surface-2);
  color: var(--fg);
  border-color: var(--border-2);
  font-weight: 500;
}
.ob-btn.ghost:hover { border-color: var(--violet); filter: none; }
.ob-link {
  display: block;
  width: 100%;
  margin-top: 12px;
  background: none;
  border: none;
  font: inherit;
  font-size: 11px;
  color: var(--fg-faint);
  cursor: pointer;
  text-align: center;
}
.ob-link:hover { color: var(--fg-muted); text-decoration: underline; }
.ob-hint {
  font-size: 11px;
  color: var(--fg-faint);
  line-height: 1.5;
  margin-top: 12px;
}
.ob-progress-note {
  font-size: 11px;
  color: var(--fg-faint);
  margin: 6px 0 14px;
  min-height: 14px;
}
.ob-model-cards {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 14px;
}
.ob-model-card {
  display: block;
  width: 100%;
  text-align: left;
  background: var(--surface-2);
  border: 1px solid var(--border-2);
  border-radius: 10px;
  padding: 9px 12px;
  font: inherit;
  color: var(--fg);
  cursor: pointer;
  transition: border-color 0.12s, background 0.12s;
}
.ob-model-card:hover { border-color: var(--violet); }
.ob-model-card.is-selected {
  border-color: var(--violet);
  background: var(--surface);
  box-shadow: 0 0 0 1px var(--violet);
}
.ob-model-head {
  display: flex;
  align-items: baseline;
  gap: 7px;
}
.ob-model-name {
  font-size: 13px;
  font-weight: 600;
}
.ob-model-size {
  margin-left: auto;
  font-size: 11px;
  color: var(--fg-faint);
}
.ob-model-badge {
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.6px;
  text-transform: uppercase;
  color: #14100d;
  background: var(--coral);
  border-radius: 99px;
  padding: 1px 7px 2px;
}
.ob-model-desc {
  display: block;
  margin-top: 2px;
  font-size: 11px;
  color: var(--fg-muted);
}
.onboarding .runtime-progress { margin-top: 0; }
.ob-try-card {
  display: none;
  background: var(--surface-2);
  border: 1px solid #1d6a55;
  border-radius: 10px;
  padding: 12px;
  margin-bottom: 14px;
  font-size: 13px;
  color: var(--fg);
  white-space: pre-wrap;
  word-break: break-word;
}
.ob-try-card.is-visible { display: block; }
.ob-state { flex-wrap: wrap; }
.ob-state-link {
  background: none;
  border: none;
  padding: 0;
  font: inherit;
  font-size: 12px;
  color: var(--blue);
  cursor: pointer;
  text-decoration: underline;
}
.ob-state-link:hover { color: var(--fg); }
#ob-try-input {
  width: 100%;
  border: 1px solid var(--border-2);
  border-radius: 9px;
  padding: 9px 10px;
  background: var(--surface-2);
  color: var(--fg);
  font: inherit;
  font-size: 13px;
  margin-bottom: 14px;
}
#ob-try-input::placeholder { color: var(--fg-faint); }

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
<body class="is-booting">

<div class="header">
  <div class="header-left">
    <span class="header-title">Blooop</span>
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
      <label for="s-model">Speech model</label>
      <select id="s-model">
        <option value="mlx-community/whisper-tiny-mlx">Tiny &mdash; fastest, lowest accuracy (71 MB)</option>
        <option value="mlx-community/whisper-small-mlx">Small &mdash; fast, good accuracy (459 MB)</option>
        <option value="mlx-community/whisper-medium-mlx">Medium &mdash; slower, better accuracy (1.4 GB)</option>
        <option value="mlx-community/whisper-large-v3-mlx">Large v3 &mdash; slowest, best accuracy (2.9 GB)</option>
      </select>
    </div>

    <div class="field">
      <label for="s-hotkey">Push-to-talk key</label>
      <select id="s-hotkey">
        <option value="right_cmd">Right Command (&#8984;)</option>
        <option value="right_option">Right Option (&#8997;)</option>
        <option value="right_shift">Right Shift (&#8679;)</option>
      </select>
    </div>

    <div class="field">
      <label for="s-silence">Trim pauses</label>
      <select id="s-silence">
        <option value="off">Off &mdash; keep everything</option>
        <option value="normal">Normal &mdash; recommended</option>
        <option value="aggressive">Aggressive &mdash; cuts long pauses</option>
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
      <label>Recording indicator</label>
      <div class="check-row">
        <input type="checkbox" id="s-pill">
        <span>Show a floating indicator while recording</span>
      </div>
    </div>

    <div class="field full">
      <label for="s-vocab">Custom vocabulary &mdash; one word or phrase per line</label>
      <textarea id="s-vocab" rows="4" spellcheck="false" placeholder="Blooop&#10;Acme Widget"></textarea>
    </div>
  </div>
  <div class="settings-hint">Hotkey and sensitivity changes apply live. Model changes download in the background and apply after relaunch. The indicator setting applies after relaunch.</div>
  <div class="settings-runtime" id="settings-runtime-wrap">
    <span class="runtime-spinner" id="runtime-spinner"></span>
    <span id="settings-runtime"></span>
  </div>
  <div class="runtime-progress" id="runtime-progress">
    <div class="runtime-progress-fill" id="runtime-progress-fill"></div>
  </div>
  <button class="settings-rerun" id="run-setup-again" title="Show the first-run setup wizard again">Run setup again</button>
</div>

<div class="cards" id="cards"></div>

<div class="onboarding" id="onboarding">
  <div class="ob-card">
    <div class="ob-dots" id="ob-dots"></div>

    <div class="ob-step is-current" id="ob-step-welcome">
      <div class="ob-kicker">Welcome to</div>
      <div class="ob-title">Blooop</div>
      <div class="ob-copy">Hold the right-hand &#8984; Command key, speak, then let go &mdash; your words appear wherever your cursor is. Setup takes about a minute: two macOS permissions and a one-time model download.</div>
      <div class="ob-actions">
        <button class="ob-btn" onclick="obSetStep('mic')">Get Started</button>
      </div>
    </div>

    <div class="ob-step" id="ob-step-mic">
      <div class="ob-kicker">Step 1 of 4</div>
      <div class="ob-title">Microphone</div>
      <div class="ob-copy">Blooop listens only while you hold the hotkey. Audio never leaves your Mac &mdash; transcription is fully local.</div>
      <div class="ob-state" id="ob-mic-state"></div>
      <div class="ob-actions">
        <button class="ob-btn" id="ob-mic-request" onclick="obSend('request_mic')">Allow microphone</button>
        <button class="ob-btn ghost" id="ob-mic-settings" onclick="obSend('open_privacy_mic')" style="display:none">Open System Settings</button>
      </div>
      <div class="ob-hint">Blooop re-checks automatically after you grant.</div>
      <button class="ob-link" onclick="obSetStep('ax')">Skip for now</button>
    </div>

    <div class="ob-step" id="ob-step-ax">
      <div class="ob-kicker">Step 2 of 4</div>
      <div class="ob-title">Accessibility</div>
      <div class="ob-copy">macOS asks for its &ldquo;Accessibility&rdquo; permission before an app can respond to a key you press while you&rsquo;re in other apps &mdash; that&rsquo;s how Blooop hears the hotkey and pastes text for you. Blooop never reads your screen or watches what you type.</div>
      <div class="ob-state" id="ob-ax-state"></div>
      <div class="ob-actions">
        <button class="ob-btn" id="ob-ax-request" onclick="obSend('request_ax')">Enable Accessibility</button>
        <button class="ob-btn ghost" id="ob-ax-settings" onclick="obSend('open_privacy_ax')" style="display:none">Open System Settings</button>
        <button class="ob-btn" id="ob-ax-continue" onclick="obSetStep('model')" style="display:none">Continue</button>
        <button class="ob-btn ghost" id="ob-ax-relaunch" onclick="obSend('relaunch_app')" style="display:none">Relaunch Blooop</button>
      </div>
      <div class="ob-hint" id="ob-ax-hint">Blooop re-checks automatically after you grant.</div>
      <button class="ob-link" onclick="obSetStep('model')">Skip for now</button>
    </div>

    <div class="ob-step" id="ob-step-model">
      <div class="ob-kicker">Step 3 of 4</div>
      <div class="ob-title">Speech model</div>
      <div class="ob-copy">Blooop needs a speech-recognition model that runs entirely on your Mac &mdash; downloaded once, then everything works offline and nothing you say ever leaves your computer. Pick one below; you can switch anytime in Settings.</div>
      <div id="ob-model-chooser">
        <div class="ob-model-cards" role="radiogroup" aria-label="Speech model">
          <button class="ob-model-card" role="radio" aria-checked="false" data-model="mlx-community/whisper-tiny-mlx" onclick="obSelectModel(this)">
            <span class="ob-model-head"><span class="ob-model-name">Tiny</span><span class="ob-model-size">71 MB</span></span>
            <span class="ob-model-desc">Fastest, least accurate</span>
          </button>
          <button class="ob-model-card" role="radio" aria-checked="false" data-model="mlx-community/whisper-small-mlx" onclick="obSelectModel(this)">
            <span class="ob-model-head"><span class="ob-model-name">Small</span><span class="ob-model-size">459 MB</span></span>
            <span class="ob-model-desc">Good balance</span>
          </button>
          <button class="ob-model-card is-selected" role="radio" aria-checked="true" data-model="mlx-community/whisper-medium-mlx" onclick="obSelectModel(this)">
            <span class="ob-model-head"><span class="ob-model-name">Medium</span><span class="ob-model-badge">Recommended</span><span class="ob-model-size">1.4 GB</span></span>
            <span class="ob-model-desc">Best accuracy for everyday dictation</span>
          </button>
          <button class="ob-model-card" role="radio" aria-checked="false" data-model="mlx-community/whisper-large-v3-mlx" onclick="obSelectModel(this)">
            <span class="ob-model-head"><span class="ob-model-name">Large v3</span><span class="ob-model-size">2.9 GB</span></span>
            <span class="ob-model-desc">Maximum accuracy, slowest download</span>
          </button>
        </div>
        <div class="ob-actions">
          <button class="ob-btn" id="ob-model-download" onclick="obChooseModel()">Download</button>
        </div>
      </div>
      <div id="ob-model-dl">
        <div class="ob-state" id="ob-model-state"></div>
        <div class="runtime-progress" id="ob-model-progress">
          <div class="runtime-progress-fill" id="ob-model-progress-fill"></div>
        </div>
        <div class="ob-progress-note" id="ob-model-note"></div>
        <div class="ob-actions">
          <button class="ob-btn" id="ob-model-retry" onclick="obSend('retry_model_download')" style="display:none">Retry download</button>
          <button class="ob-btn" id="ob-model-continue" onclick="obSetStep('try')" style="display:none">Continue</button>
        </div>
      </div>
      <button class="ob-link" onclick="obSetStep('try')">Skip for now</button>
    </div>

    <div class="ob-step" id="ob-step-try">
      <div class="ob-kicker">Step 4 of 4</div>
      <div class="ob-title">Try it</div>
      <div class="ob-copy" id="ob-try-copy">Click into the box below &mdash; or any app &mdash; hold right-&#8984;, and say something. Release, and your words paste right at your cursor.</div>
      <input id="ob-try-input" placeholder="Click here, then hold right-&#8984; and speak" spellcheck="false">
      <div class="ob-try-card" id="ob-try-success"><span id="ob-try-text"></span></div>
      <div class="ob-state" id="ob-try-state">Listening for your first take&hellip;</div>
      <div class="ob-actions">
        <button class="ob-btn" id="ob-finish" onclick="obFinish()" style="display:none">Finish</button>
      </div>
      <div class="ob-hint" id="ob-try-stuck" style="display:none">Nothing happening when you hold the key?
        <button class="ob-state-link" onclick="obSend('relaunch_app')">Relaunch Blooop</button> &mdash; macOS sometimes needs it after granting Accessibility.</div>
      <div class="ob-hint">Blooop lives in your menu bar &mdash; look for the little waveform near the clock. You can close this window anytime and reopen it from there.</div>
      <button class="ob-link" onclick="obFinish()">Skip and finish setup</button>
    </div>
  </div>
</div>

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

function setRuntimeStatus(msg, isError=false, busy=false, progress=null) {
  const wrap = document.getElementById('settings-runtime-wrap');
  const el = document.getElementById('settings-runtime');
  const spin = document.getElementById('runtime-spinner');
  if (!el || !wrap || !spin) return;
  el.textContent = msg || '';
  wrap.classList.toggle('is-error', !!isError);
  spin.classList.toggle('is-active', !!busy);
  const bar = document.getElementById('runtime-progress');
  const fill = document.getElementById('runtime-progress-fill');
  if (!bar || !fill) return;
  // progress: null = hidden, -1 = indeterminate slide, 0..100 = percent fill.
  const active = progress !== null && progress !== undefined;
  bar.classList.toggle('is-active', active);
  bar.classList.toggle('is-indeterminate', active && progress < 0);
  fill.style.width = (active && progress >= 0)
    ? `${Math.max(0, Math.min(100, progress))}%` : '';
}

const MODEL_NAMES = {
  'mlx-community/whisper-tiny-mlx': 'Tiny',
  'mlx-community/whisper-small-mlx': 'Small',
  'mlx-community/whisper-medium-mlx': 'Medium',
  'mlx-community/whisper-large-v3-mlx': 'Large v3',
};

function modelName(id) {
  return MODEL_NAMES[id] || id;
}

function summarizeRuntimeStatus(s) {
  if (!s || typeof s !== 'object') return { msg: '', isError: false, busy: false, progress: null };
  const runtime = (s.runtime_model || '').trim();
  const requested = (s.requested_model || runtime).trim();
  const state = (s.model_download_state || 'idle').trim();
  const target = (s.model_download_target || requested || '').trim();
  const error = (s.model_download_error || '').trim();
  // Boot fill: the runtime model itself is downloading — nothing is loaded,
  // so "(using X)" and "relaunch to switch" wording would both be wrong.
  const bootFill = target === runtime;
  const usingNote = bootFill ? '' : ` (using ${modelName(runtime)})`;

  if (!runtime) return { msg: '', isError: false, busy: false, progress: null };
  if (state === 'downloading' && target) {
    const p = s.model_download_progress;
    if (p && typeof p === 'object') {
      const pct = (typeof p.percent === 'number') ? p.percent : -1;
      const mb = `${p.downloaded_mb || 0}/${p.total_mb || 0} MB`;
      if (pct >= 0) {
        return { msg: `Downloading model: ${modelName(target)} — ${pct}% (${mb})${usingNote}`, isError: false, busy: true, progress: pct };
      }
      return { msg: `Downloading model: ${modelName(target)} — ${p.downloaded_mb || 0} MB so far${usingNote}`, isError: false, busy: true, progress: -1 };
    }
    return { msg: `Downloading model: ${modelName(target)}${usingNote}`, isError: false, busy: true, progress: -1 };
  }
  if (state === 'queued' && target) {
    return { msg: `Queued model download: ${modelName(target)}${usingNote}`, isError: false, busy: true, progress: -1 };
  }
  if (state === 'downloaded' && target) {
    if (bootFill) {
      return { msg: `Model downloaded: ${modelName(target)}. Warming up…`, isError: false, busy: true, progress: null };
    }
    return { msg: `Model downloaded: ${modelName(target)}. Relaunch Blooop to switch.`, isError: false, busy: false, progress: null };
  }
  if (state === 'failed') {
    if (error) console.log('model download error:', error);
    if (bootFill) {
      return { msg: 'Model download failed — check your internet. Press the hotkey to retry.', isError: true, busy: false, progress: null };
    }
    return { msg: `Model download failed: ${modelName(target)} — check your internet, then choose the model again to retry.`, isError: true, busy: false, progress: null };
  }
  if (requested && requested !== runtime) {
    return { msg: `Using ${modelName(runtime)}. Next after relaunch: ${modelName(requested)}`, isError: false, busy: false, progress: null };
  }
  return { msg: `Using model: ${modelName(runtime)}`, isError: false, busy: false, progress: null };
}

async function refreshRuntimeStatus() {
  try {
    const raw = await window.pywebview.api.get_runtime_status();
    const info = summarizeRuntimeStatus(JSON.parse(raw || '{}'));
    setRuntimeStatus(info.msg, info.isError, info.busy, info.progress);
  } catch (_) { /* bridge not ready yet */ }
}

/* ── onboarding wizard ── */
const OB_STEPS = ['welcome', 'mic', 'ax', 'model', 'try'];
const HOTKEY_GLYPHS = {
  right_cmd: 'right-\\u2318',
  right_option: 'right-\\u2325',
  right_shift: 'right-\\u21e7',
};
let obStep = 'welcome';
// Set when the model chooser's Download button is clicked: the backend
// takes up to a poll cycle to flip the download state off "idle", and the
// chooser must not flash back during that gap.
let obModelChosen = false;
let obLastStatus = {};
let obTryBaseline = null;      // {row id -> status} snapshot at try-step open
let obTryBaselineLoading = false;
let obTryEnteredAt = null;     // Date.now() when the try step opened
let obAdvanceTimer = null;
// Set when Finish/Skip is clicked: the backend needs up to a poll cycle to
// flip onboarding_active off, and a stale active:true must not obShow(true)
// the wizard back at the Welcome step. Cleared once the backend confirms
// (active goes false) or a restart_onboarding seq bump arrives.
let obFinishSent = false;

function obSend(name, arg) {
  try { window.pywebview.api.send_command(name, arg || ''); } catch (_) {}
}

function obSelectModel(card) {
  document.querySelectorAll('.ob-model-card').forEach((el) => {
    const on = el === card;
    el.classList.toggle('is-selected', on);
    el.setAttribute('aria-checked', on ? 'true' : 'false');
  });
}

function obChooseModel() {
  const sel = document.querySelector('.ob-model-card.is-selected');
  if (!sel) return;
  obModelChosen = true;
  obSend('choose_model', sel.dataset.model);
  // Flip to the progress view immediately; the backend's queued state
  // arrives on the next poll.
  obRenderModelStep(obLastStatus);
}

function obHotkeyGlyph() {
  const sel = document.getElementById('s-hotkey');
  return HOTKEY_GLYPHS[(sel && sel.value) || 'right_cmd'] || HOTKEY_GLYPHS.right_cmd;
}

function obSetStep(step) {
  if (obStep === step) return;
  obStep = step;
  if (obAdvanceTimer) { clearTimeout(obAdvanceTimer); obAdvanceTimer = null; }
  if (step === 'try') { obTryBaseline = null; obTryEnteredAt = Date.now(); obTryCaptureBaseline(); }
  OB_STEPS.forEach((s) => {
    const el = document.getElementById('ob-step-' + s);
    if (el) el.classList.toggle('is-current', s === obStep);
  });
  // Dots track the four numbered steps only — the welcome card is uncounted
  // ("Step N of 4" kickers), so it shows no dots at all.
  const dots = document.getElementById('ob-dots');
  if (dots) {
    const steps = OB_STEPS.slice(1);
    const idx = steps.indexOf(obStep);
    dots.style.display = idx < 0 ? 'none' : '';
    dots.innerHTML = idx < 0 ? '' : steps.map((s, i) =>
      `<span class="ob-dot${i === idx ? ' is-on' : ''}${i < idx ? ' is-done' : ''}"></span>`
    ).join('');
  }
}

function obScheduleAdvance(next, delay) {
  // Brief pause on the fresh checkmark before moving on, once per step.
  if (obAdvanceTimer) return;
  obAdvanceTimer = setTimeout(() => {
    obAdvanceTimer = null;
    obSetStep(next);
  }, delay);
}

function obShow(show) {
  const overlay = document.getElementById('onboarding');
  if (!overlay) return;
  const was = overlay.classList.contains('is-visible');
  overlay.classList.toggle('is-visible', !!show);
  if (show && !was) {
    // Fresh run (boot or "Run setup again"): reset to the welcome step.
    const success = document.getElementById('ob-try-success');
    if (success) success.classList.remove('is-visible');
    const tryState = document.getElementById('ob-try-state');
    if (tryState) {
      tryState.textContent = 'Listening for your first take\\u2026';
      tryState.className = 'ob-state';
    }
    const finish = document.getElementById('ob-finish');
    if (finish) finish.style.display = 'none';
    const tryInput = document.getElementById('ob-try-input');
    if (tryInput) tryInput.value = '';
    const stuck = document.getElementById('ob-try-stuck');
    if (stuck) stuck.style.display = 'none';
    // Model chooser back to its default: Medium preselected, nothing chosen.
    obModelChosen = false;
    const medium = document.querySelector(
      '.ob-model-card[data-model="mlx-community/whisper-medium-mlx"]');
    if (medium) obSelectModel(medium);
    obStep = null;
    obSetStep('welcome');
  }
  if (!show) {
    obTryBaseline = null;
    if (obAdvanceTimer) { clearTimeout(obAdvanceTimer); obAdvanceTimer = null; }
  }
}

function obFinish() {
  obFinishSent = true;
  obSend('finish_onboarding');
  obShow(false);
}

function updateOnboarding(s) {
  obLastStatus = s || {};
  const active = !!(s && s.onboarding_active);
  if (!active) obFinishSent = false;
  if (obFinishSent) { obShow(false); return; }
  obShow(active);
  if (!active) return;

  const mic = s.mic_status || 'unknown';
  const ax = s.ax_status || 'denied';
  const hotkey = !!s.hotkey_ready;

  // Microphone step
  const micState = document.getElementById('ob-mic-state');
  const micReq = document.getElementById('ob-mic-request');
  const micSet = document.getElementById('ob-mic-settings');
  if (mic === 'granted') {
    micState.textContent = '\\u2713 Microphone granted';
    micState.className = 'ob-state is-ok';
    micReq.style.display = 'none';
    micSet.style.display = 'none';
    if (obStep === 'mic') obScheduleAdvance('ax', 900);
  } else if (mic === 'denied') {
    micState.textContent = '\\u2715 Denied \\u2014 turn on Blooop in System Settings \\u2192 Privacy & Security \\u2192 Microphone, then come back.';
    micState.className = 'ob-state is-bad';
    micReq.style.display = 'none';
    micSet.style.display = '';
  } else {
    micState.textContent = 'Not granted yet';
    micState.className = 'ob-state';
    micReq.style.display = '';
    micSet.style.display = 'none';
  }

  // Accessibility step
  const axState = document.getElementById('ob-ax-state');
  const axReq = document.getElementById('ob-ax-request');
  const axSet = document.getElementById('ob-ax-settings');
  const axCont = document.getElementById('ob-ax-continue');
  const axRelaunch = document.getElementById('ob-ax-relaunch');
  const axHint = document.getElementById('ob-ax-hint');
  if (ax === 'granted') {
    axState.textContent = '\\u2713 Accessibility granted';
    axState.className = 'ob-state is-ok';
    axReq.style.display = 'none';
    axSet.style.display = 'none';
    axCont.style.display = '';
    axRelaunch.style.display = hotkey ? 'none' : '';
    axHint.textContent = hotkey
      ? 'All set \\u2014 hit Continue.'
      : "If the hotkey doesn't respond in a few seconds, relaunch \\u2014 macOS sometimes requires it.";
  } else {
    axState.textContent = 'Not granted yet';
    axState.className = 'ob-state';
    axReq.style.display = '';
    axSet.style.display = '';
    axCont.style.display = 'none';
    axRelaunch.style.display = 'none';
    axHint.textContent = 'Blooop re-checks automatically after you grant.';
  }

  // Model step (chooser + download progress; also used by the try-step
  // diagnosis below).
  const modelReady = s.model_ready !== false;
  obRenderModelStep(s);

  // Try-it step copy follows the configured hotkey.
  const glyph = obHotkeyGlyph();
  const tryCopy = document.getElementById('ob-try-copy');
  if (tryCopy) {
    tryCopy.textContent =
      `Click into the box below \\u2014 or any app \\u2014 hold ${glyph}, and say something. Release, and your words paste right at your cursor.`;
  }
  const tryInput = document.getElementById('ob-try-input');
  if (tryInput) {
    tryInput.placeholder = `Click here, then hold ${glyph} and speak`;
  }

  // Try-it step diagnosis: a skipped/denied prerequisite means the hotkey
  // will do nothing — say so and link back, instead of promising to listen.
  if (obStep === 'try') {
    const tryState = document.getElementById('ob-try-state');
    const success = document.getElementById('ob-try-success');
    const stuck = document.getElementById('ob-try-stuck');
    const done = success && success.classList.contains('is-visible');
    if (tryState && !done) {
      const backLink = (label, step) =>
        ` <button class="ob-state-link" onclick="obSetStep('${step}')">${label}</button>`;
      if (mic !== 'granted') {
        tryState.innerHTML = "\\u2715 Microphone isn't granted yet, so Blooop can't hear you."
          + backLink('Go back to the Microphone step', 'mic');
        tryState.className = 'ob-state is-bad';
      } else if (ax !== 'granted' || !hotkey) {
        tryState.innerHTML = "\\u2715 Accessibility isn't granted yet, so the hotkey can't be detected."
          + backLink('Go back to the Accessibility step', 'ax');
        tryState.className = 'ob-state is-bad';
      } else if (!modelReady) {
        const dlIdle = (s.model_download_state || 'idle') === 'idle' && !obModelChosen;
        tryState.innerHTML = (dlIdle
            ? 'No speech model yet \\u2014 pick one to download.'
            : 'Speech model still downloading \\u2014 this step unlocks when it finishes.')
          + backLink(dlIdle ? 'Back to the model step' : 'Back to download progress', 'model');
        tryState.className = 'ob-state';
      } else {
        tryState.textContent = 'Listening for your first take\\u2026';
        tryState.className = 'ob-state';
      }
      // Everything reads ready but no take has landed in ~12s: surface the
      // relaunch escape hatch here, where the dead hotkey is discovered.
      const ready = mic === 'granted' && ax === 'granted' && hotkey && modelReady;
      const stalled = ready && obTryEnteredAt !== null
        && (Date.now() - obTryEnteredAt) > 12000;
      if (stuck) stuck.style.display = stalled ? '' : 'none';
    } else if (stuck && done) {
      stuck.style.display = 'none';
    }
  }
}

function obRenderModelStep(s) {
  const dlState = (s.model_download_state || 'idle');
  const p = s.model_download_progress;
  const mChooser = document.getElementById('ob-model-chooser');
  const mDl = document.getElementById('ob-model-dl');
  const mState = document.getElementById('ob-model-state');
  const mBar = document.getElementById('ob-model-progress');
  const mFill = document.getElementById('ob-model-progress-fill');
  const mNote = document.getElementById('ob-model-note');
  const mRetry = document.getElementById('ob-model-retry');
  const mCont = document.getElementById('ob-model-continue');
  const downloading = dlState === 'downloading' || dlState === 'queued';
  const failed = dlState === 'failed';
  // model_ready is the truth about the RUNTIME model; the download fields
  // are global and may describe a model SWITCH (reachable via "Run setup
  // again"). Ready wins: a stale switch-download state must neither show a
  // phantom "warming up" nor block the wizard behind a failure the runtime
  // model doesn't have.
  const modelReady = s.model_ready !== false;
  // The chooser shows only while no download exists at all: model missing,
  // nothing chosen this session, download state idle. A ready model (resumed
  // wizard, cache already filled) or any in-flight/failed download goes
  // straight to the progress view.
  const chooserVisible = !modelReady && !obModelChosen && dlState === 'idle';
  if (mChooser) mChooser.style.display = chooserVisible ? '' : 'none';
  if (mDl) mDl.style.display = chooserVisible ? 'none' : '';
  if (chooserVisible) return;

  mRetry.style.display = (failed && !modelReady) ? '' : 'none';
  mCont.style.display = modelReady ? '' : 'none';
  mBar.classList.toggle('is-active', !modelReady && downloading);
  if (modelReady) {
    mState.textContent = '\\u2713 Model ready';
    mState.className = 'ob-state is-ok';
    mNote.textContent = '';
  } else if (downloading) {
    const pct = (p && typeof p.percent === 'number') ? p.percent : -1;
    mBar.classList.toggle('is-indeterminate', pct < 0);
    mFill.style.width = pct >= 0 ? `${Math.max(0, Math.min(100, pct))}%` : '';
    mState.textContent = dlState === 'queued' ? 'Preparing download\\u2026' : 'Downloading\\u2026';
    mState.className = 'ob-state';
    mNote.textContent = !p ? ''
      : (pct >= 0 ? `${pct}% \\u2014 ${p.downloaded_mb || 0}/${p.total_mb || 0} MB`
                  : `${p.downloaded_mb || 0} MB so far`);
  } else if (failed) {
    mState.textContent = '\\u2715 Download failed \\u2014 check your internet';
    mState.className = 'ob-state is-bad';
    mNote.textContent = 'Check your connection, then click Retry. You can also finish setup now \\u2014 Blooop retries the download whenever you press the hotkey.';
    if (s.model_download_error) console.log('model download error:', s.model_download_error);
  } else if (dlState === 'downloaded') {
    mState.textContent = '\\u2713 Downloaded \\u2014 warming up\\u2026';
    mState.className = 'ob-state is-ok';
    mNote.textContent = '';
  } else {
    mState.textContent = 'Preparing download\\u2026';
    mState.className = 'ob-state';
    mNote.textContent = '';
  }
  if (obStep === 'model' && modelReady) obScheduleAdvance('try', 900);
}

async function obTryCaptureBaseline() {
  // {id -> status} at try-step open. Latch-mode rows are created at session
  // START and updated in place to ok, so a created_at cutoff missed any take
  // spanning the step open; instead, credit rows that either appear after
  // the snapshot or flip to ok from a non-ok baseline status.
  if (obTryBaselineLoading || obTryBaseline !== null) return;
  obTryBaselineLoading = true;
  try {
    const raw = await window.pywebview.api.get_history();
    const rows = JSON.parse(raw || '[]');
    const base = {};
    for (const r of rows) base[r.id] = r.status;
    if (obStep === 'try') obTryBaseline = base;
  } catch (_) { /* bridge not ready yet; retried from obCheckTryIt */ }
  obTryBaselineLoading = false;
}

async function obCheckTryIt() {
  if (obStep !== 'try') return;
  if (obTryBaseline === null) { await obTryCaptureBaseline(); return; }
  try {
    const raw = await window.pywebview.api.get_history();
    const rows = JSON.parse(raw || '[]');
    for (const r of rows) {
      if (r.status !== 'ok' || !(r.text || '').trim()) continue;
      const before = obTryBaseline[r.id];
      if (before === undefined || before !== 'ok') {
        document.getElementById('ob-try-text').textContent = r.text.trim();
        document.getElementById('ob-try-success').classList.add('is-visible');
        const st = document.getElementById('ob-try-state');
        st.textContent = "\\u2713 That's it \\u2014 you're set.";
        st.className = 'ob-state is-ok';
        document.getElementById('ob-finish').style.display = '';
        break;
      }
    }
  } catch (_) { /* bridge not ready yet */ }
}

function bootReveal() {
  document.body.classList.remove('is-booting');
}
// Failsafe: the page must never stay permanently blank — reveal the normal
// UI even if the runtime-status bridge never comes up.
setTimeout(bootReveal, 2500);

async function pollOnboarding() {
  // Faster (1s) than the 2s data tick so wizard steps advance promptly.
  try {
    const raw = await window.pywebview.api.get_runtime_status();
    updateOnboarding(JSON.parse(raw || '{}'));
    // First wizard-or-app decision made: safe to reveal the UI.
    bootReveal();
  } catch (_) { /* bridge not ready yet */ }
  await obCheckTryIt();
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

function readSettingsForm() {
  // Latch chunking and pill_style are intentionally absent: chunking is
  // unconditional (normalize forces latch_chunk_mode true) and bubbles is the
  // only pill style. The Python save path merges this payload over the stored
  // settings, so a hand-edited latch_chunk_seconds is kept.
  return {
    model: document.getElementById('s-model').value,
    hotkey: document.getElementById('s-hotkey').value,
    silence_trim_preset: document.getElementById('s-silence').value,
    mic_sensitivity: document.getElementById('s-sensitivity').value,
    auto_paste: !!document.getElementById('s-auto-paste').checked,
    pill_window: !!document.getElementById('s-pill').checked,
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
  document.getElementById('s-pill').checked = s.pill_window !== false;
  const vocabEl = document.getElementById('s-vocab');
  // Don't rewrite the textarea mid-typing — the normalized form would yank
  // the cursor. It refreshes on next panel load / focus elsewhere.
  if (vocabEl && document.activeElement !== vocabEl) {
    vocabEl.value = Array.isArray(s.custom_vocab)
      ? s.custom_vocab.join('\\n')
      : String(s.custom_vocab || '');
  }
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
        Nothing here yet. Click into any app, hold ${obHotkeyGlyph()}, and speak \\u2014 release to paste. Every take also lands here.
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
let lastOnboardingSeq = null;
async function pollCommands() {
  try {
    const raw = await window.pywebview.api.get_command_state();
    const cmd = JSON.parse(raw || '{}');
    const r = Number(cmd.raise_seq || 0);
    const s = Number(cmd.settings_seq || 0);
    const o = Number(cmd.onboarding_seq || 0);
    if (lastRaiseSeq === null) lastRaiseSeq = r;
    if (lastSettingsSeq === null) lastSettingsSeq = s;
    if (lastOnboardingSeq === null) lastOnboardingSeq = o;
    if (r > lastRaiseSeq) {
      lastRaiseSeq = r;
      try { window.pywebview.api.activate_window(); } catch (_) {}
      try { window.focus(); } catch (_) {}
    }
    if (s > lastSettingsSeq) {
      lastSettingsSeq = s;
      setSettingsOpen(true);
    }
    if (o > lastOnboardingSeq) {
      lastOnboardingSeq = o;
      // A restart bumps the seq: re-arm the wizard even if a Finish click's
      // suppression flag is still pending (finish + restart can land inside
      // one poll window, so active:false may never be observed).
      obFinishSent = false;
      try { window.pywebview.api.activate_window(); } catch (_) {}
      pollOnboarding();
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
               's-pill', 's-vocab'];
  ids.forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    const evt = (id === 's-vocab') ? 'input' : 'change';
    el.addEventListener(evt, scheduleSettingsSave);
  });

  const rerun = document.getElementById('run-setup-again');
  if (rerun) {
    rerun.addEventListener('click', () => {
      obSend('restart_onboarding');
      setSettingsOpen(false);
    });
  }

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
  await pollOnboarding();
});
setInterval(tick, 2000);
// Commands (raise window / open settings from the menu bar) poll faster than
// the data tick so menu clicks feel immediate instead of up-to-2s laggy.
setInterval(pollCommands, 500);
// The wizard polls runtime status at 1s so permission grants, download
// progress, and the try-it success card advance the steps promptly.
setInterval(pollOnboarding, 1000);
</script>
</body>
</html>"""


# ── Entry point ────────────────────────────────────────────────────────────────

def run_history_ui():
    _set_process_program_name("Blooop")
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
        "Blooop",
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
        _set_process_program_name("Blooop")
        _force_macos_ui_element()
        _set_macos_accessory_app()

    webview.start(_on_start)


if __name__ == "__main__":
    run_history_ui()
