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


def test_the_wire_shape_is_headline_statuses_known_name_and_conditions_only() -> None:
    """The full statement stays behind the question door; the panel gets the
    headline, the buckets, the machine's name and the conditional clauses
    -- nothing that could grow into a second copy of the statement."""
    payload = compose_monitor_payload(
        None, None, None, None, None, None,
        coverage={"headline": "h", "by_status": {"watched": ["x"]}, "known": True, "classes": {"x": {}}, "statement": "long",
                  "printer_label": " Acme Meridian 3 ", "conditions": {"x": "only with the lid closed", "y": 3, "z": ""}},
    )
    assert set(payload["coverage"]) == {"headline", "by_status", "known", "printer_label", "conditions"}
    assert payload["coverage"]["printer_label"] == "Acme Meridian 3"
    assert payload["coverage"]["conditions"] == {"x": "only with the lid closed"}
    bare = compose_monitor_payload(
        None, None, None, None, None, None,
        coverage={"headline": "h", "by_status": {}, "known": True, "printer_label": "", "conditions": {}},
    )
    assert set(bare["coverage"]) == {"headline", "by_status", "known"}


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
    # No Kiln bucket on this wire: the gaps alone, and no word about Kiln.
    assert line == "Your printer can't watch for the first layer."
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
    assert "- Your printer can't watch for the first layer." in report
    assert "What is watching this print" not in report, "the essay is the panel's, not the report's"
    assert report.index("can't watch for") < report.index("Camera:")


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


# --- the watching sentence: one rule, the actor named --------------------------

#: A made-up machine, so the fixture is the RULE's and not a copy of any
#: model's detector research -- that belongs to kiln-pro, whose own tests
#: (tests/test_monitor_panel_polish.py) run the real blocks through this
#: same function and its two ports (the panel, the web's lib/api/coverage.ts).
#: The class labels are the wire's vocabulary; the name, the buckets and the
#: clauses are invented.
_GAPPY_BY_STATUS = {
    "watched": ["running out of filament", "a filament tangle"],
    "conditional": ["nozzle clumping"],
    "not_watched": ["spaghetti", "a bad first layer"],
    "unknown": ["fire"],
    "kiln_watching": ["a heater fault", "a stalled print"],
    "kiln_can_watch": ["spaghetti", "a bad first layer", "a dead camera feed"],
    "kiln_can_watch_now": ["spaghetti"],
}
_GAPPY_HEADLINE = (
    "What is watching this print — watched: running out of filament, a filament tangle. "
    "watched, with conditions: nozzle clumping. not watched: spaghetti, a bad first layer. "
    "Kiln is watching: a heater fault, a stalled print."
)
_GAPPY_CONDITIONS = {"nozzle clumping": "only with the lid closed"}


def _gappy(**over) -> dict:
    """A machine with gaps Kiln could fill: the offer shapes."""
    return {"headline": _GAPPY_HEADLINE, "by_status": {**_GAPPY_BY_STATUS, **over}, "known": True,
            "printer_label": "Acme Meridian 3", "conditions": _GAPPY_CONDITIONS}


def _covered(**over) -> dict:
    """A machine that watches the camera classes itself: the add-on shapes."""
    by = {"watched": ["a filament tangle", "running out of filament"], "conditional": ["spaghetti"],
          "unknown": ["fire"], "kiln_watching": ["a heater fault"], "kiln_can_watch": ["a dead camera feed"],
          "kiln_can_watch_now": ["spaghetti", "a filament tangle"], **over}
    return {"headline": "What is watching this print — watched: a filament tangle.", "by_status": by,
            "known": True, "printer_label": "Acme Meridian 3 Pro",
            "conditions": {"spaghetti": "after the first layers"}}


def _without(block: dict, *buckets: str) -> dict:
    """*block* with the named by_status buckets absent, as an older wire is."""
    return {**block, "by_status": {k: v for k, v in block["by_status"].items() if k not in buckets}}


