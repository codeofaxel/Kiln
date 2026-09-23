"""A file that already exists can be put on Kiln's 3D stage.

Measured 2026-09-22, live, preparing a sliced painted jar for an A1: the
print gate asks for the inline stage first, the handoff said "inline stage
first, do not re-slice", and no door could open the stage on a file that
already existed.  Every stage door made or changed something; the one tool
that shows an existing file (``visualize_model``) is the still door, so the
agent reached the browser link and nothing better.  The other routes were
worse: ``extract_model_from_3mf`` stages a third file — no paint, its own
hash — and the gate refuses it for the print file.

Pinned here:

* ``show_on_stage`` opens the stage on an existing mesh or print file,
  through the same result hook every stage door uses, and the panel's
  fetch signs off exactly the file about to print;
* a print archive carrying only Kiln's 1 mm placeholder is never staged as
  a cube — it is drawn as the mesh it was sliced from, or refused in words;
* the gate reads the same answer, so it never demands a stage no door can
  open, and its stage refusal names the door that opens one.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from kiln import local_stage, monitor_twin, preview_evidence, stage_cache
from tests.test_local_stage import _cache_the_stage, _Caps, _Host, _Result, _wire_link_door

_UI = local_stage.MCP_APPS_EXTENSION_ID


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Fresh ledgers under tmp, the stage on, the link door signed out."""
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(local_stage._OPT_OUT_ENV, raising=False)
    monkeypatch.delenv("KILN_SKIP_PREVIEW_GATE", raising=False)
    twin = tmp_path / "twin"
    monkeypatch.setattr(monitor_twin, "_TWIN_DIR", twin)
    monkeypatch.setattr(monitor_twin, "_SLICES_FILE", twin / "slices.json")
    monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", twin / "active.json")
    monkeypatch.setattr(
        "kiln.auth_session.resolve_api_bearer",
        lambda *a, **k: type("B", (), {"token": "", "state": "signed_out"})(),
    )
    from kiln import stage_link

    stage_link._cache.clear()
    stage_link._REFUSED_BEARER = None
    local_stage._reset_for_tests()
    stage_cache._reset_for_tests()
    preview_evidence._reset_for_tests()
    yield
    local_stage._reset_for_tests()
    stage_cache._reset_for_tests()
    preview_evidence._reset_for_tests()


# ---------------------------------------------------------------------------
# Real files, from the real writers
# ---------------------------------------------------------------------------

_LAYERS = ";LAYER_CHANGE\n;Z:0.2\nG1 Z0.2\nG1 X10 Y10 E1\n;LAYER_CHANGE\n;Z:0.4\nG1 Z0.4\nG1 X20 Y10 E2\n"


def _ball(path: Path) -> str:
    """A 24 mm ball as a 3MF — big enough that its model is no placeholder."""
    trimesh = pytest.importorskip("trimesh")
    mesh = trimesh.creation.icosphere(subdivisions=2, radius=12.0)
    mesh.apply_translation([0.0, 0.0, 12.0])
    mesh.export(str(path))
    return str(path)


def _block(path: Path) -> str:
    """A 30 x 20 x 10 mm block as an STL — the kind of source a Kiln wrap
    carries no model for."""
    trimesh = pytest.importorskip("trimesh")
    trimesh.creation.box(extents=(30.0, 20.0, 10.0)).export(str(path))
    return str(path)


def _sliced_archive(tmp_path: Path) -> str:
    """A ``.gcode.3mf`` built by Kiln's wrapper from a 3MF source, so it
    carries the source's real model beside the plate G-code."""
    from kiln.printers.bambu_3mf import build_bambu_3mf

    source = _ball(tmp_path / "ball.3mf")
    out = tmp_path / "ball.gcode.3mf"
    build_bambu_3mf(_LAYERS, str(out), source_3mf_path=source)
    return str(out)


