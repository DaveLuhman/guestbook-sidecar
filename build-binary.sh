#!/usr/bin/env bash
set -euo pipefail

# Build script for creating camera_sidecar binary with PyInstaller
# This script handles the complex dependencies and creates a standalone binary

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

echo "==> Building camera_sidecar binary with PyInstaller..."

# Check if PyInstaller is available via python3 -m
if ! python3 -m PyInstaller --version &> /dev/null; then
    echo "Error: PyInstaller is not installed"
    echo "Install it with: apt-get install python3-pyinstaller"
    exit 1
fi

# Clean previous builds
echo " + Cleaning previous builds..."
rm -rf build/ dist/ *.spec.bak 2>/dev/null || true

# Build using the spec file
echo " + Running PyInstaller..."
python3 -m PyInstaller camera_sidecar.spec

if [[ -f "dist/camera_sidecar" ]]; then
    echo ""
    echo "✓ Build successful!"
    echo "  Binary location: dist/camera_sidecar"
    echo ""
    echo "To test the binary:"
    echo "  ./dist/camera_sidecar"
    echo ""
    echo "Note: The binary may still require system libraries (libcamera, etc.)"
    echo "      Make sure these are installed on the target system."
else
    echo ""
    echo "✗ Build failed - binary not found"
    exit 1
fi
