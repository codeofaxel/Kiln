"""The monitor carries what is watching the print, when kiln-pro can say.

Public Kiln owns the interface: an optional ``coverage`` axis on the
``kiln.monitor.v1`` wire, a courtesy line in the ``monitor_print`` report,
and a bridge call that asks kiln-pro for the statement.  What a printer's
detectors watch — the matrices, the conditions, the wording — lives in
kiln-pro.  Without it there is no block, the way there is no camera frame
without a camera, and nothing here fails for its absence.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from kiln.monitor_payload import compose_monitor_payload

_BLOCK = {
    "headline": "What is watching this print — watched: spaghetti. not watched: the first layer.",
    "by_status": {"watched": ["spaghetti"], "not_watched": ["the first layer"]},
    "known": True,
}


def _fake_pro(available: bool = True, result: dict | None = _BLOCK):
    di = SimpleNamespace(coverage_block=lambda model, **kw: result)
    return SimpleNamespace(
        is_available=lambda feature: available and feature == "device_intelligence",
        device_intelligence=di,
    )


# --- the wire --------------------------------------------------------------


def test_the_wire_carries_a_coverage_axis_only_when_given() -> None:
    base = compose_monitor_payload(None, None, {"printer": {"state": "printing"}}, None, None, None)
    assert "coverage" not in base
    with_it = compose_monitor_payload(
        None, None, {"printer": {"state": "printing"}}, None, None, None,
        coverage={"headline": "What is watching this print — watched: spaghetti.", "by_status": {"watched": ["spaghetti"]}, "known": True},
    )
    assert with_it["coverage"] == {
        "headline": "What is watching this print — watched: spaghetti.",
        "by_status": {"watched": ["spaghetti"]},
        "known": True,
    }
    # An empty block is no block: the panel must not render an empty card.
    assert "coverage" not in compose_monitor_payload(None, None, None, None, None, None, coverage={})


def test_the_wire_shape_is_headline_statuses_and_known_only() -> None:
    """The full statement stays behind the question door; the panel gets the
    headline and the buckets, nothing that could grow into a second copy."""
    payload = compose_monitor_payload(
        None, None, None, None, None, None,
        coverage={"headline": "h", "by_status": {"watched": ["x"]}, "known": True, "classes": {"x": {}}, "statement": "long"},
    )
    assert set(payload["coverage"]) == {"headline", "by_status", "known"}


# --- the local panel -------------------------------------------------------


def test_the_local_panel_asks_kiln_pro_and_carries_the_headline() -> None:
    from kiln import local_monitor

    with mock.patch("kiln.server._pro_bridge", return_value=_fake_pro()), mock.patch(
        "kiln.server._resolve_printer_model_live", return_value="bambu_x1c"
    ):
        block = local_monitor._coverage_block("default")
    assert block == _BLOCK


def test_the_local_panel_carries_nothing_without_kiln_pro_or_a_model() -> None:
    from kiln import local_monitor

    with mock.patch("kiln.server._pro_bridge", return_value=_fake_pro(available=False)):
        assert local_monitor._coverage_block("default") is None
    with mock.patch("kiln.server._pro_bridge", return_value=_fake_pro()), mock.patch(
        "kiln.server._resolve_printer_model_live", return_value=""
    ):
        assert local_monitor._coverage_block("default") is None


def test_a_failing_bridge_call_never_breaks_the_panel() -> None:
    from kiln import local_monitor

    def _boom(model, **kw):
        raise RuntimeError("overlay unreachable")

    pro = _fake_pro()
    pro.device_intelligence = SimpleNamespace(coverage_block=_boom)
    with mock.patch("kiln.server._pro_bridge", return_value=pro), mock.patch(
        "kiln.server._resolve_printer_model_live", return_value="bambu_x1c"
    ):
        assert local_monitor._coverage_block("default") is None


# --- the monitor report ----------------------------------------------------


def test_the_report_line_is_one_short_line_and_only_with_kiln_pro() -> None:
    """The report gets the buckets compressed to a glance, never the wire's
    headline paragraph — that is the panel's, and even there it is being
    replaced by badges."""
    from kiln import server

    with mock.patch.object(server, "_pro_bridge", return_value=_fake_pro()), mock.patch.object(
        server, "_resolve_printer_model_live", return_value="bambu_x1c"
    ), mock.patch.object(server, "_resolve_adapter", side_effect=RuntimeError("no printer")):
        line = server._coverage_line_for(None)
    assert line == (
        "Watching this print — printer: 1 watched; "
        "Kiln: not watching (it did not start this print); "
        "unwatched: the first layer."
    )
    assert "\n" not in line

    with mock.patch.object(server, "_pro_bridge", return_value=_fake_pro(available=False)):
        assert server._coverage_line_for(None) is None


# The A1's real buckets, read from kiln-pro offline on 2026-09-16 for a
# print Kiln did not start.  The headline for these ran 392 characters.
_A1_BY_STATUS = {
    "watched": [
        "running out of filament", "a filament tangle",
        "the wrong or a missing build plate", "lost steps and layer shifts",
        "a power cut mid-print",
    ],
    "conditional": ["nozzle clumping", "air printing (extruding nothing)"],
    "not_watched": ["spaghetti", "the first layer"],
    "unknown": ["something left on the bed", "purge pile-up", "a part coming loose", "an open door", "fire"],
    "kiln_watching": [],
    "kiln_can_watch": ["spaghetti", "the first layer", "a filament tangle", "a dead camera feed", "a lost connection"],
}
_A1_HEADLINE = (
    "What is watching this print — watched: running out of filament, a filament tangle, "
    "the wrong or a missing build plate, lost steps and layer shifts, a power cut mid-print. "
    "watched, with conditions: nozzle clumping, air printing (extruding nothing). "
    "not watched: spaghetti, the first layer. Kiln is not watching this print "
    "(its watchdog attaches to prints Kiln starts, and this one it did not)."
)


def _a1_block(**by_status_overrides) -> dict:
    by_status = {**_A1_BY_STATUS, **by_status_overrides}
    return {"headline": _A1_HEADLINE, "by_status": by_status, "known": True}


def _watch(printing=True, attached=False, running=False) -> dict:
    return {
        "kind": "kiln.watch.v1",
        "printing": printing,
        "watchdog": {"attached": attached, "running": running},
    }


def test_the_short_line_counts_the_printer_names_the_gaps_and_says_why_kiln_is_off() -> None:
    from kiln import server

    line = server._coverage_short_line(_a1_block(), _watch(printing=True, attached=False))
    assert line == (
        "Watching this print — printer: 5 watched, 2 with conditions; "
        "Kiln: not watching (it did not start this print); "
        "unwatched: spaghetti, the first layer."
    )
    assert len(line) < len(_A1_HEADLINE) / 2


def test_a_stopped_watchdog_is_said_differently_from_one_never_attached() -> None:
    from kiln import server

    line = server._coverage_short_line(_a1_block(), _watch(printing=True, attached=True, running=False))
    assert "Kiln: not watching (its watchdog stopped)" in line


def test_kiln_watching_is_a_count_and_its_classes_leave_the_gaps() -> None:
    from kiln import server

    block = _a1_block(kiln_watching=["a heater fault", "a stalled print", "a fault the printer reports"])
    line = server._coverage_short_line(block, _watch(printing=True, attached=True, running=True))
    assert "Kiln: watching 3 classes" in line
    assert "unwatched: spaghetti, the first layer." in line
    # A class Kiln covers is not a gap, however the printer bucketed it.
    block = _a1_block(kiln_watching=["spaghetti"])
    assert "unwatched: the first layer." in server._coverage_short_line(block, _watch())


def test_no_print_running_promises_the_watchdog_for_the_next_one() -> None:
    from kiln import server

    line = server._coverage_short_line(_a1_block(), _watch(printing=False))
    assert "Kiln: attaches its watchdog to the prints it starts" in line


def test_a_detector_switched_off_is_counted_and_named_as_a_gap() -> None:
    from kiln import server

    block = _a1_block(conditional=["air printing (extruding nothing)"], off_for_this_print=["nozzle clumping"])
    line = server._coverage_short_line(block, _watch())
    assert "printer: 5 watched, 1 with conditions, 1 off for this print;" in line
    assert line.endswith("unwatched: spaghetti, the first layer, nozzle clumping.")


def test_an_unknown_machine_keeps_kiln_pros_first_sentence() -> None:
    from kiln import server

    block = {
        "headline": (
            "Kiln has no detector research for this model yet. Beyond its spec sheet, "
            "assume nothing is watching: watch it yourself, or point a camera at the bed "
            "that Kiln can watch. Kiln is not watching this print (its watchdog attaches "
            "to prints Kiln starts, and this one it did not)."
        ),
        "by_status": {"unknown": ["spaghetti", "fire"], "kiln_watching": [], "kiln_can_watch": ["spaghetti"]},
        "known": False,
    }
    assert server._coverage_short_line(block, _watch()) == (
        "Kiln has no detector research for this model yet; "
        "Kiln: not watching (it did not start this print)."
    )


def test_a_block_from_an_older_kiln_pro_without_kiln_buckets_still_reads() -> None:
    """No ``kiln_watching`` bucket and no watch state: say what the printer
    covers and the gaps, and claim nothing about Kiln."""
    from kiln import server

    line = server._coverage_short_line(_BLOCK, None)
    assert line == "Watching this print — printer: 1 watched; unwatched: the first layer."


def test_the_report_line_reaches_monitor_print_output() -> None:
    """The line rides the real report, between the state lines and the camera."""
    from unittest.mock import MagicMock

    from kiln import server

    adapter = MagicMock()
    state = MagicMock()
    state.state = "printing"
    state.to_dict.return_value = {"state": "printing"}
    adapter.get_state.return_value = state
    job = MagicMock()
    job.completion = 42.0
    job.file_name = "part.gcode"
    job.print_time_elapsed = 600
    job.print_time_left = 900
    job.to_dict.return_value = {"completion": 42.0, "file_name": "part.gcode"}
    adapter.get_job.return_value = job
    adapter.get_snapshot.return_value = None
    adapter.get_temperatures.return_value = {}

    with mock.patch.object(server, "_get_adapter", return_value=adapter), mock.patch.object(
        server, "_pro_bridge", return_value=_fake_pro()
    ), mock.patch.object(server, "_resolve_printer_model_live", return_value="bambu_x1c"):
        report = server.monitor_print(include_snapshot=False)
    assert isinstance(report, str), report
    assert "- Watching this print — printer: 1 watched;" in report
    assert "unwatched: the first layer." in report
    assert "What is watching this print" not in report, "the essay is the panel's, not the report's"
    assert report.index("Watching this print") < report.index("Camera:")


def test_no_kiln_pro_installed_means_no_block_and_no_line() -> None:
    """The bridge accessor answers None when kiln-pro is absent; nothing
    downstream may assume a module-level name that public Kiln never had."""
    from kiln import local_monitor, server

    with mock.patch.object(server, "_pro_bridge", return_value=None):
        assert server._coverage_block_for("default") is None
        assert server._coverage_line_for("default") is None
        assert local_monitor._coverage_block("default") is None


def test_a_block_without_a_headline_is_no_block() -> None:
    """kiln-pro answering an unknown printer with no headline must not
    render an empty card or an empty report line."""
    from kiln import server

    with mock.patch.object(server, "_pro_bridge", return_value=_fake_pro(result={"known": False})), mock.patch.object(
        server, "_resolve_printer_model_live", return_value="nobody_9000"
    ):
        assert server._coverage_block_for(None) is None
        assert server._coverage_line_for(None) is None


def test_the_full_status_read_names_the_catalogue_model() -> None:
    """The hosted monitor door reads the model off this key at either detail
    level — its agent-facing verb only ever polls lite."""
    from unittest.mock import MagicMock

    from kiln import server

    adapter = MagicMock()
    state = MagicMock()
    state.to_dict.return_value = {"state": "idle"}
    adapter.get_state.return_value = state
    job = MagicMock()
    job.to_dict.return_value = {}
    adapter.get_job.return_value = job
    adapter.capabilities.to_dict.return_value = {}
    with mock.patch.object(server, "_get_adapter", return_value=adapter), mock.patch.object(
        server, "read_status", return_value=(state, job)
    ), mock.patch.object(server, "_resolve_printer_model_live", return_value="bambu_x1c"):
        full = server.printer_status(detail="full")
        lite = server.printer_status(detail="lite")
    assert full.get("printer_model") == "bambu_x1c", full
    assert lite.get("printer_model") == "bambu_x1c", lite
    with mock.patch.object(server, "_get_adapter", return_value=adapter), mock.patch.object(
        server, "read_status", return_value=(state, job)
    ), mock.patch.object(server, "_resolve_printer_model_live", return_value=""):
        assert "printer_model" not in server.printer_status(detail="lite")


def test_the_local_doors_hand_kiln_pro_the_live_watch_state() -> None:
    """The card and the report say "Kiln is watching" only when it is, so
    every local door passes the state read off THIS process, never nothing."""
    from kiln import server

    seen: dict = {}

    def _block(model, **kw):
        seen.update(kw)
        return _BLOCK

    pro = _fake_pro()
    pro.device_intelligence = SimpleNamespace(coverage_block=_block)
    with mock.patch.object(server, "_pro_bridge", return_value=pro), mock.patch.object(
        server, "_resolve_printer_model_live", return_value="bambu_x1c"
    ), mock.patch.object(server, "_resolve_adapter", side_effect=RuntimeError("no printer")):
        assert server._coverage_block_for("default") == _BLOCK
    assert seen["watch"]["kind"] == "kiln.watch.v1", seen
    assert seen["watch"]["watchdog"] == {"attached": False, "running": False}
    assert seen["watch"]["printing"] is None  # no adapter: no reading to vouch either way


def test_starting_a_health_session_answers_with_what_is_watching_now() -> None:
    """The line is read AFTER the session starts, so it counts the session."""
    from kiln import server

    monitor = mock.MagicMock()
    order: list[str] = []
    monitor.start_monitoring.side_effect = lambda *a, **k: order.append("started")

    def _line(name):
        order.append("read")
        return "What is watching this print — Kiln is watching: a heater fault."

    with mock.patch("kiln.print_health_monitor.get_print_health_monitor", return_value=monitor), mock.patch.object(
        server, "_coverage_line_for", side_effect=_line
    ), mock.patch.object(server, "_get_adapter", return_value=mock.MagicMock()), mock.patch.object(
        server, "_watch_capacity_error", return_value=None
    ):
        result = server.start_printer_health_monitoring("default", interval_seconds=30)
    assert result.get("success"), result
    assert result["coverage"].startswith("What is watching this print"), result
    assert order == ["started", "read"], order
    from pathlib import Path

    src = Path(server.__file__).read_text(encoding="utf-8")
    start = src.index("def start_printer_health_monitoring(")
    body = src[start:src.index("def stop_printer_health_monitoring(")]
    assert body.index("monitor.start_monitoring(") < body.index("_coverage_line_for(printer_name)")
