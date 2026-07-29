"""Tests for the enriched VISION_FRAME_CAPTURED event payload from _PrintWatcher.

The watcher captures camera frames during every snapshot cycle.  This
test file pins the contract: every published VISION_FRAME_CAPTURED event carries
raw observability data — snapshot bytes, MD5 hash, the camera-changed
bit, the consecutive-static-frame count, and the seconds since
telemetry last advanced — so downstream subscribers (e.g. an out-of-
process vision detector or a training-corpus recorder) can consume the
stream without re-fetching from the printer adapter.

The watcher itself makes NO decisions from these values.  Threshold
choices ("5 static frames means a frozen feed", "telemetry stuck for
3× poll_interval means spaghetti") live elsewhere — the watcher only
measures and publishes.
"""

from __future__ import annotations

import base64
import hashlib
import threading
import time
from unittest.mock import MagicMock

import pytest

# Patch PrinterNotFoundError into kiln.printers so the monitoring_tools
# plugin imports succeed.  Mirrors test_monitoring_tools_parity.py.
import kiln.printers as _printers_pkg

if not hasattr(_printers_pkg, "PrinterNotFoundError"):
    from kiln.registry import PrinterNotFoundError as _PNFE

    _printers_pkg.PrinterNotFoundError = _PNFE  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(
    *,
    completion_sequence: list[float],
    snapshot_sequence: list[bytes],
    state: str = "printing",
):
    """Build a mock adapter that walks scripted telemetry + snapshot lists."""
    from kiln.printers import PrinterStatus

    adapter = MagicMock()
    adapter.capabilities = MagicMock()
    adapter.capabilities.can_snapshot = True

    state_obj = MagicMock()
    state_obj.state = PrinterStatus(state)
    state_obj.to_dict.return_value = {"state": state}
    adapter.get_state.return_value = state_obj

    completion_iter = iter(completion_sequence)
    last_completion = [completion_sequence[-1] if completion_sequence else None]

    def _get_job() -> object:
        try:
            c = next(completion_iter)
            last_completion[0] = c
        except StopIteration:
            c = last_completion[0]
        job = MagicMock()
        job.completion = c
        job.to_dict.return_value = {
            "completion": c,
            "print_time_seconds": 3600,
            "print_time_left_seconds": 1800,
        }
        return job

    adapter.get_job.side_effect = _get_job

    snapshot_iter = iter(snapshot_sequence)
    last_snapshot = [snapshot_sequence[-1] if snapshot_sequence else b""]

    def _get_snapshot() -> bytes:
        try:
            s = next(snapshot_iter)
            last_snapshot[0] = s
        except StopIteration:
            s = last_snapshot[0]
        return s

    adapter.get_snapshot.side_effect = _get_snapshot
    return adapter


def _collect_vision_frames(bus) -> list[dict]:
    """Return all VISION_FRAME_CAPTURED events the bus has received.

    Copies the history under the bus lock so this is safe to call while
    the watcher thread is still publishing.
    """
    from kiln.events import EventType

    with bus._lock:
        history = list(bus._history)
    return [e for e in history if e.type == EventType.VISION_FRAME_CAPTURED]


