"""Minimal OctoPrint API mock server for integration testing.

Implements just enough of the OctoPrint REST API to satisfy Kiln's
live test suite (test_live_octoprint.py). Simulates a virtual printer
in idle state with room-temperature readings.

Usage:
    python3 scripts/octoprint_mock.py  # starts on port 5000

Then run tests:
    KILN_LIVE_OCTOPRINT_HOST=http://localhost:5000 \
    KILN_LIVE_OCTOPRINT_KEY=mock-key \
    python3 -m pytest tests/test_live_octoprint.py -m live -x -v
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List

from flask import Flask, jsonify, request

app = Flask(__name__)

# In-memory file storage
_files: Dict[str, Dict[str, Any]] = {}

# Simulated printer state
_MOCK_API_KEY = "mock-key"


def _check_auth():
    """Validate X-Api-Key header."""
    key = request.headers.get("X-Api-Key", "")
    if key != _MOCK_API_KEY:
        return jsonify({"error": "Invalid API key"}), 403
    return None


# ---------------------------------------------------------------------------
# Printer state
# ---------------------------------------------------------------------------


@app.route("/api/printer", methods=["GET"])
def get_printer():
    auth_err = _check_auth()
    if auth_err:
        return auth_err
    return jsonify(
        {
            "temperature": {
                "tool0": {"actual": 22.5, "target": 0.0, "offset": 0},
                "bed": {"actual": 21.0, "target": 0.0, "offset": 0},
            },
            "state": {
                "text": "Operational",
                "flags": {
                    "operational": True,
                    "ready": True,
                    "printing": False,
                    "paused": False,
                    "pausing": False,
                    "cancelling": False,
                    "error": False,
                    "closedOrError": False,
                    "sdReady": True,
                    "finishing": False,
                },
            },
        }
    )


# ---------------------------------------------------------------------------
# Job status
# ---------------------------------------------------------------------------


@app.route("/api/job", methods=["GET", "POST"])
def job():
    auth_err = _check_auth()
    if auth_err:
        return auth_err

    if request.method == "GET":
        return jsonify(
            {
                "job": {"file": {"name": None}},
                "progress": {
                    "completion": None,
                    "printTime": None,
                    "printTimeLeft": None,
                },
                "state": "Operational",
            }
        )
    # POST — cancel/pause/resume commands
    return "", 204


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------


@app.route("/api/files/local", methods=["GET", "POST"])
def files_local():
    auth_err = _check_auth()
    if auth_err:
        return auth_err

    if request.method == "GET":
        file_list: List[Dict[str, Any]] = []
        for name, meta in _files.items():
            file_list.append(
                {
                    "name": name,
                    "path": name,
                    "type": "machinecode",
                    "size": meta.get("size", 0),
                    "date": meta.get("date", int(time.time())),
                }
            )
        return jsonify({"files": file_list})

    # POST — file upload
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "No file provided"}), 400

    name = uploaded.filename
    content = uploaded.read()
    _files[name] = {
        "size": len(content),
        "date": int(time.time()),
        "content": content,
    }
    return (
        jsonify(
            {
                "files": {"local": {"name": name, "origin": "local"}},
                "done": True,
            }
        ),
        201,
    )


@app.route("/api/files/local/<path:file_path>", methods=["POST", "DELETE"])
def file_action(file_path: str):
    auth_err = _check_auth()
    if auth_err:
        return auth_err

    if request.method == "DELETE":
        if file_path in _files:
            del _files[file_path]
        return "", 204

    # POST — select/print
    return "", 204


# ---------------------------------------------------------------------------
# Temperature control
# ---------------------------------------------------------------------------


@app.route("/api/printer/tool", methods=["POST"])
def printer_tool():
    auth_err = _check_auth()
    if auth_err:
        return auth_err
    return "", 204


@app.route("/api/printer/bed", methods=["POST"])
def printer_bed():
    auth_err = _check_auth()
    if auth_err:
        return auth_err
    return "", 204


# ---------------------------------------------------------------------------
# G-code commands
# ---------------------------------------------------------------------------


@app.route("/api/printer/command", methods=["POST"])
def printer_command():
    auth_err = _check_auth()
    if auth_err:
        return auth_err
    return "", 204


# ---------------------------------------------------------------------------
# Optional plugins (return empty / not-found gracefully)
# ---------------------------------------------------------------------------


@app.route("/api/plugin/filamentmanager", methods=["GET"])
def filament_manager():
    auth_err = _check_auth()
    if auth_err:
        return auth_err
    return jsonify({"error": "Plugin not installed"}), 404


@app.route("/api/plugin/bedlevelvisualizer", methods=["GET"])
def bed_level():
    auth_err = _check_auth()
    if auth_err:
        return auth_err
    return jsonify({"error": "Plugin not installed"}), 404


@app.route("/plugin/softwareupdate/check", methods=["GET"])
def software_update_check():
    auth_err = _check_auth()
    if auth_err:
        return auth_err
    return jsonify({"busy": False, "information": {}})


# ---------------------------------------------------------------------------
# Webcam (return a minimal JPEG-like response)
# ---------------------------------------------------------------------------


@app.route("/webcam/", methods=["GET"])
def webcam():
    # Return a minimal valid JPEG (SOI + EOI markers)
    return bytes([0xFF, 0xD8, 0xFF, 0xE0, 0xFF, 0xD9]), 200, {
        "Content-Type": "image/jpeg"
    }


if __name__ == "__main__":
    port = int(os.environ.get("MOCK_OCTOPRINT_PORT", "5000"))
    print(f"OctoPrint mock server starting on port {port}...")
    app.run(host="127.0.0.1", port=port, debug=False)
