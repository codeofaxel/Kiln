"""Every adapter's prints must count, not just the one with the hook.

``prints`` used to be incremented from a single place: inside
``record_print_outcome``, an explicit "log how this print went" action.
Its only automatic caller is the terminal-state hook, and that hook is
wired into the Bambu adapter alone — so a Prusa, Klipper, or OctoPrint
owner could print every day and report zero prints forever.  The usage
dashboard read that as "nobody but one Bambu user prints".

The count now happens where every adapter and every entry point meets:
``PrinterAdapter.start_print``.  These tests hold that line, and hold the
pairing that keeps a print from being counted twice when its outcome is
recorded afterwards.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from kiln import daily_stats
from kiln.printers.base import (
    JobProgress,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterState,
    PrintResult,
    UploadResult,
)

# The eight backends Kiln ships.  A print on any of them is a print.
ADAPTER_MODULES = [
    "bambu", "moonraker", "octoprint", "prusalink",
    "elegoo", "creality", "duet", "serial_adapter",
]


def _shipped_adapters() -> list[type]:
    """Every concrete adapter class, discovered rather than listed — a
    ninth backend inherits the guarantees below without editing a test."""
    import importlib

    for module_name in ADAPTER_MODULES:
        importlib.import_module(f"kiln.printers.{module_name}")

    seen: list[type] = []

    def walk(cls: type) -> None:
        for sub in cls.__subclasses__():
            if sub.__module__.startswith("kiln.printers.") and sub not in seen:
                seen.append(sub)
            walk(sub)

    walk(PrinterAdapter)
    return seen


class FakeAdapter(PrinterAdapter):
    """A non-Bambu adapter with no auto-record hook — the case that
    reported zero prints no matter how much its owner printed."""

    def __init__(self, name: str = "moonraker", *, succeed: bool = True) -> None:
        self._name = name
        self._succeed = succeed
        self.started: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> PrinterCapabilities:
        return PrinterCapabilities()

    def _start_print_impl(self, file_name: str, **kwargs) -> PrintResult:
        self.started.append(file_name)
        return PrintResult(success=self._succeed, message="ok")

    # -- remaining contract, unused by these tests ----------------------
    def get_state(self) -> PrinterState:
        return PrinterState()

    def get_job(self) -> JobProgress:
        return JobProgress()

    def list_files(self) -> list:
        return []

    def upload_file(self, file_path: str) -> UploadResult:
        return UploadResult(success=True, message="ok")

    def delete_file(self, file_name: str) -> bool:
        return True

    def cancel_print(self) -> PrintResult:
        return PrintResult(success=True, message="ok")

    def pause_print(self) -> PrintResult:
        return PrintResult(success=True, message="ok")

    def _resume_print_impl(self) -> PrintResult:
        return PrintResult(success=True, message="ok")

    def emergency_stop(self) -> PrintResult:
        return PrintResult(success=True, message="ok")

    def send_gcode(self, command: str) -> str:
        return "ok"

    def set_tool_temp(self, celsius: float, tool: int = 0) -> bool:
        return True

    def set_bed_temp(self, celsius: float) -> bool:
        return True


@pytest.fixture
def stats_file(tmp_path, monkeypatch):
    """Point the day file at a temp path and hand back a reader."""
    path = tmp_path / "daily_stats.json"
    monkeypatch.setattr(daily_stats, "_STATS_PATH", path)
    # The pre-print gate probes the printer; it soft-passes on anything
    # it can't determine, which is what an empty fake gives it.
    return lambda: json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


# ---------------------------------------------------------------------------
# The fix: a print on a non-Bambu adapter counts
# ---------------------------------------------------------------------------


def test_completed_print_on_non_bambu_adapter_counts(stats_file):
    adapter = FakeAdapter("moonraker")

    result = adapter.start_print("benchy.gcode")
    assert result.success

    # ...and the print it just ran is now visible to the heartbeat.
    assert daily_stats.get_daily_stats()["prints"] == 1

    # The outcome landing afterwards is the SAME print, not a second one.
    daily_stats.record_print_outcome_event(
        "job-1", printer_name="moonraker", file_name="benchy.gcode",
    )
    assert daily_stats.get_daily_stats()["prints"] == 1


@pytest.mark.parametrize("adapter_name", [
    "moonraker", "octoprint", "prusalink", "elegoo",
    "creality", "duet", "serial", "bambu",
])
def test_every_adapter_name_counts_the_same(adapter_name, stats_file):
    """No adapter is special.  Bambu is in the list on purpose — the bug
    was that it was the only one that counted."""
    FakeAdapter(adapter_name).start_print("part.gcode")
    assert daily_stats.get_daily_stats()["prints"] == 1


def test_no_adapter_overrides_the_counting_template_method():
    """The count lives in ``PrinterAdapter.start_print``.  An adapter that
    overrides it would silently drop out of the numbers — exactly the
    per-adapter divergence this fix exists to end."""
    adapters = _shipped_adapters()
    assert len(adapters) >= 8, (
        f"expected Kiln's eight shipped backends, discovered {len(adapters)}"
    )
    for cls in adapters:
        assert "start_print" not in cls.__dict__, (
            f"{cls.__name__} overrides start_print — it would bypass both "
            f"the pre-print safety gate and the print counter"
        )


def test_a_refused_print_is_not_counted(stats_file):
    FakeAdapter("octoprint", succeed=False).start_print("part.gcode")
    assert daily_stats.get_daily_stats()["prints"] == 0


def test_resume_3mf_does_not_count_as_a_second_print(stats_file):
    """A mid-print swap resumes the print already running."""
    adapter = FakeAdapter("bambu")
    adapter.start_print("dragon.3mf")
    adapter.start_print("transformed_resume_ab12.3mf")
    assert daily_stats.get_daily_stats()["prints"] == 1


def test_stats_failure_never_breaks_a_print(tmp_path, monkeypatch):
    """Telemetry is the least important thing in the room."""
    monkeypatch.setattr(
        daily_stats, "_STATS_PATH", tmp_path / "no-such-dir" / "x" / "s.json",
    )

    def boom(*_a, **_k):
        raise OSError("disk gone")

    monkeypatch.setattr(daily_stats, "_write", boom)
    assert FakeAdapter("duet").start_print("part.gcode").success


# ---------------------------------------------------------------------------
# Pairing: one physical print, one count
# ---------------------------------------------------------------------------


def test_outcome_without_a_start_still_counts(stats_file):
    """A print run from the printer's own screen, recorded afterwards —
    the outcome is the only signal it ever had."""
    daily_stats.record_print_outcome_event(
        "job-9", printer_name="prusalink", file_name="vase.bgcode",
    )
    assert daily_stats.get_daily_stats()["prints"] == 1


def test_refining_an_auto_recorded_outcome_does_not_recount(stats_file):
    """``record_print_outcome`` is explicitly re-callable for the same job
    so agents can refine what the hook auto-recorded."""
    FakeAdapter("moonraker").start_print("cube.gcode")
    for _ in range(3):
        daily_stats.record_print_outcome_event(
            "job-7", printer_name="moonraker", file_name="cube.gcode",
        )
    assert daily_stats.get_daily_stats()["prints"] == 1


def test_two_concurrent_prints_on_one_adapter_pair_up(stats_file):
    """A farm running two jobs through the same backend gets two counts,
    and the two outcomes that follow add none."""
    adapter = FakeAdapter("moonraker")
    adapter.start_print("left.gcode")
    adapter.start_print("right.gcode")
    assert daily_stats.get_daily_stats()["prints"] == 2

    daily_stats.record_print_outcome_event(
        "job-a", printer_name="moonraker", file_name="right.gcode",
    )
    daily_stats.record_print_outcome_event(
        "job-b", printer_name="moonraker", file_name="left.gcode",
    )
    assert daily_stats.get_daily_stats()["prints"] == 2


def test_outcome_pairs_when_the_printer_is_named_differently(stats_file):
    """The adapter counts under its own name (``"bambu"``); an outcome can
    arrive under the registry name (``"default"``).  The file is what both
    sides agree on."""
    FakeAdapter("bambu").start_print("/local/path/benchy.3mf")
    daily_stats.record_print_outcome_event(
        "job-3", printer_name="default", file_name="benchy.3mf",
    )
    assert daily_stats.get_daily_stats()["prints"] == 1


def test_a_print_spanning_midnight_is_not_counted_twice(
    tmp_path, monkeypatch, stats_file,
):
    """Started yesterday, recorded today.  The day rolled over in between,
    and the pending token has to roll with it."""
    FakeAdapter("elegoo").start_print("long_run.gcode")

    # Age the day file by a day, keeping the pending token it wrote.
    path = tmp_path / "daily_stats.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["date"] = str(date.today() - timedelta(days=1))
    path.write_text(json.dumps(data), encoding="utf-8")

    daily_stats.record_print_outcome_event(
        "job-overnight", printer_name="elegoo", file_name="long_run.gcode",
    )

    stats = daily_stats.get_daily_stats()
    assert stats["prints"] == 0, "today gets no phantom print"
    assert stats["previous_day"]["prints"] == 1, "yesterday's start still counted"


# ---------------------------------------------------------------------------
# Privacy: local-first print history is unchanged
# ---------------------------------------------------------------------------


def test_bookkeeping_never_reaches_the_heartbeat(stats_file):
    """Print history is local-first and opt-in to sync.  This fix corrects
    a number already being sent; it must not add anything to the wire."""
    FakeAdapter("moonraker").start_print("secret_prototype_v4.gcode")

    stats = daily_stats.get_daily_stats()
    assert "pending_starts" not in stats
    assert "counted_outcomes" not in stats

    # And the file name itself is never written to disk, only a hash of it.
    on_disk = json.dumps(stats_file())
    assert "secret_prototype_v4" not in on_disk