def _run_watcher_until_frames(
    adapter,
    bus,
    *,
    min_frames: int,
    poll_interval: float = 0.05,
    snapshot_interval: float = 0.05,
    stall_timeout: int = 0,
    max_snapshots: int = 10000,
    timeout: float = 30.0,
) -> list[dict]:
    """Run a _PrintWatcher until it has published *min_frames* frames.

    Every assertion in this file is about the CONTENT of the published
    VISION_FRAME_CAPTURED sequence, never about how long the watcher ran.
    So the watcher is driven by the observed event count rather than by a
    fixed ``time.sleep`` — a loaded CI runner gets through fewer poll ticks
    per second, and a wall-clock bound silently turns that into a short
    frame sequence and a spurious failure.  *timeout* is a generous
    backstop for a genuinely hung watcher, not the expected duration:
    on an unloaded machine this returns in roughly
    ``min_frames * poll_interval`` seconds.

    Returns every frame collected once the watcher has stopped, which may
    be more than *min_frames* — the thread can publish another frame or
    two before it observes the stop event.  Assertions should therefore
    anchor on the sequence itself, not on ``frames[-1]``.
    """
    from kiln.plugins.monitoring_tools import _PrintWatcher

    watcher = _PrintWatcher(
        watch_id="enrich-test",
        adapter=adapter,
        printer_name="enrich-printer",
        poll_interval=poll_interval,
        snapshot_interval=snapshot_interval,
        max_snapshots=max_snapshots,
        timeout=60,
        event_bus=bus,
        stall_timeout=stall_timeout,
    )
    watcher.start()
    deadline = time.monotonic() + timeout
    try:
        while len(_collect_vision_frames(bus)) < min_frames:
            if time.monotonic() >= deadline:
                collected = len(_collect_vision_frames(bus))
                pytest.fail(
                    f"Watcher published {collected} VISION_FRAME_CAPTURED "
                    f"event(s) in {timeout:g}s; expected at least {min_frames}."
                )
            time.sleep(0.01)
    finally:
        watcher.stop()
    return _collect_vision_frames(bus)


# ---------------------------------------------------------------------------
# Payload shape — every required field is present
# ---------------------------------------------------------------------------


class TestVisionCheckPayloadShape:
    """Pin the contract: every VISION_FRAME_CAPTURED carries the raw signals
    downstream subscribers need to run their own detectors."""

    REQUIRED_FIELDS = [
        "printer_name",
        "watch_id",
        "completion",
        "phase",
        "snapshot_index",
        "snapshot_b64",
        "frame_hash",
        "camera_changed",
        "consecutive_static_frames",
        "time_since_last_progress_seconds",
    ]

    def test_every_required_field_is_present(self) -> None:
        from kiln.events import EventBus

        bus = EventBus()
        adapter = _make_adapter(
            completion_sequence=[50.0, 51.0, 52.0, 53.0],
            snapshot_sequence=[bytes([i] * 200) for i in range(20)],
        )

        events = _run_watcher_until_frames(adapter, bus, min_frames=2)

        assert len(events) >= 2, "Expected multiple VISION_FRAME_CAPTURED events"
        for ev in events:
            data = ev.data
            for field in self.REQUIRED_FIELDS:
                assert field in data, (
                    f"VISION_FRAME_CAPTURED payload missing required field {field!r}"
                )

    def test_snapshot_b64_is_decodable_and_matches_hash(self) -> None:
        """Round-trip: snapshot_b64 decodes to bytes whose MD5 matches frame_hash."""
        from kiln.events import EventBus

        bus = EventBus()
        # Use a single distinctive snapshot blob so we can verify the
        # exact bytes survive the encode/decode round trip.
        distinct_blob = b"\xff\xd8\xff\xe0" + b"x" * 500  # JPEG SOI + filler
        adapter = _make_adapter(
            completion_sequence=[50.0, 51.0, 52.0],
            snapshot_sequence=[distinct_blob] * 20,
        )

        events = _run_watcher_until_frames(adapter, bus, min_frames=1)

        assert events, "Expected at least one VISION_FRAME_CAPTURED"
        ev = events[0]
        decoded = base64.b64decode(ev.data["snapshot_b64"])
        assert decoded == distinct_blob, (
            "snapshot_b64 should encode the exact bytes captured from the adapter"
        )
        expected_hash = hashlib.md5(distinct_blob).hexdigest()  # noqa: S324
        assert ev.data["frame_hash"] == expected_hash, (
            "frame_hash should be the MD5 of the snapshot bytes"
        )

    def test_watch_id_in_payload(self) -> None:
        """watch_id is included so subscribers can correlate events per watcher."""
        from kiln.events import EventBus

        bus = EventBus()
        adapter = _make_adapter(
            completion_sequence=[50.0, 51.0],
            snapshot_sequence=[b"x" * 200, b"y" * 200, b"z" * 200],
        )

        events = _run_watcher_until_frames(adapter, bus, min_frames=1)

        assert events
        assert all(ev.data["watch_id"] == "enrich-test" for ev in events)


# ---------------------------------------------------------------------------
# camera_changed semantics
# ---------------------------------------------------------------------------


