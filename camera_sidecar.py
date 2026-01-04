#!/usr/bin/env python3
"""
Camera Sidecar Service for Raspberry Pi Camera Barcode Scanning

This service continuously captures frames from the Pi Camera using libcamera (Picamera2)
and decodes barcodes, streaming new scans to clients via long-polling HTTP endpoints.

Usage:
    python3 sidecar/camera_sidecar.py

The service will start on http://127.0.0.1:7313

For systemd service, see sidecar/camera_sidecar.service.example
"""

import time
import threading
import gc
import sys
from datetime import datetime, timezone
from flask import Flask, jsonify, request, Response
from threading import Thread, Lock
from picamera2 import Picamera2
import cv2
import numpy as np
from pyzbar.pyzbar import decode as decode_barcodes, ZBarSymbol

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
GC_COLLECT_INTERVAL = 1000  # Force garbage collection every N decode iterations (~20 seconds)
MEMORY_MONITOR_INTERVAL = 500  # Log memory usage every N decode iterations (~10 seconds)

# Camera resolution constants
CAMERA_WIDTH = 1280
CAMERA_HEIGHT = 720
PREVIEW_WIDTH = 640
PREVIEW_HEIGHT = 360
PREVIEW_FPS = 15
PREVIEW_JPEG_QUALITY = 85

# Shared state
picam2 = None
latest_frame = None
latest_frame_seq = 0
latest_lores_frame = None
latest_lores_seq = 0
frame_condition = threading.Condition()
latest_scan_id = 0
latest_scan = None
latest_scan_ts = None
latest_scan_seq = 0
scan_condition = threading.Condition()
scan_lock = Lock()  # Keep for backward compatibility with error handling
camera_error = None
running = True


