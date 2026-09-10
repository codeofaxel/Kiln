"""The tool door accepts the argument shapes agents actually send.

Fifteen tool failures across five printer models on the founder dashboard
(2026-09-10) traced to the DOOR, not the printers: a JSON object handed to a
``str | None`` parameter, ``null`` for a non-Optional field, a bare string for
a list field, a sibling tool's parameter name.  Each raised out of pydantic
as a stack trace — and a raise was counted as a failure but never as a
call, so the tools failing hardest never reached the rate floor.

Every test here drives the REAL dispatch wrapper or the real tool body.
Each was run against the pre-fix tree first and observed failing there
(``PYTHONPATH`` pinned to the main checkout at 88f89400); the A/B result is
recorded in the commit message.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from kiln import daily_stats


@pytest.fixture(autouse=True)
def _own_stats_file(tmp_path, monkeypatch):
    monkeypatch.setattr(daily_stats, "_STATS_PATH", tmp_path / "daily_stats.json")


def _today() -> dict:
    return daily_stats._read()


def _call(name: str, arguments: dict, *, convert_result: bool = False):
    """Dispatch through the live wrapper, terms gate disarmed."""
    from kiln import server

    mgr = server.mcp._tool_manager
    with patch.object(server, "_terms_gate_blocks", lambda _n: False):
        return asyncio.run(
            mgr.call_tool(name, arguments, convert_result=convert_result)
        )


# ---------------------------------------------------------------------------
# The parse helpers
# ---------------------------------------------------------------------------


def test_parse_json_object_accepts_every_shape_it_is_handed():
    from kiln.tool_args import parse_json_object

    assert parse_json_object({"a": 1}, "x") == ({"a": 1}, None)
    assert parse_json_object('{"a": 1}', "x") == ({"a": 1}, None)
    assert parse_json_object(None, "x") == (None, None)
    assert parse_json_object("", "x") == (None, None)


def test_parse_json_object_refuses_the_wrong_shape_with_an_envelope_not_a_raise():
    from kiln.tool_args import parse_json_object

    for bad in ("not json", "[1, 2]", [1, 2], 42):
        parsed, err = parse_json_object(bad, "overrides")
        assert parsed is None
        assert err["success"] is False
        assert "overrides" in err["error"]["message"]


def test_parse_json_array_accepts_list_string_and_tuple():
    from kiln.tool_args import parse_json_array

    assert parse_json_array([0, 2], "m") == ([0, 2], None)
    assert parse_json_array("[0, 2]", "m") == ([0, 2], None)
    assert parse_json_array((0, 2), "m") == ([0, 2], None)
    assert parse_json_array('{"a": 1}', "m")[1]["success"] is False


# ---------------------------------------------------------------------------
# Chokepoint coercion
# ---------------------------------------------------------------------------


def test_null_for_a_non_optional_field_uses_the_default():
    from kiln.tool_args import coerce_tool_arguments

    from pydantic import BaseModel

    class Args(BaseModel):
        printer_id: str = ""
        commands: str

    out = coerce_tool_arguments(Args, {"printer_id": None, "commands": "G28"})
    assert "printer_id" not in out  # default applies
    # A REQUIRED field given null is left for pydantic to name.
    out = coerce_tool_arguments(Args, {"commands": None})
    assert out == {"commands": None}


def test_a_bare_string_for_a_list_field_is_wrapped_but_json_array_text_is_not():
    from kiln.tool_args import coerce_tool_arguments

    from pydantic import BaseModel

    class Args(BaseModel):
        filament_types: list[str] | None = None
        overrides: str | dict | None = None

    out = coerce_tool_arguments(Args, {"filament_types": "PLA"})
    assert out["filament_types"] == ["PLA"]
    out = coerce_tool_arguments(Args, {"filament_types": '["PLA", "PETG"]'})
    assert out["filament_types"] == '["PLA", "PETG"]'  # the SDK pre-parse owns it
    out = coerce_tool_arguments(Args, {"overrides": "x"})
    assert out["overrides"] == "x"  # accepts str: untouched


def test_invalid_arguments_envelope_names_fields_and_accepted_params():
    from kiln.tool_args import invalid_arguments_envelope

    from pydantic import BaseModel, ValidationError

    class Args(BaseModel):
        commands: str

    try:
        Args(commands=["G28"])
    except ValidationError as exc:
        env = invalid_arguments_envelope("validate_gcode_safe", exc, ["commands", "printer_id"])
    assert env["success"] is False
    assert env["error"]["code"] == "INVALID_ARGS"
    assert "commands" in env["error"]["message"]
    assert "got list" in env["error"]["message"]
    assert env["accepted_arguments"] == ["commands", "printer_id"]


# ---------------------------------------------------------------------------
# Through the live wrapper — the shapes from the dashboard
# ---------------------------------------------------------------------------


def test_reslice_with_overrides_takes_the_object_and_the_string():
    """The flagship: both documented forms raised for every 1.4.x install."""
    for overrides in ({"brim_width": "8"}, '{"brim_width": "8"}'):
        result = _call(
            "reslice_with_overrides",
            {"input_path": "/nonexistent/part.stl", "overrides": overrides},
        )
        # Past the argument door: the body's own file check answers.
        assert result["success"] is False
        assert result["error"]["code"] == "FILE_NOT_FOUND"


def test_a_pydantic_argument_error_becomes_a_counted_envelope_not_a_raise():
    result = _call("validate_gcode_safe", {"commands": {"not": "a string"}})
    assert result["success"] is False
    assert result["error"]["code"] == "INVALID_ARGS"
    assert "validate_gcode_safe accepts" in result["error"]["message"]
    assert _today()["tool_calls"]["validate_gcode_safe"] == 1
    assert _today()["tool_failures"]["validate_gcode_safe"] == 1


def test_the_envelope_survives_convert_result():
    """The lowlevel handler dispatches with convert_result=True; the
    envelope has to come back as content blocks like any tool result."""
    result = _call(
        "validate_gcode_safe", {"commands": {"not": "a string"}}, convert_result=True
    )
    blocks = result[0] if isinstance(result, tuple) else result
    payload = json.loads(blocks[0].text)
    assert payload["error"]["code"] == "INVALID_ARGS"


def test_null_printer_id_no_longer_raises():
    result = _call("validate_gcode_safe", {"commands": "G28", "printer_id": None})
    assert result["success"] is True


def test_gcode_doors_take_a_list_of_lines():
    result = _call("validate_gcode_safe", {"commands": ["G28", "G1 X10 F3000"]})
    assert result["success"] is True
    assert result["commands_accepted"] == 2


def test_a_raising_tool_is_counted_as_a_call_too(monkeypatch):
    from kiln import server

    mgr = server.mcp._tool_manager

    async def _boom(name, arguments, context=None, convert_result=False):
        raise RuntimeError("the printer exploded")

    monkeypatch.setattr(mgr, "_kiln_request_context_capture_installed", False)
    monkeypatch.setattr(mgr, "call_tool", _boom)
    server._install_mcp_request_context_capture()
    monkeypatch.setattr(server, "_terms_gate_blocks", lambda _n: False)
    with pytest.raises(RuntimeError, match="exploded"):
        asyncio.run(mgr.call_tool("start_print", {}))
    assert _today()["tool_calls"]["start_print"] == 1
    assert _today()["tool_failures"]["start_print"] == 1


def test_a_pydantic_error_from_inside_a_tool_body_is_not_rewritten(monkeypatch):
    """Only the ARGUMENT model's errors become envelopes."""
    from mcp.server.fastmcp.exceptions import ToolError
    from pydantic import BaseModel, ValidationError

    from kiln import server

    class Inner(BaseModel):
        n: int

    mgr = server.mcp._tool_manager

    async def _body_raises(name, arguments, context=None, convert_result=False):
        try:
            Inner(n="x")
        except ValidationError as exc:
            raise ToolError("Error executing tool start_print: inner") from exc

    monkeypatch.setattr(mgr, "_kiln_request_context_capture_installed", False)
    monkeypatch.setattr(mgr, "call_tool", _body_raises)
    server._install_mcp_request_context_capture()
    monkeypatch.setattr(server, "_terms_gate_blocks", lambda _n: False)
    with pytest.raises(ToolError, match="inner"):
        asyncio.run(mgr.call_tool("start_print", {}))