class TestCameraChangedBit:
    """camera_changed reflects whether THIS frame differs from the prior one."""

    def test_first_event_camera_not_changed(self) -> None:
        """Very first snapshot has nothing to compare to → camera_changed=False."""
        from kiln.events import EventBus

        bus = EventBus()
        adapter = _make_adapter(
            completion_sequence=[50.0] * 10,
            snapshot_sequence=[bytes([i] * 200) for i in range(20)],
        )

        events = _run_watcher_until_frames(adapter, bus, min_frames=1)

        assert events
        assert events[0].data["camera_changed"] is False, (
            "First VISION_FRAME_CAPTURED should report camera_changed=False (no prior frame)"
        )

    def test_subsequent_event_camera_changed_when_bytes_differ(self) -> None:
        from kiln.events import EventBus

        bus = EventBus()
        adapter = _make_adapter(
            completion_sequence=[50.0] * 20,
            snapshot_sequence=[bytes([i] * 200) for i in range(20)],
        )

        events = _run_watcher_until_frames(adapter, bus, min_frames=2)

        assert len(events) >= 2
        # Frame 2+: bytes differ, hash differs, camera_changed=True
        assert events[1].data["camera_changed"] is True

    def test_subsequent_event_camera_not_changed_when_bytes_identical(self) -> None:
        from kiln.events import EventBus

        bus = EventBus()
        identical = b"static" * 100
        adapter = _make_adapter(
            completion_sequence=[50.0, 51.0, 52.0, 53.0, 54.0] * 5,
            snapshot_sequence=[identical] * 30,
        )

        events = _run_watcher_until_frames(adapter, bus, min_frames=3)

        assert len(events) >= 3
        # First event: no prior frame, camera_changed=False
        # Subsequent events: hash matches prior, camera_changed=False
        for ev in events[1:]:
            assert ev.data["camera_changed"] is False


# ---------------------------------------------------------------------------
# consecutive_static_frames semantics
# ---------------------------------------------------------------------------


class TestConsecutiveStaticFrames:
    """Counter increments on identical hash, resets on different hash, and
    doesn't count the first capture as static."""

    def test_first_capture_counter_is_zero(self) -> None:
        from kiln.events import EventBus

        bus = EventBus()
        adapter = _make_adapter(
            completion_sequence=[50.0] * 10,
            snapshot_sequence=[bytes([i] * 200) for i in range(20)],
        )

        events = _run_watcher_until_frames(adapter, bus, min_frames=1)

        assert events
        assert events[0].data["consecutive_static_frames"] == 0

    def test_counter_increments_on_identical_frames(self) -> None:
        from kiln.events import EventBus

        bus = EventBus()
        identical = b"frozen" * 100
        adapter = _make_adapter(
            completion_sequence=[float(i) for i in range(20, 100)],
            snapshot_sequence=[identical] * 40,
        )

        events = _run_watcher_until_frames(adapter, bus, min_frames=3)

        # Build a list of consecutive_static_frames values across events.
        counter_values = [e.data["consecutive_static_frames"] for e in events]
        # Frame 0: 0 (first frame, no comparison)
        # Frame 1: 1 (same as frame 0)
        # Frame 2: 2 (same as frame 1)
        # ...
        assert counter_values[0] == 0
        assert len(counter_values) >= 3
        # The counter should strictly increase for the first several
        # frames since all are identical.
        assert counter_values[1] == 1
        assert counter_values[2] == 2

    def test_counter_resets_on_camera_change(self) -> None:
        from kiln.events import EventBus

        bus = EventBus()
        # 4 identical, then unique frames.
        identical = b"static" * 100
        seq = [identical] * 4 + [bytes([0x30 + i] * 200) for i in range(20)]
        adapter = _make_adapter(
            completion_sequence=[float(i) for i in range(20, 100)],
            snapshot_sequence=seq,
        )

        # 4 static frames, then the first differing one — the tick that
        # must reset the counter.
        events = _run_watcher_until_frames(adapter, bus, min_frames=5)

        counter_values = [e.data["consecutive_static_frames"] for e in events]
        changed_flags = [e.data["camera_changed"] for e in events]

        # Anchor on the first frame the watcher reported as changed rather
        # than on the last event collected.  The last event is whichever
        # frame the watcher happened to reach before stopping, and the
        # scripted snapshot list repeats its final entry once exhausted —
        # so a run that is short OR long enough lands on a static frame and
        # says nothing about whether the reset happened.
        change_idx = next((i for i, changed in enumerate(changed_flags) if changed), None)
        assert change_idx is not None, (
            f"Expected a camera change once the static run ended; "
            f"got camera_changed={changed_flags}"
        )
        # The counter climbed through the static run that preceded it...
        assert max(counter_values[:change_idx]) >= 2, (
            f"Counter should have accumulated past 1 during static run; "
            f"got {counter_values}"
        )
        # ...and reset to 0 on the frame whose bytes differed.
        assert counter_values[change_idx] == 0, (
            f"Counter should reset to 0 on the first changed frame "
            f"(index {change_idx}); got {counter_values}"
        )


