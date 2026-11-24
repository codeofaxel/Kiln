"""The top verdict of a reply agrees with the worst thing the same reply found.

A reply that knows the part is expected to come off the bed, does not fit
the bed it was held to, is not a closed solid, has walls the slice may
leave out, or has walls thinner than the nozzle never says "Ready to
print." or "valid".  Each case is run through
the registered tools on a broken part, and on its good twin, which still
reads ready and valid: a rule that refuses everything cannot hide here.
"""

from __future__ import annotations

import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")

from tests import _real_exports as rx  # noqa: E402

BED = {"bed_x_mm": 256.0, "bed_y_mm": 256.0, "bed_z_mm": 256.0}


def _save(mesh, tmp_path, name: str) -> str:
    path = tmp_path / name
    mesh.export(path)
    return str(path)


def _block(tmp_path, name: str, size: tuple[float, float, float]) -> str:
    mesh = trimesh.creation.box(extents=size)
    mesh.apply_translation((0.0, 0.0, size[2] / 2.0))
    return _save(mesh, tmp_path, name)


def _torn(tmp_path) -> str:
    """A 20 mm block with one face torn open: a surface that does not close."""
    mesh = trimesh.creation.box((20.0, 20.0, 20.0))
    mesh.apply_translation((0.0, 0.0, 10.0))
    return _save(trimesh.Trimesh(mesh.vertices, mesh.faces[2:], process=False), tmp_path, "torn.stl")


def _open_box(tmp_path, wall_mm: float) -> str:
    """A 40 x 40 x 30 mm open-topped box with walls and floor *wall_mm* thick."""
    outer = trimesh.creation.box((40.0, 40.0, 30.0))
    outer.apply_translation((0.0, 0.0, 15.0))
    inner = trimesh.creation.box((40.0 - 2 * wall_mm, 40.0 - 2 * wall_mm, 30.0))
    inner.apply_translation((0.0, 0.0, 15.0 + wall_mm))
    box = trimesh.boolean.difference([outer, inner], engine="manifold")
    return _save(box, tmp_path, f"box_{wall_mm}.stl")


def _call(name: str, **args):
    from kiln import server

    with rx.without_kiln_pro():
        return rx.call(server.mcp, name, args)


def _says_ready(reply: dict) -> list[str]:
    """Every sentence in *reply* that calls the part ready or the mesh valid."""
    return [
        text for _where, text in rx._sentences(reply)
        if "ready to print" in text.lower() or "print-ready" in text.lower() or "mesh is valid" in text.lower()
    ]


# ---------------------------------------------------------------------------
# A part expected to come off the bed
# ---------------------------------------------------------------------------


def test_a_part_expected_to_come_off_the_bed_is_not_called_ready(tmp_path):
    reply = _call("analyze_printability", file_path=_block(tmp_path, "pillar.stl", (4.0, 4.0, 200.0)))
    report = reply["report"]
    assert report["adhesion_force"]["will_detach"] is True, "control: Kiln expects it to come off"
    assert reply["verdict"] == "check", reply["message"]
    assert reply["message"].startswith(
        "Check before printing: It is tall for what it stands on and is likely to come off the bed "
        "before it finishes, so print it with an 8 mm brim."
    ), reply["message"]
    assert report["printable"] is False
    assert _says_ready(reply) == []


def test_a_borderline_hold_still_prints_and_says_how_to_hold_it(tmp_path):
    """A hold the force check calls borderline is no check: the verdict says
    the part prints and what holds it down."""
    reply = _call(
        "analyze_printability", file_path=_block(tmp_path, "column.stl", (20.0, 20.0, 300.0)), material="pp",
    )
    assert reply["report"]["adhesion_force"]["risk_level"] == "marginal"
    assert reply["verdict"] == "ready", reply["message"]
    assert "could come off the bed" in reply["message"] and "brim" in reply["message"], reply["message"]


def test_a_squat_part_is_still_ready(tmp_path):
    reply = _call("analyze_printability", file_path=_block(tmp_path, "squat.stl", (40.0, 40.0, 10.0)))
    assert reply["verdict"] == "ready" and reply["message"].startswith("Ready to print."), reply["message"]
    assert reply["report"]["printable"] is True
    assert reply["report"]["adhesion"]["adhesion_risk"] == "low"