def test_the_running_tool_name_is_visible_during_dispatch(monkeypatch):
    from kiln import server, tool_context

    seen: list[str | None] = []
    mgr = server.mcp._tool_manager

    async def _peek(name, arguments, context=None, convert_result=False):
        seen.append(tool_context.current_tool_name())
        return {"success": True}

    monkeypatch.setattr(mgr, "_kiln_request_context_capture_installed", False)
    monkeypatch.setattr(mgr, "call_tool", _peek)
    server._install_mcp_request_context_capture()
    monkeypatch.setattr(server, "_terms_gate_blocks", lambda _n: False)
    asyncio.run(mgr.call_tool("fleet_status", {}))
    assert seen == ["fleet_status"]
    assert tool_context.current_tool_name() is None  # reset after the call


# ---------------------------------------------------------------------------
# The sibling-name mismatch
# ---------------------------------------------------------------------------


def test_validate_openscad_code_takes_scad_code_like_its_siblings():
    from kiln import server

    with patch.object(server, "_get_generation_provider") as provider:
        provider.return_value.validate_scad.return_value = {"valid": True, "errors": [], "warnings": []}
        assert server.validate_openscad_code(scad_code="cube(1);")["success"] is True
        assert server.validate_openscad_code(code="cube(1);")["success"] is True
    empty = server.validate_openscad_code()
    assert empty["success"] is False