WATCHING_SENTENCE_CASES: dict[str, tuple[dict | None, dict | None, dict | None]] = {
    "gaps": (_gappy(), _watch(printing=True, attached=True, running=True),
             {"text": "Your Acme Meridian 3 can't watch for spaghetti or a bad first layer.",
              "ask": "Kiln can watch the camera for spaghetti"}),
    "both_now": (_gappy(kiln_can_watch_now=["spaghetti", "a bad first layer"]), _watch(),
                 {"text": "Your Acme Meridian 3 can't watch for spaghetti or a bad first layer.",
                  "ask": "Kiln can watch the camera for them"}),
    "one_gap": (_gappy(not_watched=["spaghetti"], kiln_can_watch_now=["spaghetti"]), _watch(),
                {"text": "Your Acme Meridian 3 can't watch for spaghetti.", "ask": "Kiln can watch the camera for it"}),
    "no_camera_mid_print": (_gappy(kiln_can_watch_now=[]), _watch(),
                            {"text": "Your Acme Meridian 3 can't watch for spaghetti or a bad first layer, "
                                     "and neither can Kiln on this print.", "ask": None}),
    "never": (_gappy(not_watched=["fire"], kiln_can_watch=[], kiln_can_watch_now=[]), _watch(),
              {"text": "Your Acme Meridian 3 can't watch for fire, and neither can Kiln.", "ask": None}),
    "idle": (_gappy(kiln_watching=[], kiln_can_watch_now=[]), _watch(printing=False),
             {"text": "Your Acme Meridian 3 can't watch for spaghetti or a bad first layer. "
                      "Kiln can watch the camera for them once a print is running.", "ask": None}),
    "switched_off": (_gappy(not_watched=["spaghetti"], off_for_this_print=["nozzle clumping"],
                            kiln_can_watch_now=["spaghetti"]), _watch(),
                     {"text": "Your Acme Meridian 3 can't watch for spaghetti, and has nozzle clumping switched off.",
                      "ask": "Kiln can watch the camera for spaghetti"}),
    "kiln_covers_a_gap": (_gappy(kiln_watching=["spaghetti", "a heater fault"], kiln_can_watch_now=[]), _watch(),
                          {"text": "Your Acme Meridian 3 can't watch for a bad first layer, and neither can Kiln on this print.",
                           "ask": None}),
    "adds": (_covered(), _watch(printing=True, attached=True, running=True),
             {"text": "Your Acme Meridian 3 Pro watches for spaghetti and a filament tangle itself.",
              "ask": "Kiln can add a camera watch too"}),
    "everything": (_covered(kiln_can_watch_now=[]), _watch(printing=True, attached=True, running=True),
                   {"text": "Watched by your Acme Meridian 3 Pro and Kiln. "
                            "Your Acme Meridian 3 Pro watches for spaghetti with conditions.", "ask": None}),
    "kiln_not_started": (_covered(kiln_watching=[], kiln_can_watch_now=[]), _watch(printing=True, attached=False),
                         {"text": "Watched by your Acme Meridian 3 Pro. Kiln is not watching this print (it did not start it). "
                                  "Your Acme Meridian 3 Pro watches for spaghetti with conditions.", "ask": None}),
    "no_label": ({k: v for k, v in _gappy().items() if k != "printer_label"}, _watch(),
                 {"text": "Your printer can't watch for spaghetti or a bad first layer.",
                  "ask": "Kiln can watch the camera for spaghetti"}),
    # A wire without the "now" bucket (an older kiln-pro) cannot say what a
    # watch would add on this print: the gaps alone, no word about Kiln --
    # "neither can Kiln on this print" read false beside a watch_print that
    # could have.  The details still list what Kiln could add by another door.
    "older_wire": (_without({**_gappy(), "printer_label": None}, "kiln_can_watch_now"), _watch(),
                   {"text": "Your printer can't watch for spaghetti or a bad first layer.", "ask": None}),
    # A block composed with no print in hand carries no Kiln layer at all.
    "no_kiln_layer": (_without(_gappy(), "kiln_watching", "kiln_can_watch", "kiln_can_watch_now"), None,
                      {"text": "Your Acme Meridian 3 can't watch for spaghetti or a bad first layer.", "ask": None}),
    "unknown_machine": ({"headline": "Kiln has no detector research for this model yet. Beyond its spec sheet, "
                                     "assume nothing is watching.", "by_status": {"unknown": ["spaghetti"]}, "known": False},
                        _watch(), {"text": "Kiln has no detector research for this model yet.", "ask": None}),
    "empty": ({"headline": "", "by_status": {}, "known": False}, _watch(), None),
}


def test_the_watching_sentence_names_who_watches_and_who_could() -> None:
    from kiln import server

    for name, (block, watch, expect) in WATCHING_SENTENCE_CASES.items():
        said = server.coverage_watching_sentence(block, watch)
        if expect is None:
            assert said is None, name
            continue
        assert said is not None, name
        assert {"text": said["text"], "ask": said["ask"]} == expect, name
        assert "the printer" not in said["text"].lower().replace("the printer reports", ""), name


def test_the_watching_sentence_groups_what_each_actor_watches() -> None:
    from kiln import server

    said = server.coverage_watching_sentence(*WATCHING_SENTENCE_CASES["gaps"][:2])
    assert said["gaps"] == ["spaghetti", "a bad first layer"] and said["offer"] == ["spaghetti"]
    assert said["printer"]["label"] == "Acme Meridian 3"
    assert said["printer"]["watches"] == _GAPPY_BY_STATUS["watched"]
    assert said["printer"]["with_conditions"] == _GAPPY_CONDITIONS
    assert said["printer"]["cant"] == ["spaghetti", "a bad first layer"] and said["printer"]["off"] == []
    assert said["kiln"]["watching"] == ["a heater fault", "a stalled print"]
    assert said["kiln"]["can_now"] == ["spaghetti"]
    assert said["kiln"]["can_later"] == ["a bad first layer", "a dead camera feed"]
    # The older wire keeps every "could add" in the details, where it is true.
    older = server.coverage_watching_sentence(*WATCHING_SENTENCE_CASES["older_wire"][:2])
    assert older["kiln"]["can_later"] == ["spaghetti", "a bad first layer", "a dead camera feed"]


def test_the_report_names_the_agents_own_door_where_the_panel_says_turn_on() -> None:
    from kiln import server

    with mock.patch.object(server, "_pro_bridge", return_value=_fake_pro(result=_gappy())), mock.patch.object(
        server, "_resolve_printer_model_live", return_value="bambu_a1"
    ), mock.patch.object(server, "_resolve_adapter", side_effect=RuntimeError("no printer")):
        line = server._coverage_line_for(None)
    assert line == ("Your Acme Meridian 3 can't watch for spaghetti or a bad first layer. "
                    "Kiln can watch the camera for spaghetti — start a background watch (watch_print).")
