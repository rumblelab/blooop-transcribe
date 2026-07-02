# PyInstaller spec for Blooop.
# Build via ./build_app.sh (do not run `pyinstaller Blooop.spec` directly —
# the script stages the icon and model cache first).

import os
from PyInstaller.utils.hooks import collect_all

# Packages we want bundled whole (native libs + data + every submodule).
# PyInstaller's static import analysis misses lazy imports in these, so we
# pull them in explicitly instead of debugging one ImportError at a time.
_COLLECT = [
    "mlx_whisper",
    "mlx",
    "huggingface_hub",
    "sounddevice",
    "soundfile",
    "pynput",
    "pyperclip",
    "pywebview",
    "AppKit",
    "Foundation",
    "AVFoundation",
    "Quartz",
    "objc",
    "numpy",
]

datas = [("assets", "assets")]
if os.path.isdir("bundled-models"):
    datas.append(("bundled-models", "bundled-models"))

binaries = []
hiddenimports = ["history_ui"]

for pkg in _COLLECT:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

a = Analysis(
    ["bloop.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Blooop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Blooop",
)

app = BUNDLE(
    coll,
    name="Blooop.app",
    icon="assets/AppIcon.icns",
    bundle_identifier="lol.bloop.blooop",
    info_plist={
        "CFBundleDisplayName": "Blooop",
        "CFBundleName": "Blooop",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
        # Main app: show in the Dock so the user always has a persistent way
        # to find Blooop and bring its history/settings window back.
        "LSUIElement": False,
        "NSMicrophoneUsageDescription":
            "Blooop records audio to transcribe your speech locally on this Mac. Nothing leaves your machine.",
        "NSAppleEventsUsageDescription":
            "Blooop uses System Events to paste transcript text into the app you are using.",
    },
)
