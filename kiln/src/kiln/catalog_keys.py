"""Resolving a caller's spelling of a material or printer to a catalog key.

Every curated table in ``design_intelligence`` is keyed by a short id
(``pla_plus``, ``cf_pla``, ``k1c``) and every door looked it up with a bare
``.lower()``: "PLA+", "pla-cf" and "Creality K1C" all missed, and a miss
either refused with no hint (troubleshoot_print_issue, 2026-08-19) or fell
through to the ``default`` printer profile and answered confidently for the
wrong machine (check_printer_material_support on a K1C).

One resolver, every door.  It matches SPELLING — case, separators, ``+``,
token order, a vendor prefix — and never substitutes a family: "Hyper PLA"
is not ``pla`` to an engineering table, so it returns ``None`` plus the
nearest ids for the door's error message.  A door that wants a family
fallback says so in its own words.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Vendor words a caller prefixes to a printer id that the catalog keys omit
# ("creality_k1c" → "k1c").  Keys that DO carry the vendor ("bambu_a1",
# "prusa_mk4", "elegoo_neptune4") match exactly before this is consulted.
_PRINTER_VENDOR_PREFIXES: tuple[str, ...] = (
    "creality_",
    "bambulab_",
    "bambu_lab_",
    "prusa_research_",
)


def _slug(value: str) -> str:
    """Lowercase, ``+`` → ``_plus``, any run of non-alphanumerics → ``_``."""
    s = value.strip().lower().replace("+", "_plus")
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def _tokens(slug: str) -> frozenset[str]:
    return frozenset(t for t in slug.split("_") if t)


def resolve_material_key(material_id: str, keys: Iterable[str]) -> str | None:
    """The catalog key ``material_id`` spells, or ``None``.

    Exact (case-insensitive) first; then the slug ("PLA+" → ``pla_plus``,
    "petg-cf" → ``petg_cf``); then the same tokens in any order ("pla_cf" →
    ``cf_pla``).  Never a family fallback.
    """
    if not material_id:
        return None
    catalog = list(keys)
    lower = material_id.strip().lower()
    if lower in catalog:
        return lower
    slug = _slug(material_id)
    if slug in catalog:
        return slug
    want = _tokens(slug)
    if want:
        for key in catalog:
            if _tokens(key) == want:
                return key
    return None


def suggest_keys(spelling: str, keys: Iterable[str], limit: int = 5) -> list[str]:
    """Catalog keys sharing a token with ``spelling``, nearest first.

    For the error message when a resolver returns ``None`` — "Hyper PLA"
    suggests ``pla``, ``pla_plus``, ``pla_tough``; "creality_hi" suggests
    ``creality_hi``'s vendor siblings.
    """
    want = _tokens(_slug(spelling or ""))
    if not want:
        return []
    scored: list[tuple[int, int, str]] = []
    for key in keys:
        have = _tokens(key)
        shared = len(want & have)
        if shared:
            scored.append((-shared, len(have), key))
    return [k for _, _, k in sorted(scored)[:limit]]


def suggest_material_keys(material_id: str, keys: Iterable[str], limit: int = 5) -> list[str]:
    """:func:`suggest_keys`, named for the material doors that call it."""
    return suggest_keys(material_id, keys, limit)


def printer_key_candidates(printer_id: str) -> list[str]:
    """Spellings to try for a printer id, most specific first.

    The normalised form, then with a vendor prefix stripped, then each
    with the separators removed ("ender 3 v3 ke" → ``ender3v3ke``) so a
    key spelled ``ender3_v3_ke`` still matches.
    """
    if not printer_id:
        return []
    slug = _slug(printer_id)
    out: list[str] = [slug]
    for prefix in _PRINTER_VENDOR_PREFIXES:
        if slug.startswith(prefix):
            out.append(slug.removeprefix(prefix))
    return out


def resolve_printer_key(printer_id: str, keys: Iterable[str]) -> str | None:
    """The catalog key ``printer_id`` spells, or ``None`` — never ``default``."""
    catalog = list(keys)
    candidates = printer_key_candidates(printer_id)
    for candidate in candidates:
        if candidate in catalog:
            return candidate
    compact = {re.sub(r"[^a-z0-9]", "", k): k for k in catalog}
    for candidate in candidates:
        hit = compact.get(re.sub(r"[^a-z0-9]", "", candidate))
        if hit is not None:
            return hit
    return None