def _small_part_archive(tmp_path: Path) -> str:
    """A sliced 20 mm cube: a real part whose model is as small as the
    placeholder's (about 2 KB), so only what the model holds tells them
    apart."""
    from kiln.printers.bambu_3mf import build_bambu_3mf

    trimesh = pytest.importorskip("trimesh")
    source = tmp_path / "cube20.3mf"
    trimesh.creation.box(extents=(20.0, 20.0, 20.0)).export(str(source))
    out = tmp_path / "cube20.gcode.3mf"
    build_bambu_3mf(_LAYERS, str(out), source_3mf_path=str(source))
    return str(out)


def _placeholder_archive(tmp_path: Path, *, sliced_from: str | None = None) -> str:
    """A ``.gcode.3mf`` built by Kiln's wrapper from G-code alone: its model
    is the 1 mm placeholder cube.  With *sliced_from*, the slice ledger
    records the mesh the G-code came from, as the slicer and wrapper do."""
    from kiln.printers.bambu_3mf import repackage_gcode_as_bambu_3mf

    gcode = tmp_path / "block.gcode"
    gcode.write_text("; HEADER_BLOCK_START\n; HEADER_BLOCK_END\n" + _LAYERS)
    out = str(tmp_path / "block.gcode.3mf")
    if sliced_from:
        monitor_twin.note_sliced(sliced_from, str(gcode))
    repackage_gcode_as_bambu_3mf(str(gcode), out)
    if sliced_from:
        monitor_twin.note_wrapped(str(gcode), out)
    return out


# ---------------------------------------------------------------------------
# The door, registered and stamped the way ``kiln serve`` does it
# ---------------------------------------------------------------------------


def _served() -> object:
    from kiln.mcp_compat import FastMCP
    from kiln.plugins.mesh_tools import plugin

    mcp = FastMCP("test")
    plugin.register(mcp)
    _cache_the_stage()
    local_stage.install(mcp)
    return mcp


def _door(mcp: object):
    return mcp._tool_manager._tools["show_on_stage"]  # type: ignore[attr-defined]


def _through_the_hook(mcp: object, host: _Host, body: dict) -> dict:
    """Hand *body* — the door's own return value — to the real lowlevel
    result hook, as ``tools/call`` for ``show_on_stage``, and return what
    the host receives: the structured content the hook wrote, or the body
    untouched when it wrote none (a refusal).  Same two-major shape as
    ``test_local_stage``."""
    import anyio
    from mcp.types import CallToolRequestParams

    from kiln.mcp_compat import MCP_SDK_MAJOR, lowlevel_server

    result = _Result(body)
    server = lowlevel_server(mcp)
    params = CallToolRequestParams(name="show_on_stage", arguments={})
    if MCP_SDK_MAJOR >= 2:
        entry = server.get_request_handler("tools/call")

        async def _base(_ctx, _params):
            return result

        server.add_request_handler("tools/call", entry.params_type, _base)
        local_stage._install_result_hook(mcp)
        handler = server.get_request_handler("tools/call").handler
        anyio.run(handler, host._mcp_server.request_context, params)
        return result.structuredContent or body

    from mcp.server.lowlevel.server import request_ctx
    from mcp.types import CallToolRequest

    async def _base_v1(_req):
        return type("R", (), {"root": result})()

    server.request_handlers[CallToolRequest] = _base_v1
    local_stage._install_result_hook(mcp)
    token = request_ctx.set(host._mcp_server.request_context)
    try:
        anyio.run(server.request_handlers[CallToolRequest],
                  CallToolRequest(method="tools/call", params=params))
    finally:
        request_ctx.reset(token)
    return result.structuredContent or body


def _show(file_path: str, host: _Host | None = None) -> dict:
    mcp = _served()
    body = _door(mcp).fn(file_path=file_path)
    return _through_the_hook(mcp, host or _Host(_Caps(extensions={_UI: {}})), body)


def _panel_fetches(sc: dict) -> dict:
    """What the rendered panel does next: present its token to the fetch verb."""
    from kiln.mcp_compat import FastMCP

    verb = FastMCP("panel")
    local_stage._register_payload_verb(verb)
    out = verb._tool_manager._tools["kiln_viewer_payload"].fn(sc["artifact"]["artifact_token"])
    assert out.get("success") is not False, out
    return next(iter(out.values()))


# ---------------------------------------------------------------------------


