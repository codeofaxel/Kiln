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

One typo is forgiven, and only where it cannot pick a different printer: a
name written with its maker, one letter off (added, dropped, changed, or two
neighbours swapped) inside a word of four or more letters of one of that
maker's rows, and close to no other row ("Elegoo Centuri Carbon 2" is the
Centauri Carbon 2).  A changed digit or a short model code ("5X" for "5M",
"K1D" for "K1C") is a different printer, never a typo.
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
    ("bambu_", "bambu_"),
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


#: A typo is forgiven only inside a word at least this long: in a shorter
#: one ("Pro", "5M", "K1C") one letter is the model, not a slip.
_TYPO_MIN_WORD_LEN = 4


class _TypoIndex:
    """What the one-typo rule reads: each maker's rows, and their names.

    ``makers`` maps the compact forms a maker is written in ("bambulab",
    "bambu", "originalprusa") to the maker as the catalogue names it;
    ``rows`` holds ``(key, maker, names)`` where each name is the model's
    words after the maker, split into letter and digit runs.
    """

    def __init__(self) -> None:
        self.makers: dict[str, str | None] = {}
        self.rows: list[tuple[str, str, list[tuple[str, ...]]]] = []
        self.keys: frozenset[str] = frozenset()


_names_cache: tuple[int, dict[str, str | None], _TypoIndex] | None = None


def _catalogue_index() -> tuple[dict[str, str | None], _TypoIndex]:
    """The catalogue's names (:func:`_catalogue_names`) and its typo index,
    built once per catalogue.  Both empty when the catalogue cannot be read.
    """
    global _names_cache  # noqa: PLW0603
    try:
        from kiln.printers.bed_fit import _load_printer_intelligence

        catalogue = _load_printer_intelligence() or {}
    except Exception:  # noqa: BLE001 -- no catalogue, no names; spelling rules still stand
        return {}, _TypoIndex()
    if _names_cache is not None and _names_cache[0] == id(catalogue):
        return _names_cache[1], _names_cache[2]
    rows = {k: v for k, v in catalogue.items() if not k.startswith("_") and k not in _NOT_A_PRINTER_NAME}
    keys_compact = {_compact(k): k for k in rows}
    names: dict[str, str | None] = {}
    spellings_of: dict[str, list[str]] = {}
    for key, row in rows.items():
        display = row.get("display_name") if isinstance(row, dict) else None
        # The row's aliases are names it lists too: shorthands a person
        # checked name this row and no other (the roster's ``aliases_note``).
        aliases = row.get("aliases") if isinstance(row, dict) else None
        spellings = listed_names(display or "") + [str(a) for a in (aliases or []) if isinstance(a, str)]
        spellings_of[key] = spellings
        for name in spellings:
            compact = _compact(_slug(name))
            if not compact or keys_compact.get(compact, key) != key:
                continue
            names[compact] = key if names.get(compact, key) == key else None
    typo = _build_typo_index(rows, spellings_of)
    _names_cache = (id(catalogue), names, typo)
    return names, typo


def _catalogue_names() -> dict[str, str | None]:
    """``{compact name: catalogue key}`` for every name the catalogue gives
    its printers.  A name two rows share maps to ``None``: it names neither,
    and a resolver that picked one would be guessing.  A name that is itself
    another row's key belongs to that row.  Read from the catalogue the
    bed-fit and motion doors read, so the two cannot disagree about which
    printers exist.  Empty when the catalogue cannot be read.
    """
    return _catalogue_index()[0]


