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

For printers, the catalogue's own names are spellings too: every row's
``display_name`` and every model it lists after a slash ("Prusa MK4 / MK4S"
lists the MK4S) name that row, read from the catalogue rather than typed
here, so a printer added tomorrow is found by its name the day it lands.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Vendor words a caller prefixes to a printer id, and the form the catalog
# keys write that vendor in: none for Creality ("creality_k1c" → "k1c"), a
# short one for Bambu Lab and Prusa ("bambu_lab_a1" → "bambu_a1",
# "original_prusa_mk4" → "prusa_mk4").  The vendor-less remainder is tried
# too, as it always was.  Keys that already carry the catalog's form match
# exactly before any of this is consulted.
_PRINTER_VENDOR_FORMS: tuple[tuple[str, str], ...] = (
    ("creality_", ""),
    ("bambulab_", "bambu_"),
    ("bambu_lab_", "bambu_"),
    ("original_prusa_", "prusa_"),
    ("prusa_research_", "prusa_"),
)

#: Catalogue rows that name no one printer: never the answer to a name.
_NOT_A_PRINTER_NAME = frozenset({"default", "klipper_generic"})


def _slug(value: str) -> str:
    """Lowercase, ``+`` → ``_plus``, any run of non-alphanumerics → ``_``."""
    s = value.strip().lower().replace("+", "_plus")
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def _tokens(slug: str) -> frozenset[str]:
    return frozenset(t for t in slug.split("_") if t)


#: Short names for materials the catalog spells out in full.  The same
#: material, not a family: "PC" is polycarbonate, "PA" is polyamide, which
#: the catalog calls nylon.  These are the spellings a printer's own unit
#: and its maker's slicer use ("PA-CF", "PC"), so without them a spool the
#: printer reports was a material Kiln had never heard of.
_MATERIAL_ABBREVIATIONS: dict[str, str] = {
    "pa": "nylon",
    "pa6": "nylon",
    "pa12": "nylon",
    "pc": "polycarbonate",
}


def resolve_material_key(material_id: str, keys: Iterable[str]) -> str | None:
    """The catalog key ``material_id`` spells, or ``None``.

    Exact (case-insensitive) first; then the slug ("PLA+" → ``pla_plus``,
    "petg-cf" → ``petg_cf``); then the same tokens in any order ("pla_cf" →
    ``cf_pla``); then the same tokens with an abbreviation written out
    ("PA-CF" → ``cf_nylon``, "PC" → ``polycarbonate``) -- after the plain
    tokens, so a key that uses the short name itself (``pc_abs``, ``pa6_gf``)
    still matches as spelled.  Never a family fallback.
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
    spelled_out = frozenset(_MATERIAL_ABBREVIATIONS.get(t, t) for t in want)
    for tokens in (want, spelled_out) if want != spelled_out else (want,):
        if not tokens:
            continue
        for key in catalog:
            if _tokens(key) == tokens:
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


def _compact(slug: str) -> str:
    return re.sub(r"[^a-z0-9]", "", slug)


def listed_names(display_name: str) -> list[str]:
    """Every printer a catalogue display name lists, each written out whole.

    ``"Prusa MK4 / MK4S"`` → ``["Prusa MK4", "Prusa MK4S"]``;
    ``"Creality Ender 3 / Ender 3 Pro"`` → ``[..., "Creality Ender 3 Pro"]``;
    ``"Elegoo Neptune 3 / 3 Pro / 3 Plus"`` → ``[..., "Elegoo Neptune 3 Pro",
    "Elegoo Neptune 3 Plus"]``.  A name after a slash takes the words of the
    first name that come before its own first word when that word appears
    there, else every word of the first name but its last.
    """
    parts = [p.strip() for p in str(display_name or "").split("/") if p.strip()]
    if not parts:
        return []
    first = parts[0].split()
    names = [parts[0]]
    for later in parts[1:]:
        words = later.split()
        lead = next(
            (first[:i] for i, w in enumerate(first) if w.lower() == words[0].lower()),
            first[:-1],
        )
        names.append(" ".join([*lead, *words]))
    return names


_names_cache: tuple[int, dict[str, str | None]] | None = None


def _catalogue_names() -> dict[str, str | None]:
    """``{compact name: catalogue key}`` for every name the catalogue gives
    its printers.  A name two rows share maps to ``None``: it names neither,
    and a resolver that picked one would be guessing.  A name that is itself
    another row's key belongs to that row.  Read from the catalogue the
    bed-fit and motion doors read, so the two cannot disagree about which
    printers exist.  Empty when the catalogue cannot be read.
    """
    global _names_cache  # noqa: PLW0603
    try:
        from kiln.printers.bed_fit import _load_printer_intelligence

        catalogue = _load_printer_intelligence() or {}
    except Exception:  # noqa: BLE001 -- no catalogue, no names; spelling rules still stand
        return {}
    if _names_cache is not None and _names_cache[0] == id(catalogue):
        return _names_cache[1]
    rows = {k: v for k, v in catalogue.items() if not k.startswith("_") and k not in _NOT_A_PRINTER_NAME}
    keys_compact = {_compact(k): k for k in rows}
    names: dict[str, str | None] = {}
    for key, row in rows.items():
        display = row.get("display_name") if isinstance(row, dict) else None
        for name in listed_names(display or ""):
            compact = _compact(_slug(name))
            if not compact or keys_compact.get(compact, key) != key:
                continue
            names[compact] = key if names.get(compact, key) == key else None
    _names_cache = (id(catalogue), names)
    return names


def printer_key_candidates(printer_id: str) -> list[str]:
    """Spellings to try for a printer id, most specific first.

    The normalised form; then with a vendor prefix written the catalog's
    way, and stripped; then the catalogue row whose own name it is ("Bambu
    Lab X1 Carbon" → ``bambu_x1c``, "Prusa MK4S" → ``prusa_mk4``).  The
    resolver also compares each with its separators removed ("ender 3 v3
    ke" → ``ender3v3ke``) so a key spelled ``ender3_v3_ke`` still matches.
    """
    if not printer_id:
        return []
    slug = _slug(printer_id)
    out: list[str] = [slug]
    for prefix, form in _PRINTER_VENDOR_FORMS:
        if slug.startswith(prefix):
            rest = slug.removeprefix(prefix)
            if form:
                out.append(form + rest)
            out.append(rest)
    names = _catalogue_names()
    for candidate in list(out):
        named = names.get(_compact(candidate))
        if named:
            out.append(named)
            break
    ordered: list[str] = []
    for candidate in out:
        if candidate and candidate not in ordered:
            ordered.append(candidate)
    return ordered


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
