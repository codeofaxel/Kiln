"""Whether a colouring's colours are on the printer, said only when it matters.

Coverage, through the registered tools against a stand-in printer and the
real spool inventory: a loaded colour says nothing; a colour on a spool the
person recorded but has not loaded says to load it, with no buying talk; a
colour with nothing on record gets one offer of help and no brand names;
a printer Kiln cannot read (none, no unit, colours not read, the hosted
server) leaves the field out.  The print material counts against a spool
of a clearly different material.  The print gate's refusal and the approval
dialog word an unsupplied colour the same way, with the gate's decision
unchanged.  And package-wide: no colouring reply carries a list of
filaments to buy.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiln import server
from kiln.decoration_faces import record_decoration_faces
from kiln.printers.bambu import BambuAdapter
from tests.test_color_tools import _make_triangle, _write_test_stl
from tests.test_decoration_faces import _box_triangles, _decorated_triangles, _write_stl

_PACKAGE = Path(__file__).resolve().parent.parent / "src" / "kiln"

#: Makers a buying list would name.  None of them is a printer brand, so a
#: printer's own name in a sentence never trips this.
_MAKERS = ("Polymaker", "eSUN", "Prusament", "Sunlu", "Overture", "Hatchbox", "Jayo", "Elegoo PLA")

_RED = "#F72323"
_WHITE = "#FFFFFF"


def _ams(*colours: str) -> dict[str, Any]:
    """A one-unit AMS reading with these colours loaded as PLA, slot A1 up."""
    return {
        "units": [
            {
                "unit_id": "0",
                "trays": [
                    {"slot": str(i), "tray_type": "PLA", "tray_color": f"{c.lstrip('#')}FF"}
                    for i, c in enumerate(colours)
                ],
            }
        ],
    }


class _Printer:
    """A Bambu as the colour reader sees one: it reports its AMS."""

    name = "bambu"

    def __init__(self, ams: dict[str, Any] | None) -> None:
        self._ams = ams
        self._kiln_registered_name = "workshop"

    def get_ams_status(self) -> dict[str, Any] | None:
        return self._ams


@pytest.fixture
def printer(monkeypatch):
    """Point every printer lookup at one stand-in named "workshop"; returns a setter."""
    holder: dict[str, Any] = {"adapter": _Printer(_ams(_WHITE))}

    def resolve(name=None):
        adapter = holder["adapter"]
        if adapter is None:
            raise RuntimeError("no printer registered")
        return adapter

    monkeypatch.setattr(server, "_resolve_adapter", resolve)
    monkeypatch.setattr(server, "_resolve_effective_printer_name", lambda name=None: "workshop")
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)

    def load(adapter: Any) -> None:
        holder["adapter"] = adapter

    return load


@pytest.fixture
def shelf(monkeypatch):
    """The real spool inventory, bound to this test's database."""
    monkeypatch.setattr(server, "_material_tracker", None)

    def add(material: str, color: str, brand: str | None = None) -> None:
        assert server.add_spool(material=material, color=color, brand=brand)["success"] is True

    return add


def _tools() -> dict[str, Any]:
    from kiln.plugins.color_tools import _ColorToolsPlugin

    tools: dict[str, Any] = {}

    class _FakeMcp:
        def tool(self, **_kwargs):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

    _ColorToolsPlugin().register(_FakeMcp())
    return tools


def _stl(tmp_path: Path) -> str:
    path = str(tmp_path / "part.stl")
    _write_test_stl([_make_triangle(0, 0, 0), _make_triangle(5, 5, 5), _make_triangle(10, 10, 10)], path)
    return path


def _carved(tmp_path: Path) -> str:
    """A debossed box with its decoration face record beside it."""
    original, decorated = str(tmp_path / "jar.stl"), str(tmp_path / "jar_deboss.stl")
    _write_stl(_box_triangles(), original)
    parts = _decorated_triangles()
    _write_stl([t for name in ("base", "surround", "island", "floor", "walls") for t in parts[name]], decorated)
    record_decoration_faces(original, decorated, face_normal=(0, 0, 1))
    return decorated