def test_every_door_carries_the_check_for_the_tall_part(tmp_path):
    pillar = _block(tmp_path, "pillar.stl", (4.0, 4.0, 200.0))
    readiness = _call("check_print_readiness", file_path=pillar)
    assert (readiness["verdict"], readiness["printability_verdict"]) == ("check_before_printing", "check"), readiness
    assert readiness["can_print"] is False, readiness
    prepared = _call("validate_and_prepare", input_path=pillar)
    assert prepared["printability_verdict"] == "check", prepared
    assert (prepared["ready_to_print"], prepared["status"]) == (False, "fail"), prepared
    mesh = _call("validate_and_prepare_mesh", file_path=pillar, auto_repair=False)
    assert (mesh["passed"], mesh["verdict"]) == (False, "check"), mesh
    assert "PASSED" not in mesh["message"] and "FAILED validation. Check before printing:" in mesh["message"], mesh["message"]
    assert prepared["summary"].startswith("Not ready. 1 issue: Check before printing: It is tall"), prepared["summary"]
    supports = _call("estimate_supports", file_path=pillar)
    assert supports["message"].startswith("Check before printing: "), supports["message"]
    for reply in (readiness, prepared, supports, mesh):
        assert _says_ready(reply) == [], reply


def test_every_door_still_passes_the_squat_part(tmp_path):
    """The good twin of the test above: the same doors, a part that holds."""
    squat = _block(tmp_path, "squat.stl", (40.0, 40.0, 10.0))
    readiness = _call("check_print_readiness", file_path=squat)
    assert (readiness["can_print"], readiness["verdict"]) == (True, "ready_to_print"), readiness
    prepared = _call("validate_and_prepare", input_path=squat)
    assert (prepared["ready_to_print"], prepared["status"]) == (True, "pass"), prepared
    assert prepared["summary"].startswith("Print-ready."), prepared["summary"]
    mesh = _call("validate_and_prepare_mesh", file_path=squat, auto_repair=False)
    assert mesh["passed"] is True and "PASSED validation. Ready to print." in mesh["message"], mesh


def _approval(monkeypatch, part: str) -> str:
    """The question a person is asked before ``slice_and_print`` prints *part*."""
    import asyncio

    from kiln import server
    from kiln.print_consent import CHOICE_THIS_PRINT, DialogAnswer, reset_consent

    monkeypatch.delenv("KILN_SKIP_PREVIEW_GATE", raising=False)
    monkeypatch.setattr(server, "host_can_ask_the_user", lambda mcp, ctx: True)
    asked: list[str] = []

    async def _answer(ctx, message, **_kw):
        asked.append(message)
        return DialogAnswer("accept", "", choice=CHOICE_THIS_PRINT)

    monkeypatch.setattr(server, "ask_user_to_confirm", _answer)

    async def _ask() -> None:
        token = await server._obtain_print_consent("slice_and_print", {"input_path": part}, object())
        if token is not None:
            reset_consent(token)

    with rx.without_kiln_pro():
        asyncio.run(_ask())
    [message] = asked
    return message


def test_the_print_approval_says_the_check_and_the_part_still_prints(tmp_path, monkeypatch):
    """A part the verdict asks a check of is not ready, and it is not held:
    the approval the person answers before the print says the verdict's
    sentence, word for word, and on their yes the print door sends it on."""
    from kiln.pipelines import _validate_mesh_step
    from kiln.plugins.validation_pipeline_tools import gate_for_print
    from kiln.print_consent import what_the_print_is

    pillar = _block(tmp_path, "pillar.stl", (4.0, 4.0, 200.0))
    with rx.without_kiln_pro():
        gate = gate_for_print(pillar)
        step, _ = _validate_mesh_step(pillar, printer_id="", label="pipeline")
        rows = what_the_print_is("pillar.stl", pillar)
    said = gate.report["printability_verdict_text"]
    assert said.startswith("Check before printing: It is tall for what it stands on"), said
    assert gate.report["ready_to_print"] is False, gate.report
    assert gate.reason is None, gate.reason
    assert step.success and step.message == said.rstrip("."), step
    assert rows == {"check": said}, rows
    assert f"  The part: {said}" in _approval(monkeypatch, pillar).splitlines()


def test_the_print_approval_of_a_ready_part_says_no_check(tmp_path, monkeypatch):
    squat = _block(tmp_path, "squat.stl", (40.0, 40.0, 10.0))
    with rx.without_kiln_pro():
        from kiln.print_consent import what_the_print_is

        assert what_the_print_is("squat.stl", squat) == {}
    message = _approval(monkeypatch, squat)
    assert "The part:" not in message and "Check before printing" not in message, message


