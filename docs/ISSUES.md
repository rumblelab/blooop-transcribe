# Publish Blockers / Issues

Last updated: 2026-06-10

Use this file to track launch/regression issues during the release hardening cycle.

## Open

1. Push-to-talk can appear "stuck after first use" if the key-up / key-down event edge is dropped.
Status: mitigated with more aggressive stale-key recovery in the press / release handlers; needs repeated stress-test across sleep/wake, fullscreen transitions, and Mission Control.

2. Standalone app crash (`SIGABRT`) in `mlx/core.cpython-312-darwin.so` during startup.
Status: mitigated by dispatching standalone transcriptions on the Tk main thread (queued), removing ad-hoc worker-thread MLX calls; 2026-06-10 closed a remaining race where warmup inference (main thread) could overlap a queued job (worker thread) — warmup now holds the transcribe lock. Needs confirmation on a freshly-notarized bundle on a clean Mac that has never run Blooop.

5. Intermittent native crash after long uptime / multiple uses, dying at recording start (log ends between `[hotkey] PRESS` and `● Recording…`; no .ips written).
Status: mitigated 2026-06-10. Root cause: the input stream was never closed when recording stopped, so a CoreAudio stream sat live for hours/days across sleep/wake and device swaps; the next press closed/reopened the stale stream (the known stale-AudioComponentInstance SIGSEGV path). Fixes: `stop_recording` now releases the stream (also turns the orange mic indicator off between uses), stream open failures re-initialize PortAudio and retry once instead of dying, and `faulthandler` now writes thread stacks into `~/Library/Logs/Blooop/standalone.log` so any future native crash leaves evidence. Needs multi-day soak on the rebuilt .app to confirm.

3. Microphone permission can look granted in System Settings but capture is silent, or Blooop may not appear under Microphone.
Status: open; stronger permission diagnostics and a direct audio-probe fallback help separate AVFoundation prompt issues from true silent input. Unlikely to have one single root cause — collect repro traces as they arrive.

4. Overlay pill did not show on fullscreen (green-button maximized) Spaces, despite `NSScreenSaverWindowLevel` + `FullScreenAuxiliary`.
Status: root cause PROVEN 2026-06-10 via `scripts/overlay_probe.py` (spawns every policy × window-kind × level combo, drives TextEdit fullscreen via AX, samples `CGWindowListCopyWindowInfo(onScreenOnly)`). Verdict: a borderless **nonactivating NSPanel** with `CanJoinAllSpaces|FullScreenAuxiliary|Stationary` is composited on fullscreen Spaces at ANY level (25/101/1000), even from a regular Dock app; a plain **NSWindow** (which is all Tk can create) is the combination that fails. Level reasserts and behavior flags were never the issue — the window class is. Fix implemented 2026-06-10: the pill now renders in an in-process nonactivating NSPanel (`_create_pill_panel` + `_BloopPillView`, driven by the existing tick); the Tk overlay remains only as a non-darwin/AppKit-failure fallback. Bonus: real transparency (true rounded capsule, native shadow) and level dropped to PopUpMenu (101) so the pill no longer covers notification banners. Pill redesigned per the index.html lab: `pill_style` setting — "bubbles" (default, 124×30, wordless; coral dot recording / violet thinking) or "spectrogram" (188×32 NOAA-style heat trace). Needs on-device confirmation over a fullscreen Space.

## Resolved (this cycle)

- Webview settings save silently reset `pill_window` to default (the helper's
  normalize dropped keys it didn't know). Fixed by teaching history_ui.py the
  full settings schema; the webview panel now also exposes the pill toggle and
  the custom-vocabulary editor (previously Tk-panel-only).
- History helper subprocess hardcoded `~/.bloop_flow` while the main app could
  resolve a different state dir. The resolved dir is now passed via
  `BLOOOP_STATE_DIR` and honored by the helper; the command file moved into
  the resolved state dir too.
- Menu-bar "Show History / Show Settings…" felt laggy (up to 2s command poll).
  Commands now poll at 500ms; history cards re-render every 30s so relative
  timestamps don't go stale.

- Visualizer stole focus / switched Spaces when recording started from another app.
  Fixed by removing forced `lift()` and running the overlay as a non-activating `help`-style NSPanel.
- History panel did not appear on first launch.
  Fixed by calling `start()` + `show()` at startup.
- Overlay pill chased the cursor and disappeared on Spaces where the history window wasn't present.
  Fixed by pinning the pill to the top-right corner of the primary screen and replacing conflicting Space-behavior flags (`CanJoinAllSpaces` + `MoveToActiveSpace`) with `CanJoinAllSpaces` + `Stationary` + `FullScreenAuxiliary`.
- Overlay pill hid immediately whenever another app was focused.
  Fixed by dropping `hideOnSuspend` from the MacWindowStyle call — Blooop is always suspended while the user transcribes into another app.
- Menu bar status item silently never installed.
  Fixed after discovering PyObjC's `init*`-prefixed selector handling was mis-registering `initWithCallbacks_`. Switched to plain `alloc().init()` + a separate `setCallbacks_:` configuration call. Same class of bug also bit `_renderRecordingImage_` during the live-level pulse work (PyObjC maps underscores between words to selector colons).

## Verification Checklist For Each Issue

- Reproduce on `/Applications/Blooop.app`
- Reproduce on CLI launch (`blooop`)
- Confirm behavior across:
  - normal window
  - maximized window
  - full-screen Space
- Capture result and update status (`open`, `mitigated`, `fixed`)
