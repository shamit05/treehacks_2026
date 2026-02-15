#!/bin/bash
# Launch OverlayGuide from a stable app bundle path.
# By default this ONLY launches to preserve macOS permissions.
# Use --rebuild when you intentionally want to rebuild/update the app binary.
# Usage:
#   ./run.sh
#   ./run.sh --rebuild

set -e
cd "$(dirname "$0")"

BINARY=".build/debug/OverlayGuide"
APP_NAME="OverlayGuide.app"
INSTALL_DIR="$HOME/Applications"
APP_PATH="$INSTALL_DIR/$APP_NAME"
CONTENTS="$APP_PATH/Contents"
MACOS="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"
DO_REBUILD=false
SIGNING_IDENTITY="${DEVELOPER_IDENTITY:-}"

if [ "${1:-}" = "--rebuild" ]; then
  DO_REBUILD=true
fi

if [ ! -f "$APP_PATH/Contents/MacOS/OverlayGuide" ]; then
  DO_REBUILD=true
fi

if [ "$DO_REBUILD" = true ]; then
  echo "Building OverlayGuide..."
  swift build
fi

# Keep a stable app bundle path so macOS permissions persist.
mkdir -p "$MACOS"
mkdir -p "$RESOURCES"

# Copy latest built binary into stable app bundle only on rebuild/install.
if [ "$DO_REBUILD" = true ]; then
  cp "$BINARY" "$MACOS/OverlayGuide"
  chmod +x "$MACOS/OverlayGuide"
fi

# Create Info.plist only once (keep bundle identity stable).
if [ ! -f "$CONTENTS/Info.plist" ]; then
cat > "$CONTENTS/Info.plist" << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>OverlayGuide</string>
    <key>CFBundleIdentifier</key>
    <string>com.overlayguide.app</string>
    <key>CFBundleName</key>
    <string>OverlayGuide</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
</dict>
</plist>
EOF
fi

choose_signing_identity() {
  if [ -n "$SIGNING_IDENTITY" ]; then
    echo "$SIGNING_IDENTITY"
    return
  fi
  security find-identity -v -p codesigning 2>/dev/null | /usr/bin/awk -F\" '/Apple Development/ {print $2; exit}'
}

sign_app_if_needed() {
  local identity
  identity="$(choose_signing_identity)"
  if [ -n "$identity" ]; then
    echo "Signing app with identity: $identity"
    codesign --force --deep --sign "$identity" "$APP_PATH"
    echo "Code signature verification:"
    codesign --verify --deep --strict "$APP_PATH"
  else
    echo "No Apple Development signing identity found."
    echo "Falling back to ad-hoc signing (works for local dev)."
    codesign --force --deep --sign - "$APP_PATH"
    echo "Code signature verification:"
    codesign --verify --deep --strict "$APP_PATH"
  fi
}

# Only sign once — on first install. After that, skip signing so
# the CDHash stays the same and macOS permissions persist across rebuilds.
# The binary changes but the signature stays, so macOS may complain once
# about a "damaged" app — just right-click > Open to bypass that one time.
SIGNED_MARKER="$CONTENTS/.signed_once"
if [ "$DO_REBUILD" = true ] && [ ! -f "$SIGNED_MARKER" ]; then
  sign_app_if_needed
  touch "$SIGNED_MARKER"
elif [ "$DO_REBUILD" = true ]; then
  echo "Skipping re-sign to preserve Screen Recording & Accessibility permissions."
fi

echo "Installed to $APP_PATH"
echo "Bundle identifier: com.overlayguide.app"
if [ "$DO_REBUILD" = true ]; then
  echo "Mode: rebuild + launch"
else
  echo "Mode: launch only (no rebuild)"
fi

echo ""
echo "Launching OverlayGuide..."
echo ""
echo "If hotkey (Cmd+Option+O) doesn't work:"
echo "  1. System Settings > Privacy & Security > Accessibility"
echo "  2. Remove OverlayGuide if listed, then click +"
echo "  3. Press Cmd+Shift+G, paste: $INSTALL_DIR"
echo "  4. Select OverlayGuide.app, click Open"
echo "  5. Quit OverlayGuide (Cmd+Q) and run ./run.sh again"
echo ""
echo "NOTE: Use ./run.sh for launch-only to preserve Screen Recording permission."
echo "      Use ./run.sh --rebuild only when you intentionally want new code."
echo ""

# Ensure stale instance is closed before launching the fresh build
pkill -x OverlayGuide >/dev/null 2>&1 || true
open -n "$APP_PATH"
