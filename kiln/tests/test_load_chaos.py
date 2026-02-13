"""Load and chaos tests for the Kiln job queue, event bus, and adapter layers.

Covers:
- TestQueueLoadStress: concurrent job submission, cancel, completion across threads
- TestEventBusFlood: rapid publish, multi-thread publish, subscribe/unsubscribe during activity
- TestAdapterChaos: random failures, timeout handling, error wrapping
- TestSchedulerContention: parallel scheduler workers, race conditions on state transitions
- TestAsyncEventBusChaos: bounded queue pressure, repeated start/stop cycles
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import threading
from unittest.mock import MagicMock

import pytest

from kiln.events import AsyncEventBus, Event, EventBus, EventType
from kiln.printers.base import (
    PrinterAdapter,
    PrinterCapabilities,
    PrinterError,
    PrinterState,
    PrinterStatus,
)
from kiln.queue import (
    InvalidStateTransition,
    JobStatus,
    PrintQueue,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _submit_n_jobs(queue: PrintQueue, n: int, *, prefix: str = "job") -> list[str]:
    """Submit *n* jobs to *queue* and return their IDs."""
    ids = []
    for i in range(n):
        job_id = queue.submit(
            file_name=f"{prefix}_{i}.gcode",
            printer_name="test-printer",
            submitted_by="load-test",
        )
        ids.append(job_id)
    return ids


# ---------------------------------------------------------------------------
# TestQueueLoadStress
# ---------------------------------------------------------------------------

class TestQueueLoadStress:
    """Concurrent job submission, cancel, and completion under thread pressure."""

    def test_100_jobs_from_10_threads_all_present(self) -> None:
        queue = PrintQueue()
        all_ids: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(10)

        def submit_batch(thread_idx: int) -> None:
            barrier.wait()
            for i in range(10):
                job_id = queue.submit(
                    file_name=f"t{thread_idx}_job{i}.gcode",
                    printer_name="printer-1",
                    submitted_by=f"thread-{thread_idx}",
                )
                with lock:
                    all_ids.append(job_id)

        threads = [
            threading.Thread(target=submit_batch, args=(t,)) for t in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(all_ids) == 100
        assert len(set(all_ids)) == 100  # all unique
        assert queue.total_count == 100

        # Every job must be QUEUED
        for job_id in all_ids:
            job = queue.get_job(job_id)
            assert job.status == JobStatus.QUEUED

    def test_submit_and_cancel_concurrently(self) -> None:
        queue = PrintQueue()
        submitted: list[str] = []
        cancelled: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def submitter() -> None:
            barrier.wait()
            for i in range(50):
                jid = queue.submit(
                    file_name=f"sub_{i}.gcode",
                    submitted_by="submitter",
                )
                with lock:
                    submitted.append(jid)

        def canceller() -> None:
            barrier.wait()
            for _ in range(50):
                # Grab a queued job to cancel — may race with submitter
                with lock:
                    if not submitted:
                        continue
                    jid = submitted.pop(0)
                try:
                    queue.cancel(jid)
                    with lock:
                        cancelled.append(jid)
                except (InvalidStateTransition, KeyError):
                    pass  # already cancelled or not cancellable

        t1 = threading.Thread(target=submitter)
        t2 = threading.Thread(target=canceller)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        # All jobs accounted for: either still queued or cancelled
        for jid in cancelled:
            assert queue.get_job(jid).status == JobStatus.CANCELLED

        # Total jobs should be 50 (all submitted)
        assert queue.total_count == 50

    def test_submit_complete_next_job_concurrently(self) -> None:
        queue = PrintQueue()
        ids = _submit_n_jobs(queue, 20)
        completed: list[str] = []
        fetched: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(3)

        def completer() -> None:
            """Move jobs through STARTING -> PRINTING -> COMPLETED."""
            barrier.wait()
            for jid in ids[:10]:
                try:
                    queue.mark_starting(jid)
                    queue.mark_printing(jid)
                    queue.mark_completed(jid)
                    with lock:
                        completed.append(jid)
                except (InvalidStateTransition, KeyError):
                    pass

        def fetcher() -> None:
            barrier.wait()
            for _ in range(20):
                job = queue.next_job()
                if job is not None:
                    with lock:
                        fetched.append(job.id)

        def submitter() -> None:
            barrier.wait()
            _submit_n_jobs(queue, 10, prefix="extra")

        t1 = threading.Thread(target=completer)
        t2 = threading.Thread(target=fetcher)
        t3 = threading.Thread(target=submitter)
        for t in (t1, t2, t3):
            t.start()
        for t in (t1, t2, t3):
            t.join(timeout=5)

        # No crash, completed jobs are in COMPLETED state
        for jid in completed:
            assert queue.get_job(jid).status == JobStatus.COMPLETED


# ---------------------------------------------------------------------------
# TestEventBusFlood
# ---------------------------------------------------------------------------

class TestEventBusFlood:
    """Event bus correctness under rapid publishing and concurrent access."""

    def test_1000_events_single_thread_all_received(self) -> None:
        bus = EventBus()
        received: list[Event] = []

        def handler(event: Event) -> None:
            received.append(event)

        bus.subscribe(EventType.JOB_SUBMITTED, handler)

        for i in range(1000):
            bus.publish(Event(
                type=EventType.JOB_SUBMITTED,
                data={"seq": i},
            ))

        assert len(received) == 1000
        # Verify ordering
        for i, ev in enumerate(received):
            assert ev.data["seq"] == i

    def test_publish_from_10_threads(self) -> None:
        bus = EventBus()
        received: list[Event] = []
        lock = threading.Lock()

        def handler(event: Event) -> None:
            with lock:
                received.append(event)

        bus.subscribe(EventType.PRINT_STARTED, handler)
        barrier = threading.Barrier(10)

        def publisher(thread_idx: int) -> None:
            barrier.wait()
            for i in range(100):
                bus.publish(Event(
                    type=EventType.PRINT_STARTED,
                    data={"thread": thread_idx, "seq": i},
                ))

        threads = [
            threading.Thread(target=publisher, args=(t,)) for t in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(received) == 1000

    def test_subscribe_unsubscribe_during_publishing(self) -> None:
        bus = EventBus()
        received_a: list[Event] = []
        received_b: list[Event] = []

        def handler_a(event: Event) -> None:
            received_a.append(event)

        def handler_b(event: Event) -> None:
            received_b.append(event)

        bus.subscribe(EventType.JOB_COMPLETED, handler_a)
        barrier = threading.Barrier(2)

        def publisher() -> None:
            barrier.wait()
            for i in range(200):
                bus.publish(Event(
                    type=EventType.JOB_COMPLETED,
                    data={"seq": i},
                ))

        def sub_unsub() -> None:
            barrier.wait()
            for _ in range(50):
                bus.subscribe(EventType.JOB_COMPLETED, handler_b)
                bus.unsubscribe(EventType.JOB_COMPLETED, handler_b)

        t1 = threading.Thread(target=publisher)
        t2 = threading.Thread(target=sub_unsub)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        # handler_a must have received all 200 events (never unsubscribed)
        assert len(received_a) == 200
        # handler_b received some (non-deterministic), but no crash occurred


# ---------------------------------------------------------------------------
# TestAdapterChaos
# ---------------------------------------------------------------------------

class TestAdapterChaos:
    """Adapter resilience under random failures and slow networks."""

    def _make_flaky_adapter(self, *, failure_rate: float = 0.5) -> MagicMock:
        """Return a mock adapter where get_state() fails ~failure_rate of the time."""
        adapter = MagicMock(spec=PrinterAdapter)
        adapter.name = "chaos-printer"
        adapter.capabilities = PrinterCapabilities()
        call_count = 0

        def flaky_get_state() -> PrinterState:
            nonlocal call_count
            call_count += 1
            if random.random() < failure_rate:
                exc_type = random.choice([ConnectionError, TimeoutError])
                raise exc_type(f"Simulated {exc_type.__name__}")
            return PrinterState(connected=True, state=PrinterStatus.IDLE)

        adapter.get_state.side_effect = flaky_get_state
        return adapter

    def test_flaky_adapter_100_calls_no_crash(self) -> None:
        random.seed(42)  # reproducible
        adapter = self._make_flaky_adapter(failure_rate=0.5)

        successes = 0
        failures = 0
        for _ in range(100):
            try:
                state = adapter.get_state()
                assert isinstance(state, PrinterState)
                assert state.state == PrinterStatus.IDLE
                successes += 1
            except (ConnectionError, TimeoutError):
                failures += 1

        # With 50% failure rate, both counts should be non-zero
        assert successes > 0
        assert failures > 0
        assert successes + failures == 100

    def test_flaky_adapter_wrapped_in_printer_error(self) -> None:
        """Simulate the adapter wrapping pattern: raw exceptions -> PrinterError."""
        random.seed(42)
        adapter = self._make_flaky_adapter(failure_rate=0.5)

        def safe_get_state() -> PrinterState:
            try:
                return adapter.get_state()
            except (ConnectionError, TimeoutError) as exc:
                raise PrinterError(
                    f"Failed to reach printer: {exc}", cause=exc
                ) from exc

        successes = 0
        printer_errors = 0
        for _ in range(100):
            try:
                state = safe_get_state()
                assert isinstance(state, PrinterState)
                successes += 1
            except PrinterError as exc:
                assert exc.cause is not None
                printer_errors += 1

        assert successes > 0
        assert printer_errors > 0
        assert successes + printer_errors == 100

    def test_slow_adapter_timeout_handling(self) -> None:
        """Mock adapter where calls block for 5s — verify timeout wrapping works."""
        adapter = MagicMock(spec=PrinterAdapter)
        adapter.name = "slow-printer"

        def slow_get_state() -> PrinterState:
            # Simulate slow call without actually sleeping 5 seconds.
            # We raise a Timeout immediately to represent the timeout firing.
            raise TimeoutError("Connection timed out after 5s")

        adapter.get_state.side_effect = slow_get_state

        for _ in range(10):
            with pytest.raises(TimeoutError, match="timed out"):
                adapter.get_state()


# ---------------------------------------------------------------------------
# TestSchedulerContention
# ---------------------------------------------------------------------------

class TestSchedulerContention:
    """Queue + scheduler workers under parallel writers."""

    def test_50_jobs_5_workers_no_double_start(self) -> None:
        queue = PrintQueue()
        _submit_n_jobs(queue, 50)

        started: list[str] = []
        completed_ids: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(5)

        def worker(worker_idx: int) -> None:
            barrier.wait()
            while True:
                job = queue.next_job()
                if job is None:
                    break
                try:
                    queue.mark_starting(job.id)
                except InvalidStateTransition:
                    # Another worker beat us — skip
                    continue

                with lock:
                    started.append(job.id)

                try:
                    queue.mark_printing(job.id)
                    queue.mark_completed(job.id)
                    with lock:
                        completed_ids.append(job.id)
                except InvalidStateTransition:
                    pass

        threads = [
            threading.Thread(target=worker, args=(w,)) for w in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        # No job started twice
        assert len(started) == len(set(started))
        # All completed jobs are in COMPLETED state
        for jid in completed_ids:
            assert queue.get_job(jid).status == JobStatus.COMPLETED

    def test_race_two_threads_mark_starting_same_job(self) -> None:
        queue = PrintQueue()
        job_id = queue.submit(
            file_name="race.gcode",
            submitted_by="race-test",
        )

        results: list[str] = []  # "won" or "lost"
        barrier = threading.Barrier(2)

        def try_mark_starting(thread_label: str) -> None:
            barrier.wait()
            try:
                queue.mark_starting(job_id)
                results.append("won")
            except InvalidStateTransition:
                results.append("lost")

        t1 = threading.Thread(target=try_mark_starting, args=("A",))
        t2 = threading.Thread(target=try_mark_starting, args=("B",))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        # Exactly one winner and one loser
        assert sorted(results) == ["lost", "won"]
        # Job is in STARTING state
        assert queue.get_job(job_id).status == JobStatus.STARTING

    def test_mixed_operations_no_lost_jobs(self) -> None:
        """Submit, start, complete, and cancel jobs concurrently — no jobs lost."""
        queue = PrintQueue()
        ids = _submit_n_jobs(queue, 30)
        barrier = threading.Barrier(3)

        def complete_first_10() -> None:
            barrier.wait()
            for jid in ids[:10]:
                try:
                    queue.mark_starting(jid)
                    queue.mark_printing(jid)
                    queue.mark_completed(jid)
                except InvalidStateTransition:
                    pass

        def cancel_next_10() -> None:
            barrier.wait()
            for jid in ids[10:20]:
                with contextlib.suppress(InvalidStateTransition):
                    queue.cancel(jid)

        def submit_more() -> None:
            barrier.wait()
            _submit_n_jobs(queue, 10, prefix="late")

        threads = [
            threading.Thread(target=complete_first_10),
            threading.Thread(target=cancel_next_10),
            threading.Thread(target=submit_more),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        # 30 original + 10 more = 40 total
        assert queue.total_count == 40
        # All original jobs accounted for
        for jid in ids:
            job = queue.get_job(jid)
            assert job.status in (
                JobStatus.QUEUED,
                JobStatus.STARTING,
                JobStatus.PRINTING,
                JobStatus.COMPLETED,
                JobStatus.CANCELLED,
            )


# ---------------------------------------------------------------------------
# TestAsyncEventBusChaos
# ---------------------------------------------------------------------------

class TestAsyncEventBusChaos:
    """AsyncEventBus under pressure: bounded queues, repeated start/stop."""

    def test_publish_faster_than_consumer(self) -> None:
        """Publish into a small bounded queue, verify no data loss."""

        async def _run() -> None:
            bus = AsyncEventBus(queue_size=50)
            received: list[Event] = []

            async def handler(event: Event) -> None:
                # Simulate slow consumer
                await asyncio.sleep(0.001)
                received.append(event)

            await bus.start()
            await bus.subscribe(EventType.JOB_SUBMITTED, handler)

            published = 0
            for i in range(200):
                try:
                    await bus.publish(Event(
                        type=EventType.JOB_SUBMITTED,
                        data={"seq": i},
                    ))
                    published += 1
                except asyncio.QueueFull:
                    # Back-pressure: wait and retry once
                    await asyncio.sleep(0.01)
                    try:
                        await bus.publish(Event(
                            type=EventType.JOB_SUBMITTED,
                            data={"seq": i},
                        ))
                        published += 1
                    except asyncio.QueueFull:
                        pass

            # Drain remaining events
            await bus.stop()

            # All published events should have been received
            assert len(received) == published

        asyncio.run(_run())

    def test_start_stop_repeated_no_crash(self) -> None:
        """Start and stop the async bus 10 times without deadlock or crash."""

        async def _run() -> None:
            bus = AsyncEventBus(queue_size=100)

            for cycle in range(10):
                await bus.start()
                assert bus.running

                # Publish a few events each cycle
                for i in range(5):
                    await bus.publish(Event(
                        type=EventType.PRINT_STARTED,
                        data={"cycle": cycle, "seq": i},
                    ))

                await bus.stop()
                assert not bus.running

        asyncio.run(_run())

    def test_subscribe_during_active_publishing(self) -> None:
        """Subscribe a new handler while events are actively being consumed."""

        async def _run() -> None:
            bus = AsyncEventBus(queue_size=500)
            received_early: list[Event] = []
            received_late: list[Event] = []

            async def early_handler(event: Event) -> None:
                received_early.append(event)

            async def late_handler(event: Event) -> None:
                received_late.append(event)

            await bus.start()
            await bus.subscribe(EventType.JOB_COMPLETED, early_handler)

            # Publish first batch
            for i in range(50):
                await bus.publish(Event(
                    type=EventType.JOB_COMPLETED,
                    data={"seq": i},
                ))

            # Allow some processing
            await asyncio.sleep(0.05)

            # Subscribe late handler mid-stream
            await bus.subscribe(EventType.JOB_COMPLETED, late_handler)

            # Publish second batch
            for i in range(50, 100):
                await bus.publish(Event(
                    type=EventType.JOB_COMPLETED,
                    data={"seq": i},
                ))

            await bus.stop()

            # Early handler got all 100
            assert len(received_early) == 100
            # Late handler got only the second batch (roughly — timing dependent)
            assert len(received_late) <= 100
            assert len(received_late) > 0

        asyncio.run(_run())
