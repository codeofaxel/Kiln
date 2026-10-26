"""A part designed in whole layers is sliced in those layers, by every door.

2026-10-26: ``decorate_surface`` sculpted a headshot as eight 0.2 mm levels
and said so; ``slice_model`` sliced the coaster at PrusaSlicer's 0.3 mm
default and six layers printed where eight were designed.  The layer height
is now kept beside the part (:mod:`kiln.slicer_layer`) and decided inside
``slice_file`` -- the chokepoint every slicing door passes through -- with
a caller's stated layer kept and said.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiln import slicer_layer
from kiln.slicer_layer import (
    BAMBU_OBJECT_CONFIG,
    DECIDED_NOZZLE,
    DECIDED_RECORD,
    DECIDED_STATED,
    FIRST_LAYER_KEY,
    LAYER_KEY,
    PRUSA_OBJECT_CONFIG,
    RECORD_KEY,
    DesignedLayer,
    SliceLayer,
    _first_layer_on_top,
    carry,
    ensure_profile_layer,
    for_the_slicer,
    keep_on_record,
    levels_at,
    read,
    record,
    witnessed,
)
from kiln.slicer_orca import ini_to_settings
from kiln.slicer_profiles import profile_origin, profile_with_overrides

RELIEF = DesignedLayer(height_mm=0.2, levels=8, depth_mm=1.6, made_by="decorate_surface", why="a photo relief")


@pytest.fixture
def part(tmp_path: Path) -> str:
    path = tmp_path / "coaster.stl"
    path.write_bytes(b"\0" * 84)
    return str(path)


@pytest.fixture
def relief_part(part: str) -> str:
    assert record(part, RELIEF)
    return part


def _kiln_profile(**settings: str) -> str:
    """A Kiln-written profile stating no layer height, like the bundled ones."""
    base = {"nozzle_diameter": "0.4", "bed_shape": "0x0,200x0,200x200,0x200"}
    base.update(settings)
    return profile_with_overrides(None, base, stated=False)


# ---------------------------------------------------------------------------
# The note beside the part
# ---------------------------------------------------------------------------


class TestTheNote:
    def test_round_trip(self, relief_part: str):
        assert read(relief_part) == RELIEF

    def test_no_note_reads_none(self, part: str):
        assert read(part) is None

    def test_a_changed_file_drops_its_note(self, relief_part: str):
        Path(relief_part).write_bytes(b"\1" * 84)
        assert read(relief_part) is None

    def test_carry_follows_a_copy(self, relief_part: str, tmp_path: Path):
        copy = str(tmp_path / "coaster_bedcentered.stl")
        shutil.copy(relief_part, copy)
        carry(relief_part, copy)
        assert read(copy) == RELIEF

    def test_a_bad_height_is_not_recorded(self, part: str):
        assert record(part, DesignedLayer(height_mm=0.0)) is None
        assert record(part, DesignedLayer(height_mm=float("nan"))) is None


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


class TestTheRule:
    def test_no_note_decides_nothing(self, part: str):
        profile = _kiln_profile()
        assert ensure_profile_layer(profile, part) == (profile, None)

    def test_a_kiln_profile_takes_the_designed_layer(self, relief_part: str):
        profile = _kiln_profile()
        written, decision = ensure_profile_layer(profile, relief_part)
        assert decision is not None and decision.decided_by == DECIDED_RECORD
        assert decision.height_mm == 0.2 and decision.levels_printed == 8
        assert ini_to_settings(written)[LAYER_KEY] == "0.2"
        # Kiln's own layer, not the caller's: the next part may decide again.
        assert LAYER_KEY not in profile_origin(written).stated
        assert "0.2 mm layers, as the part was designed" in decision.note

    def test_a_stated_layer_is_kept_and_said(self, relief_part: str):
        profile = profile_with_overrides(_kiln_profile(), {LAYER_KEY: "0.3"}, stated=True)
        written, decision = ensure_profile_layer(profile, relief_part)
        assert written == profile
        assert decision is not None and decision.decided_by == DECIDED_STATED
        assert decision.height_mm == 0.3 and decision.levels_printed == 5
        assert "5 of its 8 levels" in decision.note

    def test_the_callers_own_profile_is_never_changed(self, relief_part: str, tmp_path: Path):
        own = tmp_path / "mine.ini"
        own.write_text("nozzle_diameter = 0.4\nlayer_height = 0.28\n")
        written, decision = ensure_profile_layer(str(own), relief_part)
        assert written == str(own)
        assert decision is not None and decision.decided_by == DECIDED_STATED
        assert decision.levels_printed == 5

    def test_a_layer_the_nozzle_cannot_lay_is_not_applied(self, relief_part: str):
        profile = _kiln_profile(nozzle_diameter="1.0", layer_height="0.4")
        written, decision = ensure_profile_layer(profile, relief_part, nozzle_mm=1.0)
        assert written == profile
        assert decision is not None and decision.decided_by == DECIDED_NOZZLE
        assert "1 mm nozzle lays" in decision.reason and decision.levels_printed == 4

    def test_a_profile_already_printing_it_is_left_alone(self, relief_part: str):
        profile = _kiln_profile(layer_height="0.2")
        written, decision = ensure_profile_layer(profile, relief_part)
        assert written == profile and decision is not None and decision.decided_by == DECIDED_RECORD

    def test_levels_never_exceed_the_design(self):
        assert levels_at(RELIEF, 0.1) == 8
        assert levels_at(RELIEF, 0.3) == 5
        assert levels_at(DesignedLayer(0.2), 0.2) is None


class TestWitnessed:
    def test_the_gcode_names_the_layer_that_prints(self, tmp_path: Path):
        gcode = tmp_path / "part.gcode"
        gcode.write_text("; generated\nG1 X0\n; layer_height = 0.3\n")
        asked = SliceLayer(0.2, DECIDED_RECORD, RELIEF, 8, "the part's own note", {LAYER_KEY: "0.2"})
        seen = witnessed(asked, str(gcode))
        assert seen is not None and seen.height_mm == 0.3 and seen.levels_printed == 5

    def test_an_agreeing_file_changes_nothing(self, tmp_path: Path):
        gcode = tmp_path / "part.gcode"
        gcode.write_text("; layer_height = 0.2\n")
        asked = SliceLayer(0.2, DECIDED_RECORD, RELIEF, 8, "the part's own note")
        assert witnessed(asked, str(gcode)) is asked

    def test_nothing_decided_stays_nothing(self, tmp_path: Path):
        assert witnessed(None, str(tmp_path / "x.gcode")) is None


# ---------------------------------------------------------------------------
# The chokepoint: every door passes through slice_file
# ---------------------------------------------------------------------------


class TestSliceFileHonoursTheNote:
    def _run(self, monkeypatch, part: str, profile: str):
        import kiln.slicer as slicer_mod

        seen: dict[str, object] = {}

        def fake_run(cmd, **_kwargs):
            seen["profile"] = cmd[cmd.index("--load") + 1]
            out = cmd[cmd.index("--output") + 1]
            Path(out).write_text("; generated by fake\nG1 X0 Y0 Z0.2 E1\n")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(slicer_mod, "_run_slicer_with_startup_retry", fake_run)
        monkeypatch.setattr(slicer_mod, "find_slicer", lambda *_a, **_k: SimpleNamespace(
            path="/fake/prusa-slicer", name="prusaslicer", version="2.9.4",
        ))
        monkeypatch.setattr(slicer_mod, "_gcode_output_is_complete", lambda *_a: True)
        monkeypatch.setattr(slicer_mod, "_record_slice", lambda *_a, **_k: None)
        monkeypatch.setattr(slicer_mod, "_drawn_mesh_for", lambda p: p)
        result = slicer_mod.slice_file(part, profile=profile)
        return result, ini_to_settings(str(seen["profile"]))

    def test_the_slicer_is_handed_the_designed_layer(self, relief_part: str, monkeypatch):
        result, handed = self._run(monkeypatch, relief_part, _kiln_profile())
        assert handed[LAYER_KEY] == "0.2"
        assert result.layer is not None and result.layer.decided_by == DECIDED_RECORD
        assert result.to_dict()["layer"]["designed"]["levels"] == 8

    def test_a_stated_layer_reaches_the_slicer_as_stated(self, relief_part: str, monkeypatch):
        profile = profile_with_overrides(_kiln_profile(), {LAYER_KEY: "0.3"}, stated=True)
        result, handed = self._run(monkeypatch, relief_part, profile)
        assert handed[LAYER_KEY] == "0.3"
        assert result.layer is not None and result.layer.decided_by == DECIDED_STATED
        assert result.layer.levels_printed == 5

    def test_a_part_with_no_note_says_nothing(self, part: str, monkeypatch):
        result, handed = self._run(monkeypatch, part, _kiln_profile())
        assert LAYER_KEY not in handed
        assert result.layer is None and "layer" not in result.to_dict()


class TestTheCopiesCarryIt:
    def test_a_placed_copy_is_sliced_in_the_designed_layers(self, relief_part: str, tmp_path: Path, monkeypatch):
        """``_placed_slice`` hands the slicer a centred or placed COPY; the
        note follows it, or the chokepoint would read the original's note
        beside a file it is not about."""
        from kiln.plugins import slicer_tools

        copy = str(tmp_path / "coaster_bedcentered.stl")
        shutil.copy(relief_part, copy)
        monkeypatch.setattr(
            slicer_tools, "_apply_plate_placement",
            lambda *_a, **_k: (copy, None, {"plate": "clear"}),
        )
        handed: list[str] = []

        def fake_slicer(path, *, profile=None, **_k):
            handed.append(path)
            from kiln.slicer import SliceResult

            return SliceResult(success=True, output_path=str(tmp_path / "out.gcode"))

        slicer_tools._placed_slice(
            relief_part, effective_printer_id=None, printer_name=None, placement=None, slicer=fake_slicer,
        )
        assert handed == [copy]
        assert read(copy) == RELIEF


class TestTheMakeRecordsIt:
    def test_decorate_surface_leaves_the_note(self, monkeypatch, tmp_path: Path):
        """The relief block's ``layer_mm`` is the note on the carved part."""
        import struct

        from kiln.server import decorate_surface

        _decorate = getattr(decorate_surface, "fn", decorate_surface)
        src = tmp_path / "coaster.stl"
        src.write_bytes(b"\0" * 80 + struct.pack("<I", 0))
        out = tmp_path / "coaster_deboss.stl"
        out.write_bytes(b"\0" * 80 + struct.pack("<I", 1) + b"\1" * 50)
        photo = tmp_path / "photo.jpg"
        from PIL import Image

        Image.new("RGB", (64, 64), (120, 100, 90)).save(photo)

        import kiln.emboss_generator as emboss
        import kiln.surface_intelligence as surf
        import kiln.image_to_surface as its

        face_info = {
            "face_name": "top", "area_mm2": 6362.0, "width_mm": 90.0, "height_mm": 90.0,
            "center": (0, 0, 7.0), "normal": (0, 0, 1),
        }
        monkeypatch.setattr(surf, "resolve_decoratable_face", lambda _p, _f=None: face_info)
        monkeypatch.setattr(its, "prepare_image_for_emboss", lambda *_a, **_k: {
            "type": "heightmap", "dat_path": str(tmp_path / "x.dat"), "width_px": 8, "height_px": 8,
            "aspect_ratio": 1.0, "treatment": "relief", "style_applied": "coin", "levels": 8,
            "layer_mm": 0.2, "depth_mm": 1.6, "nozzle": "0.4", "base": "pool", "relief_source": "floor",
        })
        scad = tmp_path / "x.scad"
        scad.write_text("// stub", encoding="utf-8")
        monkeypatch.setattr(
            emboss, "generate_emboss_scad",
            lambda **_k: {"scad_path": str(scad), "output_stl_path": str(out)},
        )
        monkeypatch.setattr(
            emboss, "compile_embossed_model",
            lambda *_a, **_k: {"success": True, "stl_path": str(out), "compile_time_seconds": 0.1},
        )
        monkeypatch.setattr(emboss, "check_boolean_success", lambda *_a, **_k: True)
        monkeypatch.setattr(
            "kiln.decoration_quota.get_decoration_quota",
            lambda: type("Q", (), {"check_and_increment": lambda self: (True, ""), "refund": lambda self: None})(),
            raising=False,
        )
        monkeypatch.setattr("kiln.server._check_auth", lambda *_a, **_k: None)
        monkeypatch.setenv("KILN_STAGE_PREVIEW", "0")
        monkeypatch.setenv("KILN_NO_STAGE_LINKS", "1")

        result = _decorate(model_path=str(src), content=str(photo), face="top", mode="deboss", image_style="auto")
        assert result.get("success"), result
        note = read(str(out))
        assert note is not None and note.height_mm == 0.2 and note.levels == 8 and note.made_by == "decorate_surface"
        assert result["decoration"]["relief"]["layer_note"]


