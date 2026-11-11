"""Every one-machine material answer reads a linked spool's weight.

A printer whose loaded material links a spool holds that spool's count.
``find_printers_with_material``, ``optimize_fleet_assignment`` and
``suggest_spool_swaps`` answer from it, as ``check_material_sufficiency`` does.
"""

from __future__ import annotations

import pytest

from kiln.persistence import KilnDB


@pytest.fixture()
def db(tmp_path):
    db = KilnDB(str(tmp_path / "inventory.db"))
    db.save_spool({"id": "full", "material_type": "PLA", "color": "white", "brand": "Acme",
                   "weight_grams": 1000.0, "remaining_grams": 1000.0})
    db.save_spool({"id": "shelf", "material_type": "PLA", "color": "black", "brand": "Acme",
                   "weight_grams": 1000.0, "remaining_grams": 800.0})
    db.save_material("bench", 0, "PLA", color="white", spool_id="full", remaining_grams=None)
    yield db
    db.close()


@pytest.fixture()
def tools(db, monkeypatch):
    """The registered material tools, on an install with one machine."""
    monkeypatch.setattr("kiln.persistence.get_db", lambda: db)
    monkeypatch.setattr("kiln.material_inventory.registered_machines", lambda: 1)
    captured: dict = {}

    class _MCP:
        def tool(self, **_kwargs):
            def register(fn):
                captured[fn.__name__] = fn
                return fn

            return register

    from kiln.plugins.material_inventory_tools import plugin

    plugin.register(_MCP())
    return captured


def test_find_printers_counts_the_linked_spool(tools):
    reply = tools["find_printers_with_material"](material_type="PLA", min_grams=500.0)

    assert reply["success"] is True, reply
    assert [(p["printer_name"], p["remaining_grams"]) for p in reply["printers"]] == [("bench", 1000.0)]


def test_a_job_the_linked_spool_covers_is_assigned(tools):
    reply = tools["optimize_fleet_assignment"](
        jobs=[{"file_name": "part.gcode", "material_type": "PLA", "required_grams": 50.0}],
    )

    assert reply["success"] is True, reply
    (assignment,) = reply["assignments"]
    assert assignment["recommended_printer"] == "bench"
    assert assignment["material_match"] is True
    assert assignment["remaining_after_print_grams"] == 950.0


def test_no_swap_is_suggested_when_the_linked_spool_covers_the_jobs(tools):
    reply = tools["suggest_spool_swaps"](jobs=[{"material_type": "PLA", "required_grams": 50.0}])

    assert reply["success"] is True, reply
    assert reply["swap_suggestions"] == []


def test_the_summary_counts_a_linked_spool_once(tools):
    reply = tools["get_fleet_material_summary"]()

    assert reply["success"] is True, reply
    (pla,) = [row for row in reply["summary"] if row["material_type"] == "PLA"]
    assert pla["total_grams"] == 1800.0
