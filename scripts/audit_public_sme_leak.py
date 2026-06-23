#!/usr/bin/env python3
"""Public-source SME-table leak gate — public Kiln must not ship a curated
cross-vendor capability / SME table.

The kiln-pro moat is curated intelligence: per-vendor, per-model behaviour
compiled from many sources over a long time.  Three gates already protect
it — the wheel-exclusion and design-knowledge-split gates guard *where a
file lives* (user disk / public repo), and ``audit_moat_comment_leak.py``
guards the *method* narrated in prose.  They all miss one shape: a curated
DATA TABLE — a registry keyed by printer brand / firmware carrying
``limitations``, ``recovery_methods``, capability matrices, ``failure_modes``,
``design_rules`` — sitting in PUBLIC source as a ``.py`` module or ``.json``
file.

On 2026-06-23 a 459-line cross-vendor power-loss capability registry
(``resume_capabilities.py``) was found doing exactly this: world-visible,
consumed by no live code path, a free head-start for any competitor trying
to compile the same multi-vendor SME and catch up.  It fell in the blind
spot between "code" (so the data gates ignored it) and "prose" (so the
comment gate ignored it).

This gate closes that blind spot.  It flags any public-source file carrying
the signature of a compiled cross-vendor SME table — several distinct
printer-vendor names AND multiple curated-intelligence data fields — unless
the file is consciously allowlisted as a non-moat floor.  The right fix for
a flagged file is to MOVE the table to the private tier (kiln-pro) or a pro
overlay, never to allowlist it.

    python3 scripts/audit_public_sme_leak.py          # exit 0 clean, 2 leak
    python3 scripts/audit_public_sme_leak.py --json   # CI / machine format

Pairs with ``audit_moat_comment_leak.py`` (protects the method in prose) and
the wheel / overlay gates (protect the curated values); this one protects
against compiled SME *data tables* shipping in public source.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "kiln" / "src" / "kiln"

# Printer vendors / firmwares / adapters.  A compiled cross-vendor table
# names many of these; ordinary code that happens to dispatch on printer
# type names them too, which is why the vendor count ALONE is not the
# signal — it must co-occur with curated data fields (below).
_VENDORS: tuple[str, ...] = (
    "bambu", "bambulab", "prusa", "prusalink", "klipper", "moonraker",
    "octoprint", "marlin", "elegoo", "creality", "anycubic", "sovol",
    "voron", "ratrig", "flsun", "qidi", "raise3d", "ultimaker", "snapmaker",
)

# Curated-intelligence DATA fields — the hallmark of a compiled SME table.
# Matched ONLY as an assignment (``field=``) or a mapping key
# (``"field":``), never as an incidental word in prose, so a dispatcher
# that merely branches on printer type does not trip the gate.  Extend this
# list whenever a new curated-field type is introduced (it is the same
# moat-field vocabulary the design-knowledge-split gate uses).
_SME_FIELDS: tuple[str, ...] = (
    "limitations",
    "recovery_methods",
    "failure_modes",
    "capabilities",
    "agent_guidance",
    "design_rules",
    "related_patterns",
    "holding_torque_n_m",
    "cycle_count_estimates",
    "insert_table",
    "ip_rating_guidance",
    "risk_thresholds",
    "score_deductions",
    "recommendation_rules",
    "geometry_score_rules",
    "captured_ball_variant",
    "latch_variants",
    "anti_walkout_lock_options",
    "pin_retention_variants",
    "supports_firmware_recovery",
    "supports_layer_resume",
    "supports_z_offset_resume",
)

_VENDOR_MIN = 3   # distinct vendor names → looks like a cross-vendor table
_FIELD_MIN = 2    # distinct curated data fields → looks like an SME table

# Structural exoneration: a file that routes through the design-knowledge
# split — it declares the moat fields as empty-by-default dataclass fields
# and pulls the curated VALUES from the private kiln-pro overlay at runtime
# (``_merge_pro_overlay_if_available`` / ``load_overlay``).  By construction
# such a file holds the public safety-floor only; the moat lives in the
# overlay.  This is the SANCTIONED pattern (``design_intelligence.py``,
# ``printer_intelligence.py``) and the opposite of a standalone hardcoded
# table like the ``resume_capabilities.py`` leak, which had no overlay merge.
_OVERLAY_LOADER_MARKERS: tuple[str, ...] = (
    "_merge_pro_overlay_if_available",
    "load_overlay(",
)

# Reviewed public-safe files: (relative-path substring, reason).  A file
# here carries the signature but is genuinely NOT moat — a datasheet floor,
# schema boilerplate, or the tool-manifest mirror.  Adding an entry is a
# conscious moat decision, reviewed the same way the other leak-gate
# allowlists are.  Default to MOVING the table private instead.
_ALLOWLIST: tuple[tuple[str, str], ...] = ()


def _iter_sources():
    """Yield every public-source ``.py`` / ``.json`` file under kiln/src."""
    for ext in ("*.py", "*.json"):
        yield from _SRC.rglob(ext)


def _allowlisted(rel: str) -> bool:
    return any(sub in rel for sub, _reason in _ALLOWLIST)


def _is_overlay_loader(text: str) -> bool:
    """True when the file routes its moat fields through the pro-overlay
    split — a sanctioned loader holding only the public floor."""
    return any(m in text for m in _OVERLAY_LOADER_MARKERS)


def _distinct_vendors(text: str) -> set[str]:
    lowered = text.lower()
    return {v for v in _VENDORS if re.search(rf"\b{re.escape(v)}\b", lowered)}


def _distinct_fields(text: str) -> set[str]:
    """Curated fields present as an assignment or a mapping key."""
    found: set[str] = set()
    for f in _SME_FIELDS:
        # ``field =`` (python) or ``"field":`` / ``'field':`` (json/dict)
        if re.search(rf"(^|[^.\w]){re.escape(f)}\s*=", text) or re.search(
            rf"""["']{re.escape(f)}["']\s*:""", text
        ):
            found.add(f)
    return found


