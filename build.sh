#!/bin/bash
set -euo pipefail

REPO_ROOT="$( cd "$(dirname "$0")" && pwd )"
APP_NAME="Fairy Start"
APP_BUNDLE="$REPO_ROOT/$APP_NAME.app"

# ── Prerequisites ────────────────────────────────────────────────────────────
echo "Checking prerequisites..."

# Step 1: Homebrew
if ! command -v brew &>/dev/null; then
    echo "  Homebrew not found — it's required to install Python and gh CLI."
    read -r -p "  Install Homebrew now? [y/N] " answer
    if [[ ! "$answer" =~ ^[Yy]$ ]]; then
        echo "  Install it manually from: https://brew.sh"
        exit 1
    fi
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Add Homebrew to PATH for the rest of this script (Apple Silicon path)
    export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"
fi

# Step 2: Python with working tkinter
PYTHON=""
for candidate in \
    /opt/homebrew/bin/python3.14 \
    /opt/homebrew/bin/python3.13 \
    /opt/homebrew/bin/python3.12 \
    /opt/homebrew/bin/python3.11
do
    if [[ -x "$candidate" ]] && "$candidate" -c "import tkinter" 2>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done

if [[ -z "$PYTHON" ]]; then
    echo "  python-tk not found — installing..."
    brew install python-tk@3.14
    PYTHON=/opt/homebrew/bin/python3.14
fi
echo "  ✓ Python + tkinter: $PYTHON"

# Step 3: gh CLI
if ! command -v gh &>/dev/null; then
    echo "  gh CLI not found — installing..."
    brew install gh
fi
echo "  ✓ gh CLI"

# ── Convert iconset → icns ───────────────────────────────────────────────────
ICONSET="$REPO_ROOT/AppIcon.iconset"
ICNS="$REPO_ROOT/AppIcon.icns"

if [[ ! -f "$ICNS" ]] || [[ "$ICONSET" -nt "$ICNS" ]]; then
    echo "Converting AppIcon.iconset → AppIcon.icns"
    iconutil -c icns "$ICONSET" -o "$ICNS"
fi

# ── Build .app bundle ────────────────────────────────────────────────────────
echo "Building: $APP_BUNDLE"

mkdir -p "$APP_BUNDLE/Contents/MacOS"
mkdir -p "$APP_BUNDLE/Contents/Resources"

# Info.plist
cat > "$APP_BUNDLE/Contents/Info.plist" << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>fairy-start</string>
    <key>CFBundleIdentifier</key>
    <string>com.local.fairystart</string>
    <key>CFBundleName</key>
    <string>Fairy Start</string>
    <key>CFBundleDisplayName</key>
    <string>Fairy Start</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSUIElement</key>
    <false/>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
</dict>
</plist>
EOF

# Icon
cp "$ICNS" "$APP_BUNDLE/Contents/Resources/AppIcon.icns"

# Launcher — bake repo root and python path at build time
cat > "$APP_BUNDLE/Contents/MacOS/fairy-start" << EOF
#!/bin/bash
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:\$PATH"
cd "$REPO_ROOT"
exec "$PYTHON" fairy_start.py
EOF

chmod +x "$APP_BUNDLE/Contents/MacOS/fairy-start"

echo "Built: $APP_NAME.app — drag it to /Applications or your Dock"
