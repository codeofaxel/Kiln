"""Print monitoring tools plugin.

Extracts vision monitoring, background print watching, and first-layer
monitoring MCP tools from server.py into a focused plugin module.

The ``_PrintWatcher`` class and its supporting state (``_watchers``,
``_first_layer_monitors``, ``_PHASE_HINTS``, ``_detect_phase``) are
reproduced here so the plugin is self-contained and server.py can shed
those definitions.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` —
no manual imports needed.
"""

from __future__ import annotations

import logging
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Phase hints — guidance shown to agents during snapshot-based monitoring
# ---------------------------------------------------------------------------

_PHASE_HINTS: dict[str, list[str]] = {
    "first_layers": [
        "Check bed adhesion — first layer should be firmly stuck",
        "Look for warping at corners or edges lifting from bed",
        "Verify extrusion is consistent (no gaps or blobs)",
    ],
    "mid_print": [
        "Check for spaghetti — filament not adhering to previous layers",
        "Look for layer shifting (misaligned layers)",
        "Check for stringing between features",
    ],
    "final_layers": [
        "Check for cooling artifacts on overhangs",
        "Look for stringing or blobs on fine details",
        "Verify top surface is smooth and complete",
    ],
    "unknown": [
        "Verify print is progressing normally",
        "Check for any visible defects",
    ],
}


def _detect_phase(completion: float | None) -> str:
    """Classify print phase from completion percentage."""
    if completion is None or completion < 0:
        return "unknown"
    if completion < 10:
        return "first_layers"
    if completion > 90:
        return "final_layers"
    return "mid_print"


# ---------------------------------------------------------------------------
# _PrintWatcher — background thread that monitors a running print
# ---------------------------------------------------------------------------


