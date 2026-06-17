#!/usr/bin/env python3
"""Moat-comment leak gate — public Kiln must not narrate the kiln-pro
overlay's internal strategy.

Public Kiln is open source.  A comment or docstring may say a field is
"exposed for the kiln-pro overlay" (a contract note) but must NOT
describe HOW the overlay reasons — its classifiers, the signals it
combines, the verdict logic it routes through.  That hands a competitor
the methodology of the private moat for free, even though the curated
*values* stay private.

This gate scans public-Kiln comments + docstrings for an overlay mention
co-occurring with strategy-narration language, and fails on anything not
consciously allowlisted.  The right fix for a flagged block is almost
always to TRIM the moat reasoning (keep the math + the "exposed for the
kiln-pro overlay" contract), not to allowlist it.

    python3 scripts/audit_moat_comment_leak.py        # exit 0 clean, 2 leak

Pairs with the wheel-exclusion + overlay-payload gates that protect the
curated values; this one protects the *method*.
"""

from __future__ import annotations

import re
import sys
import tokenize
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "kiln" / "src" / "kiln"

# A comment/docstring that names the private overlay surface.
_OVERLAY_MENTIONS = ("overlay", "kiln-pro", "kiln_pro")

# Language that describes HOW the overlay reasons — as opposed to a plain
# "exposed for the overlay" contract.  Co-occurrence with an overlay
# mention in the same comment/docstring block is the leak signal.  Keep
# this list tuned to *strategy narration*, not incidental vocabulary.
#
# NOTE: tuned to *moat-strategy vocabulary*, not generic words.  Broad
# tokens like "classifies" were removed — the firmware and public-tier
# classifiers legitimately "classify", so that word is not a leak signal.
# Extend this list when a NEW moat subsystem's narration vocabulary
# appears (the gate flags for review; trim the block or, rarely, allowlist).
_STRATEGY_TOKENS = (
    # lattice / strut topology routing (printability overlay)
    "strut classifier",
    "strut-specific",
    "strut semantics",
    "applying strut",
    "route lattice",
    "routes lattice",
    "lattice / scaffold",
    "lattice/scaffold",
    "lattice family",
    "lattice families",
    "lattice topology",
    "confirm lattice",
    "catches them",
    "fragment into",
    "secondary signal",
    "downgrade to advisory",
    "downgrades to advisory",
    "hole-too-small",
    # nozzle-wear attribution (device-intelligence overlay)
    "weights tip wear",
    "weights bore wear",
    "wear hypothesis",
    "bore is widening",
    "bore widening faster",
    "routes its per-component",
    "correlate flow",
    "gram-count wear",
    "wear-tracking subsystem",
)

# Consciously reviewed blocks that trip the detector but are CONTRACT /
# architecture notes (merge mechanism, public-floor fallback, upgrade
# nudge, bundle/resume wiring) — NOT narration of how the overlay reasons.
# Keyed by (filename, a stable marker phrase from the block).  Only add a
# block after confirming it describes the boundary, not the method.
_ALLOWLIST: tuple[tuple[str, str], ...] = (
    ("design_intelligence.py", "Single choke point: merge the kiln-pro engineering moat overlays"),
    ("design_intelligence.py", "Recommend a material using ONLY safety-floor fields"),
    ("design_intelligence.py", "Always attach the upgrade nudge when the load detector tripped"),
    ("design_versions.py", "Design version control for parametric designs"),
    ("original_design.py", "Run a harsh audit of an original design"),
    ("slicer_tools.py", "Attach Pro+ enrichment to an EXCEEDS_BED"),
    ("print_recovery.py", "Stamp gcode_path so kiln-pro's resume engine"),
)


def _allowlisted(path: Path, text: str) -> bool:
    name = path.name
    return any(fn in name and marker in text for fn, marker in _ALLOWLIST)


