"""The closest-filament column: each colouring tool names the closest
filaments you can buy for its colours, through one helper, from kiln-pro.

Coverage: kiln-pro on this computer answers (the column comes back exactly
as it was given); Kiln's servers answer when kiln-pro is out of reach (the
request's shape, the column taken out of the envelope); a miss (the field is
``None`` and one sentence in the shared voice says why); the palette rules
(codes normalised, each colour once, capped, nothing asked for a palette
with no colour in it); and the three colouring doors, each carrying the
column beside the spool advisory.
"""

from __future__ import annotations

import ast
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import kiln.server  # noqa: F401 -- loaded as installed, before any test here blocks kiln-pro
from kiln import _pro_colour_bridge as bridge
from kiln.decoration_faces import record_decoration_faces
from tests.test_color_tools import _make_triangle, _write_test_stl
from tests.test_decoration_faces import _box_triangles, _decorated_triangles, _write_stl
from tests.test_hosted_doors_roster import _CODE_TOKEN, _SYSTEM_WORD, _lint, _misses

_COLOR_TOOLS = Path(__file__).resolve().parents[1] / "src" / "kiln" / "plugins" / "color_tools.py"

#: A column as this side sees one: a dict it attaches and never reads.
_COLUMN = {"marker": "the column, as kiln-pro gave it"}


@pytest.fixture(autouse=True)
def _fresh_bridge(monkeypatch):
    monkeypatch.setattr(bridge, "_service_down_until", 0.0)
    monkeypatch.setattr(bridge, "_service_down_miss", None)
    monkeypatch.setattr(bridge, "_last_miss", None)


def _block_kiln_pro(monkeypatch) -> None:
    """Make every ``kiln_pro`` import fail, submodules included.

    Blocking the package alone is not enough: the suite may already have
    imported the installed kiln-pro, and a submodule still in
    ``sys.modules`` answers ``from kiln_pro.x import y`` with its parent
    blocked.  The same helper as ``test_printability_one_voice.py``'s, kept
    here because that module skips itself on import wherever its geometry
    backend is missing, and would take this one with it.
    """
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    monkeypatch.setitem(sys.modules, "kiln_pro.filament_colours", None)


def _install_local_lookup(monkeypatch, lookup) -> None:
    """kiln-pro on this computer, reduced to its closest-filament lookup."""
    _block_kiln_pro(monkeypatch)
    package = types.ModuleType("kiln_pro")
    module = types.ModuleType("kiln_pro.filament_colours")
    module.closest_filaments = lookup
    package.filament_colours = module
    monkeypatch.setitem(sys.modules, "kiln_pro", package)
    monkeypatch.setitem(sys.modules, "kiln_pro.filament_colours", module)


def _served_reply(reply: Any) -> tuple[dict[str, Any], Any]:
    """A stand-in for the served door that records what it was asked."""
    seen: dict[str, Any] = {}

    def door(tool, _timeout=None, **kwargs):
        seen.update(tool=tool, timeout=_timeout, **kwargs)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    return seen, door


# ---------------------------------------------------------------------------
# Where the answer comes from
# ---------------------------------------------------------------------------


class TestKilnProOnThisComputer:
    def test_it_answers_and_the_column_is_attached_as_given(self, monkeypatch):
        lookup = MagicMock(return_value=_COLUMN)
        _install_local_lookup(monkeypatch, lookup)
        response: dict[str, Any] = {"success": True}
        with patch("kiln.server._pro_api_call") as door:
            bridge.attach_closest_filaments(response, ["#f72323", "#FFFFFF", "F72323FF"], material="PETG")
        door.assert_not_called()
        lookup.assert_called_once_with(["#F72323", "#FFFFFF"], material="PETG")
        assert response["closest_filaments"] is _COLUMN
        assert "warnings" not in response
        assert bridge.available() is True and bridge.unanswered() is None

    def test_no_answer_on_this_computer_is_a_worded_miss(self, monkeypatch):
        _install_local_lookup(monkeypatch, MagicMock(return_value=None))
        response: dict[str, Any] = {"success": True}
        bridge.attach_closest_filaments(response, ["#F72323"])
        assert response["closest_filaments"] is None
        assert response["warnings"] == [bridge.unanswered()["message"]]
        assert "gave no answer" in response["warnings"][0]

    def test_a_lookup_that_stops_never_fails_the_colouring_or_leaks_its_error(self, monkeypatch):
        _install_local_lookup(monkeypatch, MagicMock(side_effect=KeyError("internal_key_name")))
        response: dict[str, Any] = {"success": True}
        bridge.attach_closest_filaments(response, ["#F72323"])
        assert response["closest_filaments"] is None
        (sentence,) = response["warnings"]
        assert "stopped" in sentence and "internal_key_name" not in sentence


