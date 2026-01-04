#!/usr/bin/env python3
"""
Camera Sidecar Service for Raspberry Pi Camera Barcode Scanning

This service continuously captures frames from the Pi Camera using libcamera (Picamera2)
and decodes barcodes, streaming new scans to clients via long-polling HTTP endpoints.

Usage outside of the guestbook client:
    python3 camera_sidecar.py

The service will start on http://127.0.0.1:7313
"""

import time
import threading
import gc
import sys
import os
from datetime import datetime, timezone
from flask import Flask, jsonify, request, Response
from threading import Thread, Lock, Condition
from picamera2 import Picamera2
import cv2
import numpy as np
from pyzbar.pyzbar import decode as decode_barcodes, ZBarSymbol
import psutil

app = Flask(__name__)


@app.after_request
def add_cors_headers(response):
    """Add CORS headers to allow requests from Tauri frontend"""
    # Allow requests from Tauri dev server and production app
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

# Configuration
NEXT_SCAN_TIMEOUT = 2.0  # Long-poll timeout in seconds (reduced for faster response)
DEBOUNCE_MS = 800  # Ignore duplicate scans within this window (ms) - Note: debounce now handled in Tauri backend
CAPTURE_FPS = 14  # Target capture rate (~14 FPS)
CAPTURE_INTERVAL = 1.0 / CAPTURE_FPS  # ~0.07 seconds between captures
DECODE_INTERVAL = 0.02  # Reduced delay to process frames faster (~50 FPS decode rate)
DECODE_SKIP_FRAMES = 2  # Only decode every Nth frame to keep up with capture rate
GC_COLLECT_INTERVAL = 2000  # Force garbage collection every N decode iterations (~40 seconds, reduced frequency)
MEMORY_MONITOR_INTERVAL = 500  # Log memory usage every N decode iterations (~10 seconds)

# Shared state
picam2 = None
latest_frame = None
frame_lock = Lock()
frame_condition = Condition(frame_lock)  # Condition for frame notification
frame_sequence = 0  # Sequence counter for new frames
latest_scan_id = 0
latest_scan = None
scan_lock = Lock()
camera_error = None
running = True