def _real_prusaslicer() -> str | None:
    found = shutil.which("prusa-slicer") or shutil.which("PrusaSlicer")
    if found:
        return found
    mac = "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer"
    return mac if os.path.isfile(mac) and os.access(mac, os.X_OK) else None


@pytest.mark.skipif(_real_prusaslicer() is None, reason="needs a real PrusaSlicer")
class TestTheGcodeIsInTheDesignedLayers:
    def test_a_noted_cube_slices_at_its_layer(self, tmp_path: Path):
        import trimesh

        from kiln.slicer import slice_file
        from kiln.slicer_profiles import resolve_slicer_profile

        cube = tmp_path / "cube.stl"
        trimesh.creation.box(extents=(10.0, 10.0, 3.0)).export(cube)
        record(str(cube), RELIEF)
        result = slice_file(
            str(cube), output_dir=str(tmp_path), profile=resolve_slicer_profile("bambu_a1"),
            slicer_path=_real_prusaslicer(),
        )
        assert result.success and result.layer is not None
        text = Path(result.output_path).read_text(errors="replace")
        assert "; layer_height = 0.2\n" in text
        assert result.layer.height_mm == 0.2 and result.layer.decided_by == DECIDED_RECORD

    def test_the_last_layer_is_the_parts_top(self, tmp_path: Path):
        """A 7 mm part on PrusaSlicer's 0.35 mm first layer ended at 6.95 mm:
        one layer short, every relief terrace 0.05 mm off its design."""
        from kiln.slicer import slice_file

        part = _tall_part(tmp_path)
        result = slice_file(part, output_dir=str(tmp_path), slicer_path=_real_prusaslicer())
        assert result.success and result.layer is not None
        zs = _layer_zs(result.output_path)
        assert zs[-1] == pytest.approx(7.0) and zs[0] == pytest.approx(0.2)
        assert result.layer.first_layer_mm == pytest.approx(0.2)

    def test_an_exported_3mf_prints_its_layers_in_the_slicer_itself(self, tmp_path: Path):
        """The 3MF a person opens in a slicer app, sliced by that slicer with
        none of Kiln's settings: its own default is 0.3 mm."""
        import subprocess

        from kiln.generation.validation import export_3mf

        out = export_3mf(_tall_part(tmp_path), output_path=str(tmp_path / "coaster.3mf"))
        gcode = tmp_path / "direct.gcode"
        subprocess.run(
            [_real_prusaslicer(), "--export-gcode", out, "--output", str(gcode)],
            check=True, capture_output=True, timeout=180,
        )
        steps = _steps(_layer_zs(str(gcode)))
        assert steps == {0.2}

    def test_kiln_slices_an_exported_3mf_at_a_stated_layer(self, tmp_path: Path):
        """The object's own setting would have won over the stated 0.3 mm
        while the G-code's header still said 0.3."""
        from kiln.generation.validation import export_3mf
        from kiln.slicer import slice_file

        out = export_3mf(_tall_part(tmp_path), output_path=str(tmp_path / "coaster.3mf"))
        profile = profile_with_overrides(_kiln_profile(), {LAYER_KEY: "0.3"}, stated=True)
        result = slice_file(out, output_dir=str(tmp_path), profile=profile, slicer_path=_real_prusaslicer())
        assert result.success and result.layer is not None and result.layer.decided_by == DECIDED_STATED
        assert _steps(_layer_zs(result.output_path)) == {0.3}


