"""``check_material_sufficiency`` answers unknown for a weight Kiln does not hold.

The tool reads the weight of what is loaded: a linked spool's count, else the
loaded row's own figure.  With neither, ``sufficient`` is ``None``, no
shortfall is stated, ``unknown`` says what to record, and nothing is suggested.
A weight Kiln does hold is still judged: enough is ``True``, short is ``False``.
"""

from __future__ import annotations

import pytest

from kiln.persistence import KilnDB


@pytest.fixture()
def db(tmp_path):
    db = KilnDB(str(tmp_path / "inventory.db"))
    yield db
    db.close()


@pytest.fixture()
def check(db, monkeypatch):
    """The registered ``check_material_sufficiency`` tool, on one machine."""
    monkeypatch.setattr("kiln.persistence.get_db", lambda: db)
    monkeypatch.setattr("kiln.material_inventory.registered_machines", lambda: 1)
    tools: dict = {}

    class _MCP:
        def tool(self, **_kwargs):
            def register(fn):
                tools[fn.__name__] = fn
                return fn

            return register

    from kiln.plugins.material_inventory_tools import plugin

    plugin.register(_MCP())

    def ask(printer_name: str, required_grams: float) -> dict:
        reply = tools["check_material_sufficiency"](printer_name=printer_name, required_grams=required_grams)
        assert reply["success"] is True, reply
        return reply["check"]

    return ask


def _spool(db, spool_id: str, remaining_grams: float) -> None:
    db.save_spool({
        "id": spool_id, "material_type": "PLA", "color": "white", "brand": "Acme",
        "weight_grams": 1000.0, "remaining_grams": remaining_grams,
    })


def test_a_linked_spool_is_the_weight_that_is_checked(db, check):
    _spool(db, "full", 1000.0)
    db.save_material("bench", 0, "PLA", spool_id="full", remaining_grams=None)

    answer = check("bench", 50.0)

    assert answer["sufficient"] is True
    assert answer["loaded_grams"] == 1000.0
    assert answer["shortfall_grams"] == 0.0
    assert answer["unknown"] is None


def test_a_linked_spool_that_is_short_is_a_shortfall(db, check):
    _spool(db, "low", 20.0)
    db.save_material("bench", 0, "PLA", spool_id="low", remaining_grams=None)

    answer = check("bench", 50.0)

    assert answer["sufficient"] is False
    assert answer["loaded_grams"] == 20.0
    assert answer["shortfall_grams"] == 30.0
    assert answer["unknown"] is None


def test_a_material_with_no_weight_is_unknown_not_short(db, check):
    db.save_material("bench", 0, "PLA", remaining_grams=None)

    answer = check("bench", 50.0)

    assert answer["sufficient"] is None
    assert answer["loaded_grams"] is None
    assert answer["shortfall_grams"] is None
    assert "no weight" in answer["unknown"] and "not a shortfall" in answer["unknown"]
    assert answer["suggestions"] == [] and answer["alternative_printers"] == []


def test_a_printer_kiln_was_never_told_about_is_unknown_not_short(check):
    answer = check("never-described", 50.0)

    assert answer["sufficient"] is None
    assert answer["shortfall_grams"] is None
    assert "has not been told" in answer["unknown"] and "not a shortfall" in answer["unknown"]
    assert answer["suggestions"] == []


def test_an_empty_spool_is_a_shortfall_and_reads_differently_from_unknown(db, check):
    _spool(db, "empty", 0.0)
    db.save_material("empty-bench", 0, "PLA", spool_id="empty", remaining_grams=None)
    db.save_material("unweighed-bench", 0, "PLA", remaining_grams=None)

    empty = check("empty-bench", 50.0)
    unweighed = check("unweighed-bench", 50.0)

    assert empty["sufficient"] is False and empty["shortfall_grams"] == 50.0 and empty["unknown"] is None
    assert unweighed["sufficient"] is None and unweighed["shortfall_grams"] is None
    drop = {"printer_name", "unknown"}
    assert {k: v for k, v in empty.items() if k not in drop} != {k: v for k, v in unweighed.items() if k not in drop}
