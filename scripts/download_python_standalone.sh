#!/bin/bash
set -e

# Downloads python-build-standalone for macOS (arm64 + x86_64)
# Run from repo root: bash scripts/download_python_standalone.sh

RELEASE="20260211"
PY_VERSION="3.10.19"
DEST="python-standalone"

ARCH=$(uname -m)
if [ "$ARCH" = "arm64" ]; then
    PLATFORM="aarch64-apple-darwin"
elif [ "$ARCH" = "x86_64" ]; then
    PLATFORM="x86_64-apple-darwin"
else
    echo "Unsupported architecture: $ARCH"
    exit 1
fi

FILENAME="cpython-${PY_VERSION}+${RELEASE}-${PLATFORM}-install_only_stripped.tar.gz"
URL="https://github.com/astral-sh/python-build-standalone/releases/download/${RELEASE}/${FILENAME}"

echo "=== Downloading Python ${PY_VERSION} for ${PLATFORM} ==="
echo "URL: ${URL}"

rm -rf "$DEST" _python_tmp
mkdir -p _python_tmp

curl -L --progress-bar "$URL" | tar xz -C _python_tmp

# Archive extracts to python/ — rename to python-standalone/
mv _python_tmp/python "$DEST"
rm -rf _python_tmp

echo ""
echo "=== Installing agent dependencies ==="
"${DEST}/bin/pip3" install --quiet -r requirements.txt

echo ""
echo "=== Done ==="
echo "Python: ${DEST}/bin/python3"
"${DEST}/bin/python3" --version