def _tall_part(tmp_path: Path, height: float = 7.0, layer: DesignedLayer = RELIEF) -> str:
    """A noted round part standing on a 200 mm bed: *height* mm tall."""
    import trimesh

    part = tmp_path / "coaster_deboss.stl"
    disc = trimesh.creation.cylinder(radius=20.0, height=height, sections=64)
    disc.apply_translation([100, 100, height / 2])
    disc.export(part)
    assert record(str(part), layer)
    return str(part)


def _layer_zs(gcode: str) -> list[float]:
    return [float(line[3:]) for line in Path(gcode).read_text(errors="replace").splitlines() if line.startswith(";Z:")]


def _steps(zs: list[float]) -> set[float]:
    """The layer heights after the first layer, to 0.01 mm."""
    return {round(b - a, 2) for a, b in zip(zs[1:], zs[2:])}


# ---------------------------------------------------------------------------
# The first layer puts the top on a layer
# ---------------------------------------------------------------------------


class TestTheFirstLayer:
    def test_the_top_lands_on_a_whole_layer(self, tmp_path: Path):
        written, decision = ensure_profile_layer(_kiln_profile(), _tall_part(tmp_path))
        settings = ini_to_settings(written)
        assert settings[LAYER_KEY] == "0.2" and settings[FIRST_LAYER_KEY] == "0.2"
        assert decision.first_layer_mm == 0.2 and decision.part_height_mm == 7.0
        assert FIRST_LAYER_KEY not in profile_origin(written).stated
        assert "0.2 mm first layer so its 7 mm top is a whole layer" in decision.note

    def test_a_profile_already_in_the_designed_layer_still_lands_the_top(self, tmp_path: Path):
        profile = _kiln_profile(layer_height="0.2")
        written, decision = ensure_profile_layer(profile, _tall_part(tmp_path))
        assert written != profile and decision.settings == {FIRST_LAYER_KEY: "0.2"}

    def test_a_first_layer_already_on_the_grid_is_left_alone(self, tmp_path: Path):
        profile = _kiln_profile(layer_height="0.2", first_layer_height="0.2")
        written, decision = ensure_profile_layer(profile, _tall_part(tmp_path))
        assert written == profile and decision.first_layer_mm is None and decision.settings == {}

    def test_a_stated_first_layer_is_kept(self, tmp_path: Path):
        profile = profile_with_overrides(_kiln_profile(), {FIRST_LAYER_KEY: "0.35"}, stated=True)
        written, decision = ensure_profile_layer(profile, _tall_part(tmp_path))
        assert ini_to_settings(written)[FIRST_LAYER_KEY] == "0.35"
        assert decision.first_layer_mm is None and FIRST_LAYER_KEY not in decision.settings

    def test_a_top_no_first_layer_reaches_is_said(self, tmp_path: Path):
        part = _tall_part(tmp_path, height=7.05, layer=DesignedLayer(height_mm=0.28, levels=4, depth_mm=1.12))
        written, decision = ensure_profile_layer(_kiln_profile(), part)
        assert FIRST_LAYER_KEY not in ini_to_settings(written)
        assert decision.first_layer_mm is None and "falls between layers" in decision.note

    def test_the_nearest_first_layer_to_the_profiles_own_is_chosen(self):
        assert _first_layer_on_top(7.0, 0.2, (0.1, 0.3), current=None) == (0.2, "")
        assert _first_layer_on_top(7.05, 0.2, (0.1, 0.3), current=0.35) == (0.25, "")
        assert _first_layer_on_top(7.0, 0.2, (0.1, 0.3), current=0.2) == (None, "")

    def test_a_first_layer_the_gcode_names_is_the_one_reported(self, tmp_path: Path):
        gcode = tmp_path / "out.gcode"
        gcode.write_text("; layer_height = 0.2\n; first_layer_height = 0.3\n")
        decision = SliceLayer(0.2, DECIDED_RECORD, RELIEF, 8, "the part's own note", first_layer_mm=0.2, part_height_mm=7.0)
        assert witnessed(decision, str(gcode)).first_layer_mm == 0.3


