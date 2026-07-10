# Publish Blockers / Issues

Last updated: 2026-07-09

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

6. Pill visibility felt intermittent: visible over maximized/fullscreen windows but not on "the desktop view", and occasionally not overlaying at all.
Status: mitigated 2026-07-07, two changes. (a) The panel was hard-pinned to the top-right of `NSScreen.screens()[0]` (the primary display), so on multi-display setups it only ever rendered on one monitor — whether it "overlaid" depended on which screen the user was looking at. `_position_pill_panel` now targets the screen hosting the frontmost (focused) window via `CGWindowListCopyWindowInfo` (pointer's screen as fallback, primary last), re-picked on every show and refreshed every 0.5s while visible, so the pill follows the display the user is working on. (b) `orderFrontRegardless` can't detect the window server silently dropping the panel from the active Space; the tick now asks the server directly (`kCGWindowIsOnscreen` via `_pill_panel_is_composited`), recycles the window order (orderOut → orderFrontRegardless) when dropped, and writes a `pill_not_composited` breadcrumb to issues.log once per visibility episode — so any remaining "it wasn't there" report comes with forensics. Needs on-device confirmation across Space switches and a docked multi-monitor session.
UPDATE 2026-07-09: the breadcrumbs answered — 16 `pill_not_composited` episodes since 2026-07-07 and the pill never came back, so the orderOut/orderFront recycle demonstrably does NOT re-composite a dropped panel. Escalation added: after `PILL_REBUILD_AFTER_DROPS` (2) consecutive failed composited-checks the tick calls `_rebuild_native_panel()`, which creates a fresh NSPanel (new window number re-registers with the window server), swaps it in, then disposes the old one — create-then-swap so a failed create keeps the old panel and the recycle fallback. Capped at `PILL_MAX_REBUILDS_PER_EPISODE` (3) per visibility episode; breadcrumbs `pill_panel_rebuilt` / `pill_rebuild_failed`. Needs on-device confirmation that a rebuild actually restores visibility mid-recording.

7. Rogue transcriptions during silence: latch-mode chunks with no speech pasted fluent text the user never said — observed 2026-07-09 as a near-verbatim echo of the custom-vocab initial prompt ("Use the exact spellings when they match the spoken audio.").
Status: mitigated 2026-07-09, three layers. Root cause: with `silence_trim_preset: off` every silent 10s latch chunk went to the model, and Whisper's built-in no_speech gate keeps a "silent" segment whenever its text decodes confidently (`avg_logprob > logprob_threshold`) — which a prompt echo always does. (a) `_looks_like_prompt_echo` drops output that fuzzy-matches the initial prompt or either fixed half of it (difflib ratio ≥ 0.75 on normalized tokens; echoes are near-verbatim but not exact — "the" for "these"), applied per-segment and whole-text (`prompt_echo_filtered` breadcrumb). (b) `_filter_result_segments` re-screens every returned segment with a stricter combined gate (`no_speech_prob > 0.6` AND `avg_logprob < -0.45`, `WHISPER_SEGMENT_*`), logging drops as `segments_filtered`. (c) The user-facing settings default matters: trimming off + latch is a hallucination machine; the affected machine's `silence_trim_preset` flipped back to `normal` so silent chunks are gated before ever reaching the model. Needs a few days of real latch dictation to confirm no real speech gets eaten (watch `segments_filtered` breadcrumbs for false positives).

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
