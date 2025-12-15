# Building Camera Sidecar Binary

This directory contains scripts to build a standalone binary of the camera sidecar service using PyInstaller.

## Prerequisites

```bash
apt-get install python3-pyinstaller
```

## Building

### Option 1: Using the spec file (Recommended)

```bash
cd sidecar
./build-binary.sh
```

Or manually:
```bash
python3 -m PyInstaller camera_sidecar.spec
```

### Option 2: Using command-line flags

If the spec file doesn't work, try this more explicit approach:

```bash
python3 -m PyInstaller \
    --onefile \
    --name camera_sidecar \
    --hidden-import av.bytesource \
    --hidden-import av.buffer \
    --hidden-import av.frame \
    --hidden-import av.audio.frame \
    --hidden-import av.video.frame \
    --hidden-import picamera2.encoders \
    --hidden-import picamera2.encoders.encoder \
    --collect-all av \
    --collect-all picamera2 \
    --collect-all cv2 \
    camera_sidecar.py
```

## Troubleshooting

### ModuleNotFoundError: No module named 'av.bytesource'

This is a common issue with PyInstaller and the `av` (PyAV) module. Try:

1. **Use the spec file** - It includes all necessary hidden imports
2. **Add `--collect-all av`** - This collects all av submodules
3. **Check if av is properly installed**:
   ```bash
   python3 -c "import av; print(av.__file__)"
   ```

### Binary still requires system libraries

The binary may still need system libraries installed:
- `libcamera` (for picamera2)
- `libavcodec`, `libavformat`, etc. (for av/PyAV)
- OpenCV libraries

These are typically installed via:
```bash
apt-get install libcamera-dev libavcodec-dev libavformat-dev libavutil-dev libswscale-dev
```

## Alternative: Use Python Script Instead

**Note:** Since the setup script installs all dependencies via apt-get, you don't actually need a binary. The Python script works fine and is simpler to maintain:

```bash
# Just copy the script
cp camera_sidecar.py /usr/share/guestbook-kiosk/sidecar/
chmod +x /usr/share/guestbook-kiosk/sidecar/camera_sidecar.py
```

The setup script (`appliance-setup/setup-script.sh`) handles this automatically.

## Testing the Binary

After building:

```bash
# Test locally
./dist/camera_sidecar

# Should start on http://127.0.0.1:7313
curl http://127.0.0.1:7313/health
```