# ---------------------------------------------------------------------------
# The note on a saved version, and everywhere a version goes
# ---------------------------------------------------------------------------


class TestTheRecord:
    def test_the_record_takes_the_note_and_keeps_the_rest(self, relief_part: str):
        kept = keep_on_record({"origin": "make"}, relief_part)
        assert kept["origin"] == "make"
        assert kept[RECORD_KEY]["layer"]["height_mm"] == 0.2

    def test_the_record_puts_a_lost_note_back(self, relief_part: str):
        kept = keep_on_record(None, relief_part)
        os.remove(relief_part + ".layer.json")
        assert keep_on_record(kept, relief_part) == kept
        assert read(relief_part) == RELIEF

    def test_a_record_about_other_bytes_is_dropped(self, relief_part: str, tmp_path: Path):
        kept = keep_on_record({"origin": "make"}, relief_part)
        other = tmp_path / "other.stl"
        other.write_bytes(b"\2" * 84)
        assert keep_on_record(kept, str(other)) == {"origin": "make"}
        assert read(str(other)) is None

    def test_a_mesh_not_on_this_computer_leaves_the_record_as_it_is(self, relief_part: str):
        kept = keep_on_record(None, relief_part)
        assert keep_on_record(kept, "/nowhere/coaster.stl") is kept

    def test_a_version_with_no_mesh_keeps_no_layer(self, relief_part: str):
        kept = keep_on_record({"origin": "make"}, relief_part)
        assert keep_on_record(kept, None) == {"origin": "make"}


