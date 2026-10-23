#!/usr/bin/env bash
# Build RamCleanup.app — native WKWebView window, custom dock icon.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NATIVE="$ROOT/native"
APP="$ROOT/RamCleanup.app"
ARCH="$(uname -m)"

echo "Generating icon…"
python3 "$NATIVE/make_icon.py"

echo "Compiling native host ($ARCH)…"
TMPBIN="$(mktemp -t ramcleanup)"
xcrun swiftc -O \
  -target "${ARCH}-apple-macosx13.0" \
  -sdk "$(xcrun --show-sdk-path --sdk macosx)" \
  -framework Cocoa \
  -framework WebKit \
  -o "$TMPBIN" \
  "$NATIVE/main.swift"

echo "Assembling ${APP} ..."
rm -rf "${APP}"
mkdir -p "${APP}/Contents/MacOS" "${APP}/Contents/Resources"
cp "$TMPBIN" "${APP}/Contents/MacOS/RamCleanup"
chmod +x "${APP}/Contents/MacOS/RamCleanup"
cp "$NATIVE/AppIcon.icns" "${APP}/Contents/Resources/AppIcon.icns"
printf 'APPL????' > "${APP}/Contents/PkgInfo"

cat > "$APP/Contents/Info.plist" << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleExecutable</key>
	<string>RamCleanup</string>
	<key>CFBundleIconFile</key>
	<string>AppIcon</string>
	<key>CFBundleIdentifier</key>
	<string>com.universallythinking.ramcleanup</string>
	<key>CFBundleName</key>
	<string>RAM Cleanup</string>
	<key>CFBundleDisplayName</key>
	<string>RAM Cleanup</string>
	<key>CFBundlePackageType</key>
	<string>APPL</string>
	<key>CFBundleShortVersionString</key>
	<string>1.1</string>
	<key>CFBundleVersion</key>
	<string>4</string>
	<key>LSMinimumSystemVersion</key>
	<string>13.0</string>
	<key>NSHighResolutionCapable</key>
	<true/>
	<key>NSAppTransportSecurity</key>
	<dict>
		<key>NSAllowsLocalNetworking</key>
		<true/>
		<key>NSAllowsArbitraryLoads</key>
		<true/>
	</dict>
</dict>
</plist>
EOF

codesign --force --deep --sign - "${APP}" 2>/dev/null || true
xattr -cr "${APP}" 2>/dev/null || true
rm -f "$TMPBIN"
rm -rf "$ROOT/Quit RamCleanup.app"

echo "Built: ${APP}"
echo "Open it - mini widget is always on top; Open/double-click expands."
