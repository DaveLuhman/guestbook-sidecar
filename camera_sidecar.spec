# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for camera_sidecar.py

This spec file handles the complex dependencies including:
- picamera2 and its av (PyAV) dependency
- opencv-python (cv2)
- flask
- pyzbar

Usage:
    python3 -m PyInstaller camera_sidecar.spec
"""

import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# Collect all data files and submodules for problematic packages
datas = []

# Collect picamera2 data files
try:
    datas += collect_data_files('picamera2')
except:
    pass

# Collect av (PyAV) data files
try:
    datas += collect_data_files('av')
except:
    pass

# Collect opencv data files
try:
    datas += collect_data_files('cv2')
except:
    pass

# Hidden imports for modules that PyInstaller might miss
hiddenimports = [
    # av (PyAV) submodules
    'av.bytesource',
    'av.buffer',
    'av.frame',
    'av.audio.frame',
    'av.audio',
    'av.video',
    'av.codec',
    'av.format',
    'av.container',
    'av.packet',
    'av.filter',
    'av.subtitles',
    # picamera2 submodules
    'picamera2.encoders',
    'picamera2.encoders.encoder',
    'picamera2.encoders.jpeg_encoder',
    'picamera2.encoders.h264_encoder',
    'picamera2.encoders.mjpeg_encoder',
    'picamera2.previews',
    'picamera2.previews.qt',
    'picamera2.previews.null',
    'picamera2.previews.opengl',
    'picamera2.previews.drm',
    # OpenCV
    'cv2',
    'cv2.cv2',
    # Flask
    'flask',
    'werkzeug',
    'werkzeug.serving',
    'werkzeug.routing',
    # pyzbar
    'pyzbar',
    'pyzbar.pyzbar',
    # Other dependencies
    'numpy',
    'PIL',
    'PIL.Image',
]

# Collect submodules for av (handle errors gracefully)
try:
    av_modules = collect_submodules('av')
    hiddenimports += av_modules
except Exception as e:
    # If collection fails, the explicit hiddenimports above should cover essentials
    print(f"Warning: Could not collect all av submodules: {e}")
    pass

# Additional av imports that are often missed
hiddenimports += [
    'av.bytesource',
    'av.buffer',
    'av.frame',
    'av.audio.frame',
    'av.video.frame',
    'av.codec.codec',
    'av.codec.context',
    'av.format',
    'av.container',
    'av.packet',
]

# Collect submodules for picamera2 (excluding optional dependencies)
try:
    # Collect picamera2 submodules, but handle missing optional dependencies gracefully
    picamera2_modules = collect_submodules('picamera2')
    # Filter out modules that require optional dependencies
    filtered_modules = []
    for mod in picamera2_modules:
        # Skip picamera2.devices if Imath is not available (optional HDR support)
        if mod == 'picamera2.devices':
            try:
                import Imath
                filtered_modules.append(mod)
            except ImportError:
                # Imath is optional - skip this module
                pass
        else:
            filtered_modules.append(mod)
    hiddenimports += filtered_modules
except Exception as e:
    # If collection fails, at least include the essential modules
    print(f"Warning: Could not collect all picamera2 submodules: {e}")
    pass

a = Analysis(
    ['camera_sidecar.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['Imath'],  # Exclude optional Imath dependency (OpenEXR) - not needed for camera sidecar
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='camera_sidecar',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # Keep console for debugging
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