class TestAnExistingFileReachesTheStage:
    """The door exists, is stamped like every stage door, and draws the
    right file for each kind of file a person might be about to print."""

    def test_the_door_is_stamped_to_open_the_stage(self):
        tool = _door(_served())
        assert (tool.meta or {}).get("ui", {}).get("resourceUri") == local_stage.MESH_VIEWER_RESOURCE_URI
        assert local_stage.STAGE_DESCRIPTION_CLAUSE in (tool.description or "")
        assert "show_on_stage" in local_stage.VIEWER_TOOLS

    def test_a_sliced_print_file_is_drawn_as_the_model_it_carries_and_signs_itself_off(self, tmp_path):
        archive = _sliced_archive(tmp_path)
        sc = _show(archive)
        assert sc["success"] is True
        assert sc["stage_mesh_path"] == archive
        assert local_stage.resolve(sc["artifact"]["artifact_token"]) == archive
        size = _panel_fetches(sc)["bbox"]["size"]
        assert min(size) > 20.0, f"the stage drew {size}, not the 24 mm ball the archive carries"
        refusal, verdict = preview_evidence.judge(archive, "stage", host_renders=True, panel_proven=True)
        assert refusal is None, refusal
        assert verdict["door"] == "stage"

    def test_a_placeholder_archive_is_drawn_as_the_mesh_it_was_sliced_from(self, tmp_path):
        mesh = _block(tmp_path / "block.stl")
        archive = _placeholder_archive(tmp_path, sliced_from=mesh)
        sc = _show(archive)
        assert sc["stage_mesh_path"] == os.path.abspath(mesh)
        assert "placeholder" in sc["shows"]
        size = sorted(_panel_fetches(sc)["bbox"]["size"])
        assert size == pytest.approx([10.0, 20.0, 30.0], abs=0.01), (
            f"the stage drew {size}; a 1 mm cube is the placeholder, not the part"
        )
        refusal, _ = preview_evidence.judge(archive, "stage", host_renders=True, panel_proven=True)
        assert refusal is None, refusal

    def test_a_placeholder_archive_nobody_sliced_here_is_refused_in_words(self, tmp_path):
        archive = _placeholder_archive(tmp_path)
        sc = _show(archive)
        assert sc["success"] is False
        assert sc["error"]["code"] == "NOT_STAGEABLE"
        assert "placeholder" in sc["error"]["message"]
        assert "no record of the mesh" in sc["error"]["message"]
        assert "artifact" not in sc, "a cube must not reach the panel"

    def test_raw_gcode_is_drawn_as_the_mesh_it_was_sliced_from(self, tmp_path):
        mesh = _block(tmp_path / "block.stl")
        _placeholder_archive(tmp_path, sliced_from=mesh)
        sc = _show(str(tmp_path / "block.gcode"))
        assert sc["stage_mesh_path"] == os.path.abspath(mesh)
        assert "only toolpaths" in sc["shows"]

    def test_raw_gcode_nobody_sliced_here_is_refused_in_words(self, tmp_path):
        gcode = tmp_path / "found.gcode"
        gcode.write_text(_LAYERS)
        out = _door(_served()).fn(file_path=str(gcode))
        assert out["error"]["code"] == "NOT_STAGEABLE"
        assert "only toolpaths" in out["error"]["message"]
        assert "no record of the mesh" in out["error"]["message"]

    def test_a_host_with_no_panel_gets_the_link_for_the_same_file(self, tmp_path, monkeypatch):
        calls = _wire_link_door(monkeypatch)
        archive = _sliced_archive(tmp_path)
        sc = _show(archive, host=_Host(_Caps()))
        assert sc["viewer_url"]
        assert sc["shown"]["door"] == "link"
        assert len(calls) == 1
        refusal, _ = preview_evidence.judge(archive, "url", host_renders=False)
        assert refusal is None, refusal

    def test_the_door_changes_nothing(self, tmp_path):
        files = tmp_path / "files"
        files.mkdir()
        archive = _sliced_archive(files)
        before = hashlib.sha256(Path(archive).read_bytes()).hexdigest()
        listing = sorted(os.listdir(files))
        _show(archive)
        assert hashlib.sha256(Path(archive).read_bytes()).hexdigest() == before
        assert sorted(os.listdir(files)) == listing

    def test_openscad_source_is_refused_with_the_door_that_draws_it(self, tmp_path):
        scad = tmp_path / "part.scad"
        scad.write_text("cube(10);")
        out = _door(_served()).fn(file_path=str(scad))
        assert out["error"]["code"] == "NOT_STAGEABLE"
        assert "compile_scad" in out["error"]["message"]

    def test_a_switched_off_stage_says_so(self, tmp_path, monkeypatch):
        mcp = _served()
        monkeypatch.setenv(local_stage._OPT_OUT_ENV, "1")
        out = _door(mcp).fn(file_path=_sliced_archive(tmp_path))
        assert out["error"]["code"] == "STAGE_OFF"
        assert "visualize_model" in out["error"]["message"]


