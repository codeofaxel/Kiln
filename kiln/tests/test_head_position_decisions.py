"""Every printer backend says how Kiln learns its own pause and cancel moves.

A backend whose ``get_tool_position`` answers the head and the extruder as
the head moves is learned from the pauses and cancels Kiln sends
(:mod:`kiln.head_moves`); any other is taught by its owner through the
guided session's questions.  Which one a backend is must be a decision,
never an inheritance: a backend added next year states its own
``head_position`` or this fails, and the statement has to agree with what
the backend's code does.  Also pinned: the position reads behind the
backends that do say -- Klipper's live position, RepRapFirmware's object
model, and Creality's through its Moonraker backend.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest

import kiln.printers as printers_pkg
from kiln import bench
from kiln.printers.base import PrinterAdapter

#: The backends Kiln ships today; the scan must find at least these, so an
#: import that silently found nothing cannot pass.
KNOWN = {"BambuAdapter", "CrealityAdapter", "DuetAdapter", "ElegooAdapter", "MoonrakerAdapter",
         "OctoPrintAdapter", "PrusaLinkAdapter", "SerialPrinterAdapter"}


def _backends() -> list[type]:
    found: list[type] = []
    for info in pkgutil.iter_modules(printers_pkg.__path__):
        try:
            module = importlib.import_module(f"kiln.printers.{info.name}")
        except Exception:  # noqa: BLE001 -- a module needing an optional extra is checked where it imports
            continue
        for _name, cls in inspect.getmembers(module, inspect.isclass):
            if (issubclass(cls, PrinterAdapter) and cls is not PrinterAdapter and cls.__module__ == module.__name__
                    and not inspect.isabstract(cls)):
                found.append(cls)
    return found


class TestEveryBackendDecides:
    def test_the_scan_finds_every_backend_kiln_ships(self):
        assert {cls.__name__ for cls in _backends()} >= KNOWN

    @pytest.mark.parametrize("cls", _backends(), ids=lambda c: c.__name__)
    def test_each_states_its_own_answer_and_the_answer_matches_its_code(self, cls):
        stated = cls.__dict__.get("head_position")
        assert isinstance(stated, str) and stated.strip(), (
            f"{cls.__name__} does not state head_position: say 'head_and_extruder' if its get_tool_position "
            "answers x, y, z and e while the head moves, else a sentence saying why not")
        reads = cls.get_tool_position is not PrinterAdapter.get_tool_position
        assert (stated == "head_and_extruder") == reads, (
            f"{cls.__name__} says {stated!r} but its get_tool_position is "
            f"{'its own' if reads else 'the base one, which answers nothing'}")

    def test_only_a_backend_that_says_head_and_extruder_is_ever_polled(self):
        polled = {cls.__name__ for cls in _backends() if cls.__dict__.get("head_position") == "head_and_extruder"}
        assert polled == {"CrealityAdapter", "DuetAdapter", "MoonrakerAdapter", "SerialPrinterAdapter"}


class TestThePositionReads:
    def test_klipper_reads_where_the_head_is_and_the_extruder(self, monkeypatch):
        from kiln.printers.moonraker import MoonrakerAdapter

        adapter = MoonrakerAdapter(host="http://klipper.local:7125")
        asked: list = []

        def _get_json(path, params=None):
            asked.append((path, params))
            return {"result": {"status": {"motion_report": {"live_position": [10.0, 20.0, 5.0, 123.4]}}}}

        monkeypatch.setattr(adapter, "_get_json", _get_json)
        assert adapter.get_tool_position() == {"x": 10.0, "y": 20.0, "z": 5.0, "e": 123.4}
        assert asked == [("/printer/objects/query", {"motion_report": "live_position"})]
        assert bench.position_reading(adapter) == (10.0, 20.0, 5.0, 123.4) and bench.logs_positions(adapter)

    def test_reprapfirmware_3_reads_the_object_model_and_2_reads_nothing(self, monkeypatch):
        from kiln.printers.duet import DuetAdapter

        adapter = DuetAdapter(host="http://duet.local", timeout=5, retries=1)
        move = {"axes": [{"letter": "X", "machinePosition": 12.5}, {"letter": "Y", "machinePosition": 30.0},
                         {"letter": "Z", "machinePosition": 4.2}, {"letter": "U", "machinePosition": 0.0}],
                "extruders": [{"position": 88.0}]}
        monkeypatch.setattr(adapter, "_generation", lambda: 3)
        monkeypatch.setattr(adapter, "_model", lambda key, flags="d99": move if key == "move" else None)
        assert adapter.get_tool_position() == {"x": 12.5, "y": 30.0, "z": 4.2, "e": 88.0}
        monkeypatch.setattr(adapter, "_generation", lambda: 2)
        assert adapter.get_tool_position() is None

    def test_creality_reads_through_its_moonraker_backend(self):
        from types import SimpleNamespace

        from kiln.printers.creality import CrealityAdapter

        # Built without its constructor, which goes looking for Moonraker on the network.
        adapter = CrealityAdapter.__new__(CrealityAdapter)
        adapter._backend = SimpleNamespace(get_tool_position=lambda: {"x": 1.0, "y": 2.0, "z": 3.0, "e": 4.0})
        adapter.host = ""
        assert adapter.get_tool_position() == {"x": 1.0, "y": 2.0, "z": 3.0, "e": 4.0}

    def test_a_backend_that_cannot_say_is_never_asked_at_all(self):
        from kiln.printers.octoprint import OctoPrintAdapter

        adapter = OctoPrintAdapter.__new__(OctoPrintAdapter)
        assert bench.logs_positions(adapter) is False
