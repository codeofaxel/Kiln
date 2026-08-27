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

And one thing has to hold or the bed is useless: the part has to be ON it.
Model coordinates put the origin at a corner of the part, the plate is
centred on the origin, so an uncentred payload draws a correct bed with the
part parked in a quadrant of it.
"""

from __future__ import annotations

import base64
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


def _corner_box(path, extents=(120.0, 150.0, 20.0)):
    """A part in raw model coordinates: origin at its own corner, resting on
    z = 0.  This is how most parametric templates actually arrive — the SCAD
    that made them starts a cube at the origin and grows it."""
    trimesh = pytest.importorskip("trimesh")
    box = trimesh.creation.box(extents=extents)
    box.apply_translation([e / 2.0 for e in extents])
    box.export(str(path))
    return str(path)


def _mesh_space_positions(payload):
    """The payload's own vertices, read back into mesh space.

    ``positions`` ship viewer-space (x, y, z) = (mesh x, mesh z, -mesh y),
    so this is the inverse of the rotation :mod:`kiln.mesh_payload` bakes in.
    """
    np = pytest.importorskip("numpy")
    xyz = np.frombuffer(base64.b64decode(payload["positions"]), dtype="<f4")
    xyz = xyz.reshape(-1, 3)
    return xyz[:, 0], -xyz[:, 2], xyz[:, 1]


class TestTheModelSitsOnThePlate:
    """The plate is centred on the origin; a template's coordinates are not.
    Without this the bed is drawn correctly and the part is parked in a
    corner of it — which reads as a part that will not print."""

    def test_an_off_origin_part_comes_back_centred(self, tmp_path):
        mesh = _corner_box(tmp_path / "panel.stl")
        bbox = local_stage._payload_for_mesh(mesh)["bbox"]
        assert bbox["min"][0] == pytest.approx(-60.0, abs=1e-3)
        assert bbox["max"][0] == pytest.approx(60.0, abs=1e-3)
        assert bbox["min"][1] == pytest.approx(-75.0, abs=1e-3)
        assert bbox["max"][1] == pytest.approx(75.0, abs=1e-3)

    def test_the_part_still_rests_on_the_bed(self, tmp_path):
        """Z is not ours to move.  A part lifted off the plate — or sunk into
        it — is a lie about the print, not a nicer camera angle."""
        mesh = _corner_box(tmp_path / "panel.stl")
        payload = local_stage._payload_for_mesh(mesh)
        assert payload["bbox"]["min"][2] == pytest.approx(0.0, abs=1e-3)
        assert payload["bbox"]["max"][2] == pytest.approx(20.0, abs=1e-3)
        _x, _y, z = _mesh_space_positions(payload)
        assert float(z.min()) == pytest.approx(0.0, abs=1e-3)

    def test_a_part_below_the_bed_keeps_its_own_z(self, tmp_path):
        """Not every model rests on z = 0 — some straddle it.  Centring is an
        X/Y act; whatever the part claimed about height survives it."""
        trimesh = pytest.importorskip("trimesh")
        box = trimesh.creation.box(extents=(20.0, 20.0, 80.0))
        box.apply_translation([100.0, 40.0, 0.0])  # z spans -40 .. +40
        mesh = str(tmp_path / "straddle.stl")
        box.export(mesh)
        bbox = local_stage._payload_for_mesh(mesh)["bbox"]
        assert bbox["min"][2] == pytest.approx(-40.0, abs=1e-3)
        assert bbox["max"][2] == pytest.approx(40.0, abs=1e-3)

    def test_the_bbox_describes_where_the_vertices_actually_are(self, tmp_path):
        """A payload whose bbox names a different place than its positions is
        worse than one that never centred anything."""
        mesh = _corner_box(tmp_path / "panel.stl")
        payload = local_stage._payload_for_mesh(mesh)
        x, y, z = _mesh_space_positions(payload)
        bbox = payload["bbox"]
        for axis, values in enumerate((x, y, z)):
            assert float(values.min()) == pytest.approx(bbox["min"][axis], abs=1e-3)
            assert float(values.max()) == pytest.approx(bbox["max"][axis], abs=1e-3)

    def test_the_part_keeps_its_size(self, tmp_path):
        """Centring is a translation.  Nothing about how big the part is —
        the number the user reads off the stage — may move."""
        mesh = _corner_box(tmp_path / "panel.stl")
        size = local_stage._payload_for_mesh(mesh)["bbox"]["size"]
        assert size == pytest.approx([120.0, 150.0, 20.0], abs=1e-3)

    def test_an_already_centred_part_is_left_exactly_alone(self, tmp_path):
        mesh = _real_cube(tmp_path / "cube.stl")  # trimesh boxes are origin-centred
        payload = local_stage._payload_for_mesh(mesh)
        assert payload["bbox"]["min"][:2] == pytest.approx([-10.0, -10.0], abs=1e-3)
        x, y, _z = _mesh_space_positions(payload)
        assert float(x.min()) == pytest.approx(-10.0, abs=1e-3)
        assert float(y.min()) == pytest.approx(-10.0, abs=1e-3)

    def test_a_downgraded_payload_passes_through_untouched(self, tmp_path):
        """The too-big-to-send shape carries a bbox and NO geometry.  Moving
        the bbox alone would manufacture the disagreement above."""
        trimesh = pytest.importorskip("trimesh")
        ball = trimesh.creation.icosphere(subdivisions=3, radius=10.0)
        ball.apply_translation([10.0, 10.0, 10.0])  # off origin, resting on z=0
        mesh = str(tmp_path / "ball.stl")
        ball.export(mesh)
        payload = local_stage._payload_for_mesh(mesh, max_bytes=1_000)
        assert payload["downgraded"] is True
        assert "positions" not in payload
        assert payload["bbox"]["min"][:2] == pytest.approx([0.0, 0.0], abs=1e-3)
        assert payload["plate"]["source"] in {"printer", "default"}

    def test_a_downgraded_payload_with_no_bbox_at_all_does_not_crash(self):
        payload = {"kind": "kiln.mesh.v1", "downgraded": True, "reason": "too big"}
        assert local_stage._center_on_plate(dict(payload)) == payload

    def test_unreadable_geometry_degrades_to_uncentred_never_raises(self):
        """Requirement of the whole module: the stage must not be able to
        fail a tool call.  Corrupt positions come back exactly as they went
        in — off centre, and still drawable."""
        broken = {
            "kind": "kiln.mesh.v1",
            "positions": base64.b64encode(b"\x00" * 5).decode("ascii"),  # not xyz-sized
            "bbox": {"min": [0.0, 0.0, 0.0], "max": [10.0, 10.0, 10.0],
                     "size": [10.0, 10.0, 10.0]},
            "downgraded": False,
        }
        out = local_stage._center_on_plate(dict(broken))
        assert out["positions"] == broken["positions"]
        assert out["bbox"] == broken["bbox"]

    def test_none_passes_through(self):
        assert local_stage._center_on_plate(None) is None


class TestEveryDoorGoesThroughThePayloadBuilder:
    """No module may build a stage payload around the chokepoint.

    ``local_stage._payload_for_mesh`` is what stands a model on the plate
    and stamps this install's bed on it.  A caller that reaches for
    ``mesh_to_viewer_payload`` directly gets neither, and the result is
    two surfaces disagreeing about the same mesh — which is exactly what
    happened: ``stage_still`` photographs this very stage and built its
    own payload, so its stills showed the part parked in a corner over a
    bed the document had to invent.

    Pinning the door that was fixed would not have caught that one.  This
    pins the RULE, so the fourth caller fails here instead of shipping.
    """

    def test_only_local_stage_calls_the_encoder_directly(self):
        import pathlib

        src_root = pathlib.Path(__file__).parent.parent / "src" / "kiln"
        allowed = {"mesh_payload.py", "local_stage.py"}
        offenders = []
        for path in src_root.rglob("*.py"):
            if path.name in allowed:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "mesh_to_viewer_payload(" in text:
                offenders.append(path.relative_to(src_root).as_posix())

        assert not offenders, (
            "these modules build a stage payload without the plate or the "
            f"centring: {offenders}. Call local_stage._payload_for_mesh "
            "instead — it takes the same encoder kwargs."
        )

    def test_the_stills_renderer_uses_the_door(self):
        """The door the rule above was written for, named explicitly."""
        import pathlib

        still = (
            pathlib.Path(__file__).parent.parent
            / "src" / "kiln" / "stage_still.py"
        ).read_text(encoding="utf-8")

        assert "_payload_for_mesh(" in still
        assert "mesh_to_viewer_payload(" not in still
