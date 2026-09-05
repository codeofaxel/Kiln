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


def test_the_report_line_is_the_headline_and_only_with_kiln_pro() -> None:
    from kiln import server

    with mock.patch.object(server, "_pro_bridge", return_value=_fake_pro()), mock.patch.object(
        server, "_resolve_printer_model_live", return_value="bambu_x1c"
    ):
        line = server._coverage_line_for(None)
    assert line == "What is watching this print — watched: spaghetti. not watched: the first layer."

    with mock.patch.object(server, "_pro_bridge", return_value=_fake_pro(available=False)):
        assert server._coverage_line_for(None) is None


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
    assert "What is watching this print — watched: spaghetti." in report
    assert report.index("What is watching") < report.index("Camera:")


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
