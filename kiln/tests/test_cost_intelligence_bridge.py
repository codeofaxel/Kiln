"""The cost-intelligence bridge: a print file's cost estimate carries what
kiln-pro answers, verbatim, through one helper every door calls.

Coverage: the served request (the file's G-code gzipped, a sliced 3MF's
plate, the size bound, the backoff, a miss's sentence); the local answer
when kiln-pro is installed; the four doors (the cost tool, the comparison,
the two CLI commands, the pre-flight with its printer); and a gate that
keeps the public side to the words "cost intelligence" -- the bridge and
this file never say what kiln-pro measures.
"""

from __future__ import annotations

import base64
import gzip
import json
import sys
import types
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiln import _pro_cost_bridge as bridge
from tests.test_one_material_answer import _orca_plate, _state

_SRC = Path(__file__).resolve().parents[1] / "src" / "kiln"


@pytest.fixture(autouse=True)
def _fresh_bridge(monkeypatch):
    monkeypatch.setattr(bridge, "_service_down_until", 0.0)
    monkeypatch.setattr(bridge, "_service_down_miss", None)
    monkeypatch.setattr(bridge, "_last_miss", None)
    monkeypatch.setattr(bridge, "available", lambda: False)


def _estimate() -> dict:
    return {
        "filaments": [{"tool": 0, "material": "PLA"}, {"tool": 1, "material": "PETG"}],
        "total_cost_usd": 1.5,
        "estimated_time_seconds": 3600,
        "warnings": [],
    }


def _plate(tmp_path: Path) -> str:
    path = tmp_path / "plate.gcode"
    path.write_text(_orca_plate())
    return str(path)


class TestTheServedRequest:
    def test_the_gcode_travels_gzipped_and_comes_back_exact(self, tmp_path):
        path = _plate(tmp_path)
        seen: dict = {}

        def door(tool, _timeout=None, **kwargs):
            seen.update(tool=tool, timeout=_timeout, **kwargs)
            return {"success": True, "answer": 1}

        with patch("kiln.server._pro_api_call", side_effect=door):
            answer = bridge.consult_print_cost(
                path, filaments=_estimate()["filaments"], total_cost_usd=1.5, estimated_time_seconds=3600, printer_id="a1"
            )
        assert answer == {"success": True, "answer": 1}
        assert seen["tool"] == bridge.WIRE_TOOL and seen["timeout"] == 30.0
        assert gzip.decompress(base64.b64decode(seen["gcode_gz_b64"])).decode() == _orca_plate()
        assert (seen["file_name"], seen["total_cost_usd"], seen["estimated_time_seconds"], seen["printer_id"]) == (
            "plate.gcode", 1.5, 3600, "a1"
        )
        assert seen["filaments"] == _estimate()["filaments"]
        assert bridge.unanswered() is None

    def test_a_sliced_3mf_sends_its_plate(self, tmp_path):
        path = tmp_path / "plate.gcode.3mf"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("Metadata/plate_1.gcode", _orca_plate())
        seen: dict = {}
        with patch("kiln.server._pro_api_call", side_effect=lambda t, **kw: seen.update(kw) or {"success": True}):
            bridge.consult_print_cost(str(path), filaments=[], total_cost_usd=0.0, estimated_time_seconds=None)
        assert gzip.decompress(base64.b64decode(seen["gcode_gz_b64"])).decode() == _orca_plate()

    def test_a_model_3mf_sends_nothing(self, tmp_path):
        path = tmp_path / "model.3mf"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("3D/3dmodel.model", "<model/>")
        with patch("kiln.server._pro_api_call") as door:
            assert bridge.consult_print_cost(str(path), filaments=[], total_cost_usd=0.0, estimated_time_seconds=None) is None
        door.assert_not_called()
        assert "model" in bridge.unanswered()["message"]

    def test_a_file_over_the_bound_is_not_sent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bridge, "MAX_SENT_BYTES", 16)
        path = _plate(tmp_path)
        with patch("kiln.server._pro_api_call") as door:
            assert bridge.consult_print_cost(path, filaments=[], total_cost_usd=0.0, estimated_time_seconds=None) is None
        door.assert_not_called()
        assert "too large" in bridge.unanswered()["message"]

    def test_a_dead_link_is_left_alone_for_a_while(self, tmp_path):
        path = _plate(tmp_path)
        with patch("kiln.server._pro_api_call", side_effect=OSError("down")) as door:
            bridge.consult_print_cost(path, filaments=[], total_cost_usd=0.0, estimated_time_seconds=None)
            bridge.consult_print_cost(path, filaments=[], total_cost_usd=0.0, estimated_time_seconds=None)
        assert door.call_count == 1
        assert bridge.unanswered()["why"] in ("offline", "unanswered")

    def test_a_refusal_is_a_miss_with_its_sentence(self, tmp_path):
        path = _plate(tmp_path)
        with patch("kiln.server._pro_api_call", return_value={"success": False, "code": "TIER_REQUIRED", "error": "no"}):
            assert bridge.consult_print_cost(path, filaments=[], total_cost_usd=0.0, estimated_time_seconds=None) is None
        gap = bridge.unanswered()
        assert gap is not None and gap["message"]


