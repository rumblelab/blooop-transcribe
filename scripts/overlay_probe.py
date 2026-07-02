#!/usr/bin/env python3
"""Empirically test which overlay-window recipes survive a fullscreen Space.

The pill overlay shows on every normal Space but not on fullscreen
(green-button) Spaces. Rather than guess at level/behavior/policy folklore,
this spawns one window per (activation policy x window kind x level) combo,
drives TextEdit into native fullscreen via AX, and samples
CGWindowListCopyWindowInfo(onScreenOnly) — which reflects the active Space —
to see which combos are actually composited there.

Usage:
  .venv/bin/python scripts/overlay_probe.py            # driver (run this)
  .venv/bin/python scripts/overlay_probe.py spawn X    # internal window host
"""
import json
import os
import subprocess
import sys
import time

CONFIGS = [
    ("window_lvl1000", "window", 1000),  # current app recipe (NSScreenSaverWindowLevel)
    ("window_lvl101", "window", 101),    # old recipe (NSPopUpMenuWindowLevel)
    ("window_lvl25", "window", 25),      # NSStatusWindowLevel
    ("panel_lvl25", "panel", 25),        # nonactivating NSPanel — utility-app recipe
    ("panel_lvl101", "panel", 101),
    ("panel_lvl1000", "panel", 1000),
]


def spawn_host(policy):
    from AppKit import (
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSApplicationActivationPolicyRegular,
        NSBackingStoreBuffered,
        NSColor,
        NSPanel,
        NSScreen,
        NSWindow,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorFullScreenAuxiliary,
        NSWindowCollectionBehaviorStationary,
        NSWindowStyleMaskBorderless,
        NSWindowStyleMaskNonactivatingPanel,
    )
    from Foundation import NSDate, NSDefaultRunLoopMode, NSMakeRect, NSRunLoop

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(
        NSApplicationActivationPolicyAccessory
        if policy == "accessory"
        else NSApplicationActivationPolicyRegular
    )
    behavior = (
        NSWindowCollectionBehaviorCanJoinAllSpaces
        | NSWindowCollectionBehaviorFullScreenAuxiliary
        | NSWindowCollectionBehaviorStationary
    )
    frame = NSScreen.mainScreen().frame()
    x_base = frame.size.width - (440 if policy == "accessory" else 220)
    keep, out = [], {}
    for i, (tag, kind, level) in enumerate(CONFIGS):
        rect = NSMakeRect(x_base, frame.size.height - 90 - i * 46, 180, 36)
        if kind == "panel":
            w = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                rect,
                NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
                NSBackingStoreBuffered,
                False,
            )
        else:
            w = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                rect, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False
            )
        w.setLevel_(level)
        w.setCollectionBehavior_(behavior)
        w.setIgnoresMouseEvents_(True)
        w.setOpaque_(True)
        w.setBackgroundColor_(
            NSColor.colorWithSRGBRed_green_blue_alpha_(
                0.15 + 0.12 * i, 0.45, 0.85 - 0.1 * i, 1.0
            )
        )
        w.setReleasedWhenClosed_(False)
        w.orderFrontRegardless()
        keep.append(w)
        out[f"{policy}.{tag}"] = int(w.windowNumber())

    print(json.dumps(out), flush=True)
    end = time.time() + 120
    while time.time() < end:
        NSRunLoop.currentRunLoop().runMode_beforeDate_(
            NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(0.25)
        )


def onscreen_ids():
    import Quartz

    info = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
    )
    return {int(w["kCGWindowNumber"]) for w in (info or [])}


def report(label, mapping):
    ids = onscreen_ids()
    print(f"== {label} ==")
    for tag in sorted(mapping):
        state = "VISIBLE" if mapping[tag] in ids else "hidden"
        print(f"  {tag:<34} {state}")
    return ids


def drive():
    procs, mapping = [], {}
    for policy in ("accessory", "regular"):
        p = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "spawn", policy],
            stdout=subprocess.PIPE,
            text=True,
        )
        line = p.stdout.readline()
        mapping.update(json.loads(line))
        procs.append(p)
    time.sleep(1.5)

    try:
        report("normal Space (baseline)", mapping)

        print("→ driving TextEdit into native fullscreen via AX…")
        fs_script = (
            'tell application "TextEdit"\n'
            "  activate\n"
            "  if (count of documents) = 0 then make new document\n"
            "end tell\n"
            "delay 0.6\n"
            'tell application "System Events" to tell process "TextEdit"\n'
            '  set value of attribute "AXFullScreen" of window 1 to true\n'
            "end tell\n"
        )
        r = subprocess.run(
            ["osascript", "-e", fs_script], capture_output=True, text=True, timeout=30
        )
        if r.returncode != 0:
            print("✗ AX fullscreen automation failed (likely Accessibility/TCC):")
            print("  " + (r.stderr or "").strip().replace("\n", "\n  "))
            print()
            print("MANUAL MODE: green-button-fullscreen any app now.")
            print("Sampling every 5s for 30s…")
            for _ in range(6):
                time.sleep(5)
                report("sample", mapping)
            return

        time.sleep(3.5)  # let the Space transition finish
        report("FULLSCREEN Space", mapping)

        subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to tell process "TextEdit" '
                'to set value of attribute "AXFullScreen" of window 1 to false',
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        time.sleep(2.5)
        subprocess.run(
            ["osascript", "-e", 'tell application "TextEdit" to quit saving no'],
            capture_output=True,
            timeout=10,
        )
    finally:
        for p in procs:
            p.terminate()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "spawn":
        spawn_host(sys.argv[2])
    else:
        drive()
