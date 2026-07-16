#!/usr/bin/env bash
# Build /Applications/Blooop.app via PyInstaller.
# Usage: ./build_app.sh [--install] [--notarize] [--release] [--bundle-models]
#   --install   copy the result to /Applications after a successful build
#   --notarize  submit the signed .app to Apple's notary service and staple
#               the ticket. Requires a keychain profile saved beforehand via
#               `xcrun notarytool store-credentials <name>` (App Store Connect
#               API key or Apple ID + team ID + app-specific password).
#               The profile name is read from $BLOOOP_NOTARY_PROFILE or the
#               first line of .blooop_notary_profile. Ad-hoc-signed builds
#               cannot be notarized.
#   --release   convenience: implies --notarize and produces dist/Blooop.zip
#               ready to hand to another Mac (Gatekeeper-approved via the
#               stapled ticket, no first-launch right-click-open needed).
#   --bundle-models  embed bundled-models/ into the .app (~500MB). Default
#               builds ship model-free: the app downloads the Whisper model
#               on demand at first launch instead.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PY="$DIR/.venv/bin/python"
DIST_APP="$DIR/dist/Blooop.app"
INSTALLED_APP="/Applications/Blooop.app"
ICON_SRC_FALLBACK="$INSTALLED_APP/Contents/Resources/AppIcon.icns"
ICON_DST="$DIR/assets/AppIcon.icns"
LOCAL_BIN="$HOME/.local/bin"
BLOOOP_CMD="$LOCAL_BIN/blooop"
BLOOP_COMPAT_CMD="$LOCAL_BIN/bloop"
SHELL_NAME="$(basename "${SHELL:-zsh}")"
if [ "$SHELL_NAME" = "zsh" ]; then
    RC_FILE="$HOME/.zshrc"
elif [ "$SHELL_NAME" = "bash" ]; then
    RC_FILE="$HOME/.bashrc"
else
    RC_FILE="$HOME/.profile"
fi

INSTALL=0
NOTARIZE=0
RELEASE=0
BUNDLE_MODELS=0
for arg in "$@"; do
    case "$arg" in
        --install) INSTALL=1 ;;
        --notarize) NOTARIZE=1 ;;
        --release) NOTARIZE=1; RELEASE=1 ;;
        --bundle-models) BUNDLE_MODELS=1 ;;
        --help|-h)
            sed -n '2,18p' "$0"
            exit 0
            ;;
        *)
            echo "✗ Unknown arg: $arg" >&2
            exit 2
            ;;
    esac
done

if [ ! -x "$VENV_PY" ]; then
    echo "✗ Missing venv Python: $VENV_PY  (run ./setup.sh first)"
    exit 1
fi
if ! "$VENV_PY" -m pip show pyinstaller >/dev/null 2>&1; then
    echo "→ Installing PyInstaller into venv…"
    "$VENV_PY" -m pip install pyinstaller
fi

# Stage the icon: BUNDLE needs assets/AppIcon.icns. If you don't have one
# in the repo, copy it out of the currently-installed bundle.
if [ ! -f "$ICON_DST" ]; then
    if [ -f "$ICON_SRC_FALLBACK" ]; then
        echo "→ Staging AppIcon.icns from $INSTALLED_APP"
        cp "$ICON_SRC_FALLBACK" "$ICON_DST"
    else
        echo "⚠  No icon found at $ICON_DST and no fallback in $INSTALLED_APP."
        echo "    Build will succeed without an icon. To add one:"
        echo "      iconutil -c icns assets/blooop.iconset -o assets/AppIcon.icns"
        echo "    (or drop a prebuilt AppIcon.icns into assets/)"
    fi
fi

# Bundled models are opt-in (--bundle-models). Default builds are model-free
# — the app downloads the Whisper model on demand at first launch — and the
# spec only picks up a local bundled-models/ dir when BLOOOP_BUNDLE_MODELS=1,
# so a stale local copy can't sneak ~500MB into a default build.
if [ "$BUNDLE_MODELS" -eq 1 ]; then
    export BLOOOP_BUNDLE_MODELS=1
    # Stage bundled models from the installed bundle if we don't already have
    # a local copy. 459MB — kept out of git; copied from the old bundle so a
    # fresh build still boots offline.
    if [ ! -d "$DIR/bundled-models" ] && [ -d "$INSTALLED_APP/Contents/Resources/bundled-models" ]; then
        echo "→ Copying bundled-models from $INSTALLED_APP (one-time; ~500MB)"
        cp -R "$INSTALLED_APP/Contents/Resources/bundled-models" "$DIR/bundled-models"
    fi