class TestKilnsServers:
    def test_the_request_and_the_column_out_of_its_envelope(self, monkeypatch):
        _block_kiln_pro(monkeypatch)
        seen, door = _served_reply({"success": True, "closest_filaments": _COLUMN, "other": "ignored"})
        response: dict[str, Any] = {"success": True}
        with patch("kiln.server._pro_api_call", side_effect=door):
            bridge.attach_closest_filaments(response, ["#ffffff", None, "F72323FF", "#FFFFFF", "not a colour"])
        assert bridge.available() is False
        assert seen == {
            "tool": bridge.WIRE_TOOL,
            "timeout": bridge._ASK_TIMEOUT_S,
            "_asked_by_user": False,
            "colours": ["#FFFFFF", "#F72323"],
            "material": "",
        }
        assert response["closest_filaments"] == _COLUMN
        assert "warnings" not in response

    def test_a_named_material_is_sent(self, monkeypatch):
        _block_kiln_pro(monkeypatch)
        seen, door = _served_reply({"success": True, "closest_filaments": _COLUMN})
        with patch("kiln.server._pro_api_call", side_effect=door):
            assert bridge.closest_filaments(["#F72323"], material="PLA") == _COLUMN
        assert seen["material"] == "PLA"

    def test_a_success_with_no_column_is_a_miss_not_an_empty_column(self, monkeypatch):
        _block_kiln_pro(monkeypatch)
        _seen, door = _served_reply({"success": True})
        response: dict[str, Any] = {"success": True}
        with patch("kiln.server._pro_api_call", side_effect=door):
            bridge.attach_closest_filaments(response, ["#F72323"])
        assert response["closest_filaments"] is None
        assert len(response["warnings"]) == 1


# ---------------------------------------------------------------------------
# A miss: None, and one sentence saying why
# ---------------------------------------------------------------------------


