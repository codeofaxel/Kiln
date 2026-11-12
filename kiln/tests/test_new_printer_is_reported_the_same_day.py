"""A printer added after the day's heartbeat is reported the same day.

Kiln's servers hand a head-motion plan only to an install that has reported
the printer's model, and an install reports once a day.  So the day is
walked here the way a new install lives it: the day's row goes out with no
printer, a printer is added, and the first home is asked for.  The servers
are stood in for by the one rule that matters: a model is known only once a
heartbeat row has named it.
"""

from __future__ import annotations

import json
import sys
from datetime import date as real_date
from datetime import timedelta
from unittest import mock

import pytest

from kiln import _pro_motion_bridge as bridge
from kiln import heartbeat

MODEL = "bambu_h2s"


class _FakeDate(real_date):
    _today = real_date(2026, 11, 10)

    @classmethod
    def today(cls):
        return cls._today


class _Adapter:
    def __init__(self, model: str = MODEL, serial: str = "0309AA000000001"):
        self._printer_model = model
        self.serial = serial


class _Registry:
    def __init__(self):
        self.adapters: dict[str, object] = {}

    @property
    def count(self):
        return len(self.adapters)

    def get(self, name):
        return self.adapters.get(name)

    def list_names(self):
        return list(self.adapters)


def _plan(verb: str = "home") -> dict:
    return {
        "schema": bridge.SCHEMA, "printer_id": MODEL, "verb": verb, "ok": True,
        "steps": [{"number": 1, "label": "raise", "you_will_see": "lift", "stops_when": "ends", "gcode": ["G91"]}],
        "homed_axes": ["X"], "summary": "ran", "raise_clearance_mm": 7.0,
    }


class _Day:
    """The install's side of one day, and the servers' memory of it."""

    def __init__(self):
        self.registry = _Registry()
        self.rows: list[dict] = []          # every heartbeat row that went out
        self.asks: list[dict] = []          # every ask of the motion door
        self.extras = 0                     # the once-a-day sends that ride a heartbeat
        self.network_up = True

    def reported(self) -> set[str]:
        named: set[str] = set()
        for row in self.rows:
            if row.get("p_printer_model"):
                named.add(str(row["p_printer_model"]).lower())
            named.update(str(m).lower() for m in row["p_details"].get("printer_models", []))
        return named

    def service(self, tool, **kw):
        assert tool == "motion_plan"
        self.asks.append(kw)
        if kw["printer_id"] not in self.reported():
            return {"status": "error", "success": False, "code": "MACHINE_NOT_PAIRED",
                    "error": f"This device's Kiln has not reported a {kw['printer_id']}."}
        if "plate" in kw:
            return {"around_plate": [{"number": 1, "gcode": ["G1 X10"]}]}
        return {"plan": _plan(kw["verb"])}


