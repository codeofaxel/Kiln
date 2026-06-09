"""Integration tests for the Pro+ pre-print-fix enrichment hook.

The free gate is unchanged; these assert the ADDITIVE contract of the
``enrichment`` key the gate now attaches:

  * a BLOCK carries an ``enrichment`` key (``None`` on free / kiln-pro absent,
    a dict on Pro+);
  * the enrichment NEVER changes the verdict's core block fields — the
    free-tier and Pro+ verdicts are byte-identical except for ``enrichment``
    (never-false-block is sacred);
  * a fault inside the kiln-pro enrichment can never make the gate block
    more — it degrades to ``enrichment=None`` and the block stands.

The Pro-path assertions ``importorskip`` kiln-pro, so this file passes
unchanged in a pure-public-Kiln CI (where enrichment is always ``None``).
"""

from __future__ import annotations

import pytest

from kiln.printers.print_gate import evaluate_pre_print_gate as G

trimesh = pytest.importorskip("trimesh")


def _box(size_mm: float, path: str) -> str:
    m = trimesh.creation.box(extents=[size_mm, size_mm, size_mm])
    m.apply_translation([size_mm / 2, size_mm / 2, size_mm / 2])
    m.export(path)
    return path


@pytest.fixture
def big(tmp_path) -> str:
    return _box(400.0, str(tmp_path / "big.stl"))  # exceeds every 256mm bed


@pytest.fixture
def small(tmp_path) -> str:
    return _box(100.0, str(tmp_path / "small.stl"))


def _core(verdict: dict) -> dict:
    """Verdict fields that the enrichment must never touch."""
    return {k: v for k, v in verdict.items() if k != "enrichment"}


# --- Contract that holds with or without kiln-pro --------------------------

def test_block_carries_enrichment_key(big):
    r = G(big, "bambu_a1")
    assert r["blocked"] is True
    assert "enrichment" in r  # present; None on free, dict on Pro+


def test_enrichment_does_not_change_core_block_fields(big):
    r = G(big, "bambu_a1")
    assert r["ok"] is False and r["blocked"] is True
    assert r["code"] == "EXCEEDS_BED"
    assert r["suggestions"], "free suggestions must survive"
    assert r["fit"] is not None
    assert "override_hint" in r


def test_pass_verdict_is_not_enriched(small):
    r = G(small, "bambu_a1")
    assert r["blocked"] is False
    # The pass path never attaches enrichment.
    assert r.get("enrichment") is None


def test_overridden_verdict_is_not_enriched(big):
    r = G(big, "bambu_a1", allow_oversize=True)
    assert r["blocked"] is False and r.get("overridden") is True
    assert r.get("enrichment") is None


# --- Pro-path: the never-false-block invariant (needs kiln-pro) ------------

def test_free_and_pro_verdict_cores_are_identical(big, monkeypatch):
    lic = pytest.importorskip("kiln_pro.enterprise.licensing")
    pro_gate = pytest.importorskip("kiln_pro.pro_gate")
    pytest.importorskip("kiln_pro.print_fix.engine")

    # PRO: a Pro+ caller-tier override always grants Pro (the override can
    # only RAISE above the local license, never lower it — which is exactly
    # how the license-less Fly server hands a paying caller their tier).
    tok = lic.set_caller_tier(lic.LicenseTier.PRO)
    try:
        pro = G(big, "bambu_a1", material_id="pla")
    finally:
        lic.reset_caller_tier(tok)

    # FREE: deny the Pro+ gate directly.  This is deterministic regardless of
    # whether the test box has a local license (the override can't downgrade
    # below it), and mirrors the Fly server denying a free caller.
    monkeypatch.setattr(pro_gate, "check_pro", lambda *a, **k: {"code": "TIER_REQUIRED"})
    free = G(big, "bambu_a1", material_id="pla")

    assert free["enrichment"] is None
    assert isinstance(pro["enrichment"], dict)
    assert "split_plan" in pro["enrichment"]
    # The whole point: enrichment is purely additive.
    assert _core(free) == _core(pro)


def test_pro_enrichment_has_envelope_and_split(big):
    lic = pytest.importorskip("kiln_pro.enterprise.licensing")
    pytest.importorskip("kiln_pro.print_fix.engine")

    tok = lic.set_caller_tier(lic.LicenseTier.PRO)
    try:
        r = G(big, "bambu_a1", material_id="pla")
    finally:
        lic.reset_caller_tier(tok)

    enr = r["enrichment"]
    assert enr["build_volume"]["rated_build_volume_mm"] == [256, 256, 256]
    assert enr["split_plan"]["all_fit"] is True


def test_material_block_enriched_with_swap(small):
    """polycarbonate (270C) on an Ender 3 (260C) — unmeltable -> swap fix."""
    lic = pytest.importorskip("kiln_pro.enterprise.licensing")
    pytest.importorskip("kiln_pro.print_fix.engine")

    tok = lic.set_caller_tier(lic.LicenseTier.PRO)
    try:
        r = G(small, "ender3", material_id="polycarbonate")
    finally:
        lic.reset_caller_tier(tok)

    assert r["blocked"] is True and r["code"] == "MATERIAL_EXCEEDS_HOTEND"
    assert r["enrichment"]["material_swap"]["available"] is True


# --- A fault in enrichment must NEVER make the gate block more -------------

def test_enrichment_fault_degrades_to_none(big, monkeypatch):
    eng = pytest.importorskip("kiln_pro.print_fix.engine")

    def boom(*a, **k):
        raise RuntimeError("enrichment exploded")

    monkeypatch.setattr(eng, "enrich_block", boom)
    r = G(big, "bambu_a1", material_id="pla")
    # Still a clean block — the fault was swallowed, not propagated.
    assert r["blocked"] is True
    assert r["code"] == "EXCEEDS_BED"
    assert r["enrichment"] is None


def test_non_import_error_at_engine_load_does_not_fail_the_gate_open(big, monkeypatch):
    """Regression (adversarial swarm BRK-1): a kiln-pro fault that raises a
    NON-ImportError at engine *import* time must degrade to enrichment=None,
    never propagate out of the gate.  If it did, start_print's broad
    ``except Exception`` would fail the never-false-block gate OPEN and let an
    impossible print through — the worst-case regression this whole feature
    must not introduce."""
    import sys

    pytest.importorskip("kiln_pro.print_fix.engine")

    class _PoisonEngine:
        # ``from kiln_pro.print_fix.engine import enrich_block`` does a getattr
        # on the module object found in sys.modules; a property that raises a
        # non-ImportError faithfully simulates a broken module-level init.
        @property
        def enrich_block(self):
            raise ValueError("engine module init failed (simulated)")

    monkeypatch.setitem(sys.modules, "kiln_pro.print_fix.engine", _PoisonEngine())

    # Must NOT raise, must still block the oversize part, enrichment None.
    r = G(big, "bambu_a1", material_id="pla")
    assert r["blocked"] is True
    assert r["code"] == "EXCEEDS_BED"
    assert r["enrichment"] is None

    # And _maybe_enrich_block itself honors its "never raises" docstring.
    from kiln.printers.print_gate import _maybe_enrich_block

    assert _maybe_enrich_block({"blocked": True, "code": "EXCEEDS_BED"}, printer_id="bambu_a1") is None