def camera_capture_loop():
    """
    Continuously captures frames from the camera using Picamera2 (libcamera).
    Runs in a background thread.
    """
    global latest_frame, camera_error, picam2, frame_sequence

    print("Starting camera capture loop...")

    try:
        pic = Picamera2()
        picam2 = pic

        # Use multi-stream configuration:
        # - Main stream: high-res RGB for barcode detection
        # - LoRes stream: lower-res for MJPEG preview (reduces CPU load)
        config = pic.create_video_configuration(
            main={"size": (1280, 720), "format": "RGB888"},
            lores={"size": (640, 360), "format": "YUV420"}  # Lower res for preview
        )
        pic.configure(config)

        # Enable autofocus
        autofocus_enabled = False
        try:
            pic.set_controls({"AfMode": 1, "AfTrigger": 0})  # Continuous autofocus
            print("[CAPTURE] Autofocus enabled (continuous mode)")
            autofocus_enabled = True
        except Exception as e:
            print(f"[CAPTURE] Warning: Could not enable autofocus: {e}")
            # Try alternative autofocus method
            try:
                pic.set_controls({"AfMode": 2})  # Auto mode
                print("[CAPTURE] Autofocus enabled (auto mode)")
                autofocus_enabled = True
            except Exception as e2:
                print(f"[CAPTURE] Warning: Alternative autofocus also failed: {e2}")

        pic.start()

        # Wait a moment for autofocus to settle
        time.sleep(1.0)

        # Trigger initial autofocus
        if autofocus_enabled:
            try:
                pic.set_controls({"AfTrigger": 1})  # Trigger autofocus
                time.sleep(0.5)  # Give it a moment to focus
                pic.set_controls({"AfTrigger": 0})  # Return to continuous mode
                print("[CAPTURE] Initial autofocus triggered")
            except Exception as e:
                print(f"[CAPTURE] Warning: Could not trigger initial autofocus: {e}")

        print("Camera opened successfully (1280x720 RGB)")

        # Capture a test frame to verify camera is working
        try:
            test_frame = pic.capture_array()
            print(f"[CAPTURE] Test frame captured: shape={test_frame.shape}, dtype={test_frame.dtype}")
            # Save test frame for debugging
            cv2.imwrite('/tmp/camera_test_frame.jpg', cv2.cvtColor(test_frame, cv2.COLOR_RGB2BGR))
            print("[CAPTURE] Test frame saved to /tmp/camera_test_frame.jpg")
        except Exception as e:
            print(f"[CAPTURE] Failed to capture test frame: {e}")

        frame_count = 0
        autofocus_trigger_interval = 150  # Trigger autofocus every ~10 seconds (150 frames at 14fps)
        while running:
            try:
                frame = pic.capture_array()
                frame_count += 1
                with frame_condition:
                    # Replace old frame reference (let GC clean it up)
                    old_frame = latest_frame
                    latest_frame = frame
                    frame_sequence += 1
                    # Explicitly delete old frame reference to help GC
                    del old_frame
                    # Notify waiting decode thread of new frame
                    frame_condition.notify_all()

                # Periodically trigger autofocus to keep it adjusting
                if autofocus_enabled and frame_count % autofocus_trigger_interval == 0:
                    try:
                        pic.set_controls({"AfTrigger": 1})  # Trigger autofocus
                        time.sleep(0.1)  # Brief pause for focus adjustment
                        pic.set_controls({"AfTrigger": 0})  # Return to continuous mode
                        if frame_count % (autofocus_trigger_interval * 3) == 0:  # Log every 3rd trigger
                            print(f"[CAPTURE] Autofocus retriggered (frame {frame_count})")
                    except Exception as e:
                        print(f"[CAPTURE] Warning: Could not retrigger autofocus: {e}")

                # Log every 50 frames (~3.5 seconds at 14fps)
                if frame_count % 50 == 0:
                    print(f"[CAPTURE] Captured {frame_count} frames, latest frame shape: {frame.shape}")

            except Exception as e:
                print(f"Error capturing frame: {e}")
                with scan_lock:
                    camera_error = f"Camera capture error: {str(e)}"
                time.sleep(1)  # Back off on errors

            time.sleep(CAPTURE_INTERVAL)  # ~14 fps

    except Exception as e:
        print(f"Fatal error in camera capture loop: {e}")
        with scan_lock:
            camera_error = f"Fatal camera error: {str(e)}"
    finally:
        if picam2 is not None:
            try:
                picam2.stop()
                picam2.close()
            except Exception:
                pass
            picam2 = None
            print("Camera released")


