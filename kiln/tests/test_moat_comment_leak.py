"""CI backstop for the moat-comment leak gate.

Public Kiln must not, in comments / docstrings OR shipped data JSON:
- narrate HOW the kiln-pro overlay reasons (strategy),
- name the overlay's research PROVENANCE (vendor datasheets),
- brand the overlay "the moat",
- name a private overlay file PATH.

A green run is the proof we didn't ship the method.  The detection tests below
also prove the gate CAN fail (a gate that can't fail is theatre) and that it
stays precise (no false positives on brand examples / public standards /
plain contract notes).  See ``scripts/audit_moat_comment_leak.py``.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


def _load_gate():
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "audit_moat_comment_leak", root / "scripts" / "audit_moat_comment_leak.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


_GATE = _load_gate()


# ── The live gate stays green (the in-suite backstop) ───────────────────────

def test_no_moat_comment_leak() -> None:
    root = Path(__file__).resolve().parents[2]
    script = root / "scripts" / "audit_moat_comment_leak.py"
    assert script.exists(), f"gate script missing: {script}"
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    assert result.returncode == 0, (
        "Moat-comment leak — public Kiln narrates the kiln-pro overlay's "
        "strategy / provenance / moat-label / path:\n\n" + result.stdout
    )


# ── The gate CATCHES each leak class (proves it can fail) ───────────────────

def test_catches_datasheet_provenance() -> None:
    assert _GATE._is_leak("# the kiln-pro overlay values are datasheet-grounded")


def test_catches_moat_self_label() -> None:
    assert _GATE._is_leak("# the kiln-pro engineering-moat overlay")
    assert _GATE._is_leak("# Curated content is the engineering moat in kiln-pro")


def test_catches_private_overlay_path() -> None:
    assert _GATE._is_leak("# tuned by kiln_pro/data/foo_pro_overlay.json")


def test_json_catches_moat_label() -> None:
    assert _GATE._is_json_leak('"methodology": "these values are the engineering moat"')


def test_json_catches_datasheet_provenance() -> None:
    assert _GATE._is_json_leak('"note": "datasheet-grounded per-material values"')


# ── The gate does NOT flag legitimate notes (precision, no false positives) ──

def test_allows_plain_overlay_contract() -> None:
    assert not _GATE._is_leak("# exposed for the kiln-pro overlay")
    assert not _GATE._is_leak("# the Pro overlay supplies curated per-material values")


def test_allows_material_brand_example() -> None:
    # A brand used as a MATERIAL example is not provenance (the false positive
    # that was removed) — only "datasheet-grounded" phrasing is.
    assert not _GATE._is_leak("# the kiln-pro overlay tunes Polymaker PETG on brass")


def test_allows_public_standard_reference() -> None:
    # Public STANDARDS (ASTM / ISO) are textbook, not datasheet provenance.
    assert not _GATE._is_leak("# the kiln-pro overlay values follow ASTM D638")


def test_json_allows_clean_line() -> None:
    assert not _GATE._is_json_leak('"description": "A drink coaster"')
