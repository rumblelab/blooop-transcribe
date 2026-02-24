#!/usr/bin/env python3
"""
bloop_flow  –  local voice transcription for Mac Silicon

Keys
----
  Hold   Right Command       push-to-talk  (release → transcribe & copy)
  Double-tap Right Command   enable latch mode (hands-free)
  Tap    Right Command       stop and unlatch
  Ctrl-C                     quit

Config at the top of this file.
"""

import os
import re
import sys
import signal
import sqlite3
import subprocess
import threading
import tempfile
import time
from datetime import UTC, datetime, timedelta
from collections import deque

import numpy as np
import sounddevice as sd
import soundfile as sf
import pyperclip
from pynput import keyboard
from pynput.keyboard import Controller as KBController, Key

try:
    import tkinter as tk
    from tkinter import font as tkfont
    _TK = True
except ImportError:
    tkfont = None
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


# ── Config ────────────────────────────────────────────────────────────────────

# MLX Whisper model from HuggingFace (downloaded on first use, cached after).
# Faster / smaller  →  mlx-community/whisper-tiny-mlx   (~39 MB)
# Good balance      →  mlx-community/whisper-small-mlx  (~250 MB)  ← current
# Higher accuracy   →  mlx-community/whisper-medium-mlx (~770 MB)
# Best quality      →  mlx-community/whisper-large-v3-mlx (~3 GB)
MODEL = "mlx-community/whisper-small-mlx"

# Push-to-talk: hold right Command (avoids clobbering terminal Ctrl-C).
PTT_KEY = keyboard.Key.cmd_r

# Double-tap window to enter latch mode.
DOUBLE_TAP_MS = 360

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

SAMPLE_RATE = 16_000   # Whisper expects 16 kHz
CHANNELS    = 1

# History is text-only (no audio), pruned automatically.
HISTORY_ENABLED   = True
HISTORY_DB_PATH   = os.path.expanduser("~/.bloop_flow/history.db")
HISTORY_DB_FALLBACK = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    ".bloop_flow",
    "history.db",
)
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

# ─────────────────────────────────────────────────────────────────────────────

_HISTORY_LOCK = threading.Lock()
_HISTORY_ACTIVE_PATH = None


def _import_whisper():
    import mlx_whisper
    return mlx_whisper


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
    raise last_exc if last_exc is not None else OSError("No usable history path")


def _history_path():
    return _HISTORY_ACTIVE_PATH or HISTORY_DB_PATH