def barcode_decode_loop():
    """
    Continuously decodes barcodes from captured frames.
    Runs in a background thread separate from capture.
    """
    global latest_scan_id, latest_scan, camera_error

    print("Starting barcode decode loop...")

    last_code = None
    last_time = 0.0

    decode_count = 0
    frame_skip_counter = 0
    last_frame_sequence = 0

    while running:
        try:
            frame = None
            current_sequence = 0

            # Wait for new frame using condition variable instead of polling
            with frame_condition:
                # Wait for a new frame (with timeout to allow shutdown)
                frame_condition.wait(timeout=0.1)

                if latest_frame is not None:
                    current_sequence = frame_sequence
                    # Skip frames to keep up with capture rate
                    if current_sequence != last_frame_sequence:
                        frame_skip_counter += 1
                        if frame_skip_counter >= DECODE_SKIP_FRAMES:
                            frame_skip_counter = 0
                            last_frame_sequence = current_sequence
                            # Assign frame reference directly (no copy!)
                            frame = latest_frame
                        else:
                            last_frame_sequence = current_sequence

            if frame is not None:
                decode_count += 1

                # Convert RGB → GRAY directly (eliminates RGB → BGR → GRAY double conversion)
                gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)

                # Lazy preprocessing: try grayscale first, only compute expensive transforms on failure
                barcodes = []

                # Try grayscale first (fastest)
                try:
                    detected = decode_barcodes(gray, symbols=[
                        ZBarSymbol.CODE128,
                        ZBarSymbol.CODE39,
                        ZBarSymbol.EAN13,
                        ZBarSymbol.EAN8,
                        ZBarSymbol.UPCA,
                        ZBarSymbol.UPCE,
                        ZBarSymbol.I25,
                        ZBarSymbol.CODABAR,
                    ])
                    if detected:
                        barcodes.extend(detected)
                        if decode_count % 50 == 0:
                            print(f"[DECODE] Found {len(detected)} barcode(s) using grayscale")
                except Exception as e:
                    if decode_count % 50 == 0:
                        print(f"[DECODE] Error with grayscale: {e}")

                # Only try threshold if grayscale failed (or every N frames for robustness)
                if not barcodes and decode_count % 3 == 0:
                    try:
                        thresholded = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
                        detected = decode_barcodes(thresholded, symbols=[
                            ZBarSymbol.CODE128,
                            ZBarSymbol.CODE39,
                            ZBarSymbol.EAN13,
                            ZBarSymbol.EAN8,
                            ZBarSymbol.UPCA,
                            ZBarSymbol.UPCE,
                            ZBarSymbol.I25,
                            ZBarSymbol.CODABAR,
                        ])
                        if detected:
                            barcodes.extend(detected)
                            if decode_count % 50 == 0:
                                print(f"[DECODE] Found {len(detected)} barcode(s) using threshold")
                    except Exception as e:
                        if decode_count % 50 == 0:
                            print(f"[DECODE] Error with threshold: {e}")

                # Only try contrast enhancement as last resort
                if not barcodes and decode_count % 5 == 0:
                    try:
                        contrast = cv2.convertScaleAbs(gray, alpha=1.5, beta=30)
                        detected = decode_barcodes(contrast, symbols=[
                            ZBarSymbol.CODE128,
                            ZBarSymbol.CODE39,
                            ZBarSymbol.EAN13,
                            ZBarSymbol.EAN8,
                            ZBarSymbol.UPCA,
                            ZBarSymbol.UPCE,
                            ZBarSymbol.I25,
                            ZBarSymbol.CODABAR,
                        ])
                        if detected:
                            barcodes.extend(detected)
                            if decode_count % 50 == 0:
                                print(f"[DECODE] Found {len(detected)} barcode(s) using contrast")
                    except Exception as e:
                        if decode_count % 50 == 0:
                            print(f"[DECODE] Error with contrast: {e}")

                # Remove duplicates (same code detected multiple times)
                seen_codes = set()
                unique_barcodes = []
                for bc in barcodes:
                    try:
                        code = bc.data.decode("utf-8").strip()
                        if code and code not in seen_codes:
                            seen_codes.add(code)
                            unique_barcodes.append(bc)
                    except:
                        pass
                barcodes = unique_barcodes

                # Debug: log frame processing every 50 decodes (~2.5 seconds)
                if decode_count % 50 == 0:
                    print(f"[DECODE] Processed {decode_count} frames, current frame: {len(barcodes)} barcode(s)")

                # Memory monitoring and garbage collection
                if decode_count % MEMORY_MONITOR_INTERVAL == 0:
                    try:
                        process = psutil.Process(os.getpid())
                        mem_info = process.memory_info()
                        mem_mb = mem_info.rss / 1024 / 1024
                        print(f"[MEMORY] RSS: {mem_mb:.1f} MB")
                    except Exception as e:
                        print(f"[MEMORY] Error monitoring memory: {e}")

                # Periodic garbage collection to free up numpy arrays (reduced frequency)
                if decode_count % GC_COLLECT_INTERVAL == 0:
                    collected = gc.collect()
                    if collected > 0:
                        print(f"[GC] Collected {collected} objects")

                now = time.time() * 1000

                for bc in barcodes:
                    try:
                        code = bc.data.decode("utf-8").strip()
                        if not code:
                            print(f"[DECODE] Found barcode but code is empty")
                            continue

                        print(f"[DECODE] Detected barcode: {code} (type: {bc.type})")
                    except Exception as decode_err:
                        print(f"[DECODE] Error decoding barcode data: {decode_err}")
                        continue

                    # Debounce: ignore if same code within debounce window
                    if code == last_code and (now - last_time) < DEBOUNCE_MS:
                        # Same label still in front of camera, ignore
                        print(f"[DECODE] Ignoring duplicate (debounce): {code}")
                        continue

                    # New scan detected
                    with scan_lock:
                        latest_scan_id += 1
                        latest_scan = {
                            "id": latest_scan_id,
                            "code": code,
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        }
                        camera_error = None  # Clear any previous error

                    print(f"[DECODE] Scan #{latest_scan_id}: {code}")

                    last_code = code
                    last_time = now
                    break  # Only handle one per frame

            # No sleep needed - condition variable handles waiting efficiently

        except Exception as e:
            print(f"Error in barcode decode loop: {e}")
            with scan_lock:
                camera_error = f"Barcode decode error: {str(e)}"
            time.sleep(1)  # Back off on errors