class _PrintWatcher:
    """Background thread that monitors a running print.

    Polls printer state and captures snapshots in a daemon thread so
    that the MCP tool can return immediately.  Use :meth:`status` to
    read current progress and :meth:`stop` to cancel monitoring.
    """

    def __init__(
        self,
        watch_id: str,
        adapter: Any,
        printer_name: str,
        *,
        snapshot_interval: int = 60,
        max_snapshots: int = 5,
        timeout: int = 7200,
        poll_interval: int = 15,
        event_bus: Any | None = None,
        stall_timeout: int = 600,
        save_to_disk: bool = False,
    ) -> None:
        self._watch_id = watch_id
        self._adapter = adapter
        self._printer_name = printer_name
        self._snapshot_interval = snapshot_interval
        self._max_snapshots = max_snapshots
        self._timeout = timeout
        self._poll_interval = poll_interval
        self._event_bus = event_bus
        self._stall_timeout = stall_timeout
        self._save_to_disk = save_to_disk

        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._snapshots: list[dict] = []
        self._progress_log: list[dict] = []
        self._snapshot_failures: int = 0
        self._result: dict | None = None
        self._outcome: str = "running"
        self._start_time: float = 0.0
        self._thread: threading.Thread | None = None
        self._save_dir: str | None = None
        if self._save_to_disk:
            self._save_dir = os.path.join(str(Path.home()), ".kiln", "timelapses", watch_id)

    # -- public API --------------------------------------------------------

    def start(self) -> None:
        """Start the background monitoring thread."""
        self._start_time = time.time()
        self._thread = threading.Thread(
            target=self._run,
            name=f"print-watcher-{self._watch_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> dict:
        """Signal the watcher thread to stop and return final state."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        with self._lock:
            if self._result is not None:
                return self._result
            elapsed = round(time.time() - self._start_time, 1)
            return {
                "success": True,
                "watch_id": self._watch_id,
                "outcome": "stopped",
                "elapsed_seconds": elapsed,
                "progress_log": list(self._progress_log[-20:]),
                "snapshots": list(self._snapshots),
                "snapshot_failures": self._snapshot_failures,
            }

    def status(self) -> dict:
        """Return current watcher state (thread-safe snapshot)."""
        with self._lock:
            elapsed = round(time.time() - self._start_time, 1)
            return {
                "watch_id": self._watch_id,
                "printer_name": self._printer_name,
                "outcome": self._outcome,
                "elapsed_seconds": elapsed,
                "snapshots_collected": len(self._snapshots),
                "snapshot_failures": self._snapshot_failures,
                "progress_log": list(self._progress_log[-20:]),
                "snapshots": list(self._snapshots),
                "finished": self._result is not None,
                "result": self._result,
            }

    # -- internal ----------------------------------------------------------

    def _finish(self, result: dict) -> None:
        """Store the final result and publish a completion event."""
        with self._lock:
            self._result = result
            self._outcome = result.get("outcome", "unknown")
        if self._event_bus is not None:
            try:
                from kiln.events import EventType

                self._event_bus.publish(
                    EventType.PRINT_TERMINAL,
                    {
                        "watch_id": self._watch_id,
                        "printer_name": self._printer_name,
                        "outcome": result.get("outcome"),
                        "elapsed_seconds": result.get("elapsed_seconds"),
                    },
                    source="watch_print",
                )
            except Exception as exc:
                _logger.debug("Failed to publish print terminal event: %s", exc)

    def _run(self) -> None:
        """Main monitoring loop — runs in a background thread."""
        from kiln.events import EventType
        from kiln.printers import PrinterStatus

        adapter = self._adapter
        can_snap = getattr(adapter.capabilities, "can_snapshot", False)
        last_snapshot_time = 0.0

        # Stall detection state
        _last_completion: float | None = None
        _last_progress_time: float = time.time()

        try:
            while not self._stop_event.is_set():
                elapsed = time.time() - self._start_time
                if elapsed > self._timeout:
                    result = {
                        "success": True,
                        "watch_id": self._watch_id,
                        "outcome": "timeout",
                        "elapsed_seconds": round(elapsed, 1),
                        "progress_log": list(self._progress_log[-20:]),
                        "snapshots": list(self._snapshots),
                        "snapshot_failures": self._snapshot_failures,
                        "final_state": adapter.get_state().to_dict(),
                    }
                    self._finish(result)
                    return

                state = adapter.get_state()
                job = adapter.get_job()

                # Record progress
                if job.completion is not None:
                    with self._lock:
                        self._progress_log.append(
                            {
                                "time": round(elapsed, 1),
                                "completion": job.completion,
                            }
                        )

                # Stall detection — check if completion has changed
                if job.completion is not None:
                    if _last_completion is None or abs(job.completion - _last_completion) > 0.1:
                        _last_completion = job.completion
                        _last_progress_time = time.time()
                    elif self._stall_timeout > 0 and (time.time() - _last_progress_time) > self._stall_timeout:
                        stall_duration = round(time.time() - _last_progress_time, 1)
                        if self._event_bus is not None:
                            try:
                                self._event_bus.publish(
                                    EventType.VISION_ALERT,
                                    {
                                        "printer_name": self._printer_name,
                                        "alert_type": "stall",
                                        "completion": job.completion,
                                        "stall_duration_seconds": stall_duration,
                                        "elapsed_seconds": round(elapsed, 1),
                                    },
                                    source="watch_print",
                                )
                            except Exception as exc:
                                _logger.debug("Failed to publish stall vision alert: %s", exc)
                        result = {
                            "success": True,
                            "watch_id": self._watch_id,
                            "outcome": "stalled",
                            "elapsed_seconds": round(elapsed, 1),
                            "stall_duration_seconds": stall_duration,
                            "stalled_at_completion": job.completion,
                            "progress_log": list(self._progress_log[-20:]),
                            "snapshots": list(self._snapshots),
                            "snapshot_failures": self._snapshot_failures,
                            "final_state": state.to_dict(),
                            "message": (
                                f"Print appears stalled at {job.completion:.1f}% "
                                f"for {stall_duration:.0f} seconds. "
                                "Consider checking the printer or cancelling the print."
                            ),
                        }
                        self._finish(result)
                        return

                # Check terminal states
                if state.state == PrinterStatus.IDLE and elapsed > 30:
                    result = {
                        "success": True,
                        "watch_id": self._watch_id,
                        "outcome": "completed",
                        "elapsed_seconds": round(elapsed, 1),
                        "progress_log": list(self._progress_log[-20:]),
                        "snapshots": list(self._snapshots),
                        "snapshot_failures": self._snapshot_failures,
                        "final_state": state.to_dict(),
                    }
                    self._finish(result)
                    return

                if state.state in (PrinterStatus.ERROR, PrinterStatus.OFFLINE):
                    if self._event_bus is not None:
                        try:
                            self._event_bus.publish(
                                EventType.VISION_ALERT,
                                {
                                    "printer_name": self._printer_name,
                                    "alert_type": "printer_state",
                                    "state": state.state.value,
                                    "completion": job.completion,
                                    "elapsed_seconds": round(elapsed, 1),
                                },
                                source="vision",
                            )
                        except Exception as exc:
                            _logger.debug("Failed to publish printer state vision alert: %s", exc)
                    result = {
                        "success": True,
                        "watch_id": self._watch_id,
                        "outcome": "failed",
                        "elapsed_seconds": round(elapsed, 1),
                        "progress_log": list(self._progress_log[-20:]),
                        "snapshots": list(self._snapshots),
                        "snapshot_failures": self._snapshot_failures,
                        "final_state": state.to_dict(),
                        "error": f"Printer entered {state.state.value} state",
                    }
                    self._finish(result)
                    return

                if state.state == PrinterStatus.PAUSED:
                    result = {
                        "success": True,
                        "watch_id": self._watch_id,
                        "outcome": "paused",
                        "elapsed_seconds": round(elapsed, 1),
                        "progress_log": list(self._progress_log[-20:]),
                        "snapshots": list(self._snapshots),
                        "snapshot_failures": self._snapshot_failures,
                        "final_state": state.to_dict(),
                        "message": (
                            "Print is paused. Call resume_print to continue, or cancel_print to abort."
                        ),
                    }
                    self._finish(result)
                    return

                if state.state == PrinterStatus.CANCELLING:
                    result = {
                        "success": True,
                        "watch_id": self._watch_id,
                        "outcome": "cancelling",
                        "elapsed_seconds": round(elapsed, 1),
                        "progress_log": list(self._progress_log[-20:]),
                        "snapshots": list(self._snapshots),
                        "snapshot_failures": self._snapshot_failures,
                        "final_state": state.to_dict(),
                    }
                    self._finish(result)
                    return

                # Snapshot capture
                now = time.time()
                if can_snap and (now - last_snapshot_time) >= self._snapshot_interval:
                    try:
                        image_data = adapter.get_snapshot()
                        if image_data and len(image_data) > 100:
                            import base64

                            phase = _detect_phase(job.completion)
                            snap = {
                                "captured_at": now,
                                "completion_percent": job.completion,
                                "print_phase": phase,
                                "image_base64": base64.b64encode(image_data).decode("ascii"),
                            }

                            # Persist to disk + DB when save_to_disk is enabled
                            if self._save_to_disk and self._save_dir is not None:
                                try:
                                    from kiln.persistence import get_db

                                    os.makedirs(self._save_dir, exist_ok=True)
                                    frame_idx = len(self._snapshots)
                                    fpath = os.path.join(
                                        self._save_dir, f"frame_{frame_idx:04d}.jpg"
                                    )
                                    with open(fpath, "wb") as f:
                                        f.write(image_data)
                                    snap["saved_path"] = fpath
                                    get_db().save_snapshot(
                                        printer_name=self._printer_name,
                                        image_path=fpath,
                                        job_id=self._watch_id,
                                        phase=phase,
                                        image_size_bytes=len(image_data),
                                        completion_pct=job.completion,
                                    )
                                except Exception:
                                    _logger.debug(
                                        "Failed to persist snapshot to disk/DB",
                                        exc_info=True,
                                    )

                            with self._lock:
                                self._snapshots.append(snap)
                            if self._event_bus is not None:
                                try:
                                    self._event_bus.publish(
                                        EventType.VISION_CHECK,
                                        {
                                            "printer_name": self._printer_name,
                                            "completion": job.completion,
                                            "phase": phase,
                                            "snapshot_index": len(self._snapshots),
                                        },
                                        source="vision",
                                    )
                                except Exception as exc:
                                    _logger.debug(
                                        "Failed to publish vision check event: %s", exc
                                    )
                        else:
                            with self._lock:
                                self._snapshot_failures += 1
                    except Exception as exc:
                        _logger.debug(
                            "Failed to capture snapshot in print watcher: %s", exc
                        )
                        with self._lock:
                            self._snapshot_failures += 1
                    last_snapshot_time = now

                # Return batch when enough snapshots accumulated
                with self._lock:
                    snap_count = len(self._snapshots)
                if snap_count >= self._max_snapshots:
                    result = {
                        "success": True,
                        "watch_id": self._watch_id,
                        "outcome": "snapshot_check",
                        "elapsed_seconds": round(time.time() - self._start_time, 1),
                        "progress_log": list(self._progress_log[-20:]),
                        "snapshots": list(self._snapshots),
                        "snapshot_failures": self._snapshot_failures,
                        "final_state": state.to_dict(),
                        "message": (
                            f"Captured {snap_count} snapshots. "
                            "Review them for print quality issues. "
                            "Call pause_print or cancel_print if problems are detected, "
                            "then call watch_print again to continue monitoring."
                        ),
                    }
                    self._finish(result)
                    return

                # Wait using the stop event so stop() can wake us
                self._stop_event.wait(self._poll_interval)

        except Exception as exc:
            _logger.exception("Error in print watcher %s", self._watch_id)
            self._finish(
                {
                    "success": False,
                    "watch_id": self._watch_id,
                    "outcome": "error",
                    "error": str(exc),
                    "elapsed_seconds": round(time.time() - self._start_time, 1),
                    "progress_log": list(self._progress_log[-20:]),
                    "snapshots": list(self._snapshots),
                    "snapshot_failures": self._snapshot_failures,
                }
            )


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------


class _MonitoringToolsPlugin:
    """Print monitoring tools — vision, background watchers, first-layer checks.

    Tools:
        - monitor_print_vision
        - watch_print
        - watch_print_status
        - stop_watch_print
        - start_monitored_print
        - first_layer_status
    """

    @property
    def name(self) -> str:
        return "monitoring_tools"

    @property
    def description(self) -> str:
        return "Print monitoring tools (vision, background watcher, first-layer)"

    def register(self, mcp: Any) -> None:  # noqa: PLR0915
        """Register monitoring tools with the MCP server."""

        # Per-plugin state registries (keyed by watch_id / monitor_id)
        _watchers: dict[str, _PrintWatcher] = {}
        _first_layer_monitors: dict[str, Any] = {}

        @mcp.tool()
        def monitor_print_vision(
            printer_name: str | None = None,
            include_snapshot: bool = True,
            save_snapshot: str | None = None,
            failure_type: str | None = None,
            failure_confidence: float | None = None,
            auto_pause: bool | None = None,
        ) -> dict:
            """Capture a snapshot and printer state for visual inspection of an in-progress print.

            Returns the webcam image alongside structured metadata (temperatures,
            progress, print phase, failure hints) so the agent can visually assess
            print quality and decide whether to intervene.

            This is the *during-print* counterpart to ``validate_print_quality``
            (which runs after a print finishes).

            Args:
                printer_name: Target printer.  Omit for the default printer.
                include_snapshot: Whether to capture a webcam snapshot (default True).
                save_snapshot: Optional path to save the snapshot image.
                failure_type: Optional detected failure type (e.g. "spaghetti",
                    "layer_shift", "warping").  Reported by the agent after visual
                    inspection of a previous snapshot.
                failure_confidence: Confidence score (0.0-1.0) of the failure detection.
                auto_pause: If True, automatically pause the print when a failure is
                    detected with confidence >= 0.8.  Defaults to the value of the
                    ``KILN_VISION_AUTO_PAUSE`` environment variable (default False).
            """
            import kiln.server as _srv
            from kiln.events import EventType
            from kiln.printers import PrinterError, PrinterNotFoundError, PrinterStatus

            if err := _srv._check_auth("monitoring"):
                return err
            try:
                adapter = (
                    _srv._registry.get(printer_name) if printer_name else _srv._get_adapter()
                )
                state = adapter.get_state()
                job = adapter.get_job()
                is_printing = state.state == PrinterStatus.PRINTING
                phase = _detect_phase(job.completion)
                hints = _PHASE_HINTS.get(phase, _PHASE_HINTS["unknown"])

                result: dict[str, Any] = {
                    "success": True,
                    "printer_state": state.to_dict(),
                    "job_progress": job.to_dict(),
                    "monitoring_context": {
                        "is_printing": is_printing,
                        "print_phase": phase,
                        "completion_percent": job.completion,
                        "failure_hints": hints,
                    },
                    "actions_available": {
                        "pause": "pause_print",
                        "cancel": "cancel_print",
                        "annotate": "annotate_print",
                    },
                }

                # Snapshot capture — respect can_snapshot capability
                if include_snapshot and not getattr(adapter.capabilities, "can_snapshot", False):
                    result["snapshot"] = {"available": False, "reason": "no_capability"}
                elif include_snapshot:
                    try:
                        image_data = adapter.get_snapshot()
                        if image_data and len(image_data) > 100:
                            import base64

                            snap: dict[str, Any] = {
                                "available": True,
                                "size_bytes": len(image_data),
                                "captured_at": time.time(),
                            }
                            if save_snapshot:
                                _safe = os.path.abspath(save_snapshot)
                                _home = os.path.expanduser("~")
                                _tmpdir = os.path.realpath(tempfile.gettempdir())
                                if not (_safe.startswith(_home) or _safe.startswith(_tmpdir)):
                                    return _srv._error_dict(
                                        "save_snapshot path must be under home directory or temp directory.",
                                        code="VALIDATION_ERROR",
                                    )
                                os.makedirs(os.path.dirname(_safe) or ".", exist_ok=True)
                                with open(_safe, "wb") as f:
                                    f.write(image_data)
                                snap["saved_to"] = _safe
                            else:
                                snap["image_base64"] = base64.b64encode(image_data).decode("ascii")
                            result["snapshot"] = snap
                        else:
                            result["snapshot"] = {"available": False}
                    except Exception as exc:
                        _logger.debug(
                            "Failed to capture snapshot for vision monitoring: %s", exc
                        )
                        result["snapshot"] = {"available": False}
                else:
                    result["snapshot"] = {"available": False, "reason": "not_requested"}

                # -- Auto-pause on failure detection --------------------------------
                _auto_pause = auto_pause
                if _auto_pause is None:
                    _auto_pause = os.environ.get("KILN_VISION_AUTO_PAUSE", "").lower() in (
                        "1",
                        "true",
                        "yes",
                    )

                auto_paused = False
                if failure_type and failure_confidence is not None:
                    result["failure_detection"] = {
                        "type": failure_type,
                        "confidence": failure_confidence,
                        "auto_pause_enabled": _auto_pause,
                    }
                    if _auto_pause and failure_confidence >= 0.8 and is_printing:
                        try:
                            adapter.pause_print()
                            auto_paused = True
                            result["failure_detection"]["auto_paused"] = True
                            result["failure_detection"]["message"] = (
                                f"Print auto-paused due to detected {failure_type} "
                                f"(confidence: {failure_confidence:.0%})"
                            )
                            _logger.warning(
                                "Vision auto-pause triggered: %s (confidence=%.2f) on printer %s",
                                failure_type,
                                failure_confidence,
                                printer_name or "default",
                            )
                        except Exception as pause_exc:
                            result["failure_detection"]["auto_pause_error"] = str(pause_exc)
                            _logger.error(
                                "Vision auto-pause failed: %s on printer %s",
                                pause_exc,
                                printer_name or "default",
                            )

                # Publish vision check event
                _srv._event_bus.publish(
                    EventType.VISION_CHECK,
                    {
                        "printer_name": printer_name or "default",
                        "completion": job.completion,
                        "phase": phase,
                        "snapshot_captured": result["snapshot"].get("available", False),
                        "auto_paused": auto_paused,
                    },
                    source="vision",
                )

                if auto_paused:
                    _srv._event_bus.publish(
                        EventType.VISION_ALERT,
                        {
                            "printer_name": printer_name or "default",
                            "alert_type": "auto_pause",
                            "failure_type": failure_type,
                            "failure_confidence": failure_confidence,
                            "completion": job.completion,
                        },
                        source="vision",
                    )

                return result

            except PrinterNotFoundError:
                return _srv._error_dict(
                    f"Printer {printer_name!r} not found.", code="NOT_FOUND"
                )
            except (PrinterError, RuntimeError) as exc:
                return _srv._error_dict(
                    f"Failed to run vision monitoring: {exc}. Check that the printer is online and has a webcam."
                )
            except Exception as exc:
                _logger.exception("Unexpected error in monitor_print_vision")
                return _srv._error_dict(
                    f"Unexpected error in monitor_print_vision: {exc}", code="INTERNAL_ERROR"
                )

        @mcp.tool()
        def watch_print(
            printer_name: str | None = None,
            snapshot_interval: int = 60,
            max_snapshots: int = 5,
            timeout: int = 7200,
            poll_interval: int = 15,
            stall_timeout: int = 600,
            save_to_disk: bool = False,
        ) -> dict:
            """Start background monitoring of an in-progress print.

            Launches a background thread that polls the printer state every
            *poll_interval* seconds and captures webcam snapshots every
            *snapshot_interval* seconds.  Returns immediately with a
            ``watch_id`` that can be used with ``watch_print_status`` and
            ``stop_watch_print``.

            The watcher finishes automatically when:

            1. **Print terminal state** — completed, failed, cancelled, or offline.
            2. **Snapshot batch ready** — *max_snapshots* images collected.
            3. **Timeout** — the print has not finished within *timeout* seconds.

            Args:
                printer_name: Target printer.  Omit for the default printer.
                snapshot_interval: Seconds between snapshot captures (default 60).
                max_snapshots: Return after this many snapshots (default 5).
                timeout: Maximum seconds to monitor (default 7200 = 2 hours).
                poll_interval: Seconds between state polls (default 15).
                stall_timeout: Seconds of zero progress before declaring stall
                    (default 600 = 10 min).  Set to 0 to disable stall detection.
                save_to_disk: Save snapshots as JPEG files to
                    ``~/.kiln/timelapses/<watch_id>/`` and persist metadata to the
                    database.  Use ``list_snapshots`` to query saved frames after
                    the print completes (default False).
            """
            import kiln.server as _srv
            from kiln.printers import PrinterError, PrinterNotFoundError, PrinterStatus

            if err := _srv._check_auth("monitoring"):
                return err
            try:
                adapter = (
                    _srv._registry.get(printer_name) if printer_name else _srv._get_adapter()
                )

                # Early exit: if printer is idle with no active job, don't start
                initial_state = adapter.get_state()
                initial_job = adapter.get_job()
                if initial_state.state == PrinterStatus.IDLE and initial_job.completion is None:
                    return {
                        "success": True,
                        "outcome": "no_active_print",
                        "elapsed_seconds": 0,
                        "progress_log": [],
                        "snapshots": [],
                        "final_state": initial_state.to_dict(),
                        "message": "Printer is idle with no active print job.",
                    }

                watch_id = secrets.token_hex(6)
                watcher = _PrintWatcher(
                    watch_id,
                    adapter,
                    printer_name or "default",
                    snapshot_interval=snapshot_interval,
                    max_snapshots=max_snapshots,
                    timeout=timeout,
                    poll_interval=poll_interval,
                    event_bus=_srv._event_bus,
                    stall_timeout=stall_timeout,
                    save_to_disk=save_to_disk,
                )
                _watchers[watch_id] = watcher
                watcher.start()

                resp: dict[str, Any] = {
                    "success": True,
                    "watch_id": watch_id,
                    "status": "started",
                    "printer_name": printer_name or "default",
                    "snapshot_interval": snapshot_interval,
                    "max_snapshots": max_snapshots,
                    "timeout": timeout,
                    "poll_interval": poll_interval,
                    "stall_timeout": stall_timeout,
                    "save_to_disk": save_to_disk,
                    "message": (
                        f"Background watcher started (id={watch_id}). "
                        "Use watch_print_status to check progress, "
                        "or stop_watch_print to cancel."
                    ),
                }
                if save_to_disk and watcher._save_dir:
                    resp["save_dir"] = watcher._save_dir
                return resp

            except PrinterNotFoundError:
                return _srv._error_dict(
                    f"Printer {printer_name!r} not found.", code="NOT_FOUND"
                )
            except (PrinterError, RuntimeError) as exc:
                return _srv._error_dict(
                    f"Failed to start print watcher: {exc}. Check that the printer is online."
                )
            except Exception as exc:
                _logger.exception("Unexpected error in watch_print")
                return _srv._error_dict(
                    f"Unexpected error in watch_print: {exc}", code="INTERNAL_ERROR"
                )

        @mcp.tool()
        def watch_print_status(watch_id: str) -> dict:
            """Check the current status of a background print watcher.

            Returns progress, collected snapshots, and whether the watcher
            has finished.

            Args:
                watch_id: The watcher ID returned by ``watch_print``.
            """
            import kiln.server as _srv

            if err := _srv._check_auth("monitoring"):
                return err
            watcher = _watchers.get(watch_id)
            if watcher is None:
                return _srv._error_dict(
                    f"No active watcher with id {watch_id!r}. It may have already been stopped or never existed.",
                    code="NOT_FOUND",
                )
            return {"success": True, **watcher.status()}

        @mcp.tool()
        def stop_watch_print(watch_id: str) -> dict:
            """Stop a background print watcher and return its final state.

            Signals the watcher thread to exit and removes it from the
            active watchers registry.

            Args:
                watch_id: The watcher ID returned by ``watch_print``.
            """
            import kiln.server as _srv

            if err := _srv._check_auth("monitoring"):
                return err
            watcher = _watchers.pop(watch_id, None)
            if watcher is None:
                return _srv._error_dict(
                    f"No active watcher with id {watch_id!r}. It may have already been stopped or never existed.",
                    code="NOT_FOUND",
                )
            result = watcher.stop()
            return {"success": True, **result}

        @mcp.tool()
        def start_monitored_print(
            file_name: str,
            printer_name: str | None = None,
            first_layer_delay: int = 120,
            first_layer_checks: int = 3,
            first_layer_interval: int = 60,
            auto_pause: bool = True,
        ) -> dict:
            """Start a print and automatically monitor the first layer.

            This is the recommended way to start prints autonomously. It combines
            start_print with first-layer monitoring in a single operation:

            1. Starts the print
            2. Waits for the configured delay (default 2 minutes)
            3. Captures snapshots during first layers
            4. Returns snapshots for you to visually inspect
            5. Optionally auto-pauses if you report a failure

            Use this instead of start_print when operating autonomously (Level 1/2)
            to satisfy the first-layer monitoring safety requirement.

            Args:
                file_name: Name of the file to print (must exist on printer).
                printer_name: Target printer. Omit for default.
                first_layer_delay: Seconds to wait before first snapshot (default 120).
                first_layer_checks: Number of first-layer snapshots to capture (default 3).
                first_layer_interval: Seconds between snapshots (default 60).
                auto_pause: Auto-pause if snapshot analysis detects failure (default True).
            """
            import kiln.server as _srv
            from kiln.printers import PrinterError, PrinterNotFoundError

            if err := _srv._check_auth("print"):
                return err
            if err := _srv._check_rate_limit("start_monitored_print"):
                return err
            if conf := _srv._check_confirmation("start_monitored_print", {"file_name": file_name}):
                return conf
            try:
                adapter = (
                    _srv._registry.get(printer_name) if printer_name else _srv._get_adapter()
                )

                # -- Automatic pre-flight safety gate (mandatory) --
                pf = _srv.preflight_check()
                if not pf.get("ready", False):
                    _srv._audit(
                        "start_monitored_print",
                        "preflight_failed",
                        details={
                            "file": file_name,
                            "summary": pf.get("summary", ""),
                        },
                    )
                    result = _srv._error_dict(
                        pf.get("summary", "Pre-flight checks failed"),
                        code="PREFLIGHT_FAILED",
                    )
                    result["preflight"] = pf
                    return result

                # Start the print
                print_result = adapter.start_print(file_name)
                _srv._heater_watchdog.notify_print_started()
                _srv._audit(
                    "start_monitored_print", "print_started", details={"file": file_name}
                )

                # Set up first-layer monitoring in background
                from kiln.print_monitor import FirstLayerMonitor, MonitorPolicy

                monitor_id = secrets.token_hex(6)
                policy = MonitorPolicy(
                    delay_seconds=first_layer_delay,
                    num_checks=first_layer_checks,
                    interval_seconds=first_layer_interval,
                    auto_pause=auto_pause,
                )
                monitor = FirstLayerMonitor(
                    adapter,
                    policy=policy,
                    monitor_id=monitor_id,
                )
                _first_layer_monitors[monitor_id] = monitor
                monitor.start()

                return {
                    "success": True,
                    "print_result": print_result.to_dict(),
                    "monitor_id": monitor_id,
                    "monitor_status": "started",
                    "first_layer_policy": policy.to_dict(),
                    "message": (
                        f"Print started and first-layer monitor launched (id={monitor_id}). "
                        "Use watch_print_status or check back after "
                        f"~{first_layer_delay + first_layer_checks * first_layer_interval}s "
                        "for first-layer snapshots."
                    ),
                }
            except PrinterNotFoundError:
                return _srv._error_dict(
                    f"Printer {printer_name!r} not found.", code="NOT_FOUND"
                )
            except (PrinterError, RuntimeError) as exc:
                return _srv._error_dict(
                    f"Failed to start monitored print: {exc}. Check that the printer is online and idle."
                )
            except Exception as exc:
                _logger.exception("Unexpected error in start_monitored_print")
                return _srv._error_dict(
                    f"Unexpected error in start_monitored_print: {exc}", code="INTERNAL_ERROR"
                )

        @mcp.tool()
        def first_layer_status(monitor_id: str) -> dict:
            """Check the status of a first-layer monitor.

            Returns the current monitoring state, including any captured snapshots
            once monitoring is complete.

            Args:
                monitor_id: The monitor ID returned by ``start_monitored_print``.
            """
            import kiln.server as _srv

            if err := _srv._check_auth("monitoring"):
                return err
            monitor = _first_layer_monitors.get(monitor_id)
            if monitor is None:
                return _srv._error_dict(
                    f"No active first-layer monitor with id {monitor_id!r}. It may have already completed or never existed.",
                    code="NOT_FOUND",
                )
            result = monitor.result()
            if result is not None:
                # Clean up completed monitors
                _first_layer_monitors.pop(monitor_id, None)
                return {"success": True, "monitor_id": monitor_id, "finished": True, **result.to_dict()}
            return {
                "success": True,
                "monitor_id": monitor_id,
                "finished": False,
                "message": "First-layer monitoring still in progress.",
            }

        _logger.debug("Registered monitoring tools")


plugin = _MonitoringToolsPlugin()