# ---------------------------------------------------------------------------
# Catalog spellings
# ---------------------------------------------------------------------------


def test_material_doors_resolve_spelling_but_never_a_family():
    from kiln import design_intelligence as di

    assert di.troubleshoot_print_issue("PLA+", "stringing") is not None
    assert di.get_material_profile("pla-cf").material_id == "cf_pla"
    assert di.troubleshoot_print_issue("Hyper PLA", "stringing") is None


def test_troubleshoot_door_suggests_the_nearest_ids():
    from kiln.plugins import design_tools

    mcp = MagicMock()
    tools: dict = {}
    mcp.tool.return_value = lambda fn: tools.__setitem__(fn.__name__, fn) or fn
    from kiln import server

    design_tools.register(mcp, server) if hasattr(design_tools, "register") else design_tools._DesignToolsPlugin().register(mcp)
    out = tools["troubleshoot_print_issue"](material="Hyper PLA", symptom="stringing")
    assert out["success"] is False
    assert "pla" in out["suggested_material_ids"]
    assert "Did you mean" in out["error"]


def test_compat_report_says_when_the_generic_profile_answered():
    from kiln import design_intelligence as di

    exact = di.check_printer_material_compatibility("creality_k1c", "ASA")
    assert (exact.printer_id, exact.resolved_from) == ("k1c", "exact")
    generic = di.check_printer_material_compatibility("no_such_printer", "pla")
    assert (generic.printer_id, generic.resolved_from) == ("default", "default")


def test_check_printer_material_support_flags_a_default_answer():
    from kiln import server

    with patch.object(server, "_check_auth", return_value=None):
        out = server.check_printer_material_support("no_such_printer", "pla")
    assert out["profile_is_default"] is True
    assert "generic default profile" in out["note"]
    with patch.object(server, "_check_auth", return_value=None):
        out = server.check_printer_material_support("Creality K1C", "asa")
    assert out["printer_id"] == "k1c"
    assert "profile_is_default" not in out


def test_safety_profile_accepts_the_vendor_spelling():
    from kiln import safety_profiles

    assert safety_profiles.get_profile("Creality K1C").id == safety_profiles.get_profile("k1c").id


# ---------------------------------------------------------------------------
# Engine defects behind the same rows
# ---------------------------------------------------------------------------