@pytest.fixture
def day(tmp_path, monkeypatch):
    d = _Day()
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    for name in list(sys.modules):
        if name == "kiln_pro" or name.startswith("kiln_pro."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    monkeypatch.setattr(bridge, "_service_down_until", 0.0)
    monkeypatch.setattr(bridge, "_misses", {})

    monkeypatch.setattr(heartbeat, "date", _FakeDate)
    _FakeDate._today = real_date(2026, 11, 10)
    monkeypatch.setattr(heartbeat, "_is_ci_environment", lambda: False)
    monkeypatch.setattr(heartbeat, "_is_ephemeral_runner", lambda: False)
    monkeypatch.setattr(heartbeat, "_is_hosted_multitenant", lambda: False)
    monkeypatch.setattr(heartbeat, "_telemetry_enabled", lambda: True)
    monkeypatch.setattr(heartbeat, "_sent_on", None)
    monkeypatch.setattr(heartbeat, "_named_in_process", frozenset())
    monkeypatch.setattr(heartbeat, "_LAST_BEAT_PATH", tmp_path / ".last_heartbeat")

    import kiln.printer_model_resolver as resolver
    import kiln.registry as registry_mod
    import kiln.server as srv

    monkeypatch.setattr(registry_mod, "get_registry", lambda: d.registry)
    monkeypatch.setattr(resolver, "resolve_all_printer_models", lambda: [])
    monkeypatch.setattr(resolver, "resolve_printer_model", lambda: None)
    monkeypatch.delenv("KILN_PRINTER_MODEL", raising=False)
    monkeypatch.setattr(srv, "_pro_api_call", d.service)

    def _count_extra(*a, **kw):
        d.extras += 1

    monkeypatch.setattr("kiln.printer_motion_report.send_after_heartbeat", _count_extra)
    monkeypatch.setattr("kiln.printer_bed_report.send_after_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr("kiln.bench.send_after_heartbeat", lambda *a, **kw: None)

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _urlopen(req, timeout=None):
        if not d.network_up:
            raise OSError("offline")
        body = json.loads(req.data.decode())
        if "p_installation_id" in body:
            d.rows.append(body)
        return _Resp()

    with mock.patch("urllib.request.urlopen", _urlopen):
        yield d


def test_a_printer_added_after_the_days_row_can_be_homed_the_same_day(day):
    assert heartbeat._send_heartbeat() is True              # the day's row: no printer yet
    assert day.reported() == set()

    day.registry.adapters["default"] = _Adapter()           # the person adds their printer
    doc = bridge.plan_for(day.registry.adapters["default"], "home")

    assert doc is not None and doc["verb"] == "home", "the first home of the day was refused"
    assert len(day.asks) == 2, "one refusal, one report, one more ask"
    assert len(day.rows) == 2 and MODEL in day.reported()
    assert bridge.miss_for(day.registry.adapters["default"], "home") is None

    # every later ask that day is served on the first try, with no further row
    assert bridge.plan_for(day.registry.adapters["default"], "park", axes="XY")["verb"] == "park"
    assert len(day.asks) == 3 and len(day.rows) == 2


def test_the_path_around_a_part_is_asked_again_the_same_way(day):
    heartbeat._send_heartbeat()
    day.registry.adapters["default"] = _Adapter()
    record = {"status": "occupied", "job": {"file": "part.3mf", "footprint_mm": [10, 10, 40, 40], "max_z_mm": 12.0}}

    steps = bridge.around_plate(record, printer_model=MODEL, action="park", clearance_mm=5.0)

    assert steps and len(day.asks) == 2 and MODEL in day.reported()


def test_a_model_only_the_adapter_knows_is_still_reported(day):
    """The adapter asking is not in the registry or config.yaml: its model rides the row."""
    heartbeat._send_heartbeat()

    doc = bridge.plan_for(_Adapter(), "home")

    assert doc is not None
    assert day.rows[-1]["p_details"]["printer_models"] == [MODEL]


def test_a_refusal_for_a_printer_already_reported_is_not_asked_twice(day):
    day.registry.adapters["default"] = _Adapter()
    heartbeat._send_heartbeat()                             # today's row names the printer
    rows_before = len(day.rows)
    day.rows.clear()                                        # the servers disagree anyway

    assert bridge.plan_for(day.registry.adapters["default"], "home") is None
    assert len(day.asks) == 1, "nothing new to report, so the first answer stands"
    assert rows_before == 1 and day.rows == []
    miss = bridge.miss_for(day.registry.adapters["default"], "home")
    assert miss is not None and miss.cause == "refused" and miss.code == "MACHINE_NOT_PAIRED"


def test_with_sharing_off_nothing_is_sent_and_the_refusal_stands(day, monkeypatch):
    monkeypatch.setattr(heartbeat, "_telemetry_enabled", lambda: False)
    day.registry.adapters["default"] = _Adapter()

    assert bridge.plan_for(day.registry.adapters["default"], "home") is None
    assert day.rows == [] and len(day.asks) == 1


def test_a_report_that_cannot_go_out_leaves_the_refusal_and_is_tried_again(day):
    heartbeat._send_heartbeat()
    day.registry.adapters["default"] = _Adapter()
    day.network_up = False

    assert bridge.plan_for(day.registry.adapters["default"], "home") is None
    assert len(day.asks) == 1 and len(day.rows) == 1

    day.network_up = True
    assert heartbeat._send_heartbeat() is True, "an unsent report is still owed"
    assert MODEL in day.reported()


def test_the_scheduler_reports_a_new_printer_without_being_asked(day):
    assert heartbeat._send_heartbeat() is True
    assert heartbeat._send_heartbeat() is False             # same day, nothing new
    day.registry.adapters["default"] = _Adapter()

    assert heartbeat._send_heartbeat() is True
    assert heartbeat._send_heartbeat() is False
    assert len(day.rows) == 2 and MODEL in day.reported()

    day.registry.adapters["second"] = _Adapter("prusa_mk4")
    assert heartbeat._send_heartbeat() is True
    assert heartbeat._send_heartbeat() is False
    assert len(day.rows) == 3
    assert sorted(day.rows[-1]["p_details"]["printer_models"]) == [MODEL, "prusa_mk4"]


def test_the_second_row_is_the_same_row_and_the_daily_sends_ride_only_the_first(day):
    heartbeat._send_heartbeat()
    day.registry.adapters["default"] = _Adapter()
    heartbeat._send_heartbeat()

    first, second = day.rows
    assert set(first) == set(second)
    assert set(first["p_details"]) == set(second["p_details"])
    assert first["p_installation_id"] == second["p_installation_id"]
    assert day.extras == 1


def test_the_same_model_spelled_in_another_case_is_not_a_new_printer(day):
    day.registry.adapters["default"] = _Adapter("Bambu_H2S")
    heartbeat._send_heartbeat()
    day.registry.adapters["default"] = _Adapter("bambu_h2s")

    assert heartbeat._send_heartbeat() is False
    assert len(day.rows) == 1


def test_a_restart_remembers_what_the_day_already_named(day, monkeypatch):
    day.registry.adapters["default"] = _Adapter()
    heartbeat._send_heartbeat()
    monkeypatch.setattr(heartbeat, "_sent_on", None)        # a new process, same day
    monkeypatch.setattr(heartbeat, "_named_in_process", frozenset())

    assert heartbeat._send_heartbeat() is False
    assert len(day.rows) == 1


def test_a_marker_that_holds_only_the_date_names_no_printer(day):
    """The marker an earlier release wrote: the printer here is reported once, then quiet."""
    heartbeat._LAST_BEAT_PATH.write_text(str(_FakeDate.today()), encoding="utf-8")
    day.registry.adapters["default"] = _Adapter()

    assert heartbeat._already_sent_today() is True
    assert heartbeat._send_heartbeat() is True
    assert heartbeat._send_heartbeat() is False
    assert len(day.rows) == 1 and day.extras == 0


def test_a_new_day_starts_from_nothing_named(day):
    day.registry.adapters["default"] = _Adapter()
    heartbeat._send_heartbeat()
    _FakeDate._today = _FakeDate._today + timedelta(days=1)

    assert heartbeat._send_heartbeat() is True              # the new day's own row
    assert heartbeat._send_heartbeat() is False
    assert len(day.rows) == 2 and day.extras == 2


def test_sharing_turned_off_while_the_server_runs_stops_every_later_row(day, monkeypatch):
    assert heartbeat._send_heartbeat() is True
    monkeypatch.setattr(heartbeat, "_telemetry_enabled", lambda: False)
    day.registry.adapters["default"] = _Adapter()

    assert heartbeat._send_heartbeat() is False             # the scheduler's next look
    _FakeDate._today = _FakeDate._today + timedelta(days=1)
    assert heartbeat._send_heartbeat() is False             # and the next day's
    assert len(day.rows) == 1


def test_every_ask_of_the_motion_door_goes_through_the_one_helper():
    """A second way to ask the door would be a second place the report is forgotten."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(bridge))
    askers = {
        fn.name
        for fn in ast.walk(tree) if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        for call in ast.walk(fn)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "_pro_api_call"
    }
    assert askers == {"_ask_service"}