def _build_typo_index(rows: dict[str, object], spellings_of: dict[str, list[str]]) -> _TypoIndex:
    """Each row's maker (the manufacturer rule printer setup and design
    profiles use) and its names with the maker's words taken off."""
    from kiln.design_intelligence import _derive_manufacturer

    index = _TypoIndex()
    index.keys = frozenset(_compact(k) for k in rows)
    maker_of: dict[str, str] = {}
    for key, row in rows.items():
        display = str(row.get("display_name") or "") if isinstance(row, dict) else ""
        maker = _derive_manufacturer(key, display)
        if maker in ("Unknown", "Generic"):
            continue
        maker_of[key] = maker

    def add_form(form: str, maker: str) -> None:
        if form:
            # A form two makers share names neither.
            index.makers[form] = maker if index.makers.get(form, maker) == maker else None

    for maker in set(maker_of.values()):
        add_form(_compact(_slug(maker)), maker)
        add_form(_compact(_slug(maker.split()[0])), maker)
    # The ways owners write a maker that the catalogue's keys abbreviate
    # ("Original Prusa" for the rows keyed ``prusa_``).
    for prefix, form in _PRINTER_VENDOR_FORMS:
        owners = {maker_of[k] for k in maker_of if form and k.startswith(form)}
        if len(owners) == 1:
            add_form(_compact(prefix), next(iter(owners)))

    for key, maker in maker_of.items():
        names: list[tuple[str, ...]] = []
        for spelling in [key, *spellings_of.get(key, [])]:
            found, words = _split_maker(spelling, index)
            # A row's alias may omit its maker; one under another maker's
            # name is left whole, so it can never match that maker's typo.
            model = words if found in (None, maker) else []
            runs = _runs(model)
            if runs and runs not in names:
                names.append(runs)
        index.rows.append((key, maker, names))
    return index


def _split_maker(spelling: str, index: _TypoIndex) -> tuple[str | None, list[str]]:
    """``(maker, model words)``: the longest run of leading words that
    writes a maker, and the words after it; ``(None, all words)`` when the
    spelling does not start with one."""
    words = [w for w in _slug(spelling).split("_") if w]
    for i in range(len(words), 0, -1):
        maker = index.makers.get("".join(words[:i]))
        if maker:
            return maker, words[i:]
    return None, words


def _runs(words: list[str]) -> tuple[str, ...]:
    """The model's letter and digit runs: ``["x1", "carbon"]`` ->
    ``("x", "1", "carbon")``, so a typo can be told from a changed digit."""
    return tuple(re.findall(r"[a-z]+|[0-9]+", "_".join(words)))


def _one_letter_apart(a: str, b: str) -> bool:
    """One letter added, dropped or changed, or two neighbours swapped."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        if len(diff) == 1:
            return True
        return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]
    short, long_ = sorted((a, b), key=len)
    i = 0
    while i < len(short) and short[i] == long_[i]:
        i += 1
    return short[i:] == long_[i + 1 :]


def _a_typo_of(typed: tuple[str, ...], name: tuple[str, ...]) -> bool:
    """*typed* is *name* with one typo in one word of letters, every other
    run (each digit, each short code) exactly as the name writes it."""
    if len(typed) != len(name):
        return False
    diff = [(t, n) for t, n in zip(typed, name, strict=True) if t != n]
    if len(diff) != 1:
        return False
    t, n = diff[0]
    return t.isalpha() and n.isalpha() and min(len(t), len(n)) >= _TYPO_MIN_WORD_LEN and _one_letter_apart(t, n)


def _misspelled_row(spelling: str, index: _TypoIndex) -> str | None:
    """The one row of the spelling's own maker that it names with one typo,
    or ``None`` -- when it names no maker, when no row is that close, and
    when two are (a typo that could be either printer is neither)."""
    maker, words = _split_maker(spelling, index)
    typed = _runs(words)
    if maker is None or not typed:
        return None
    hits = {
        key
        for key, row_maker, names in index.rows
        if row_maker == maker and any(_a_typo_of(typed, name) for name in names)
    }
    return next(iter(hits)) if len(hits) == 1 else None


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
                # A vendor-stripped spelling stays with its vendor: "Prusa
                # Research A1" is never the Bambu Lab A1 (2026-10-27).
                out.append(form + rest)
            else:
                out.append(rest)
    names, typo = _catalogue_index()
    for candidate in list(out):
        named = names.get(_compact(candidate))
        if named:
            out.append(named)
            break
    else:
        # Only a spelling that reaches no row as written is read as a typo.
        if not any(_compact(c) in typo.keys for c in out):
            misspelled = _misspelled_row(printer_id, typo)
            if misspelled:
                out.append(misspelled)
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
