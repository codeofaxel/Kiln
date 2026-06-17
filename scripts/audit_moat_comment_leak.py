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

import hashlib
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
)

# Consciously reviewed blocks that mention the overlay AND trip a strategy
# token but do NOT leak methodology.  Keyed by sha1 of the normalized
# block text → reason.  Keep SMALL — trimming beats allowlisting.
_ALLOWLIST: dict[str, str] = {}


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _fingerprint(text: str) -> str:
    return hashlib.sha1(_norm(text).encode()).hexdigest()


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


def _is_leak(text: str) -> bool:
    low = text.lower()
    if not any(m in low for m in _OVERLAY_MENTIONS):
        return False
    return any(tok in low for tok in _STRATEGY_TOKENS)


def main() -> int:
    leaks: list[tuple[Path, int, str]] = []
    for path in sorted(_SRC.rglob("*.py")):
        for line, text in _blocks(path):
            if _is_leak(text) and _fingerprint(text) not in _ALLOWLIST:
                leaks.append((path, line, text))

    if not leaks:
        print("Moat-comment audit: clean.")
        return 0

    root = _SRC.parent.parent.parent
    print("MOAT-COMMENT LEAK — public Kiln narrates the kiln-pro overlay's strategy:")
    for path, line, text in leaks:
        print(f"\n  {path.relative_to(root)}:{line}")
        for ln in text.splitlines()[:6]:
            print(f"    {ln.strip()}")
    print(
        f"\n{len(leaks)} block(s).  Trim the moat reasoning (keep the math + the "
        "'exposed for the kiln-pro overlay' contract).  If a block is genuinely "
        "a contract note, add its fingerprint to _ALLOWLIST with a reason."
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