def _colour_red(tmp_path: Path) -> dict[str, Any]:
    result = _tools()["auto_color_by_height"](input_path=_stl(tmp_path), num_colors=1, color_palette=[_RED])
    assert result["success"] is True, result.get("error")
    return result


def _assert_no_buying_talk(result: dict[str, Any]) -> None:
    text = json.dumps(result)
    assert "closest_filaments" not in result
    assert "ams_advisory" not in result
    for maker in _MAKERS:
        assert maker not in text, maker


# ---------------------------------------------------------------------------
# The four answers, through a colouring tool
# ---------------------------------------------------------------------------


class TestWhatAColouringSays:
    def test_a_loaded_colour_says_nothing_about_filament(self, printer, tmp_path):
        printer(_Printer(_ams(_WHITE, _RED)))
        result = _colour_red(tmp_path)
        assert result["colour_availability"] == {
            "printer": "workshop",
            "colours": [{"colour": _RED, "state": "loaded", "slot": "slot A2"}],
            "say": "",
        }
        _assert_no_buying_talk(result)

    def test_a_spool_on_record_but_not_loaded_is_named_with_no_buying_talk(self, printer, shelf, tmp_path):
        shelf("PLA", "Red", brand="Polymaker")
        result = _colour_red(tmp_path)
        availability = result["colour_availability"]
        assert availability["colours"] == [{"colour": _RED, "state": "owned", "spool": "Polymaker Red PLA"}]
        assert availability["say"] == (
            "You have Polymaker Red PLA but it isn't loaded on workshop. Load it before printing."
        )
        assert "help finding" not in availability["say"]
        assert "closest_filaments" not in result

    def test_nothing_on_record_gets_one_offer_and_no_names(self, printer, tmp_path):
        result = _colour_red(tmp_path)
        availability = result["colour_availability"]
        assert availability["colours"] == [{"colour": _RED, "state": "missing"}]
        assert availability["say"] == (
            "I don't see red loaded on workshop. Want help finding a filament that suits this print?"
        )
        _assert_no_buying_talk(result)

    def test_the_prints_material_rules_out_a_spool_of_another_plastic(self, printer, shelf, tmp_path):
        """A red PLA spool is not "one to load" for a PETG print; with the
        material unsaid, the colouring cannot know and names it."""
        shelf("PLA", "red", brand="Polymaker")
        tool = _tools()["auto_color_by_height"]
        unsaid = tool(input_path=_stl(tmp_path), num_colors=1, color_palette=[_RED])
        assert unsaid["colour_availability"]["colours"][0]["state"] == "owned"
        petg = tool(input_path=_stl(tmp_path), num_colors=1, color_palette=[_RED], material="PETG")
        assert petg["colour_availability"]["colours"] == [{"colour": _RED, "state": "missing"}]
        assert "Polymaker" not in petg["colour_availability"]["say"]

    def test_a_shade_with_no_name_is_said_in_plain_words_and_once(self, printer, tmp_path):
        """A textured model's shades are codes nobody named.  The sentence
        says them as the print gate's own messages do, and two shades that
        share a plain name are said once."""
        result = _tools()["auto_color_by_height"](
            input_path=_stl(tmp_path), num_colors=3, color_palette=["#0A0A14", "#606070", "#70707F"],
        )
        say = result["colour_availability"]["say"]
        assert say == (
            "I don't see black or grey loaded on workshop. Want help finding filaments that suit this print?"
        )
        assert "#" not in say

    @pytest.mark.parametrize(
        "reading",
        [
            pytest.param(None, id="no-printer"),
            pytest.param(_Printer({"units": [], "ams_exist_bits": "0", "tray_exist_bits": "0"}), id="no-unit"),
            pytest.param(_Printer(_ams("#000000")), id="colours-not-read"),
        ],
    )
    def test_a_printer_kiln_cannot_read_leaves_the_field_out(self, printer, tmp_path, reading):
        printer(reading)
        result = _colour_red(tmp_path)
        assert "colour_availability" not in result
        _assert_no_buying_talk(result)

    def test_the_hosted_server_says_nothing(self, printer, shelf, tmp_path, monkeypatch):
        shelf("PLA", "red", brand="Polymaker")
        # A printer reading and a spool on record both exist here, so only
        # the hosted posture can be what keeps the reply quiet.
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        result = _colour_red(tmp_path)
        assert "colour_availability" not in result
        _assert_no_buying_talk(result)

    def test_a_spool_already_loaded_is_not_also_on_the_shelf(self, printer, shelf, tmp_path):
        # Two reds asked for, one red spool, and it is in the printer: the
        # second red is not "a red you have but haven't loaded".
        printer(_Printer(_ams(_RED)))
        shelf("PLA", "red", brand="Polymaker")
        result = _tools()["auto_color_by_height"](
            input_path=_stl(tmp_path),
            num_colors=2,
            color_palette=[_RED, "#E01B1B"],
        )
        states = [c["state"] for c in result["colour_availability"]["colours"]]
        assert states == ["loaded", "missing"]

    def test_several_colours_make_at_most_two_sentences(self, printer, shelf, tmp_path):
        shelf("PLA", "red", brand="Polymaker")
        shelf("PLA", "black", brand="Sunlu")
        result = _tools()["auto_color_by_height"](
            input_path=_stl(tmp_path),
            num_colors=4,
            color_palette=[_WHITE, _RED, "#161616", "#3A7BD5"],
        )
        availability = result["colour_availability"]
        assert [c["state"] for c in availability["colours"]] == ["loaded", "owned", "owned", "missing"]
        say = availability["say"]
        assert say == (
            "You have red PLA and black PLA but they aren't loaded on workshop, so load them before "
            "printing. I don't see blue loaded there either; want help finding a filament that suits "
            "this print?"
        )
        # Several spools are named by colour and material, never as a list of brands.
        assert "Polymaker" not in say and "Sunlu" not in say