def _blocks(path: Path):
    """Yield ``(start_line, text)`` for each comment block and docstring."""
    try:
        with open(path, "rb") as fh:
            tokens = list(tokenize.tokenize(fh.readline))
    except (tokenize.TokenError, SyntaxError, ValueError):
        return

    block: list[str] = []
    block_line = 0
    prev_line = -2
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            # Break the block when comment lines aren't contiguous.
            if tok.start[0] != prev_line + 1 and block:
                yield block_line, "\n".join(block)
                block = []
            if not block:
                block_line = tok.start[0]
            block.append(tok.string)
            prev_line = tok.start[0]
        elif tok.type == tokenize.STRING:
            # Docstrings / prose strings carry the same risk as comments.
            if len(tok.string) > 40:
                yield tok.start[0], tok.string
    if block:
        yield block_line, "\n".join(block)


# Beyond the curated vocabulary, catch the GENERAL narration signature:
# the overlay as the *active subject* of a reasoning verb ("the overlay
# routes / reads / flags / detects / classifies / weights X"), or a
# purpose clause that exists to serve the overlay ("exposed so the overlay
# can …").  A plain contract ("exposed for the kiln-pro overlay",
# "consumed by the overlay") has the overlay as a passive recipient — no
# trailing reasoning verb — and does NOT trip these.  This is what makes
# the gate catch novel narration, not just the phrases we already trimmed.
_OVERLAY_ACTION = re.compile(
    r"(?:overlay|kiln-pro|kiln_pro)\b[^.\n]{0,60}?\b(?:"
    r"uses?|reads?|routes?|flags?|detects?|classif\w+|confirms?|applies|"
    r"apply|weights?|correlat\w+|cross-references?|distinguish\w*|catches|"
    r"enrich\w+|infers?|derives?|downgrade\w*"
    r")\b",
    re.IGNORECASE,
)
_OVERLAY_PURPOSE = re.compile(
    r"\b(?:so (?:the |that )?)[^.\n]{0,50}?(?:overlay|kiln-pro|kiln_pro)\b"
    r"[^.\n]{0,40}?\b(?:can|to|will)\b",
    re.IGNORECASE,
)


def _is_leak(text: str, *, broad: bool = False) -> bool:
    low = text.lower()
    if not any(m in low for m in _OVERLAY_MENTIONS):
        return False
    if any(tok in low for tok in _STRATEGY_TOKENS):
        return True
    # The broad subject-verb heuristic ("the overlay routes/reads/flags X")
    # catches NOVEL narration, but it also trips legitimate seam / contract /
    # marketed-feature notes — so it is advisory-only (``--sweep``), never
    # part of the hard CI gate.  Run it periodically and review by hand.
    if broad:
        return bool(_OVERLAY_ACTION.search(text) or _OVERLAY_PURPOSE.search(text))
    return False


def main() -> int:
    broad = "--sweep" in sys.argv
    leaks: list[tuple[Path, int, str]] = []
    for path in sorted(_SRC.rglob("*.py")):
        for line, text in _blocks(path):
            if _is_leak(text, broad=broad) and not _allowlisted(path, text):
                leaks.append((path, line, text))

    if not leaks:
        print("Moat-comment audit: clean." + (" (--sweep)" if broad else ""))
        return 0

    root = _SRC.parent.parent.parent
    print(
        "MOAT-COMMENT SWEEP (advisory) — review each by hand:"
        if broad
        else "MOAT-COMMENT LEAK — public Kiln narrates the kiln-pro overlay's strategy:"
    )
    for path, line, text in leaks:
        print(f"\n  {path.relative_to(root)}:{line}")
        for ln in text.splitlines()[:6]:
            print(f"    {ln.strip()}")

    if broad:
        print(
            f"\n{len(leaks)} block(s) flagged by the broad heuristic — most are "
            "legitimate contract / seam / marketed-feature notes.  Trim only the "
            "ones that narrate HOW the overlay reasons, then add the new vocabulary "
            "to _STRATEGY_TOKENS so the hard gate catches it next time."
        )
        return 0  # advisory — never fails the build

    print(
        f"\n{len(leaks)} block(s).  Trim the moat reasoning (keep the math + the "
        "'exposed for the kiln-pro overlay' contract), or — if genuinely a contract "
        "note — add a (filename, marker) entry to _ALLOWLIST."
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
