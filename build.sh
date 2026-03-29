#!/bin/bash
set -e

SIGN_IDENTITY="Developer ID Application: Ian Mironov (WHY6PAKA5V)"
TEAM_ID="WHY6PAKA5V"
BUNDLE_ID="com.ouroboros.agent"
ENTITLEMENTS="entitlements.plist"
NOTARYTOOL_PROFILE="ouroboros-notarize"

APP_PATH="dist/Ouroboros.app"
DMG_NAME="Ouroboros-$(cat VERSION | tr -d '[:space:]').dmg"
DMG_PATH="dist/$DMG_NAME"

echo "=== Building Ouroboros.app ==="

if [ ! -f "python-standalone/bin/python3" ]; then
    echo "ERROR: python-standalone/ not found."
    echo "Run first:  bash scripts/download_python_standalone.sh"
    exit 1
fi

echo "--- Installing launcher dependencies ---"
pip install -q -r requirements-launcher.txt

echo "--- Installing agent dependencies into python-standalone ---"
python-standalone/bin/pip3 install -q -r requirements.txt

rm -rf build dist

echo "--- Running PyInstaller ---"
python -m PyInstaller Ouroboros.spec --clean --noconfirm

# ── Codesign ──────────────────────────────────────────────────────

echo ""
echo "=== Signing Ouroboros.app ==="

echo "--- Finding and signing all Mach-O binaries ---"
SIGNED=0
find "$APP_PATH" -type f | while read -r f; do
    if file "$f" | grep -q "Mach-O"; then
        codesign -s "$SIGN_IDENTITY" --timestamp --force --options runtime \
            --entitlements "$ENTITLEMENTS" "$f" 2>&1 || true
        SIGNED=$((SIGNED + 1))
    fi
done
echo "Signed embedded binaries"

echo "--- Signing the app bundle ---"
codesign -s "$SIGN_IDENTITY" --timestamp --force --options runtime \
    --entitlements "$ENTITLEMENTS" "$APP_PATH"

echo "--- Verifying signature ---"
codesign -dvv "$APP_PATH"
codesign --verify --strict "$APP_PATH"
echo "Signature OK"

# ── Notarize ──────────────────────────────────────────────────────

echo ""
echo "=== Notarizing ==="

echo "--- Creating ZIP for notarization ---"
ditto -c -k --keepParent "$APP_PATH" dist/Ouroboros-notarize.zip

echo "--- Submitting to Apple (this may take several minutes) ---"
xcrun notarytool submit dist/Ouroboros-notarize.zip \
    --keychain-profile "$NOTARYTOOL_PROFILE" \
    --wait

echo "--- Stapling notarization ticket to app ---"
xcrun stapler staple "$APP_PATH"

rm -f dist/Ouroboros-notarize.zip

# ── DMG ───────────────────────────────────────────────────────────

echo ""
echo "=== Creating DMG ==="
hdiutil create -volname Ouroboros -srcfolder "$APP_PATH" -ov -format UDZO "$DMG_PATH"

codesign -s "$SIGN_IDENTITY" --timestamp "$DMG_PATH"

echo "--- Notarizing DMG ---"
xcrun notarytool submit "$DMG_PATH" \
    --keychain-profile "$NOTARYTOOL_PROFILE" \
    --wait

xcrun stapler staple "$DMG_PATH"

echo ""
echo "=== Done ==="
echo "Signed & notarized app: $APP_PATH"
echo "Signed & notarized DMG: $DMG_PATH"