class TestTheGateReadsTheSameAnswer:
    """The door and the gate cannot disagree about what the stage can show."""

    def test_png_is_not_refused_for_a_stage_no_door_can_open(self, tmp_path):
        archive = _placeholder_archive(tmp_path)
        preview_evidence.record("png", archive, renderer="stage", shown_sha="abc")
        preview_evidence.record_url_refusal(archive, "signed_out")
        refusal, verdict = preview_evidence.judge(archive, "png", host_renders=True, panel_proven=True)
        assert refusal is None, refusal
        assert "placeholder" in verdict["skipped"]["stage"]

    def test_png_is_still_refused_when_the_door_could_have_opened_the_stage(self, tmp_path):
        archive = _sliced_archive(tmp_path)
        preview_evidence.record("png", archive, renderer="stage", shown_sha="abc")
        preview_evidence.record_url_refusal(archive, "signed_out")
        refusal, _ = preview_evidence.judge(archive, "png", host_renders=True, panel_proven=True)
        assert refusal is not None
        assert "show_on_stage(" in refusal["message"]

    def test_the_stage_refusal_names_a_door_that_exists(self, tmp_path):
        archive = _sliced_archive(tmp_path)
        refusal, _ = preview_evidence.judge(archive, "stage", host_renders=True, panel_proven=True)
        assert refusal is not None
        assert "show_on_stage(" in refusal["message"]
        assert "show_on_stage" in local_stage.VIEWER_TOOLS
        assert "show_on_stage" in _served()._tool_manager._tools  # type: ignore[attr-defined]


class TestTheGuidanceNamesTheDoor:
    """Every surface that tells an agent how to show a print names the stage
    door.  On 2026-09-22 the only tool Kiln's guidance named for showing a
    file was the still door, whose description called itself the primary
    preview tool — so an agent following it to the letter could not reach
    the stage the gate asks for first."""

    def test_visualize_model_no_longer_claims_to_be_the_primary_door(self):
        from kiln import server

        desc = server.mcp._tool_manager._tools["visualize_model"].description or ""
        assert "Primary 3D preview" not in desc
        assert "USE THIS ONE" not in desc
        assert "show_on_stage(file_path)" in desc

    def test_the_print_refusal_names_the_stage_door(self):
        from kiln.print_signoff import not_confirmed_message

        assert "show_on_stage(file_path)" in not_confirmed_message("start_print")

    def test_the_connect_preamble_names_the_stage_door(self):
        from kiln.server import _build_instructions

        assert "`show_on_stage(file_path)`" in _build_instructions()

    def test_onboarding_prints_a_file_through_the_stage_and_the_token(self):
        from kiln.server import get_started

        flow = get_started()["core_workflows"]["print_a_file"]
        assert flow.index("show_on_stage") < flow.index("issue_preview_token") < flow.index("start_print")

    def test_the_skill_manifest_names_the_stage_door(self):
        from kiln.skill_manifest import SkillManifest

        blob = str(SkillManifest().to_dict())
        assert "show_on_stage(file_path)" in blob


