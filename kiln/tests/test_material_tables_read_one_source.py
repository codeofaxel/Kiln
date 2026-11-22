"""Two material lookups read the table that owns their answer, never a copy.

* ``kiln.materials.normalise_material_type`` writes a short name out through
  the shared spelling map, ``kiln.catalog_keys._MATERIAL_ABBREVIATIONS``.
* ``kiln preflight --material`` checks the printer's targets against the
  ranges the live-printer G-code checks read, ``kiln.gcode._MATERIAL_TEMPS``.

Each lookup gets two tests: every name it answered still gets the same
answer, and the answer moves when the owning table moves.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from kiln import catalog_keys, gcode
from kiln.cli.main import cli
from kiln.materials import MATERIAL_TEMPS, normalise_material_type
from kiln.printers.base import PrinterCapabilities, PrinterState, PrinterStatus

# ---------------------------------------------------------------------------
# normalise_material_type
# ---------------------------------------------------------------------------

#: The short names normalise_material_type wrote out from its own map, and
#: what it answered for each.
SHORT_NAMES_ANSWERED = {"PA": "NYLON", "PA6": "NYLON", "PA12": "NYLON"}


@pytest.mark.parametrize(("raw", "answer"), sorted(SHORT_NAMES_ANSWERED.items()))
def test_every_short_name_gets_the_answer_it_got(raw, answer):
    assert normalise_material_type(raw) == answer
    assert normalise_material_type(f"  {raw.lower()} ") == answer


@pytest.mark.parametrize("name", sorted(MATERIAL_TEMPS))
def test_a_name_with_defaults_is_returned_as_it_is(name):
    assert normalise_material_type(name) == name
    assert normalise_material_type(name.lower()) == name


@pytest.mark.parametrize("raw", ["", None, "POLYCARBONATE", "PETG-CF", "PA11", "wood"])
def test_a_name_without_defaults_is_unknown(raw):
    # "POLYCARBONATE" is what the shared map writes "PC" out to; this table
    # spells it "PC", so the long form stays unknown here, as it was.
    assert normalise_material_type(raw) is None


def test_a_short_name_added_to_the_shared_map_is_read(monkeypatch):
    monkeypatch.setitem(catalog_keys._MATERIAL_ABBREVIATIONS, "pa11", "nylon")
    assert normalise_material_type("PA11") == "NYLON"


# ---------------------------------------------------------------------------
# kiln preflight --material
# ---------------------------------------------------------------------------

#: Every material ``kiln preflight --material`` accepts.
PREFLIGHT_MATERIALS = ("PLA", "PETG", "ABS", "TPU", "ASA", "Nylon", "PC")


def _preflight(material: str, tool_target: float, bed_target: float) -> dict:
    adapter = MagicMock()
    adapter.name = "mock"
    adapter.capabilities = PrinterCapabilities()
    adapter.get_state.return_value = PrinterState(
        connected=True,
        state=PrinterStatus.IDLE,
        tool_temp_actual=22.0,
        tool_temp_target=tool_target,
        bed_temp_actual=22.0,
        bed_temp_target=bed_target,
    )
    with patch("kiln.cli.main._get_adapter_from_ctx", return_value=adapter):
        result = CliRunner().invoke(cli, ["preflight", "--material", material, "--json"], catch_exceptions=False)
    checks = json.loads(result.output)["data"]["checks"]
    (match,) = [c for c in checks if c["name"] == "material_match"]
    return match


@pytest.mark.parametrize("material", PREFLIGHT_MATERIALS)
def test_preflight_checks_every_material_against_the_kept_range(material):
    tool_min, tool_max, bed_min, bed_max = gcode._MATERIAL_TEMPS[material]

    assert _preflight(material, tool_min, bed_min)["passed"] is True
    assert _preflight(material, tool_max, bed_max)["passed"] is True

    too_hot = _preflight(material, tool_max + 1, bed_max + 1)
    assert too_hot["passed"] is False
    assert too_hot["message"] == (
        f"Tool target ({tool_max + 1:.0f}C) outside {material} range ({tool_min:g}-{tool_max:g}C); "
        f"Bed target ({bed_max + 1:.0f}C) outside {material} range ({bed_min:g}-{bed_max:g}C)"
    )
    # A whole-degree range reads as whole degrees, never "180.0-220.0C".
    assert ".0-" not in too_hot["message"]


def test_preflight_follows_the_kept_range(monkeypatch):
    assert _preflight("PLA", 200.0, 60.0)["passed"] is True
    monkeypatch.setitem(gcode._MATERIAL_TEMPS, "PLA", (100.0, 120.0, 10.0, 20.0))
    moved = _preflight("PLA", 200.0, 60.0)
    assert moved["passed"] is False
    assert "outside PLA range (100-120C)" in moved["message"]