def camera_capture_loop():
    """
    Continuously captures frames from the camera using Picamera2 (libcamera).
    Runs in a background thread.
    """
    global latest_frame, camera_error, picam2

    print("Starting camera capture loop...")

    try:
        pic = Picamera2()
        picam2 = pic

        # Use multi-stream configuration:
        # - main: RGB888 at 1280x720 for barcode detection
        # - lores: YUV420 at 640x360 for preview (hardware requirement: lores must be YUV)
        # Note: YUV420 is planar format, we'll convert it properly in the video endpoint
        config = pic.create_video_configuration(
            main={"size": (CAMERA_WIDTH, CAMERA_HEIGHT), "format": "RGB888"},
            lores={"size": (PREVIEW_WIDTH, PREVIEW_HEIGHT), "format": "YUV420"}
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

        print(f"Camera opened successfully (main: {CAMERA_WIDTH}x{CAMERA_HEIGHT} RGB, lores: {PREVIEW_WIDTH}x{PREVIEW_HEIGHT} YUV)")

        # Capture a test frame to verify camera is working
        try:
            test_frame = pic.capture_array("main")
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
                # Capture both main and lores streams
                main_frame = pic.capture_array("main")
                lores_frame = pic.capture_array("lores")
                frame_count += 1
                with frame_condition:
                    # Replace old frame references (let GC clean them up)
                    old_frame = latest_frame
                    old_lores_frame = latest_lores_frame
                    latest_frame = main_frame
                    latest_frame_seq += 1
                    latest_lores_frame = lores_frame
                    latest_lores_seq += 1
                    # Explicitly delete old frame references to help GC
                    del old_frame
                    del old_lores_frame
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
                    print(f"[CAPTURE] Captured {frame_count} frames, latest frame shape: {main_frame.shape}")

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
    while running:
        try:
            frame = None
            with frame_condition:
                if latest_frame is not None:
                    frame = latest_frame.copy()

            if frame is not None:
                # Skip frames to keep up with capture rate
                frame_skip_counter += 1
                if frame_skip_counter < DECODE_SKIP_FRAMES:
                    time.sleep(DECODE_INTERVAL)
                    continue
                frame_skip_counter = 0

                decode_count += 1
                # Picamera2 gives RGB, OpenCV/pyzbar likes BGR
                bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

                # Preprocess image for better barcode detection
                # Convert to grayscale (pyzbar works better on grayscale)
                gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

                # Try preprocessing approaches in order of speed/effectiveness
                # Start with fastest methods first, only try slower ones if needed
                processed_images = [
                    ("grayscale", gray),  # Fastest and most effective
                    ("grayscale_threshold", cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]),  # Good for blurry images
                    ("grayscale_contrast", cv2.convertScaleAbs(gray, alpha=1.5, beta=30)),  # Increase contrast
                ]

                barcodes = []
                for method_name, processed_img in processed_images:
                    try:
                        # Try decoding with all barcode types enabled
                        detected = decode_barcodes(processed_img, symbols=[
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
                                print(f"[DECODE] Found {len(detected)} barcode(s) using {method_name}")
                            # If we found barcodes, we can stop trying other methods
                            break
                    except Exception as e:
                        if decode_count % 50 == 0:
                            print(f"[DECODE] Error with {method_name}: {e}")
                        continue

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
                    import psutil
                    import os
                    try:
                        process = psutil.Process(os.getpid())
                        mem_info = process.memory_info()
                        mem_mb = mem_info.rss / 1024 / 1024
                        print(f"[MEMORY] RSS: {mem_mb:.1f} MB")
                    except ImportError:
                        # psutil not available, skip monitoring
                        pass
                    except Exception as e:
                        print(f"[MEMORY] Error monitoring memory: {e}")

                # Periodic garbage collection to free up numpy arrays
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
                    with scan_condition:
                        latest_scan_id += 1
                        latest_scan = {
                            "id": latest_scan_id,
                            "code": code,
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        }
                        latest_scan_seq = latest_scan_id  # Use same ID for seq
                        latest_scan_ts = time.time()
                        camera_error = None  # Clear any previous error
                        scan_condition.notify_all()

                    print(f"[DECODE] Scan #{latest_scan_id}: {code}")

                    last_code = code
                    last_time = now
                    break  # Only handle one per frame

            time.sleep(DECODE_INTERVAL)  # Small delay to avoid pegging CPU

        except Exception as e:
            print(f"Error in barcode decode loop: {e}")
            with scan_lock:
                camera_error = f"Barcode decode error: {str(e)}"
            time.sleep(1)  # Back off on errors


@app.route('/debug/frame', methods=['GET'])
def debug_frame():
    """Debug endpoint to capture and save current frame"""
    try:
        with frame_condition:
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
    Only intended for debug/dev use - not for production.

    Uses lores stream frames (no capture calls, just reads from shared state).
    """
    def generate():
        """Generator function to stream MJPEG frames"""
        last_seen_seq = 0
        while running:
            try:
                # Wait for new lores frame
                frame = None
                with frame_condition:
                    # Wait until latest_lores_seq advances or timeout
                    frame_condition.wait(timeout=1.0)
                    if latest_lores_frame is not None and latest_lores_seq > last_seen_seq:
                        frame = latest_lores_frame.copy()
                        last_seen_seq = latest_lores_seq

                if frame is None:
                    continue

                # Convert YUV420 to BGR for OpenCV encoding
                # Picamera2 returns YUV420 in planar format: shape is (height*3/2, width)
                # Use cv2.cvtColor with COLOR_YUV2BGR_I420 which handles planar YUV420
                height, width = PREVIEW_HEIGHT, PREVIEW_WIDTH
                if len(frame.shape) == 2 and frame.shape[0] == height * 3 // 2:
                    # Planar YUV420 format - convert directly
                    bgr = cv2.cvtColor(frame, cv2.COLOR_YUV2BGR_I420)
                else:
                    # Fallback: try to reshape and convert
                    # If frame is already in a different format, attempt conversion
                    try:
                        bgr = cv2.cvtColor(frame, cv2.COLOR_YUV2BGR_I420)
                    except:
                        # Last resort: assume it's interleaved YUV and convert
                        bgr = cv2.cvtColor(frame, cv2.COLOR_YUV2BGR)

                # Encode as JPEG with preview quality
                ret, jpeg = cv2.imencode('.jpg', bgr, [cv2.IMWRITE_JPEG_QUALITY, PREVIEW_JPEG_QUALITY])
                if not ret:
                    time.sleep(1.0 / PREVIEW_FPS)
                    continue

                # Yield MJPEG frame
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')

                # Throttle to PREVIEW_FPS
                time.sleep(1.0 / PREVIEW_FPS)
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
        import psutil
        import os
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
        since (int, optional): The last scan sequence number the client has seen.
                               Only return scans with seq > since. Default: 0
        timeout (float, optional): Maximum time to wait for a new scan in seconds.
                                   Default: 15

    Returns immediately if a new scan is available, otherwise waits up to
    timeout seconds for a new scan using efficient Condition.wait().

    Response format:
        {
            "ok": true,
            "scan": {
                "id": 123,
                "code": "1234567890",
                "timestamp": "2025-01-01T00:00:00Z"
            } or null,
            "seq": 123
        }
    """
    if request.method == 'OPTIONS':
        return '', 200
    try:
        # Parse query parameters - support both old API (since_id) and new API (since)
        since = 0
        if 'since' in request.args:
            try:
                since = int(request.args.get('since', 0))
            except ValueError:
                since = 0
        elif 'since_id' in request.args:
            # Backward compatibility with old API
            try:
                since = int(request.args.get('since_id', 0))
            except ValueError:
                since = 0

        timeout = 15.0
        if 'timeout' in request.args:
            try:
                timeout = float(request.args.get('timeout', 15.0))
            except ValueError:
                timeout = 15.0

        # Log which API format was used
        using_old_api = 'since_id' in request.args
        api_format = 'since_id' if using_old_api else 'since'
        print(f"[HTTP] GET /next_scan?{api_format}={since}&timeout={timeout}")

        # Check for errors first
        with scan_lock:
            if camera_error:
                if using_old_api:
                    # Old API error format
                    return jsonify({
                        "success": False,
                        "code": None,
                        "error": camera_error
                    }), 500
                else:
                    # New API error format
                    return jsonify({
                        "ok": False,
                        "scan": None,
                        "error": camera_error
                    }), 500

        # Use Condition.wait() for efficient long-polling (no busy polling)
        with scan_condition:
            # Check if there's already a new scan available
            if latest_scan_seq > since:
                scan_data = latest_scan if latest_scan else None
                print(f"[HTTP] /next_scan returning scan immediately (seq={latest_scan_seq})")
                if using_old_api:
                    # Old API response format
                    if scan_data:
                        return jsonify({
                            "success": True,
                            **scan_data
                        })
                    else:
                        return jsonify({
                            "success": False,
                            "id": since,
                            "code": None,
                            "timeout": True
                        })
                else:
                    # New API response format
                    return jsonify({
                        "ok": True,
                        "scan": scan_data,
                        "seq": latest_scan_seq
                    })

            # Wait for new scan or timeout
            print(f"[HTTP] /next_scan no new scan, waiting (since={since}, timeout={timeout}s)")
            scan_condition.wait(timeout)

            # Check again after wait
            if latest_scan_seq > since:
                scan_data = latest_scan if latest_scan else None
                print(f"[HTTP] /next_scan found new scan (seq={latest_scan_seq})")
                if using_old_api:
                    # Old API response format
                    if scan_data:
                        return jsonify({
                            "success": True,
                            **scan_data
                        })
                    else:
                        return jsonify({
                            "success": False,
                            "id": since,
                            "code": None,
                            "timeout": True
                        })
                else:
                    # New API response format
                    return jsonify({
                        "ok": True,
                        "scan": scan_data,
                        "seq": latest_scan_seq
                    })
            else:
                # Timeout - no new scan
                print(f"[HTTP] /next_scan timeout, returning (seq={latest_scan_seq})")
                if using_old_api:
                    # Old API timeout format
                    current_id = latest_scan["id"] if latest_scan else since
                    return jsonify({
                        "success": False,
                        "id": current_id,
                        "code": None,
                        "timeout": True
                    })
                else:
                    # New API timeout format
                    return jsonify({
                        "ok": True,
                        "scan": None,
                        "seq": latest_scan_seq
                    })

    except Exception as e:
        print(f"[HTTP] /next_scan error: {e}")
        # Determine API format from request (default to new if can't determine)
        using_old_api = 'since_id' in request.args if hasattr(request, 'args') else False
        if using_old_api:
            return jsonify({
                "success": False,
                "code": None,
                "error": f"Server error: {str(e)}"
            }), 500
        else:
            return jsonify({
                "ok": False,
                "scan": None,
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