else
    echo "→ Model-free build (models download on demand; pass --bundle-models to embed)"
fi

echo "→ Cleaning build/ and dist/"
rm -rf "$DIR/build" "$DIR/dist"

echo "→ Running PyInstaller"
cd "$DIR"
"$VENV_PY" -m PyInstaller --noconfirm Blooop.spec

if [ ! -d "$DIST_APP" ]; then
    echo "✗ Build produced no bundle at $DIST_APP" >&2
    exit 1
fi

# Signing: Developer ID if configured (BLOOOP_SIGN_IDENTITY env or
# .blooop_signing file), otherwise ad-hoc.
IDENTITY="${BLOOOP_SIGN_IDENTITY:-}"
if [ -z "$IDENTITY" ] && [ -f "$DIR/.blooop_signing" ]; then
    IDENTITY="$(head -n 1 "$DIR/.blooop_signing" | tr -d '\r\n')"
fi

if [ -n "$IDENTITY" ]; then
    if ! security find-identity -v -p codesigning 2>/dev/null | grep -Fq "$IDENTITY"; then
        echo "⚠  Signing identity not available in the current keychain: $IDENTITY"
        echo "   Falling back to ad-hoc signing for this build."
        IDENTITY=""
    fi
fi

if [ -n "$IDENTITY" ]; then
    echo "→ Signing with: $IDENTITY"
    codesign --force --deep --timestamp \
        --options runtime \
        --entitlements "$DIR/entitlements.plist" \
        --sign "$IDENTITY" \
        "$DIST_APP"
    codesign --verify --verbose "$DIST_APP" 2>&1 | sed 's/^/  /'
else
    echo "→ Ad-hoc signing"
    echo "   Set a Developer ID via: echo 'Developer ID Application: …' > .blooop_signing"
    codesign --force --deep --sign - "$DIST_APP"
fi
codesign -dvvv "$DIST_APP" 2>&1 | grep -E "^(Identifier|Signature|TeamIdentifier|Authority)="