# ---------------------------------------------------------------------------
# A part that does not fit the bed it was held to
# ---------------------------------------------------------------------------


def test_a_part_too_big_for_the_stated_bed_is_not_called_ready(tmp_path):
    reply = _call("check_print_readiness", file_path=_block(tmp_path, "big.stl", (400.0, 400.0, 50.0)), **BED)
    assert (reply["can_print"], reply["verdict"]) == (False, "needs_fixes"), reply
    assert reply["printability_verdict"] == "wont_print", reply
    assert "exceeds build volume" in reply["printability_verdict_text"], reply
    assert _says_ready(reply) == []


def test_a_part_that_fits_the_stated_bed_is_still_ready(tmp_path):
    reply = _call("check_print_readiness", file_path=_block(tmp_path, "small.stl", (20.0, 20.0, 20.0)), **BED)
    assert (reply["can_print"], reply["verdict"], reply["printability_verdict"]) == (True, "ready_to_print", "ready")
    assert reply["printability_verdict_text"] == "Ready to print."


# ---------------------------------------------------------------------------
# A mesh that is not a closed solid
# ---------------------------------------------------------------------------


def test_a_torn_mesh_is_not_called_ready_at_the_readiness_door(tmp_path):
    reply = _call("check_print_readiness", file_path=_torn(tmp_path))
    assert (reply["can_print"], reply["verdict"]) == (False, "needs_fixes"), reply
    assert reply["printability_verdict"] == "check", reply
    assert "open edges" in reply["printability_verdict_text"], reply
    assert _says_ready(reply) == []


def test_a_torn_mesh_is_not_called_ready_by_the_printability_check(tmp_path):
    reply = _call("analyze_printability", file_path=_torn(tmp_path))
    assert reply["verdict"] == "check", reply["message"]
    assert reply["message"].startswith("Check before printing: Its surface has holes in it (4 open edges)")
    assert reply["report"]["pieces"]["open_surface"] in reply["report"]["recommendations"]
    assert _says_ready(reply) == []


def test_a_torn_mesh_is_not_called_valid(tmp_path):
    reply = _call("validate_generated_mesh", file_path=_torn(tmp_path))
    assert reply["validation"]["valid"] is True, "the file still reads as a mesh"
    assert reply["validation"]["closed_solid"] is False
    assert "not a closed solid: 4 open edges" in reply["message"], reply["message"]
    assert _says_ready(reply) == []


def test_a_closed_mesh_is_still_valid_and_ready(tmp_path):
    closed = _block(tmp_path, "closed.stl", (20.0, 20.0, 20.0))
    valid = _call("validate_generated_mesh", file_path=closed)
    assert valid["message"] == "Mesh is valid." and valid["validation"]["closed_solid"] is True
    assert _call("check_print_readiness", file_path=closed)["printability_verdict"] == "ready"
    report = _call("analyze_printability", file_path=closed)["report"]
    assert not report["pieces"].get("open_surface")


def test_a_mesh_fault_the_report_does_not_hold_still_moves_the_carried_verdict():
    """A door's own finding reaches the verdict it carries."""
    from types import SimpleNamespace

    from kiln.printability import verdict_block

    ready = SimpleNamespace(printable=True)
    assert verdict_block(ready)["printability_verdict"] == "ready"
    held = verdict_block(ready, found=["3 zero-area triangles"])
    assert held == {
        "printability_verdict": "check",
        "printability_verdict_text": "Check before printing: 3 zero-area triangles",
    }


# ---------------------------------------------------------------------------
# Walls the slice may leave out
# ---------------------------------------------------------------------------


def test_walls_half_the_nozzle_wide_are_not_called_ready(tmp_path):
    reply = _call("analyze_printability", file_path=_open_box(tmp_path, 0.2))
    walls = reply["report"]["thin_walls"]
    assert walls["may_be_left_out_count"] > 0 or walls["dropped_regions"], walls
    assert reply["verdict"] in ("check", "wont_print"), reply["message"]
    assert "0.20 mm" in reply["message"], reply["message"]
    assert reply["report"]["printable"] is False
    assert _says_ready(reply) == []


def test_walls_five_nozzles_wide_are_still_ready(tmp_path):
    reply = _call("analyze_printability", file_path=_open_box(tmp_path, 2.0))
    assert reply["verdict"] == "ready" and reply["message"].startswith("Ready to print."), reply["message"]


