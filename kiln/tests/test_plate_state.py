"""The plate record's public face: the shape every door shares, and the floor
without the served record.

The record itself -- who wrote it, what it read from a print file, when it
is re-asserted -- is served through kiln-pro and pinned there.  What public
Kiln promises is narrower and is pinned here: the two dataclasses round-trip
and describe themselves in one clause, a plate with no served record reads
as ``unknown`` and never as ``clear``, the raise arithmetic is honest, and a
served plan reaches the doors only when it is well-formed.
"""

from __future__ import annotations

import sys

import pytest

from kiln.plate_state import (
    STATUSES,
    PlateJob,
    PlateState,
    machine_id,
    mark_clear,
    mark_occupied,
    mark_occupied_by_start,
    plan_motion_around_plate,
    plate_occupancy,
    raise_clearance_mm,
    read,
)


@pytest.fixture
def no_kiln_pro(monkeypatch):
    for name in list(sys.modules):
        if name == "kiln_pro" or name.startswith("kiln_pro."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


class _Machine:
    """An adapter with a durable identity, and one without."""

    def __init__(self, serial: str | None = "01P00A000000001"):
        self.serial = serial
        self.host = None if serial is None else "192.168.1.5"


class TestTheShape:
    def test_a_job_round_trips_and_tolerates_bad_geometry(self):
        job = PlateJob(file="vase.gcode", footprint_mm=[1, 2, 3, 4], max_z_mm=60.0, printer_id="bambu_a1")
        assert PlateJob.from_dict(job.to_dict()) == job
        assert PlateJob.from_dict({"file": "x", "footprint_mm": [1, 2], "max_z_mm": "tall"}) == PlateJob(file="x")
        assert PlateJob.from_dict({"footprint_mm": [1, 2, 3, 4]}) is None
        assert PlateJob.from_dict("vase.gcode") is None

    def test_a_state_round_trips_and_anything_malformed_reads_as_unknown(self):
        state = PlateState(machine="m", status="occupied", source="kiln_started_print", since="2026-09-16T18:12:00",
                           job=PlateJob(file="vase.gcode", max_z_mm=60.0))
        again = PlateState.from_dict("m", state.to_dict())
        assert again.status == "occupied" and again.job == state.job and again.since == state.since
        for bad in (None, "clear", {"status": "empty"}, {"status": "clear", "since": 5}):
            got = PlateState.from_dict("m", bad)
            assert got.status == "unknown" or (bad == {"status": "clear", "since": 5} and got.since is None)
        assert set(STATUSES) == {"unknown", "occupied", "clear"}

    def test_describe_is_one_quotable_clause(self):
        occupied = PlateState(machine="m", status="occupied", since="2026-09-16T18:12:00",
                              job=PlateJob(file="vase.gcode", max_z_mm=60.0))
        assert "vase.gcode" in occupied.describe() and "up to 60 mm tall" in occupied.describe()
        nameless = PlateState(machine="m", status="occupied", since="not-a-time")
        assert "holds a part" in nameless.describe() and "height unknown" in nameless.describe() and "not-a-time" in nameless.describe()
        cleared = PlateState(machine="m", status="clear", source="human", since="2026-09-16T18:12:00")
        assert "cleared at" in cleared.describe() and "a person said so" in cleared.describe()
        assert PlateState(machine="m").describe() == "Kiln has no record of what is on the plate"
        assert PlateState(machine="m").since_clock() == "an unknown time"

    def test_to_dict_carries_the_description(self):
        d = PlateState(machine="m").to_dict()
        assert d["status"] == "unknown" and d["description"] and d["job"] is None


class TestTheFloor:
    def test_a_machine_without_identity_cannot_be_recorded(self, no_kiln_pro):
        state = read(_Machine(serial=None))
        assert state.status == "unknown" and "neither a serial nor an address" in state.note
        assert machine_id(_Machine(serial=None)) == ""

    def test_without_the_served_record_every_plate_is_unknown_and_nothing_is_written(self, no_kiln_pro):
        machine = _Machine()
        assert machine_id(machine)
        state = plate_occupancy(machine)
        assert state.status == "unknown" and not state.clear and not state.occupied
        assert "served through Kiln's hosted service" in state.note
        assert mark_clear(machine, "human") is False
        assert mark_occupied(machine, PlateJob(file="vase.gcode"), source="kiln_started_print") is False
        assert mark_occupied(machine, {"file": "vase.gcode"}) is False
        assert mark_occupied_by_start(machine, "vase.gcode", plate_number=1) is False
        assert plate_occupancy(machine).status == "unknown"

    def test_a_served_fault_reads_as_unknown_never_as_clear(self, monkeypatch):
        from kiln import _pro_motion_bridge as bridge

        def _boom(adapter):
            raise RuntimeError("overlay exploded")

        monkeypatch.setattr(bridge, "plate_occupancy", _boom)
        assert read(_Machine()).status == "unknown"
        monkeypatch.setattr(bridge, "plate_occupancy", lambda a: {"status": "clear"})  # the wrong type is not a record
        assert read(_Machine()).status == "unknown"

    def test_a_served_record_is_handed_back_as_is(self, monkeypatch):
        from kiln import _pro_motion_bridge as bridge

        served = PlateState(machine="m", status="clear", source="human", since="2026-09-16T18:12:00")
        monkeypatch.setattr(bridge, "plate_occupancy", lambda a: served)
        assert plate_occupancy(_Machine()) is served
        seen = {}
        monkeypatch.setattr(bridge, "mark_clear", lambda a, source, note: seen.update(source=source, note=note) or True)
        assert mark_clear(_Machine(), "human", note="looked") is True and seen == {"source": "human", "note": "looked"}


class TestTheRaise:
    def test_clearance_is_the_probe_up_minus_the_settle(self):
        assert raise_clearance_mm({"raise_before_travel": {"probe_up_mm": 40, "back_down_mm": 15}}) == 25
        assert raise_clearance_mm({"raise_before_travel": {"probe_up_mm": 25, "back_down_mm": 15}}) == 10
        assert raise_clearance_mm(None) is None
        assert raise_clearance_mm({"raise_before_travel": {"probe_up_mm": "up"}}) is None
        assert raise_clearance_mm({"chute": {}}) is None


class TestTheServedPlan:
    def _served(self, monkeypatch, plan):
        from kiln import _pro_motion_bridge as bridge

        monkeypatch.setattr(bridge, "plan_motion_around_plate", lambda *a: plan)

    def test_no_kiln_pro_means_no_plan(self, no_kiln_pro):
        assert plan_motion_around_plate(PlateState(machine="m"), None, action="park", clearance_mm=25) is None

    def test_a_well_formed_plan_is_handed_to_the_doors(self, monkeypatch):
        step = {"label": "lift", "you_will_see": "the head lifts", "stops_when": "the move ends", "gcode": ["G91", "G1 Z10"]}
        self._served(monkeypatch, [step])
        got = plan_motion_around_plate(PlateState(machine="m"), None, action="park", clearance_mm=25)
        assert got == [{**step, "leaves": []}]

    @pytest.mark.parametrize("plan", [
        [], "lift", [{"label": "lift"}],
        [{"label": "lift", "you_will_see": "x", "stops_when": "y", "gcode": "G1 Z10"}],
        [{"label": "lift", "you_will_see": "x", "stops_when": "y", "gcode": ["G1 Z10"], "leaves": "heater"}],
        [{"label": "", "you_will_see": "x", "stops_when": "y", "gcode": ["G1 Z10"]}],
    ])
    def test_anything_malformed_reads_as_no_plan(self, monkeypatch, plan):
        self._served(monkeypatch, plan)
        assert plan_motion_around_plate(PlateState(machine="m"), None, action="park", clearance_mm=25) is None
