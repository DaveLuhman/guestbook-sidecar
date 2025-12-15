# Camera Sidecar Service

This Python service continuously captures frames from the Raspberry Pi Camera using libcamera (Picamera2) and decodes barcodes, streaming new scans to clients via long-polling HTTP endpoints.

## Overview

The camera sidecar is automatically spawned by the Tauri Rust backend when the application starts. It runs as a separate process and communicates with the frontend via HTTP on `http://127.0.0.1:7313`.

**Key Features:**
- Uses Picamera2 for native libcamera integration (Raspberry Pi Camera)
- Continuous frame capture at ~14 FPS
- Multi-threaded architecture: separate capture and decode loops
- Advanced image preprocessing for improved barcode detection
- Automatic debouncing to prevent duplicate scans
- Memory management and garbage collection
- CORS-enabled for frontend access

## Installation

Install Python dependencies:

```bash
pip3 install -r requirements.txt
```

Required packages:
- `flask>=2.3.0` - HTTP server
- `picamera2>=0.3.12` - Raspberry Pi Camera access via libcamera
- `opencv-python>=4.8.0` - Image processing
- `pyzbar>=0.1.9` - Barcode decoding
- `psutil>=5.9.0` - Memory monitoring

## Running

### Automatic (Production)

The sidecar is automatically started by the Tauri application when `startCameraScanner()` is called. No manual intervention needed.

### Manual (Development/Testing)

For testing or debugging, you can run the sidecar manually:

```bash
python3 sidecar/camera_sidecar.py
```

The service will start on `http://127.0.0.1:7313` and begin continuously capturing frames from the camera.

## Architecture

The service uses a multi-threaded architecture:

### Camera Capture Loop (`camera_capture_loop`)
- Opens the camera once on startup using Picamera2
- Continuously captures frames at ~14 FPS (configurable via `CAPTURE_FPS`)
- Stores latest frame in shared memory with thread-safe locking
- Handles camera errors and recovery

### Barcode Decode Loop (`barcode_decode_loop`)
- Retrieves frames from shared memory (with copying to avoid race conditions)
- Applies image preprocessing techniques:
  - Grayscale conversion
  - Contrast adjustment
  - Adaptive thresholding
- Attempts barcode decoding using pyzbar with explicit symbol types
- Implements debouncing (800ms cooldown) to prevent duplicate scans
- Skips frames (`DECODE_SKIP_FRAMES = 2`) to keep up with capture rate
- Performs periodic garbage collection for memory management

### Configuration

Key configuration constants (in `camera_sidecar.py`):
- `CAPTURE_FPS = 14` - Target capture rate
- `DECODE_INTERVAL = 0.02` - Delay between decode attempts (~50 FPS decode rate)
- `DECODE_SKIP_FRAMES = 2` - Process every Nth frame
- `DEBOUNCE_MS = 800` - Ignore duplicate scans within this window
- `NEXT_SCAN_TIMEOUT = 8.0` - Long-poll timeout in seconds
- `GC_COLLECT_INTERVAL = 1000` - Force GC every N decode iterations
- `MEMORY_MONITOR_INTERVAL = 500` - Log memory usage every N iterations

## API Endpoints

### GET /health

Health check endpoint. Returns the status of the camera service.

**Response (ok):**
```json
{
  "status": "ok"
}
```

**Response (error):**
```json
{
  "status": "error",
  "error": "Camera capture error: ..."
}
```

### GET /next_scan

Long-polling endpoint to receive the next barcode scan. This is the primary endpoint for continuous scanning.

**Query Parameters:**
- `since_id` (int, optional): The last scan ID the client has seen. Only returns scans with id > since_id.

**Behavior:**
- If a new scan is available (id > since_id), returns immediately
- Otherwise, waits up to 8 seconds for a new scan (long-polling)
- Automatically handles debouncing - duplicate scans within 800ms are filtered out

**Response (new scan):**
```json
{
  "success": true,
  "id": 123,
  "code": "2730067",
  "timestamp": "2025-12-07T00:47:03.172597+00:00"
}
```

**Response (timeout - no new scan):**
```json
{
  "success": false,
  "id": 122,
  "code": null,
  "timeout": true
}
```

**Response (error):**
```json
{
  "success": false,
  "code": null,
  "error": "Could not read from camera"
}
```

### GET /debug/frame

Debug endpoint to capture and analyze the current frame. Useful for troubleshooting barcode detection issues.

**Response:**
```json
{
  "success": true,
  "frame_saved": "/tmp/debug_frame_1234567890.jpg",
  "frame_shape": [720, 1280, 3],
  "barcodes_found": 1,
  "barcodes": [
    {"code": "2730067", "type": "I25"}
  ],
  "decode_results_by_method": {
    "original_bgr": 0,
    "grayscale": 0,
    "grayscale_contrast": 1,
    "grayscale_sharpened": 0,
    "grayscale_threshold": 1
  }
}
```

This endpoint:
- Captures the current frame
- Saves it to `/tmp/debug_frame_*.jpg`
- Tests all preprocessing methods
- Saves successful preprocessing results to `/tmp/`
- Returns detection results for each method

### GET /debug/memory

Debug endpoint to check memory usage and garbage collection statistics.

**Response:**
```json
{
  "success": true,
  "memory": {
    "rss_mb": 245.32,
    "vms_mb": 512.45,
    "percent": 2.34
  },
  "gc": {
    "collections": [...],
    "threshold": [700, 10, 10]
  },
  "threads": 3
}
```

## Frontend Integration

The TypeScript frontend uses this service via `startScanStream()` which continuously polls `/next_scan`:

```typescript
import { startScanStream } from './cameraSidecarClient';

const stopStream = startScanStream((scan) => {
  console.log('Barcode detected:', scan.code);
  // Process the scan...
});
```

The frontend automatically:
- Starts the sidecar process via Tauri IPC (`start_camera_sidecar`)
- Waits for the sidecar to initialize (with retry logic)
- Starts the continuous scan stream
- Processes barcode events and submits them to the backend

## Debugging

### Browser Console Commands

The following debugging functions are available in the browser console:

#### `checkSidecarStatus()`

Check the status of the camera sidecar process:

```javascript
checkSidecarStatus()
```

Returns:
```javascript
{
  Running: "✓ Yes" | "✗ No",
  PID: 76858 | "N/A",
  Exited: "✓ Yes" | "✗ No",
  "Exit Code": 1 | "N/A",
  "Last Error": "..." | "None"
}
```

#### `testNextScan()`

Test the `/next_scan` endpoint directly:

```javascript
testNextScan()
```

This will:
- Call `/next_scan?since_id=0` directly
- Return the response (scan or timeout)
- Useful for verifying barcode detection without the frontend stream

#### `startCameraScanner()`

Manually start the camera scanner (useful if initialization failed):

```javascript
startCameraScanner()
```

This will:
- Attempt to start the sidecar process
- Retry health checks up to 10 times
- Start the continuous scan stream

### Python Sidecar Logs

The sidecar logs to stdout/stderr, which are captured by the Rust backend and logged with the prefix `[Camera Sidecar PID ...]`. Check:
- Rust application logs (console or log file)
- For manual runs: terminal output

**Common log messages:**
- `[CAPTURE] Captured N frames` - Frame capture progress
- `[DECODE] Detected barcode: ...` - Successful barcode detection
- `[DECODE] Scan #N: ...` - New scan queued
- `[HTTP] GET /next_scan` - HTTP request logs
- `[HTTP] /next_scan returning scan #N` - Scan delivery

### Debug Endpoints

Use the debug endpoints to troubleshoot:

```bash
# Capture current frame and test detection
curl http://127.0.0.1:7313/debug/frame

# Check memory usage
curl http://127.0.0.1:7313/debug/memory
```

## Barcode Format Support

The sidecar detects various barcode formats:
- CODE128
- CODE39
- EAN13, EAN8
- UPCA, UPCE
- I25 (Interleaved 2 of 5)
- CODABAR

The frontend expects:
- Format: `^1234567^` (with carets) OR plain `1234567` (7 digits)
- The frontend automatically handles both formats

## Camera Configuration

The sidecar uses:
- **Resolution**: 1280x720 (RGB888)
- **Autofocus**: Continuous autofocus enabled (`AfMode: 1`)
- **Backend**: Picamera2 (libcamera) - native Raspberry Pi camera stack

## Memory Management

The service includes several memory management features:
- Explicit `del` statements for large numpy arrays
- Periodic `gc.collect()` calls
- Frame skipping to reduce processing load
- Memory monitoring via `/debug/memory` endpoint

## CORS

The service includes CORS headers to allow requests from the Tauri frontend:
- `Access-Control-Allow-Origin: *`
- Supports OPTIONS preflight requests

## Troubleshooting

### Sidecar Not Starting

1. Check if Python process is running:
   ```javascript
   checkSidecarStatus()
   ```

2. Check Rust logs for Python stderr output (prefixed with `[Camera Sidecar PID ...]`)

3. Verify Python dependencies are installed:
   ```bash
   pip3 install -r requirements.txt
   ```

### Barcodes Not Detected

1. Test the sidecar directly:
   ```javascript
   testNextScan()
   ```

2. Use debug endpoint to capture and analyze frames:
   ```bash
   curl http://127.0.0.1:7313/debug/frame
   ```
   Check the saved images in `/tmp/debug_frame_*.jpg`

3. Verify camera is accessible:
   ```bash
   libcamera-hello --list-cameras
   ```

4. Check camera focus - ensure barcode is in focus and well-lit

### Health Check Failing

The frontend includes retry logic (10 attempts with exponential backoff). If health checks consistently fail:
- Check if port 7313 is already in use
- Verify the Python script path is correct
- Check Python process logs for errors

### Automatic Restart

The system includes automatic restart logic:
- **Rust backend**: If `start_camera_sidecar` is called and the process has exited, it will automatically restart it
- **Frontend monitoring**: The `checkCameraHealth()` function runs every 5 seconds and will automatically restart the sidecar if it detects the process has exited

This ensures the camera sidecar stays running even if it crashes or is killed.

### GTK Widget Errors

If you see GTK widget assertion errors in system logs (e.g., `gtk_widget_hide: assertion 'GTK_IS_WIDGET (widget)' failed`):
- These are typically WebKitGTK/Tauri UI errors, not camera sidecar errors
- The camera sidecar runs as a separate Python process and is unaffected by GTK errors
- However, if the Tauri app crashes due to GTK errors, it may kill the sidecar process
- The automatic restart logic will restart the sidecar when the app recovers
- To investigate GTK errors, check Tauri/WebKitGTK logs and ensure proper window/widget lifecycle management

## Systemd Service (Optional)

To run as a standalone systemd service (instead of being spawned by Tauri), create a service file:

```ini
[Unit]
Description=Camera Sidecar Service
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/path/to/guestbook-client
ExecStart=/usr/bin/python3 /path/to/guestbook-client/sidecar/camera_sidecar.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:
```bash
sudo systemctl daemon-reload
sudo systemctl enable camera_sidecar.service
sudo systemctl start camera_sidecar.service
```

**Note**: The Tauri app spawns the sidecar automatically, so a systemd service is typically not needed unless running the sidecar independently.
