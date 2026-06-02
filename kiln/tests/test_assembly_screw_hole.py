"""Tests for the kiln-pro screw-hole wire-up in ``kiln.assembly``.

When a joint drives a screw INTO a printed part AND kiln-pro is
installed, ``validate_joint`` attaches a ``screw_hole`` block
(compensated diameter, engagement, torque, chamfer) plus a factual
recommendation.  Free tier (kiln-pro not installed) degrades silently to
``screw_hole=None`` and the historic behaviour is unchanged.

The happy-path tests stub the lazy pro lookup
(``kiln.assembly._screw_hole_detail_for_joint``) so they run with or
without kiln-pro installed; one test exercises the REAL helper to prove
the ImportError fallback is genuine.
"""

from __future__ import annotations

from kiln import assembly as assembly_mod
from kiln.assembly import (
    Assembly,
    AssemblyPart,
    FastenerSpec,
    MatingInterface,
    validate_assembly,
    validate_joint,
)

_CALIBRATED = {
    "fastener_size": "M3",
    "tier": "bambu_calibrated",
    "hole_diameter_horizontal_mm": 2.9,
    "hole_diameter_vertical_mm": 2.72,
    "engagement_length_mm": 5.0,
    "install_torque_nm": 0.25,
    "lead_in_chamfer_deg": 45,
}


def _parts() -> list[AssemblyPart]:
    return [
        AssemblyPart(part_id="base", file_path="/tmp/base.stl", material="PLA"),
        AssemblyPart(part_id="lid", file_path="/tmp/lid.stl", material="PLA"),
    ]


def _iface(surface: str = "printed") -> MatingInterface:
    return MatingInterface(
        part_a_id="base", part_b_id="lid", joint_type="clearance_fit",
        clearance_mm=2.0,
        fastener_spec=FastenerSpec(
            size="M3", family="metric_machine_screw",
            surface_type=surface, quantity_per_interface=4,
        ),
    )


def test_screw_hole_attached_when_pro_available(monkeypatch):
    monkeypatch.setattr(
        assembly_mod, "_screw_hole_detail_for_joint",
        lambda spec, material, printer_id: dict(_CALIBRATED),
    )
    jv = validate_joint(_iface(), _parts(), printer_id="bambu_a1")
    assert jv.screw_hole is not None
    assert jv.screw_hole["hole_diameter_horizontal_mm"] == 2.9
    assert "compensated_screw_hole" in jv.design_rules_checked
    assert any("self-threading hole" in r for r in jv.recommendations)
    assert jv.to_dict()["screw_hole"]["engagement_length_mm"] == 5.0


def test_no_screw_hole_when_helper_returns_none(monkeypatch):
    monkeypatch.setattr(
        assembly_mod, "_screw_hole_detail_for_joint",
        lambda spec, material, printer_id: None,
    )
    jv = validate_joint(_iface(), _parts(), printer_id="bambu_a1")
    assert jv.screw_hole is None
    assert "compensated_screw_hole" not in jv.design_rules_checked
    # to_dict still serialises the field as None.
    assert jv.to_dict()["screw_hole"] is None


def test_real_helper_degrades_to_none_without_pro_resolver():
    # Genuine graceful path: kiln-pro's resolver isn't importable here
    # until kiln-pro ships it, so the helper catches ImportError -> None.
    spec = FastenerSpec(
        size="M3", family="metric_machine_screw", surface_type="printed",
    )
    assert assembly_mod._screw_hole_detail_for_joint(spec, "PLA", "bambu_a1") is None


def test_no_fastener_spec_no_screw_hole():
    assert assembly_mod._screw_hole_detail_for_joint(None, "PLA", "bambu_a1") is None
    iface = MatingInterface(
        part_a_id="base", part_b_id="lid", joint_type="snap_fit", clearance_mm=0.15,
    )
    jv = validate_joint(iface, _parts())
    assert jv.screw_hole is None


def test_validate_assembly_threads_printer_id(monkeypatch):
    monkeypatch.setattr(
        assembly_mod, "_screw_hole_detail_for_joint",
        lambda spec, material, printer_id: dict(_CALIBRATED) if printer_id else None,
    )
    # Skip the geometry clearance pass (needs real meshes on disk) — this
    # test is about printer_id reaching validate_joint, not clearance math.
    def _noop_clearances(asm, **_kw):
        asm.clearance_checks = []
        return []
    monkeypatch.setattr(assembly_mod, "check_all_clearances", _noop_clearances)

    asm = Assembly(assembly_id="box", name="Box")
    asm.parts.extend(_parts())
    asm.interfaces.append(_iface())
    validated = validate_assembly(asm, printer_id="bambu_a1")
    assert validated.joint_validations[0].screw_hole is not None

    asm2 = Assembly(assembly_id="box2", name="Box2")
    asm2.parts.extend(_parts())
    asm2.interfaces.append(_iface())
    validated2 = validate_assembly(asm2)  # no printer_id -> stub returns None
    assert validated2.joint_validations[0].screw_hole is None


def test_recommendation_factual_calibrated_and_generic():
    cal = assembly_mod._screw_hole_recommendation(_CALIBRATED)
    assert "2.9 mm horizontal" in cal and "0.25" in cal and "calibrated" in cal
    gen = assembly_mod._screw_hole_recommendation({
        "fastener_size": "M3", "tier": "generic_methodology",
        "hole_diameter_horizontal_mm": 2.7, "lead_in_chamfer_deg": 45,
    })
    assert "~2.7 mm" in gen and "verify on a test print" in gen