def _version_tools() -> dict:
    from kiln.plugins.version_tools import _VersionToolsPlugin

    tools: dict = {}

    class _Mcp:
        def tool(self, **_k):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn
            return deco

    _VersionToolsPlugin().register(_Mcp())
    return tools


class TestSavedVersionsKeepIt:
    def test_a_restored_version_is_its_mesh_in_its_layers(self, relief_part: str, tmp_path: Path, monkeypatch):
        """A version saved from a relief, the working file and every note
        since lost, restored: it slices in the layers it was designed in."""
        import json

        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path / "designs"))
        tools = _version_tools()
        first = tools["save_design_version"](design_id="headshot", scad_source="", stl_path=relief_part)
        kept_mesh = first["version"]["stl_path"]
        saved = json.loads(Path(first["version"]["versioned_path"]).read_text())
        assert saved["provenance"][RECORD_KEY]["layer"]["height_mm"] == 0.2
        plain = tmp_path / "plain.stl"
        plain.write_bytes(b"\3" * 84)
        tools["save_design_version"](design_id="headshot", scad_source="", stl_path=str(plain))
        for gone in (relief_part, relief_part + ".layer.json", kept_mesh + ".layer.json"):
            os.remove(gone)

        restored = tools["rollback_design_version"](design_id="headshot", to_version_id="1")

        assert restored["ok"], restored
        assert restored["version"]["stl_path"] == kept_mesh
        assert read(kept_mesh) == RELIEF

    def test_a_design_cloned_onto_another_computer_is_sliced_in_its_layers(
        self, relief_part: str, tmp_path: Path, monkeypatch,
    ):
        from kiln import history_courier as courier
        from kiln.history_vault import HistoryVault

        monkeypatch.setenv("HOME", str(tmp_path / "first"))
        _version_tools()["save_design_version"](design_id="headshot", scad_source="", stl_path=relief_part)
        here = HistoryVault()
        try:
            here.sync_from_library("headshot")
            answer = here.bundle("headshot")
        finally:
            here.close()

        monkeypatch.setenv("HOME", str(tmp_path / "second"))
        monkeypatch.setattr(courier, "_request", lambda tool, method, path, body=None: dict(answer))
        cloned = courier.clone("cloud_clone_design", "headshot")

        assert cloned["status"] == "success", cloned
        folder = tmp_path / "second" / ".kiln" / "designs" / "headshot"
        (mesh,) = [p for p in folder.iterdir() if p.suffix == ".stl"]
        assert read(str(mesh)) == RELIEF

    def test_a_cached_design_keeps_its_note(self, relief_part: str, tmp_path: Path):
        from kiln.design_cache import DesignCache

        cache = DesignCache(cache_dir=str(tmp_path / "cache"))
        entry = cache.add(relief_part)
        assert entry.file_path != relief_part and read(entry.file_path) == RELIEF