@app.route('/debug/frame', methods=['GET'])
def debug_frame():
    """Debug endpoint to capture and save current frame"""
    try:
        with frame_lock:
            if latest_frame is None:
                return jsonify({"error": "No frame available"}), 404

            frame = latest_frame.copy()

        # Convert RGB to BGR and save
        bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        filename = f'/tmp/debug_frame_{int(time.time())}.jpg'
        cv2.imwrite(filename, bgr)

        # Try to decode barcodes from this frame using multiple preprocessing methods
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

        processed_images = [
            ("original_bgr", bgr),
            ("grayscale", gray),
            ("grayscale_contrast", cv2.convertScaleAbs(gray, alpha=1.5, beta=30)),
            ("grayscale_sharpened", cv2.filter2D(gray, -1, np.array([[-1,-1,-1],[-1,9,-1],[-1,-1,-1]]))),
            ("grayscale_threshold", cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]),
        ]

        all_barcodes = []
        results_by_method = {}

        for method_name, processed_img in processed_images:
            try:
                barcodes = decode_barcodes(processed_img, symbols=[
                    ZBarSymbol.CODE128,
                    ZBarSymbol.CODE39,
                    ZBarSymbol.EAN13,
                    ZBarSymbol.EAN8,
                    ZBarSymbol.UPCA,
                    ZBarSymbol.UPCE,
                    ZBarSymbol.I25,
                    ZBarSymbol.CODABAR,
                ])
                results_by_method[method_name] = len(barcodes)
                if barcodes:
                    all_barcodes.extend(barcodes)
                    # Save the successful preprocessing result
                    method_filename = filename.replace('.jpg', f'_{method_name}.jpg')
                    cv2.imwrite(method_filename, processed_img)
            except Exception as e:
                results_by_method[method_name] = f"error: {str(e)}"

        # Remove duplicates
        seen_codes = set()
        detected = []
        for bc in all_barcodes:
            try:
                code = bc.data.decode("utf-8").strip()
                if code and code not in seen_codes:
                    seen_codes.add(code)
                    detected.append({"code": code, "type": bc.type})
            except:
                pass

        return jsonify({
            "success": True,
            "frame_saved": filename,
            "frame_shape": list(frame.shape),
            "barcodes_found": len(detected),
            "barcodes": detected,
            "decode_results_by_method": results_by_method
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/video', methods=['GET'])
def video_stream():
    """
    MJPEG video stream endpoint for displaying camera feed.
    Uses separate lores stream to avoid impacting detection performance.
    Only intended for debug/dev use - not for production.
    """
    def generate():
        """Generator function to stream MJPEG frames"""
        last_frame_time = 0.0
        preview_fps = 6.0  # Target ~6 FPS for preview (reduces CPU load)
        preview_interval = 1.0 / preview_fps  # ~0.167 seconds

        while running:
            try:
                current_time = time.time()

                # Throttle preview to target FPS
                if current_time - last_frame_time < preview_interval:
                    time.sleep(0.01)  # Small sleep to avoid busy-waiting
                    continue

                # Use lores stream if available (separate from detection stream)
                preview_frame = None
                if picam2 is not None:
                    try:
                        # Capture from lores stream (lower resolution, less CPU)
                        preview_frame = picam2.capture_array("lores")
                        # Handle YUV420 format from lores stream
                        if preview_frame.ndim == 2:  # Y plane only (grayscale)
                            preview_frame = cv2.cvtColor(preview_frame, cv2.COLOR_GRAY2BGR)
                        elif preview_frame.ndim == 3 and preview_frame.shape[2] == 3:
                            # Already RGB/BGR format
                            preview_frame = cv2.cvtColor(preview_frame, cv2.COLOR_RGB2BGR)
                        else:
                            # Try YUV420 conversion (Picamera2 may return planar YUV)
                            try:
                                preview_frame = cv2.cvtColor(preview_frame, cv2.COLOR_YUV420p2BGR)
                            except:
                                # If conversion fails, treat as grayscale
                                if preview_frame.ndim == 2:
                                    preview_frame = cv2.cvtColor(preview_frame, cv2.COLOR_GRAY2BGR)
                    except Exception as e:
                        # Fallback to main stream if lores not available
                        print(f"[VIDEO] Could not use lores stream, falling back: {e}")
                        with frame_lock:
                            if latest_frame is not None:
                                preview_frame = latest_frame.copy()
                                # Convert RGB to BGR for OpenCV encoding
                                preview_frame = cv2.cvtColor(preview_frame, cv2.COLOR_RGB2BGR)

                if preview_frame is None:
                    time.sleep(0.1)
                    continue

                # Downscale for lower bandwidth and CPU usage
                preview_frame = cv2.resize(preview_frame, (640, 360))

                # Encode as JPEG with lower quality for faster encoding
                ret, jpeg = cv2.imencode('.jpg', preview_frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
                if not ret:
                    time.sleep(preview_interval)
                    continue

                # Yield MJPEG frame
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')

                last_frame_time = current_time
            except Exception as e:
                print(f"[VIDEO] Error in video stream: {e}")
                time.sleep(0.1)

    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/health', methods=['GET', 'OPTIONS'])
def health():
    """Health check endpoint"""
    if request.method == 'OPTIONS':
        return '', 200
    print(f"[HTTP] GET /health")
    with scan_lock:
        if camera_error:
            print(f"[HTTP] /health returning error: {camera_error}")
            return jsonify({"status": "error", "error": camera_error}), 500
        if picam2 is None and running:
            print(f"[HTTP] /health returning error: Camera not initialized")
            return jsonify({"status": "error", "error": "Camera not initialized"}), 500
    print(f"[HTTP] /health returning OK")
    return jsonify({"status": "ok"})


@app.route('/trigger_autofocus', methods=['POST', 'OPTIONS'])
def trigger_autofocus():
    """Manually trigger autofocus"""
    if request.method == 'OPTIONS':
        return '', 200
    print(f"[HTTP] POST /trigger_autofocus")
    try:
        if picam2 is None:
            return jsonify({"success": False, "error": "Camera not initialized"}), 500

        # Trigger autofocus
        picam2.set_controls({"AfTrigger": 1})
        time.sleep(0.2)  # Give it a moment to focus
        picam2.set_controls({"AfTrigger": 0})  # Return to continuous mode

        print("[HTTP] Autofocus triggered manually")
        return jsonify({"success": True, "message": "Autofocus triggered"})
    except Exception as e:
        print(f"[HTTP] Error triggering autofocus: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/debug/memory', methods=['GET'])
def debug_memory():
    """Debug endpoint to check memory usage"""
    try:
        process = psutil.Process(os.getpid())
        mem_info = process.memory_info()

        # Get GC stats
        gc_stats = gc.get_stats()

        return jsonify({
            "success": True,
            "memory": {
                "rss_mb": round(mem_info.rss / 1024 / 1024, 2),
                "vms_mb": round(mem_info.vms / 1024 / 1024, 2),
                "percent": round(process.memory_percent(), 2),
            },
            "gc": {
                "collections": gc_stats,
                "threshold": gc.get_threshold(),
            },
            "threads": threading.active_count(),
        })
    except ImportError:
        return jsonify({
            "success": False,
            "error": "psutil not installed. Install with: pip3 install psutil"
        }), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/next_scan', methods=['GET', 'OPTIONS'])
def next_scan():
    """
    Long-polling endpoint to get the next barcode scan.

    Query parameters:
        since_id (int, optional): The last scan ID the client has seen.
                                  Only return scans with id > since_id.

    Returns immediately if a new scan is available, otherwise waits up to
    NEXT_SCAN_TIMEOUT seconds for a new scan.

    Response formats:
        Success with new scan:
        {
            "success": true,
            "id": 123,
            "code": "1234567890",
            "timestamp": "2025-01-01T00:00:00Z"
        }

        Timeout (no new scan):
        {
            "success": false,
            "id": since_id,
            "code": null,
            "timeout": true
        }

        Error:
        {
            "success": false,
            "code": null,
            "error": "Could not read from camera"
        }
    """
    if request.method == 'OPTIONS':
        return '', 200
    try:
        # Parse since_id parameter
        since_id = 0
        if 'since_id' in request.args:
            try:
                since_id = int(request.args.get('since_id', 0))
            except ValueError:
                since_id = 0

        print(f"[HTTP] GET /next_scan?since_id={since_id}")

        # Check if there's already a new scan available
        with scan_lock:
            if camera_error:
                return jsonify({
                    "success": False,
                    "code": None,
                    "error": camera_error
                }), 500

            if latest_scan is not None and latest_scan["id"] > since_id:
                # Return immediately
                print(f"[HTTP] /next_scan returning scan #{latest_scan['id']}: {latest_scan['code']}")
                return jsonify({
                    "success": True,
                    **latest_scan
                })

        # No new scan available, wait for one (long-polling)
        print(f"[HTTP] /next_scan no new scan, long-polling (timeout={NEXT_SCAN_TIMEOUT}s)")
        deadline = time.time() + NEXT_SCAN_TIMEOUT
        start_id = since_id

        while time.time() < deadline:
            time.sleep(0.1)  # Check every 100ms

            with scan_lock:
                # Check for errors
                if camera_error:
                    print(f"[HTTP] /next_scan error during poll: {camera_error}")
                    return jsonify({
                        "success": False,
                        "code": None,
                        "error": camera_error
                    }), 500

                # Check for new scan
                if latest_scan is not None and latest_scan["id"] > since_id:
                    print(f"[HTTP] /next_scan found new scan #{latest_scan['id']}: {latest_scan['code']}")
                    return jsonify({
                        "success": True,
                        **latest_scan
                    })

        # Timeout - no new scan
        with scan_lock:
            current_id = latest_scan["id"] if latest_scan else start_id
        print(f"[HTTP] /next_scan timeout, returning (current_id={current_id})")
        return jsonify({
            "success": False,
            "id": current_id,
            "code": None,
            "timeout": True
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "code": None,
            "error": f"Server error: {str(e)}"
        }), 500


def start_threads():
    """Start camera capture and barcode decode threads"""
    global running

    running = True

    t1 = Thread(target=camera_capture_loop, daemon=True)
    t2 = Thread(target=barcode_decode_loop, daemon=True)

    t1.start()
    t2.start()

    print("Camera capture and barcode decode threads started")


def stop_threads():
    """Stop camera capture and barcode decode threads"""
    global running

    running = False
    # Give threads a moment to finish
    time.sleep(0.5)
    print("Camera threads stopped")


if __name__ == '__main__':
    print("Starting Camera Sidecar Service...")
    print("Using libcamera (Picamera2) for camera access")
    print("Listening on http://127.0.0.1:7313")
    print("Endpoints:")
    print("  GET  /health     - Health check")
    print("  GET  /next_scan  - Long-poll for next barcode scan")
    print("  GET  /video      - MJPEG video stream (debug/dev only)")
    print("\nPress Ctrl+C to stop")

    # Start camera capture and decode threads
    start_threads()

    try:
        app.run(host='127.0.0.1', port=7313, debug=False, threaded=True)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        stop_threads()
        print("Camera sidecar stopped")
