"""The print bed the 3D stage stands a model on.

The bed is the only object in the frame with a known size, so it is what
tells a person how big the part is — and, when the part hangs off the edge,
that it will not print in one piece.  Two things have to hold or it lies:

* the dimensions are the CALLER's machine or they are declared a reference
  plate.  A 256 mm square drawn as if it were somebody's 350 mm bed is a
  wrong answer delivered confidently;
* every door that builds a payload attaches one.  The stage draws what
  arrives; a door that forgets is a stage with no bed under it, which is the
  bug this file exists for.
"""

from __future__ import annotations

import struct

import pytest

from kiln import local_stage, stage_plate


def _real_cube(path):
    trimesh = pytest.importorskip("trimesh")
    trimesh.creation.box(extents=(20.0, 20.0, 20.0)).export(str(path))
    return str(path)


class TestResolution:
    def test_a_known_printer_gives_its_own_bed(self):
        plate = stage_plate.resolve_stage_plate("prusa_mk4")
        assert (plate["x_mm"], plate["y_mm"]) == (250.0, 210.0)
        assert plate["z_mm"] == 220.0
        assert plate["source"] == "printer"
        assert plate["printer_id"] == "prusa_mk4"
        assert plate["label"] == "Prusa MK4 / MK4S"

    def test_a_non_square_bed_survives_as_a_rectangle(self):
        """The reason this module exists: one number cannot describe a bed."""
        plate = stage_plate.resolve_stage_plate("prusa_mk4")
        assert plate["x_mm"] != plate["y_mm"]

    def test_a_human_label_resolves_the_same_as_the_id(self):
        assert stage_plate.resolve_stage_plate("Bambu Lab A1")["printer_id"] == "bambu_a1"

    def test_an_unknown_printer_is_a_reference_plate_not_a_guess(self):
        plate = stage_plate.resolve_stage_plate("totally_made_up_xyz")
        assert plate == stage_plate.default_stage_plate()
        assert plate["source"] == "default"
        assert plate["label"] is None, "a plate must not claim a printer we cannot name"

    def test_the_reference_plate_names_no_volume(self):
        """z_mm is what licenses drawing a build envelope — never invent one."""
        assert stage_plate.default_stage_plate()["z_mm"] is None

    def test_no_configured_model_falls_back_rather_than_inferring(self, monkeypatch):
        monkeypatch.setattr(
            "kiln.printer_model_resolver.resolve_printer_model", lambda: None
        )
        assert stage_plate.resolve_stage_plate()["source"] == "default"

    def test_the_hosted_process_never_serves_its_own_box_as_yours(self, monkeypatch):
        """One process, one ~/.kiln, every customer.  That file's printer_model
        belongs to the Fly machine, not to whoever is calling."""
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        monkeypatch.setattr(
            "kiln.printer_model_resolver.resolve_printer_model",
            lambda: pytest.fail("hosted resolution read the shared config"),
        )
        assert stage_plate.resolve_stage_plate()["source"] == "default"

    def test_an_explicit_printer_id_is_honoured_even_hosted(self, monkeypatch):
        """The hosted skip is about the SHARED file, not about refusing to
        answer for a printer the caller named."""
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        assert stage_plate.resolve_stage_plate("prusa_mk4")["source"] == "printer"

    def test_a_broken_catalogue_still_yields_a_bed(self, monkeypatch):
        monkeypatch.setattr(
            "kiln.printers.bed_fit.resolve_build_volume",
            lambda _pid: (_ for _ in ()).throw(RuntimeError("catalogue on fire")),
        )
        assert stage_plate.resolve_stage_plate("prusa_mk4")["source"] == "default"


class TestAttach:
    def test_stamps_the_plate_onto_a_payload(self):
        payload = stage_plate.attach_stage_plate({"kind": "kiln.mesh.v1"}, "prusa_mk4")
        assert payload["plate"]["x_mm"] == 250.0

    def test_no_payload_passes_through(self):
        assert stage_plate.attach_stage_plate(None) is None


class TestEveryDoorAttachesIt:
    """The one-door fallacy, applied to furniture: fixing the door a user
    knocks on is half the job."""

    def test_the_result_hook_payload_carries_a_plate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "kiln.printer_model_resolver.resolve_printer_model", lambda: "prusa_mk4"
        )
        mesh = _real_cube(tmp_path / "cube.stl")
        token = local_stage._mint(mesh)
        payload = local_stage._inline_payload(token)
        assert payload["plate"]["printer_id"] == "prusa_mk4"

    def test_the_panels_own_fetch_verb_carries_a_plate(self, tmp_path, monkeypatch):
        """The lazy-fetch door: a host that renders the panel but hands the
        geometry back through a tool call gets the same bed as the inline one."""
        monkeypatch.setattr(
            "kiln.printer_model_resolver.resolve_printer_model", lambda: "prusa_mk4"
        )
        mesh = _real_cube(tmp_path / "cube.stl")
        payload = local_stage._payload_for_mesh(mesh)
        assert payload["plate"]["printer_id"] == "prusa_mk4"

    def test_an_unreadable_mesh_still_raises_for_the_door_that_answers_a_person(
        self, tmp_path
    ):
        """_payload_for_mesh must not swallow: the fetch verb turns the error
        into a sentence, and a silent None strands the panel waiting."""
        bad = tmp_path / "empty.stl"
        bad.write_bytes(bytes(bytearray(b"\x00" * 80) + struct.pack("<I", 0)))
        with pytest.raises(Exception):
            local_stage._payload_for_mesh(str(bad))