# ---------------------------------------------------------------------------
# The layer inside a 3MF
# ---------------------------------------------------------------------------


class TestThe3MF:
    def test_an_exported_3mf_says_its_layer_to_every_slicer(self, tmp_path: Path):
        import zipfile

        from kiln.generation.validation import export_3mf

        out = export_3mf(_tall_part(tmp_path), output_path=str(tmp_path / "coaster.3mf"))
        with zipfile.ZipFile(out) as zf:
            for name in (PRUSA_OBJECT_CONFIG, BAMBU_OBJECT_CONFIG):
                assert 'key="layer_height" value="0.2"' in zf.read(name).decode()
        assert read(out) == RELIEF

    def test_a_part_with_no_note_exports_no_setting(self, tmp_path: Path):
        import zipfile

        import trimesh

        from kiln.generation.validation import export_3mf

        plain = tmp_path / "plain.stl"
        trimesh.creation.box(extents=(10, 10, 3)).export(plain)
        with zipfile.ZipFile(export_3mf(str(plain))) as zf:
            assert PRUSA_OBJECT_CONFIG not in zf.namelist()

    def test_a_stated_layer_hands_the_slicer_a_copy_without_the_designs(self, tmp_path: Path):
        import zipfile

        from kiln.generation.validation import export_3mf

        out = export_3mf(_tall_part(tmp_path), output_path=str(tmp_path / "coaster.3mf"))
        stated = SliceLayer(0.3, DECIDED_STATED, RELIEF, 5, "layer_height 0.3 mm was asked for")
        handed, scratch = for_the_slicer(out, stated)
        try:
            assert handed != out
            with zipfile.ZipFile(handed) as zf:
                for name in (PRUSA_OBJECT_CONFIG, BAMBU_OBJECT_CONFIG):
                    assert "layer_height" not in zf.read(name).decode()
        finally:
            shutil.rmtree(scratch)
        designed = SliceLayer(0.2, DECIDED_RECORD, RELIEF, 8, "the part's own note")
        assert for_the_slicer(out, designed) == (out, None)