# ---------------------------------------------------------------------------
# time_since_last_progress_seconds semantics
# ---------------------------------------------------------------------------


class TestTimeSinceLastProgress:
    """The field reports seconds since telemetry last advanced.  Resets
    each time job.completion changes by >0.1."""

    def test_field_is_non_negative_number(self) -> None:
        from kiln.events import EventBus

        bus = EventBus()
        adapter = _make_adapter(
            completion_sequence=[50.0] * 10,
            snapshot_sequence=[bytes([i] * 200) for i in range(20)],
        )

        events = _run_watcher_until_frames(adapter, bus, min_frames=1)

        assert events
        for ev in events:
            value = ev.data["time_since_last_progress_seconds"]
            assert isinstance(value, (int, float))
            assert value >= 0

    def test_value_grows_when_telemetry_stuck(self) -> None:
        """If completion never changes, time_since_last_progress should
        increase monotonically across successive events."""
        from kiln.events import EventBus

        bus = EventBus()
        adapter = _make_adapter(
            completion_sequence=[50.0] * 100,
            snapshot_sequence=[bytes([i] * 200) for i in range(100)],
        )

        # Six frames at a 0.05s poll interval span at least 0.25s, so the
        # 0.1s growth below is guaranteed by the frame count rather than by
        # the runner keeping up with a wall-clock budget.  A slow runner
        # only spreads the frames further apart, which makes the assertion
        # more true, never less.
        events = _run_watcher_until_frames(adapter, bus, min_frames=6)

        assert len(events) >= 3
        values = [e.data["time_since_last_progress_seconds"] for e in events]
        # Strictly monotonic increase (allow small float noise).
        assert values[-1] > values[0] + 0.1, (
            f"Expected time_since_last_progress to grow; got {values}"
        )


# ---------------------------------------------------------------------------
# No decisions in public Kiln — confirm absence of alert-bearing fields
# ---------------------------------------------------------------------------


class TestNoDecisionsInPublicWatcher:
    """The watcher publishes raw signals only.  Alert-bearing fields
    (alert_type, recommended_action, is_heartbeat, mismatch_duration_seconds)
    must NOT appear in VISION_FRAME_CAPTURED events from the watcher — those are
    downstream-detector responsibilities, not raw-stream responsibilities.
    """

    FORBIDDEN_FIELDS = [
        "alert_type",
        "recommended_action",
        "is_heartbeat",
        "mismatch_duration_seconds",
        "static_frame_count",  # legacy name for the same idea
    ]

    def test_no_alert_taxonomy_in_vision_check_payload(self) -> None:
        from kiln.events import EventBus

        bus = EventBus()
        adapter = _make_adapter(
            completion_sequence=[50.0] * 20,
            snapshot_sequence=[bytes([i] * 200) for i in range(20)],
        )

        events = _run_watcher_until_frames(adapter, bus, min_frames=1)

        assert events
        for ev in events:
            for forbidden in self.FORBIDDEN_FIELDS:
                assert forbidden not in ev.data, (
                    f"VISION_FRAME_CAPTURED payload should NOT carry decision field "
                    f"{forbidden!r} — that belongs to downstream detectors"
                )
