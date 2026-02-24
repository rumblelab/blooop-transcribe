#!/usr/bin/env python3
"""
Bloop Flow — History Viewer
Standalone pywebview window. Launched as a subprocess by bloop.py.
"""

import json
import os
import sqlite3
import sys

DB_PATH    = os.path.expanduser("~/.bloop_flow/history.db")
LIMIT      = 40
HIDE_NOISE = True
_NOISE     = {"no_audio", "too_short"}


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


# ── JS API ─────────────────────────────────────────────────────────────────────

class API:
    def get_history(self):
        return json.dumps(_list_rows())

    def copy_text(self, text):
        try:
            import pyperclip
            pyperclip.copy(text)
        except Exception:
            pass

    def delete_row(self, hid):
        _delete_row(hid)


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
  --bg:        #0d1117;
  --surface:   #161b22;
  --surface-2: #1c2128;
  --border:    #21262d;
  --border-2:  #30363d;
  --fg:        #e6edf3;
  --fg-muted:  #7d8590;
  --fg-faint:  #484f58;
  --green:     #3fb950;
  --blue:      #58a6ff;
  --yellow:    #d29922;
  --red:       #f85149;
  --gray:      #6e7681;
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
  background: var(--bg);
  border-bottom: 1px solid var(--border);
  padding: 13px 16px 11px;
  display: flex;
  align-items: baseline;
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

/* ── card list ── */
.cards {
  padding: 8px 8px 60px;
  display: flex;
  flex-direction: column;
  gap: 5px;
}

/* ── card shell ── */
.card {
  display: flex;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
  transition: border-color 0.12s;
}
.card:hover { border-color: var(--border-2); }

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
  background: #1a3a26;
  color: var(--green);
  border-color: #2a6a3a;
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
  <span class="header-title">Bloop History</span>
  <span class="header-count" id="count"></span>
</div>

<div class="cards" id="cards"></div>

<script>
const COLORS = {
  ok:        '#3fb950',
  recording: '#58a6ff',
  no_speech: '#d29922',
  no_audio:  '#6e7681',
  too_short: '#6e7681',
  error:     '#f85149',
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
<div class="card" id="card-${r.id}">
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

  const sig = rows.map(r => `${r.id}:${r.status}:${r.text.length}`).join('|');
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

async function refresh() {
  try {
    const raw  = await window.pywebview.api.get_history();
    const rows = JSON.parse(raw);
    renderAll(rows);
  } catch (_) { /* bridge not ready yet */ }
}

window.addEventListener('pywebviewready', refresh);
setInterval(refresh, 2000);
</script>
</body>
</html>"""


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _set_macos_accessory_app()
    try:
        import webview
    except ImportError:
        print("pywebview is not installed. Run:  pip install pywebview", flush=True)
        raise SystemExit(1)

    api    = API()
    window = webview.create_window(
        "Bloop History",
        html=HTML,
        width=420,
        height=720,
        resizable=True,
        min_size=(320, 400),
        background_color="#0d1117",
        js_api=api,
    )
    webview.start()