class TestExtractionNeverWritesThePlaceholder:
    """``extract_model_from_3mf`` is stamped for the stage, so whatever it
    writes is what the person is shown.  From a placeholder archive it used
    to write the 1 mm cube and call it the part."""

    def test_a_placeholder_archive_is_refused_instead_of_extracted(self, tmp_path):
        from kiln.generation.validation import extract_model_from_3mf

        archive = _placeholder_archive(tmp_path)
        out = tmp_path / "part.stl"
        with pytest.raises(ValueError, match="placeholder"):
            extract_model_from_3mf(archive, output_path=str(out))
        assert not out.exists()

    def test_the_refusal_names_the_mesh_it_was_sliced_from(self, tmp_path):
        from kiln.generation.validation import extract_model_from_3mf

        mesh = _block(tmp_path / "block.stl")
        archive = _placeholder_archive(tmp_path, sliced_from=mesh)
        with pytest.raises(ValueError, match="block.stl"):
            extract_model_from_3mf(archive, output_path=str(tmp_path / "part.stl"))

    def test_an_archive_carrying_its_model_still_extracts(self, tmp_path):
        from kiln.generation.validation import extract_model_from_3mf

        out = extract_model_from_3mf(_sliced_archive(tmp_path), output_path=str(tmp_path / "ball.stl"))
        assert out["triangle_count"] > 12
        assert out["dimensions"]["z_mm"] == pytest.approx(24.0, abs=0.5)


class TestAPlaceholderIsWhatTheModelHolds:
    """The placeholder is told apart by what its model holds — a 1 mm cube,
    or nothing — never by the size of its file.  A small real part and a
    project keeping its meshes in sub-parts both have a tiny model entry."""

    def test_the_wrappers_own_placeholder_is_recognised(self, tmp_path):
        from kiln.printers.bambu_3mf import carries_placeholder_model

        assert carries_placeholder_model(_placeholder_archive(tmp_path)) is True

    def test_a_small_real_part_is_not_a_placeholder(self, tmp_path):
        from kiln.printers.bambu_3mf import carries_placeholder_model

        assert carries_placeholder_model(_small_part_archive(tmp_path)) is False

    def test_a_model_3mf_with_no_print_inside_is_never_a_placeholder(self, tmp_path):
        """Even one whose model IS a 1 mm cube: without plate G-code it is a
        part someone made, not a print archive's stand-in."""
        import zipfile

        from kiln.printers.bambu_3mf import _MINIMAL_3D_MODEL, carries_placeholder_model

        tiny = tmp_path / "tiny.3mf"
        with zipfile.ZipFile(tiny, "w") as zf:
            zf.writestr("3D/3dmodel.model", _MINIMAL_3D_MODEL)
        assert carries_placeholder_model(str(tiny)) is False

    def test_meshes_in_sub_parts_are_not_a_placeholder(self, tmp_path):
        import zipfile

        from kiln.printers.bambu_3mf import carries_placeholder_model

        root = (
            '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
            'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">'
            '<resources><object id="2" type="model"><components>'
            '<component p:path="/3D/Objects/object_1.model" objectid="1"/>'
            "</components></object></resources><build><item objectid=\"2\"/></build></model>"
        )
        archive = tmp_path / "project.gcode.3mf"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("3D/3dmodel.model", root)
            zf.writestr("3D/Objects/object_1.model", "<model>real geometry lives here</model>")
            zf.writestr("Metadata/plate_1.gcode", _LAYERS)
        assert carries_placeholder_model(str(archive)) is False

    def test_the_stage_draws_a_small_real_part_as_itself(self, tmp_path):
        archive = _small_part_archive(tmp_path)
        sc = _show(archive)
        assert sc["success"] is True, sc
        assert sc["stage_mesh_path"] == archive
        assert "placeholder" not in sc["shows"]
        assert sorted(_panel_fetches(sc)["bbox"]["size"]) == pytest.approx([20.0, 20.0, 20.0], abs=0.01)

    def test_a_small_real_part_still_extracts(self, tmp_path):
        from kiln.generation.validation import extract_model_from_3mf

        out = extract_model_from_3mf(_small_part_archive(tmp_path), output_path=str(tmp_path / "c.stl"))
        assert out["dimensions"]["x_mm"] == pytest.approx(20.0, abs=0.01)