# ---------------------------------------------------------------------------
# Walls thinner than the nozzle, judged by Kiln's own slice figures
# ---------------------------------------------------------------------------

#: The stand-in server's narrowest wall Kiln's slice lays a line on, and its
#: thinnest round pin, at a 0.4 mm nozzle.  Figures of this file's own, under
#: the 0.2 mm walls below, so the slice keeps them as one thin line each.
WALL_LINE = 0.17
PIN_LINE = 0.7


@pytest.fixture
def kilns_own_figures(monkeypatch):
    """Kiln's servers answer what Kiln's own slice keeps at the nozzle, as
    they do for a signed-in install; every other ask is a miss."""
    from kiln import _pro_material_limits_bridge as bridge
    from kiln import material_limits as helper
    from kiln import server

    def serve(tool_name, _timeout=None, _asked_by_user=True, _feature=None, **kwargs):
        if tool_name != bridge.WIRE_TOOL:
            return {"status": "error", "code": "NO_ANSWER", "error": "no answer", "why": "no_answer"}
        return {
            "success": True,
            "status": "ok",
            "known": False,
            "material_id": None,
            "scenario": {"nozzle_mm": kwargs.get("nozzle_mm", 0.4)},
            "limits": {
                "sliced_wall_min_mm": {"value": WALL_LINE, "unit": "mm", "basis": "general"},
                "sliced_pin_min_mm": {"value": PIN_LINE, "unit": "mm", "basis": "general"},
            },
            "note": "",
        }

    def forget() -> None:
        helper._answers.clear()
        helper._said.clear()
        bridge._service_down_until, bridge._service_down_miss = 0.0, None

    forget()
    monkeypatch.setattr(server, "_pro_api_call", serve)
    yield
    forget()


def test_walls_thinner_than_the_nozzle_are_not_called_ready_on_kilns_own_figures(tmp_path, kilns_own_figures):
    reply = _call("analyze_printability", file_path=_open_box(tmp_path, 0.2))
    report = reply["report"]
    walls = report["thin_walls"]
    # Control: Kiln's own figures judged the walls, and the slice keeps each as one thin line.
    assert walls["slicer_check"] == "exact", walls
    assert (walls["thin_line_kind"], walls["may_be_left_out_count"], walls["dropped_regions"]) == ("wall", 0, [])
    assert reply["verdict"] == "check", reply["message"]
    assert reply["message"].startswith(
        "Check before printing: Walls as thin as 0.20 mm are thinner than the nozzle"
    ), reply["message"]
    said = reply["message"].removeprefix("Check before printing: ").split("  Checked for")[0]
    assert any(said.startswith(advice) for advice in report["recommendations"]), (said, report["recommendations"])
    assert report["printable"] is False
    assert _says_ready(reply) == []


def test_every_door_carries_the_check_for_the_thin_walls(tmp_path, kilns_own_figures):
    box = _open_box(tmp_path, 0.2)
    readiness = _call("check_print_readiness", file_path=box)
    assert (readiness["printability_verdict"], readiness["can_print"]) == ("check", False), readiness
    prepared = _call("validate_and_prepare", input_path=box)
    assert (prepared["printability_verdict"], prepared["ready_to_print"]) == ("check", False), prepared
    mesh = _call("validate_and_prepare_mesh", file_path=box, auto_repair=False)
    assert (mesh["passed"], mesh["verdict"]) == (False, "check"), mesh
    audit = _call("audit_original_design", file_path=box, requirements="an open box")
    [gate] = [g for g in audit["gates"] if g["name"] == "printability"]
    assert gate["passed"] is False and gate["message"].startswith("Check before printing: Walls as thin"), gate
    for reply in (readiness, prepared, mesh):
        assert _says_ready(reply) == [], reply


@pytest.mark.parametrize("wall_mm", [0.6, 2.0])
def test_walls_at_least_a_nozzle_wide_are_still_ready_on_kilns_own_figures(tmp_path, kilns_own_figures, wall_mm):
    """The good twins: a wall past the nozzle prints as drawn, so the same
    doors still call the part ready."""
    box = _open_box(tmp_path, wall_mm)
    reply = _call("analyze_printability", file_path=box)
    assert reply["report"]["thin_walls"]["slicer_check"] == "exact"
    assert reply["verdict"] == "ready" and reply["message"].startswith("Ready to print."), reply["message"]
    readiness = _call("check_print_readiness", file_path=box)
    assert (readiness["printability_verdict"], readiness["can_print"]) == ("ready", True), readiness
