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

echo "Installed to $APP_PATH"
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