class TestTheLocalAnswer:
    def test_an_installed_kiln_pro_answers_from_the_path(self, tmp_path, monkeypatch):
        path = _plate(tmp_path)
        fake = types.ModuleType("kiln_pro.cost_intelligence")
        fake.consult = MagicMock(return_value={"success": True, "local": True})
        monkeypatch.setitem(sys.modules, "kiln_pro.cost_intelligence", fake)
        monkeypatch.setattr(bridge, "available", lambda: True)
        with patch("kiln.server._pro_api_call") as door:
            answer = bridge.consult_print_cost(path, filaments=[], total_cost_usd=2.0, estimated_time_seconds=None, printer_id="a1")
        door.assert_not_called()
        assert answer == {"success": True, "local": True}
        fake.consult.assert_called_once_with(file_path=path, filaments=[], total_cost_usd=2.0, estimated_time_seconds=None, printer_id="a1")


class TestAttaching:
    def test_an_answer_is_attached_verbatim(self, tmp_path):
        est = _estimate()
        with patch("kiln.server._pro_api_call", return_value={"success": True, "whatever": [1, 2]}):
            bridge.attach_cost_intelligence(est, _plate(tmp_path))
        assert est["cost_intelligence"] == {"success": True, "whatever": [1, 2]}
        assert est["warnings"] == []

    def test_no_answer_is_none_plus_one_sentence(self, tmp_path):
        est = _estimate()
        with patch("kiln.server._pro_api_call", side_effect=OSError("down")):
            bridge.attach_cost_intelligence(est, _plate(tmp_path))
        assert est["cost_intelligence"] is None
        assert len(est["warnings"]) == 1 and est["warnings"][0].startswith("Kiln could not ask for cost intelligence: ")

    def test_one_filament_and_no_printer_asks_nothing(self, tmp_path):
        est = _estimate()
        est["filaments"] = est["filaments"][:1]
        with patch("kiln.server._pro_api_call") as door:
            bridge.attach_cost_intelligence(est, _plate(tmp_path))
        door.assert_not_called()
        assert "cost_intelligence" not in est

    def test_a_printer_asks_even_for_one_filament(self, tmp_path):
        est = _estimate()
        with patch("kiln.server._pro_api_call", return_value={"success": True}) as door:
            bridge.attach_cost_intelligence(est, _plate(tmp_path), printer_id="a1", filament_count=1)
        assert door.call_args.kwargs["printer_id"] == "a1"


def _estimate_tools() -> dict:
    from kiln.plugins.estimate_tools import _EstimateToolsPlugin

    tools: dict = {}

    class _Mcp:
        def tool(self, name=None, **_kwargs):
            def deco(fn):
                tools[name or fn.__name__] = fn
                return fn

            return deco

    _EstimateToolsPlugin().register(_Mcp())
    return tools