def _utc_now_iso():
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            return {
                "help": True,
                "show_history": False,
                "history_limit": history_limit,
                "recopy_last": False,
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
        else:
            raise ValueError(f"Unknown argument: {arg}")
        i += 1

    return {
        "help": False,
        "show_history": show_history,
        "history_limit": history_limit,
        "recopy_last": recopy_last,
    }


def _print_usage():
    print("Usage:")
    print("  bloop.py                   # run push-to-talk")
    print("  bloop.py --history [N]     # show newest history rows")
    print("  bloop.py --recopy-last     # copy last successful transcript")
    print("  bloop.py --help")


# ── Waveform visualizer ───────────────────────────────────────────────────────

class WaveformVisualizer:
    """Small floating pill with 6 rounded bars, shown while recording."""

    BAR_COUNT = 6
    BAR_W     = 8       # thickness of each bar (also = min height when silent)
    BAR_GAP   = 10      # gap between bars
    PAD_X     = 20      # horizontal padding inside pill
    PAD_Y     = 10      # vertical padding inside pill
    H         = 44      # pill height
    BG        = "#0e0e10"
    FPS       = 30

    def __init__(self):
        # Derive width from bar layout
        content_w = self.BAR_COUNT * self.BAR_W + (self.BAR_COUNT - 1) * self.BAR_GAP
        self.W    = content_w + self.PAD_X * 2

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self._canvas_bg = self.BG
        # Try to make window corners transparent so only the rounded pill is
        # visible. Fallback to opaque rectangle if unsupported.
        try:
            self.root.wm_attributes("-transparent", True)
            self.root.configure(bg="systemTransparent")
            self._canvas_bg = "systemTransparent"
        except Exception:
            try:
                self.root.wm_attributes("-transparentcolor", self.BG)
            except Exception:
                try:
                    self.root.attributes("-alpha", 0.93)
                except Exception:
                    pass
                self.root.configure(bg=self.BG)
            else:
                self.root.configure(bg=self.BG)

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x  = (sw - self.W) // 2
        y  = sh - self.H - 72           # close to the Dock
        self.root.geometry(f"{self.W}x{self.H}+{x}+{y}")

        self.canvas = tk.Canvas(
            self.root, width=self.W, height=self.H,
            bg=self._canvas_bg, highlightthickness=0,
        )
        self.canvas.pack()

        self._levels   = deque([0.0] * self.BAR_COUNT, maxlen=self.BAR_COUNT)
        self._smooth   = [0.0] * self.BAR_COUNT
        self._visible  = False
        self._show_req = False
        self._hide_req = False

    # ── Thread-safe API ───────────────────────────────────────────────────────

    def show(self):
        self._show_req = True
        self._hide_req = False

    def hide(self):
        self._hide_req = True
        self._show_req = False

    def push_level(self, rms: float):
        self._levels.append(min(rms, 1.0))

    # ── Main-thread animation loop ────────────────────────────────────────────

    def _tick(self):
        if self._show_req:
            self._show_req = False
            self._visible  = True
            self.root.deiconify()
        if self._hide_req:
            self._hide_req = False
            self._visible  = False
            self.root.withdraw()

        if self._visible:
            self._draw()

        self.root.after(1000 // self.FPS, self._tick)

    def _bar_color(self, s: float) -> str:
        """Dark green → bright green based on amplitude."""
        t = min(s / 0.65, 1.0)
        r = int(0x1a + t * (0x4a - 0x1a))
        g = int(0x3d + t * (0xde - 0x3d))
        b = int(0x2b + t * (0x80 - 0x2b))
        return f"#{r:02x}{g:02x}{b:02x}"

    def _draw(self):
        c   = self.canvas
        c.delete("all")

        # Pill background (corner radius = H/2 → perfect pill)
        r = self.H // 2
        self._rrect(c, 0, 0, self.W, self.H, r, self.BG)

        max_h  = self.H - self.PAD_Y * 2
        cy     = self.H // 2
        # x-center of first bar
        x0     = self.PAD_X + self.BAR_W // 2

        for i, lvl in enumerate(list(self._levels)):
            self._smooth[i] += (lvl - self._smooth[i]) * 0.38
            s = self._smooth[i]

            h     = max(self.BAR_W, int(s * max_h))   # min = BAR_W → tiny dot
            cx    = x0 + i * (self.BAR_W + self.BAR_GAP)
            color = self._bar_color(s) if s > 0.03 else "#252528"

            # create_line with ROUND caps = capsule-shaped bar
            c.create_line(
                cx, cy - h // 2,
                cx, cy + h // 2,
                width=self.BAR_W, fill=color, capstyle=tk.ROUND,
            )

    def _rrect(self, c, x1, y1, x2, y2, r, fill):
        # Compose the pill from two circles + center rect to avoid seam lines
        # that can appear when layering multiple arc segments.
        c.create_oval(x1, y1, x1 + 2 * r, y2, fill=fill, outline="")
        c.create_oval(x2 - 2 * r, y1, x2, y2, fill=fill, outline="")
        c.create_rectangle(x1 + r, y1, x2 - r, y2, fill=fill, outline="")

    def start(self):
        self._tick()
        self.root.mainloop()

    def stop(self):
        try:
            self.root.quit()
        except Exception:
            pass


# ── History panel ──────────────────────────────────────────────────────────────

class HistoryPanel:
    """Launches history_ui.py as a child process (pywebview window)."""

    def __init__(self, _root=None):
        _ui = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history_ui.py")
        self._proc = subprocess.Popen(
            [sys.executable, _ui],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def start(self):
        pass  # process already running from __init__

    def stop(self):
        try:
            if self._proc.poll() is None:
                self._proc.terminate()
                self._proc.wait(timeout=2)
        except Exception:
            pass


# ── Main app ──────────────────────────────────────────────────────────────────

class BloopFlow:
    def __init__(self):
        _set_macos_accessory_app()
        self.recording  = False
        self.latched    = False
        self.frames     = []
        self._lock      = threading.Lock()
        self._stream    = None
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
        self._kb          = KBController()
        self._viz         = WaveformVisualizer() if _TK else None
        self._history_panel = None
        if self._viz and HISTORY_ENABLED and HISTORY_UI_ENABLED:
            try:
                self._history_panel = HistoryPanel(self._viz.root)
            except Exception as exc:
                print(f"⚠ History panel unavailable: {exc}")
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

    # ── Audio ─────────────────────────────────────────────────────────────────

    def _audio_callback(self, indata, n_frames, t, status):
        if self.recording:
            with self._lock:
                self.frames.append(indata.copy())
            if self._viz:
                rms = float(np.sqrt(np.mean(indata ** 2)))
                self._viz.push_level(min(rms * 14, 1.0))

    def _ensure_stream(self):
        if self._stream is None:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="float32",
                callback=self._audio_callback,
            )
            self._stream.start()

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
        timer, self._start_timer = self._start_timer, None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    def _delayed_start_if_held(self):
        self._start_timer = None
        if self._key_held and not self.recording and not self.latched:
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

    def _flush_chunk(self, final=False):
        with self._lock:
            if not self.recording and not final:
                return False
            frames = self.frames[:]
            self.frames = []
            paste_target = self._active_paste_target
        if final:
            paste_target = self._resolve_paste_target(paste_target)
        session_id = self._active_latch_session_id()

        if not frames and not (final and session_id is not None):
            return False

        self._print("⟳ Transcribing…" if final else "⟳ Transcribing chunk…")
        threading.Thread(
            target=self._transcribe,
            args=(frames, paste_target, final, session_id, final),
            daemon=True,
        ).start()
        return True

    def _chunk_worker(self):
        while not self._chunk_stop.wait(LATCH_CHUNK_SECONDS):
            with self._lock:
                if not (self.recording and self.latched):
                    return
            self._flush_chunk(final=False)

    def start_recording(self):
        self._ensure_stream()
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
        hint = "tap right cmd to stop" if self.latched else "release right cmd to transcribe"
        self._print(f"● Recording…  ({hint})")
        self._start_chunk_worker_if_needed()

    def stop_recording(self):
        self._cancel_start_timer()
        self._stop_chunk_worker()
        with self._lock:
            if not self.recording:
                return
            self.recording = False
            frames = self.frames[:]
            self.frames    = []
            paste_target = self._active_paste_target
            self._active_paste_target = None
        paste_target = self._resolve_paste_target(paste_target)
        session_id = self._active_latch_session_id()
        if self._viz:
            self._viz.hide()
        if not frames and session_id is None:
            self._print("○ Stopped.")
            return
        self._print("⟳ Finalizing…" if not frames else "⟳ Transcribing…")
        threading.Thread(
            target=self._transcribe,
            args=(frames, paste_target, True, session_id, True),
            daemon=True,
        ).start()

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

    def _transcribe_audio(self, frames):
        duration = None
        if not frames:
            return "no_audio", "", duration, None

        audio = np.concatenate(frames).flatten()
        duration = len(audio) / SAMPLE_RATE

        if duration < MIN_DURATION:
            return "too_short", "", duration, f"{duration:.2f}s"

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
            tmp = fh.name
        try:
            sf.write(tmp, audio, SAMPLE_RATE)
            w = self._get_whisper()
            result = w.transcribe(tmp, path_or_hf_repo=MODEL)
            text = result["text"].strip()
        except Exception as exc:
            return "error", "", duration, str(exc)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

        if not text:
            return "no_speech", "", duration, None
        return "ok", text, duration, None

    def _transcribe(self, frames, paste_target, auto_paste=True, session_id=None, final_chunk=True):
        with self._tx_lock:
            self._transcribe_locked(
                frames=frames,
                paste_target=paste_target,
                auto_paste=auto_paste,
                session_id=session_id,
                final_chunk=final_chunk,
            )

    def _transcribe_locked(self, frames, paste_target, auto_paste=True, session_id=None, final_chunk=True):
        status, text, duration, error = self._transcribe_audio(frames)
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
                self._print("○ No speech detected.")
                self._record_history("no_speech", duration_sec=duration, app_bundle=paste_target)
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
                error=error if status == "error" else (error if status == "too_short" else None),
            )
            self._print_history_live(
                row_id=row_id,
                status=status,
                text=None,
                duration_sec=effective_duration,
                paste_ok=None,
                error=error if status == "error" else (error if status == "too_short" else None),
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
        if self._key_held:
            return
        self._key_held = True
        now = time.monotonic()
        self._press_time = now
        # Kick off the bundle-ID lookup immediately on key-down so the 110ms
        # start-delay gives it time to complete before recording begins.
        if not (self.latched and self.recording):
            self._active_paste_target = None
            threading.Thread(
                target=self._snapshot_paste_target, daemon=True
            ).start()

        # Double-tap right cmd enters latch mode.
        if (not self.latched and self._last_was_tap and
                (now - self._last_release) <= (DOUBLE_TAP_MS / 1000)):
            self.latched = True
            # Ignore this release so latch doesn't immediately turn off.
            self._ignore_release_once = True
            self._cancel_start_timer()
            if not self.recording:
                self.start_recording()
            else:
                self._print("● Recording…  (latched; tap right cmd to stop)")
            return

        if self.latched:
            if not self.recording:
                self.start_recording()
            else:
                self._print("● Recording…  (latched; tap right cmd to stop)")
            return

        # Non-latched PTT: defer start slightly so quick taps for double-tap
        # latch don't create empty recordings/history rows.
        if PTT_START_DELAY_MS <= 0:
            self.start_recording()
        else:
            self._cancel_start_timer()
            timer = threading.Timer(PTT_START_DELAY_MS / 1000.0, self._delayed_start_if_held)
            timer.daemon = True
            self._start_timer = timer
            timer.start()

    def _handle_ptt_release(self):
        if not self._key_held:
            return
        self._key_held = False
        self._cancel_start_timer()
        now = time.monotonic()
        held = now - self._press_time
        self._last_release = now
        self._last_was_tap = held <= (DOUBLE_TAP_MS / 1000)

        if self.latched:
            if self._ignore_release_once:
                self._ignore_release_once = False
                return
            self.latched = False
            if self.recording:
                self.stop_recording()
            return

        if self.recording:
            self.stop_recording()

    def _is_ptt_key(self, key):
        return key == PTT_KEY

    def _on_press(self, key):
        if self._is_ptt_key(key):
            self._handle_ptt_press()
            return

    def _on_release(self, key):
        if not self._is_ptt_key(key):
            return

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

    def _prewarm(self):
        try:
            self._get_whisper()
            silent = np.zeros(SAMPLE_RATE // 4, dtype="float32")
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
                tmp = fh.name
            sf.write(tmp, silent, SAMPLE_RATE)
            self._whisper.transcribe(tmp, path_or_hf_repo=MODEL)
            os.unlink(tmp)
            self._print("○ Model ready.  Waiting for input…")
        except Exception as exc:
            self._print(f"⚠ Prewarm failed ({exc}) – will load on first use.")

    # ── Run ───────────────────────────────────────────────────────────────────

    def _quit(self):
        self._cancel_start_timer()
        self._stop_chunk_worker()
        if self._history_panel:
            self._history_panel.stop()
        if self._viz:
            self._viz.stop()

    def run(self):
        print("┌─────────────────────────────────────────────────────┐")
        print("│  bloop_flow  –  local whisper on Apple Silicon      │")
        print("├─────────────────────────────────────────────────────┤")
        print(f"│  model    : {MODEL:<40}│")
        print( "│  PTT      : hold Right Cmd  →  release to transcribe  │")
        print( "│  latch    : dbl-tap Right Cmd on, tap to turn off    │")
        chunk_desc = f"{int(LATCH_CHUNK_SECONDS)}s chunks" if LATCH_CHUNK_MODE else "off"
        print(f"│  chunks   : {chunk_desc:<37}│")
        print( "│  paste    : auto-paste after transcription          │")
        print( "│  quit     : Ctrl-C                                  │")
        print("└─────────────────────────────────────────────────────┘")
        print()

        if not _TK:
            print("⚠ tkinter not found – waveform overlay disabled.")
            print("  Install with: brew install python-tk@3.x")
            if HISTORY_ENABLED and HISTORY_UI_ENABLED:
                print("  History UI disabled (tkinter unavailable).")
            print()

        if HISTORY_ENABLED:
            try:
                _history_init()
                print(f"○ History enabled: {_history_path()}")
                print(f"  Retention: max {HISTORY_MAX_ROWS} rows, {HISTORY_MAX_DAYS} days")
                print(f"  Live feed: {'on' if HISTORY_LIVE_FEED else 'off'}")
                if HISTORY_UI_ENABLED:
                    print(f"  UI panel: {'on' if self._history_panel else 'off'}")
            except Exception as exc:
                print(f"⚠ History init failed: {exc}")

        if self._history_panel:
            self._history_panel.start()

        print("⚙ Loading model in background…")
        threading.Thread(target=self._prewarm, daemon=True).start()

        if self._viz:
            # pynput must move to a background thread so tkinter can own main
            def _listen():
                with keyboard.Listener(
                    on_press=self._on_press,
                    on_release=self._on_release,
                ) as lst:
                    lst.join()

            threading.Thread(target=_listen, daemon=True).start()
            signal.signal(signal.SIGINT, lambda s, f: self._quit())

            try:
                self._viz.start()           # blocks on main thread (macOS req)
            finally:
                if self._stream:
                    self._stream.stop()
                    self._stream.close()
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
            if self._stream:
                self._stream.stop()
                self._stream.close()

        print("\n  bye 👋")


if __name__ == "__main__":
    try:
        opts = _parse_cli(sys.argv[1:])
    except ValueError as exc:
        print(f"✗ {exc}")
        _print_usage()
        sys.exit(2)

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