# Notarization: submit the signed .app to Apple, wait for the ticket, staple
# it back onto the bundle so Gatekeeper accepts it offline on any Mac. Only
# runs when --notarize or --release was passed AND the build was signed with
# a real Developer ID (ad-hoc signatures cannot be notarized).
if [ "$NOTARIZE" -eq 1 ]; then
    if [ -z "$IDENTITY" ]; then
        echo "✗ --notarize requires a Developer ID signing identity." >&2
        echo "  Set one via .blooop_signing or BLOOOP_SIGN_IDENTITY." >&2
        exit 3
    fi

    NOTARY_PROFILE="${BLOOOP_NOTARY_PROFILE:-}"
    if [ -z "$NOTARY_PROFILE" ] && [ -f "$DIR/.blooop_notary_profile" ]; then
        NOTARY_PROFILE="$(head -n 1 "$DIR/.blooop_notary_profile" | tr -d '\r\n')"
    fi
    if [ -z "$NOTARY_PROFILE" ]; then
        echo "✗ --notarize needs a keychain-profile name." >&2
        echo "  Create one once with:" >&2
        echo "    xcrun notarytool store-credentials blooop --apple-id <email> \\" >&2
        echo "      --team-id VR82S46UR7 --password <app-specific-password>" >&2
        echo "  then:  echo 'blooop' > .blooop_notary_profile" >&2
        echo "  or export BLOOOP_NOTARY_PROFILE=blooop." >&2
        exit 3
    fi

    # notarytool only accepts .zip / .pkg / .dmg — zip the .app and submit.
    # Use `ditto` (Apple's archiver) rather than plain `zip` so extended
    # attributes, symlinks, and code-signature metadata round-trip correctly.
    NOTARY_ZIP="$DIR/dist/Blooop-notarize.zip"
    rm -f "$NOTARY_ZIP"
    echo "→ Packaging $DIST_APP for notarization"
    /usr/bin/ditto -c -k --keepParent "$DIST_APP" "$NOTARY_ZIP"

    echo "→ Submitting to Apple notary service (profile: $NOTARY_PROFILE)"
    echo "  This typically takes 1–5 minutes."
    if ! xcrun notarytool submit "$NOTARY_ZIP" \
        --keychain-profile "$NOTARY_PROFILE" \
        --wait; then
        echo "✗ Notarization submission failed. Check the log above;" >&2
        echo "  common cause: missing hardened runtime (--options runtime) or" >&2
        echo "  an entitlements key Apple rejects. Pull the full log with:" >&2
        echo "    xcrun notarytool log <submission-id> --keychain-profile $NOTARY_PROFILE" >&2
        exit 4
    fi

    echo "→ Stapling ticket to $DIST_APP"
    xcrun stapler staple "$DIST_APP"
    xcrun stapler validate "$DIST_APP" | sed 's/^/  /'
    rm -f "$NOTARY_ZIP"

    if [ "$RELEASE" -eq 1 ]; then
        # Versioned zip name: every release must be distinguishable from the
        # last, both on disk and in users' Downloads folders.
        VERSION="$(sed -n 's/.*"CFBundleShortVersionString": "\([^"]*\)".*/\1/p' "$DIR/Blooop.spec" | head -n 1)"
        VERSION="${VERSION:-0.0.0}"
        RELEASE_ZIP="$DIR/dist/Blooop-${VERSION}.zip"
        rm -f "$RELEASE_ZIP"
        /usr/bin/ditto -c -k --keepParent "$DIST_APP" "$RELEASE_ZIP"
        echo "→ Release archive: $RELEASE_ZIP"
        du -sh "$RELEASE_ZIP" | awk '{print "  size: " $1}'
    fi
fi

echo
echo "✓ Built: $DIST_APP"
du -sh "$DIST_APP" | awk '{print "  size: " $1}'

if [ "$INSTALL" -eq 1 ]; then
    # macOS doesn't auto-restart a running .app when you overwrite it on
    # disk — any live process keeps running the stale build. Kill it first
    # so the next launch actually uses the new bundle.
    if pgrep -x Blooop >/dev/null 2>&1; then
        echo "→ Quitting running Blooop (stale build still in memory)"
        pkill -x Blooop || true
        for _ in 1 2 3 4 5; do
            pgrep -x Blooop >/dev/null 2>&1 || break
            sleep 0.4
        done
    fi
    if [ -d "$INSTALLED_APP" ]; then
        echo "→ Removing old $INSTALLED_APP"
        rm -rf "$INSTALLED_APP"
    fi
    echo "→ Installing to $INSTALLED_APP"
    cp -R "$DIST_APP" "$INSTALLED_APP"
    # Refresh Launch Services so the next `open` finds the new bundle
    # immediately (guards against -609 kLSServerCommunicationErr).
    /System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister \
        -f "$INSTALLED_APP" >/dev/null 2>&1 || true
    echo "→ Installing launcher commands in $LOCAL_BIN"
    mkdir -p "$LOCAL_BIN"
    cat > "$BLOOOP_CMD" <<EOF
#!/usr/bin/env bash
APP="/Applications/Blooop.app"
APP_BIN="\$APP/Contents/MacOS/Blooop"

if [ ! -d "\$APP" ]; then
  echo "✗ Blooop.app not found at: \$APP"
  echo "  Reinstall with: ./build_app.sh --install"
  exit 1
fi

if [ "\$#" -eq 0 ]; then
  exec open "\$APP"
fi

if [ ! -x "\$APP_BIN" ]; then
  echo "✗ Missing app executable: \$APP_BIN"
  exit 1
fi

exec "\$APP_BIN" "\$@"
EOF
    chmod +x "$BLOOOP_CMD"
    cat > "$BLOOP_COMPAT_CMD" <<EOF
#!/usr/bin/env bash
exec "$BLOOOP_CMD" "\$@"
EOF
    chmod +x "$BLOOP_COMPAT_CMD"
    PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
    if [ -f "$RC_FILE" ]; then
        if ! grep -Fqs "$PATH_LINE" "$RC_FILE"; then
            printf '\n%s\n' "$PATH_LINE" >> "$RC_FILE"
        fi
    else
        printf '%s\n' "$PATH_LINE" > "$RC_FILE"
    fi
    echo "→ Launching Blooop"
    open -n "$INSTALLED_APP" || true
    echo "✓ Installed and launched."
    echo
    echo "Launcher commands:"
    echo "  $BLOOOP_CMD"
    echo "  $BLOOP_COMPAT_CMD"
    echo "If this is your first launcher install, open a new shell or run:"
    echo "  source \"$RC_FILE\""
    echo
    if [ -n "$IDENTITY" ]; then
        echo "First-time setup after moving off ad-hoc signing — grant once, then"
        echo "future rebuilds with the same Developer ID will keep these grants:"
    else
        echo "⚠  macOS TCC resets on every ad-hoc rebuild. Re-grant:"
    fi
    echo "     • System Settings → Privacy & Security → Microphone → enable Blooop"
    echo "     • System Settings → Privacy & Security → Accessibility → enable Blooop"
else
    echo
    echo "Not installed. To install:"
    echo "  ./build_app.sh --install"
    echo "or drag $DIST_APP into /Applications yourself."
fi
