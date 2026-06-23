"""CI backstop for the public SME-table leak gate.

Public Kiln must not ship a curated cross-vendor capability / SME table —
a registry keyed by printer brand carrying limitations, recovery_methods,
capability matrices, failure_modes.  That is moat; it belongs in the
private tier (or a pro overlay), never in public source.  A green run here
is the proof we didn't ship one.  See ``scripts/audit_public_sme_leak.py``.

The 2026-06-23 incident: a 459-line ``resume_capabilities.py`` cross-vendor
power-loss table, consumed by no live code, sat world-visible for months —
between the data gates ("where files live") and the comment gate ("the
method in prose").  The regression tests below prove this gate catches that
exact shape, and exonerates the sanctioned overlay-split loaders.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "audit_public_sme_leak.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("audit_public_sme_leak", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["audit_public_sme_leak"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_no_public_sme_leak() -> None:
    """The whole of public source is clean (or exonerated) — the backstop."""
    assert _SCRIPT.exists(), f"gate script missing: {_SCRIPT}"
    result = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "Public SME-table leak — a curated cross-vendor capability/SME table "
        "is in public source. Move it to the private tier (kiln-pro) or a pro "
        "overlay; do not allowlist it:\n\n" + result.stdout
    )


# --- regression: the gate must catch the shape that slipped through -------

# A standalone hardcoded cross-vendor capability table, NO overlay merge —
# the resume_capabilities.py shape.
_LEAK_SHAPE = '''
REGISTRY = {
    "bambu": dict(recovery_methods=["builtin"], limitations=["MQTT only"]),
    "prusa": dict(recovery_methods=["m413"], limitations=["MINI has none"]),
    "klipper": dict(recovery_methods=["probe"], limitations=["no native PLR"]),
    "elegoo": dict(recovery_methods=["sdcp"], limitations=["cancel+restart"]),
}
'''

# Same field/vendor density but routed through the design-knowledge split —
# the design_intelligence.py / printer_intelligence.py shape (safe): the
# public file holds an empty floor and pulls the curated values from the
# private overlay at runtime.
_LOADER_SHAPE = '''
# Handles bambu / prusa / klipper / elegoo printer intelligence.
_PUBLIC_FLOOR = {
    "capabilities": {},
    "failure_modes": [],
}

def load(printer):
    raw = dict(_PUBLIC_FLOOR)
    return _merge_pro_overlay_if_available(raw)
'''


def test_detector_catches_standalone_cross_vendor_table() -> None:
    gate = _load_gate()
    assert len(gate._distinct_vendors(_LEAK_SHAPE)) >= gate._VENDOR_MIN
    assert len(gate._distinct_fields(_LEAK_SHAPE)) >= gate._FIELD_MIN
    assert gate._is_overlay_loader(_LEAK_SHAPE) is False
    finding = {
        "overlay_loader": gate._is_overlay_loader(_LEAK_SHAPE),
        "allowlisted": False,
    }
    assert gate._is_leak(finding) is True, "must flag a standalone SME table"


def test_detector_exonerates_overlay_split_loader() -> None:
    gate = _load_gate()
    # Still trips vendor + field density...
    assert len(gate._distinct_vendors(_LOADER_SHAPE)) >= gate._VENDOR_MIN
    assert len(gate._distinct_fields(_LOADER_SHAPE)) >= gate._FIELD_MIN
    # ...but the overlay-split marker exonerates it (values live private).
    assert gate._is_overlay_loader(_LOADER_SHAPE) is True
    finding = {
        "overlay_loader": gate._is_overlay_loader(_LOADER_SHAPE),
        "allowlisted": False,
    }
    assert gate._is_leak(finding) is False, "loaders must not be flagged"


def test_field_match_ignores_prose() -> None:
    """A field name in prose (not an assignment/key) must not count."""
    gate = _load_gate()
    prose = (
        "This bambu / prusa / klipper helper documents the limitations and "
        "recovery_methods of each printer in words, with no data structure."
    )
    # Vendors present, but no `field=` / `\"field\":` → not an SME table.
    assert len(gate._distinct_fields(prose)) == 0