def test_estimate_step_never_escapes_the_pipeline():
    from kiln.plugins import _validation_pipeline_internals as internals

    report = internals._PipelineReport(input_path="x.stl") if hasattr(internals, "_PipelineReport") else None
    if report is None:
        pytest.skip("pipeline report type not importable")
    report.model_info["bounding_box_volume_cm3"] = 10.0
    with patch("kiln.generation.validation.estimate_print_time_from_mesh", side_effect=ValueError("Unsupported format: .3mf")):
        internals._step_estimate(report, "part.3mf")
    assert "estimate_note" in report.model_info
    assert report.model_info["estimated_print_time_min"] > 0


def test_slice_and_estimate_refuses_an_oversized_part_like_slice_model():
    from kiln.plugins import estimate_tools, slicer_tools

    mcp = MagicMock()
    tools: dict = {}
    mcp.tool.return_value = lambda fn: tools.__setitem__(fn.__name__, fn) or fn
    from kiln import server

    estimate_tools._EstimateToolsPlugin().register(mcp) if hasattr(estimate_tools, "_EstimateToolsPlugin") else estimate_tools.register(mcp, server)
    gate_err = {"code": "EXCEEDS_BED", "message": "300mm part on a 220mm bed"}
    with patch.object(server, "_check_auth", return_value=None), patch.object(
        server, "_resolve_slice_profile_context", return_value=("ender3", "/tmp/p.ini")
    ), patch.object(
        slicer_tools, "_apply_bed_fit_gate", return_value=("/tmp/big.stl", gate_err, {})
    ), patch("kiln.slicer.slice_file") as slice_file:
        out = tools["slice_and_estimate"](input_path="/tmp/big.stl", printer_id="ender3")
    assert out["success"] is False
    assert out["error"]["code"] in ("EXCEEDS_BED", "BED_FIT_ERROR")
    slice_file.assert_not_called()


@patch("kiln.server._resolve_adapter")
@patch("kiln.gcode.scan_gcode_file")
@patch("kiln.slicer.slice_file")
@patch("kiln.slicer_profiles.resolve_slicer_profile", return_value="/tmp/profile.ini")
def test_quick_print_wraps_gcode_for_a_bambu_like_reslice_does(
    mock_resolve, mock_slice, mock_gcode, mock_adapter_resolver, tmp_path
):
    from kiln.pipelines import quick_print

    gcode = tmp_path / "out.gcode"
    gcode.write_text("G28\nG1 X10\n")
    slice_result = MagicMock()
    slice_result.output_path = str(gcode)
    slice_result.message = "Sliced OK"
    slice_result.slicer = "prusaslicer"
    mock_slice.return_value = slice_result
    gcode_result = MagicMock()
    gcode_result.valid = True
    gcode_result.commands = ["G28"]
    gcode_result.blocked_commands = []
    gcode_result.warnings = []
    gcode_result.errors = []
    mock_gcode.return_value = gcode_result
    adapter = MagicMock()  # has wrap_gcode_as_3mf, like a Bambu adapter
    adapter.wrap_gcode_as_3mf.return_value = str(tmp_path / "out.3mf")
    adapter.upload_file.return_value = {"name": "out.3mf"}
    state = MagicMock()
    state.connected = True
    state.state.value = "idle"
    adapter.get_state.return_value = state
    mock_adapter_resolver.return_value = adapter

    result = quick_print(model_path="/tmp/model.stl", printer_name="bambu", printer_id="bambu_p1s", skip_validation=True)

    upload = [s for s in result.steps if s.name == "upload"]
    assert upload and upload[0].success, [s.message for s in result.steps]
    adapter.wrap_gcode_as_3mf.assert_called_once()
    adapter.upload_file.assert_called_once_with(str(tmp_path / "out.3mf"))
    # And the slice was prepared for the wrap: relative E, no slicer start/end.
    slice_profile = mock_slice.call_args.kwargs["profile"]
    assert slice_profile != "/tmp/profile.ini"