# ---------------------------------------------------------------------------
# Every colouring door, and the material a print names
# ---------------------------------------------------------------------------


class TestEveryColouringDoor:
    def test_colour_by_region(self, printer, tmp_path):
        result = _tools()["auto_color_by_region"](
            input_path=_stl(tmp_path),
            num_colors=2,
            method="normal",
            color_palette=[_WHITE, _RED],
        )
        assert [c["state"] for c in result["colour_availability"]["colours"]] == ["loaded", "missing"]
        _assert_no_buying_talk(result)

    def test_paint_decoration_faces(self, printer, tmp_path):
        result = _tools()["paint_decoration_faces"](
            model_path=_carved(tmp_path),
            color=_RED,
            base_color=_WHITE,
            output_path=str(tmp_path / "painted.3mf"),
        )
        assert result["success"] is True, result.get("error")
        assert {c["colour"]: c["state"] for c in result["colour_availability"]["colours"]} == {
            _RED: "missing",
            _WHITE: "loaded",
        }
        _assert_no_buying_talk(result)

    def test_compose_multicolor_3mf(self, printer, tmp_path):
        body, mark = str(tmp_path / "body.stl"), str(tmp_path / "mark.stl")
        _write_test_stl([_make_triangle(0, 0, 0), _make_triangle(5, 5, 5)], body)
        _write_test_stl([_make_triangle(1, 1, 1), _make_triangle(2, 2, 2)], mark)
        with patch("kiln.server._check_auth", return_value=None):
            result = server.compose_multicolor_3mf(
                parts=[
                    {"stl_path": body, "extruder": 1, "color": _WHITE},
                    {"stl_path": mark, "extruder": 2, "color": _RED},
                ],
                output_path=str(tmp_path / "plate.3mf"),
            )
        assert result["success"] is True, result.get("error")
        assert [c["state"] for c in result["colour_availability"]["colours"]] == ["loaded", "missing"]
        _assert_no_buying_talk(result)

    @pytest.mark.parametrize(("spool_material", "state"), [("PLA", "owned"), ("PETG", "missing")])
    def test_wrap_gcode_counts_a_spool_only_of_the_prints_material(
        self,
        printer,
        shelf,
        monkeypatch,
        spool_material,
        state,
    ):
        shelf(spool_material, "red", brand="Polymaker")
        adapter = MagicMock(spec=BambuAdapter)
        adapter.wrap_gcode_as_3mf.return_value = "/tmp/output.3mf"
        adapter.get_ams_status.return_value = _ams(_WHITE)
        monkeypatch.setattr(server, "_get_adapter", lambda *a, **k: adapter)
        result = server.wrap_gcode_as_3mf(
            gcode_path="/tmp/test.gcode",
            filament_type="PLA",
            num_filaments=2,
            filament_colors=[_WHITE, _RED],
            filament_types=["PLA", "PLA"],
        )
        assert result["success"] is True, result.get("error")
        assert [c["state"] for c in result["colour_availability"]["colours"]] == ["loaded", state]
        assert "closest_filaments" not in result and "ams_advisory" not in result