class TestAMiss:
    @pytest.mark.parametrize(
        ("reply", "why", "words"),
        [
            (
                {"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "why": "signed_out", "error": "wall"},
                "signed_out",
                "sign in and colour it again",
            ),
            ({"success": False, "code": "SOME_RULING", "error": "Not for this palette."}, "refused", "Not for this palette."),
            ({"status": "error", "code": "SERVER_UNREACHABLE", "why": "offline", "error": "x"}, "offline", "reconnect"),
        ],
    )
    def test_a_served_miss_is_none_plus_the_shared_sentence(self, monkeypatch, reply, why, words):
        _block_kiln_pro(monkeypatch)
        _seen, door = _served_reply(reply)
        response: dict[str, Any] = {"success": True}
        with patch("kiln.server._pro_api_call", side_effect=door):
            bridge.attach_closest_filaments(response, ["#F72323"])
        assert response["closest_filaments"] is None
        gap = bridge.unanswered()
        assert gap["why"] == why
        assert response["warnings"] == [gap["message"]]
        assert words in gap["message"]

    @pytest.mark.parametrize(
        "reply", [OSError("down"), {"status": "error", "code": "SERVER_UNREACHABLE", "why": "unanswered", "error": "x"}]
    )
    def test_a_dead_link_is_left_alone_for_a_while(self, monkeypatch, reply):
        _block_kiln_pro(monkeypatch)
        first: dict[str, Any] = {"success": True}
        second: dict[str, Any] = {"success": True}
        with patch("kiln.server._pro_api_call", side_effect=_served_reply(reply)[1]) as door:
            bridge.attach_closest_filaments(first, ["#F72323"])
            bridge.attach_closest_filaments(second, ["#00AE42"])
        assert door.call_count == 1
        for response in (first, second):
            assert response["closest_filaments"] is None and len(response["warnings"]) == 1
        assert first["warnings"] == second["warnings"]

    def test_earlier_warnings_are_kept(self, monkeypatch):
        _block_kiln_pro(monkeypatch)
        response: dict[str, Any] = {"success": True, "warnings": ["an earlier warning"]}
        with patch("kiln.server._pro_api_call", side_effect=OSError("down")):
            bridge.attach_closest_filaments(response, ["#F72323"])
        assert response["warnings"][0] == "an earlier warning" and len(response["warnings"]) == 2

    def test_every_served_cause_reads_in_the_one_voice(self):
        seen = set()
        for miss in _misses():
            text = bridge._message(miss)
            _lint(text, where=f"closest_filaments/{miss.cause}")
            seen.add(text)
        assert len(seen) == len(_misses())

    def test_the_sentences_for_nothing_asked_carry_no_code_or_system_word(self, monkeypatch):
        texts = []
        for lookup in (MagicMock(return_value=None), MagicMock(side_effect=RuntimeError("x"))):
            _install_local_lookup(monkeypatch, lookup)
            bridge.closest_filaments(["#F72323"])
            texts.append(bridge.unanswered()["message"])
        for text in texts:
            assert text.endswith(".") and text == " ".join(text.split()), text
            assert not _CODE_TOKEN.search(text) and not _SYSTEM_WORD.search(text), text


# ---------------------------------------------------------------------------
# The palette
# ---------------------------------------------------------------------------


class TestThePalette:
    def test_a_palette_with_no_colour_asks_nothing_and_attaches_nothing(self, monkeypatch):
        _block_kiln_pro(monkeypatch)
        response: dict[str, Any] = {"success": True}
        with patch("kiln.server._pro_api_call") as door:
            bridge.attach_closest_filaments(response, [None, "", "not a colour"])
        door.assert_not_called()
        assert response == {"success": True}
        assert bridge.unanswered() is None

    def test_a_long_palette_asks_about_its_first_colours_and_says_so(self, monkeypatch):
        _block_kiln_pro(monkeypatch)
        palette = [f"#{i:02X}0000" for i in range(bridge.MAX_COLOURS + 4)]
        seen, door = _served_reply({"success": True, "closest_filaments": _COLUMN})
        response: dict[str, Any] = {"success": True}
        with patch("kiln.server._pro_api_call", side_effect=door):
            bridge.attach_closest_filaments(response, palette)
        assert seen["colours"] == palette[: bridge.MAX_COLOURS]
        assert response["closest_filaments"] == _COLUMN
        (note,) = response["warnings"]
        assert f"first {bridge.MAX_COLOURS} of these {len(palette)} colours" in note

    def test_nothing_it_is_handed_makes_it_raise(self, monkeypatch):
        _block_kiln_pro(monkeypatch)
        with patch("kiln.server._pro_api_call", side_effect=AssertionError("never asked")):
            for colours in (12, object(), [object()], None):
                response: dict[str, Any] = {"success": True}
                bridge.attach_closest_filaments(response, colours)
                assert response == {"success": True}
            assert bridge.closest_filaments(12) is None


# ---------------------------------------------------------------------------
# The doors
# ---------------------------------------------------------------------------


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


@pytest.fixture
def stubbed(monkeypatch):
    """Both halves of a colouring's advice stubbed; what each was handed."""
    from kiln import server

    seen: dict[str, Any] = {}

    def advisory(colours, *, printer_name=None, adapter=None):
        seen["advisory"] = list(colours)
        return {"verdict": "true", "message": "Every colour is loaded."}

    def column(response, colours, *, material=None):
        seen["column"] = list(colours)
        response["closest_filaments"] = _COLUMN

    monkeypatch.setattr(server, "_spool_advisory", advisory)
    monkeypatch.setattr(bridge, "attach_closest_filaments", column)
    return seen


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


class TestEveryColouringDoor:
    def test_colour_by_height(self, stubbed, tmp_path):
        result = _tools()["auto_color_by_height"](
            input_path=_stl(tmp_path), num_colors=2, color_palette=["#FFFFFF", "#F72323", "#161616"],
        )
        assert result["success"] is True
        assert result["closest_filaments"] == _COLUMN
        assert result["ams_advisory"]["verdict"] == "true"
        assert stubbed["column"] == stubbed["advisory"] == ["#FFFFFF", "#F72323"]

    def test_colour_by_region(self, stubbed, tmp_path):
        result = _tools()["auto_color_by_region"](
            input_path=_stl(tmp_path), num_colors=3, method="normal",
            color_palette=["#FF0000", "#00FF00", "#0000FF"],
        )
        assert result["success"] is True
        assert result["closest_filaments"] == _COLUMN
        assert result["ams_advisory"]["verdict"] == "true"
        assert stubbed["column"] == stubbed["advisory"] == ["#FF0000", "#00FF00", "#0000FF"]

    def test_paint_decoration_faces(self, stubbed, tmp_path):
        result = _tools()["paint_decoration_faces"](
            model_path=_carved(tmp_path), color="#F72323", base_color="#FFFFFF",
            output_path=str(tmp_path / "painted.3mf"),
        )
        assert result["success"] is True, result.get("error")
        assert result["closest_filaments"] == _COLUMN
        assert result["ams_advisory"]["verdict"] == "true"
        assert stubbed["column"] == stubbed["advisory"] == ["#F72323", "#FFFFFF"]

    def test_a_door_reaches_the_real_bridge_and_a_miss_reads_there_too(self, monkeypatch, tmp_path):
        """No stub between the door and the bridge: the served door is the
        only stand-in, and a miss arrives at the door as None plus the
        sentence."""
        _block_kiln_pro(monkeypatch)
        monkeypatch.setattr(kiln.server, "_spool_advisory", lambda *a, **k: None)
        tools = _tools()
        with patch("kiln.server._pro_api_call", return_value={"success": True, "closest_filaments": _COLUMN}):
            answered = tools["auto_color_by_height"](input_path=_stl(tmp_path), num_colors=2)
        with patch("kiln.server._pro_api_call", side_effect=OSError("down")):
            missed = tools["auto_color_by_height"](input_path=_stl(tmp_path), num_colors=2)
        assert answered["success"] is True and answered["closest_filaments"] == _COLUMN
        assert missed["success"] is True and missed["closest_filaments"] is None
        assert missed["warnings"] == [bridge.unanswered()["message"]]

    def test_a_failing_column_never_fails_a_good_colouring(self, monkeypatch, tmp_path):
        monkeypatch.setattr(kiln.server, "_spool_advisory", lambda *a, **k: None)

        def explode(*_a, **_k):
            raise RuntimeError("the column fell over")

        monkeypatch.setattr(bridge, "attach_closest_filaments", explode)
        result = _tools()["auto_color_by_height"](input_path=_stl(tmp_path), num_colors=2)
        assert result["success"] is True and "closest_filaments" not in result


class TestOneHelper:
    def test_the_two_halves_are_only_ever_called_together(self):
        """Inside color_tools, the spool advisory and the column are each
        asked for in one place -- the helper -- and the helper is what the
        three colouring doors call, so no door can carry one without the
        other."""
        names = {"_attach_spool_advisory", "attach_closest_filaments", "_attach_colour_advice"}
        callers: dict[str, list[str]] = {name: [] for name in names}

        class _Calls(ast.NodeVisitor):
            def __init__(self) -> None:
                self.stack: list[str] = []

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            def visit_Call(self, node: ast.Call) -> None:
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name in callers:
                    callers[name].append(self.stack[-1] if self.stack else "<module>")
                self.generic_visit(node)

        _Calls().visit(ast.parse(_COLOR_TOOLS.read_text(encoding="utf-8")))
        assert callers["_attach_spool_advisory"] == ["_attach_colour_advice"]
        assert callers["attach_closest_filaments"] == ["_attach_colour_advice"]
        assert sorted(callers["_attach_colour_advice"]) == [
            "auto_color_by_height", "auto_color_by_region", "paint_decoration_faces",
        ]

    def test_every_tool_that_says_what_is_loaded_also_says_what_to_buy(self):
        """Package-wide: a function that asks whether its colours are loaded
        (``_spool_advisory``) also attaches the closest filaments -- the
        two server doors that choose colours outside color_tools included.
        ``_attach_spool_advisory`` is the one exception, and the test above
        pins that its only caller is the helper that attaches both."""
        package = Path(__file__).resolve().parent.parent / "src" / "kiln"
        missing: list[str] = []
        doors: list[str] = []
        for path in sorted(package.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef) or node.name == "_attach_spool_advisory":
                    continue
                called = {
                    getattr(call.func, "id", None) or getattr(call.func, "attr", None)
                    for call in ast.walk(node)
                    if isinstance(call, ast.Call)
                }
                if "_spool_advisory" not in called:
                    continue
                doors.append(node.name)
                if "attach_closest_filaments" not in called:
                    missing.append(f"{path.relative_to(package)}::{node.name}")
        assert missing == [], missing
        assert {"compose_multicolor_3mf", "wrap_gcode_as_3mf"} <= set(doors), doors


class TestNobodyAskedForTheColumn:
    """A signed-out install still gets the honest miss, but the column's ask
    is not counted as a person reaching for the feature: the account-wall
    counter means a person asked (``kiln.daily_stats.record_account_wall``)."""

    @staticmethod
    def _unpaired(tmp_path, monkeypatch):
        monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
        monkeypatch.delenv("KILN_API_URL", raising=False)
        monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)

    def test_the_bridge_marks_its_ask_as_not_the_users(self, monkeypatch):
        seen: dict[str, Any] = {}

        def door(tool, **kwargs):
            seen.update(kwargs)
            return {"success": True, "closest_filaments": {"colours": []}}

        monkeypatch.setattr(bridge, "available", lambda: False)
        with patch("kiln.server._pro_api_call", side_effect=door):
            bridge.attach_closest_filaments({}, ["#C12E1F"])
        assert seen["_asked_by_user"] is False

    def test_an_unasked_call_is_not_counted_as_an_account_wall(self, tmp_path, monkeypatch):
        import kiln.daily_stats as stats
        from kiln.server import _pro_api_call

        self._unpaired(tmp_path, monkeypatch)
        counted: list[str] = []
        monkeypatch.setattr(stats, "record_account_wall", counted.append)

        quiet = _pro_api_call("find_closest_filaments", _asked_by_user=False, colours=["#C12E1F"])
        assert quiet["code"] == "KILN_ACCOUNT_NOT_PAIRED"
        assert counted == []

        _pro_api_call("find_closest_filaments", colours=["#C12E1F"])
        assert counted == ["find_closest_filaments"]