class TestEveryDoor:
    def test_the_cost_tool(self, tmp_path):
        with patch("kiln.server._pro_api_call", return_value={"success": True, "door": "tool"}):
            out = _estimate_tools()["estimate_cost"](_plate(tmp_path))
        assert out["estimate"]["cost_intelligence"] == {"success": True, "door": "tool"}

    def test_the_comparison(self, tmp_path):
        from kiln.server import compare_print_options

        with patch("kiln.server._pro_api_call", return_value={"success": True, "door": "compare"}):
            out = compare_print_options(_plate(tmp_path))
        assert out["local"]["estimate"]["cost_intelligence"] == {"success": True, "door": "compare"}

    @pytest.mark.parametrize("command", ["cost", "compare-cost"])
    def test_the_cli(self, tmp_path, command):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        with patch("kiln.server._pro_api_call", return_value={"success": True, "summary": "one line"}):
            result = CliRunner().invoke(cli, [command, _plate(tmp_path), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        estimate = data if command == "cost" else data["local"]["estimate"]
        assert estimate["cost_intelligence"]["summary"] == "one line"

    def test_the_cli_prints_the_summary_line(self, tmp_path):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        with patch("kiln.server._pro_api_call", return_value={"success": True, "summary": "one line"}):
            result = CliRunner().invoke(cli, ["cost", _plate(tmp_path)])
        assert "Kiln:       one line" in result.output

    @patch("kiln.server._get_adapter")
    @patch("kiln.server._get_temp_limits", return_value=(280.0, 120.0))
    @patch("kiln.server.get_db")
    @patch("kiln.server._registry")
    def test_the_preflight_names_its_printer(self, registry, get_db, _limits, adapter, tmp_path):
        adapter.return_value.get_state.return_value = _state()
        registry.count = 1
        registry.list_names.return_value = ["default"]
        get_db.return_value.get_printer_learning_insights.return_value = {"total_outcomes": 0}
        from kiln.server import preflight_check

        seen: dict = {}
        with patch("kiln.server._pro_api_call", side_effect=lambda t, **kw: seen.update(kw) or {"success": True, "door": "preflight"}):
            result = preflight_check(file_path=_plate(tmp_path))
        assert result["estimated_cost"]["cost_intelligence"] == {"success": True, "door": "preflight"}
        assert seen["printer_id"] and len(seen["filaments"]) == 3 and seen["estimated_time_seconds"] == 3600


# ---------------------------------------------------------------------------
# The public side knows only the words "cost intelligence"
# ---------------------------------------------------------------------------

#: What kiln-pro measures and advises, held encoded so this file itself
#: never says it.  A public reader of the bridge or this test learns only
#: that an answer comes back.
_NOT_IN_PUBLIC = [base64.b64decode(w).decode() for w in (
    b"dG93ZXI=", b"d2lwZQ==", b"cHVyZ2U=", b"Zmx1c2g=", b"d2FzdGU=", b"Y29sb3VyIGNoYW5nZQ==",
    b"Y29sb3IgY2hhbmdl", b"ZmlsYW1lbnQgY2hhbmdl", b"cmVvcmRlcg==", b"cmlzay1hZGp1c3RlZA==", b"cmVwcmludA==",
)]


class TestThePublicSideSaysNothing:
    def test_the_bridge_and_this_file_carry_none_of_the_words(self):
        for path in (Path(bridge.__file__), Path(__file__)):
            low = path.read_text(encoding="utf-8").lower()
            found = [w for w in _NOT_IN_PUBLIC if w in low]
            assert found == [], f"{path.name} says {found}; the public side knows only 'cost intelligence'"

    def test_every_door_calls_the_one_helper(self):
        import ast

        doors = {"plugins/estimate_tools.py": 1, "server.py": 2, "cli/main.py": 2}
        for rel, expected in doors.items():
            tree = ast.parse((_SRC / rel).read_text(encoding="utf-8"))
            calls = [
                n for n in ast.walk(tree)
                if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "attach_cost_intelligence"
            ]
            assert len(calls) == expected, (rel, len(calls))