def scan() -> list[dict]:
    """Return one record per flagged file (compiled-SME-table signature)."""
    findings: list[dict] = []
    for path in sorted(_iter_sources()):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        vendors = _distinct_vendors(text)
        if len(vendors) < _VENDOR_MIN:
            continue
        fields = _distinct_fields(text)
        if len(fields) < _FIELD_MIN:
            continue
        rel = str(path.relative_to(_SRC.parent.parent))
        findings.append(
            {
                "file": rel,
                "vendors": sorted(vendors),
                "fields": sorted(fields),
                "overlay_loader": _is_overlay_loader(text),
                "allowlisted": _allowlisted(rel),
            }
        )
    return findings


def _is_leak(finding: dict) -> bool:
    return not finding["overlay_loader"] and not finding["allowlisted"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="machine output")
    args = parser.parse_args(argv)

    findings = scan()
    leaks = [f for f in findings if _is_leak(f)]
    exonerated = [f for f in findings if not _is_leak(f)]

    if args.json:
        print(json.dumps({"leaks": leaks, "all": findings}, indent=2))
        return 2 if leaks else 0

    if not leaks:
        print(
            "public SME-table leak gate: clean — no compiled cross-vendor "
            f"SME tables in public source ({len(exonerated)} exonerated as "
            "overlay-split loaders / allowlisted)."
        )
        return 0

    print("PUBLIC SME-TABLE LEAK — compiled cross-vendor curated data in "
          "public source:\n")
    for f in leaks:
        print(f"  {f['file']}")
        print(f"      vendors ({len(f['vendors'])}): {', '.join(f['vendors'])}")
        print(f"      fields  ({len(f['fields'])}): {', '.join(f['fields'])}")
    print(
        "\nFix: MOVE the table to the private tier (kiln-pro) or a pro "
        "overlay — do not allowlist it.  A curated cross-vendor capability / "
        "SME table is moat; public source must not ship it.  If the file is "
        "genuinely a non-moat floor (datasheet, schema, manifest mirror), add "
        "a reviewed (path, reason) entry to _ALLOWLIST."
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