class TestNoColouringReplyCarriesABuyingList:
    def test_no_module_writes_a_closest_filaments_field(self):
        """Package-wide: no code under ``kiln`` names the field a buying list rode in."""
        writers: list[str] = []
        for path in sorted(_PACKAGE.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                field = isinstance(node, ast.Constant) and node.value == "closest_filaments"
                helper = (
                    isinstance(node, (ast.Name, ast.Attribute))
                    and (getattr(node, "id", None) or getattr(node, "attr", None)) == "attach_closest_filaments"
                )
                if field or helper:
                    writers.append(f"{path.relative_to(_PACKAGE)}:{node.lineno}")
        assert writers == [], writers

    def test_every_colouring_tool_tells_the_agent_to_relay_and_not_to_sell(self):
        docs = {name: fn.__doc__ or "" for name, fn in _tools().items()}
        docs["wrap_gcode_as_3mf"] = server.wrap_gcode_as_3mf.__doc__ or ""
        docs["compose_multicolor_3mf"] = server.compose_multicolor_3mf.__doc__ or ""
        for name, doc in docs.items():
            flat = " ".join(doc.split())
            assert "colour_availability" in flat, name
            assert "never suggest buying filament" in flat, name
            assert "find_closest_filaments" in flat, name


# ---------------------------------------------------------------------------
# The print doors word an unsupplied colour the same way
# ---------------------------------------------------------------------------


def _two_colour_gcode(tmp_path: Path) -> str:
    path = tmp_path / "jar.gcode"
    path.write_text("G28\n; filament_colour = #FFFFFF;#F72323\n; filament_type = PLA;PLA\n")
    return str(path)


class TestThePrintDoorsSayTheSame:
    def test_the_gate_names_the_spool_to_load_and_still_refuses(self, printer, shelf, tmp_path):
        shelf("PLA", "red", brand="Polymaker")
        got = server._resolve_use_ams("auto", None, _Printer(_ams(_WHITE)), file_path=_two_colour_gcode(tmp_path))
        assert got["blocked"] is True and got["ams_mapping"] is None
        assert "cannot supply them all" in got["warnings"][0]
        assert (
            "You have Polymaker red PLA but it isn't loaded on workshop. Load it before printing." in got["warnings"][0]
        )

    def test_the_gate_offers_help_once_when_nothing_is_on_record(self, printer, tmp_path):
        got = server._resolve_use_ams("auto", None, _Printer(_ams(_WHITE)), file_path=_two_colour_gcode(tmp_path))
        assert got["blocked"] is True and got["ams_mapping"] is None
        refusal = got["warnings"][0]
        assert refusal.count("Want help finding a filament that suits this print?") == 1
        assert "I don't see red loaded on workshop." in refusal
        for maker in _MAKERS:
            assert maker not in refusal

    def test_the_approval_dialog_says_what_to_load_without_a_question(self, printer, shelf, tmp_path):
        shelf("PLA", "red", brand="Polymaker")
        line = server._consent_filament_line("slice_and_print", _two_colour_gcode(tmp_path), "workshop")
        assert line.startswith("MISSING COLOUR")
        assert "You have Polymaker red PLA but it isn't loaded on workshop. Load it before printing." in line
        assert "?" not in line
