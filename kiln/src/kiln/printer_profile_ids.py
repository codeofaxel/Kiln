"""Free-form printer model hint → bundled slicer profile id.

One table, every door.  The MCP server and the ``kiln`` CLI each grew
their own copy of this mapping, and the copies drifted: the CLI's knew
no Bambu at all, so ``kiln --printer <a Bambu> slice`` resolved no
bundled profile and sliced with PrusaSlicer's generic defaults — no
relative extrusion, no empty start block — while the identical MCP call
resolved ``bambu_a1``.  The server's copy meanwhile answered
``ender3_v3`` for an Ender 3 V3 Plus, a printer whose own bundled
profile has shipped all along.

A mapping table with two homes is a table that is wrong in one of them.
Both callers now import this function; neither keeps a table.

The hints are free-form on purpose — they arrive from config files
users typed, from ``KILN_PRINTER_MODEL``, and from printer firmware
strings — so matching is deliberately loose.  Order matters: the most
specific variant of a family has to be tested before the family, or an
Ender 3 V3 SE answers "ender3".
"""
from __future__ import annotations

#: The model codes a Creality machine's own firmware and slicer use for it,
#: as the vendor's own slicer profiles publish them, keyed to the
#: catalogue row.  A Creality printer names its code in its own
#: ``printer.cfg`` header (``# F008 / 350*350*350``), its config directory
#: (``F016_CR4CU220812S11``) and its firmware image file name -- so a
#: discovery or pairing flow can name the row from what the machine already
#: says about itself, instead of guessing from a display string.  Codes the
#: catalogue has no row for are deliberately absent: an unknown code is an
#: unknown printer, never a neighbour.
CREALITY_MODEL_CODES: dict[str, str] = {
    "F001": "ender3_v3",
    "F002": "ender3_v3_plus",
    "F003": "cr10_se",
    "F004": "ender5_max",
    "F005": "ender3_v3_ke",
    "F008": "k2_plus",
    "F009": "ender3_v4",
    "F012": "k2_pro",
    "F016": "k2_se",
    "F018": "creality_hi",
    "F021": "k2",
    "F022": "sparkx_i7",
}


def profile_id_from_vendor_code(code: str | None) -> str | None:
    """A catalogue row from a vendor model code (``"F016"`` → ``"k2_se"``).

    Reads the code out of the strings a machine actually exposes -- a bare
    code, a config directory name like ``F016_CR4CU220812S11``, a firmware
    file name like ``F004_ota_img_V1.2.0.20.img`` -- and answers only for
    codes the catalogue keys.
    """
    if not code:
        return None
    token = str(code).strip().upper()
    for candidate in (token, token.split("_", 1)[0], token.split("-", 1)[0]):
        if candidate in CREALITY_MODEL_CODES:
            return CREALITY_MODEL_CODES[candidate]
    return None


def map_printer_hint_to_profile_id(raw: str | None) -> str | None:
    """The catalogue row a free-form printer name spells, or ``None``.

    A name resolves only to a row it NAMES: the row's id, a name the row's
    ``display_name`` lists, a vendor form of one ("Bambu Lab X1C", "Original
    Prusa MK4"), a vendor model code the machine reports (Creality ``F016``),
    or an alias written on the row as data (``printer_intelligence.json``,
    ``aliases``).  ``None`` means the catalogue has no row for it -- the
    caller then slices with the generic profile and holds the machine to
    the generic floor, never to some other machine's.

    Until 2026-10-27 this was 69 substring rules written to pick a decent
    SLICING profile ("vcore" anywhere -> V-Core 3, "neptune 4" anywhere ->
    Neptune 4), and the declared-model check reused it for safety limits: a
    V-Core 4 was held to the V-Core 3's, a Neptune 4 Max to a 225 mm bed.
    A shorthand that really names one row now lives on that row.

    A printer outside the catalogue that its owner set up on this machine
    (:mod:`kiln.printer_setup`) answers to its own key first.
    """
    if not raw:
        return None
    hint = raw.strip()
    if not hint:
        return None

    from kiln.printer_setup import LOCAL_PREFIX

    local = hint.lower().replace("-", "_").replace(" ", "_")
    if local.startswith(LOCAL_PREFIX):
        from kiln.printers.bed_fit import owner_stated_build_volume

        if owner_stated_build_volume(local) is not None:
            return local

    # A vendor model code, bare or as the machine spells it in a config
    # directory or firmware file name, names the row outright.
    from_code = profile_id_from_vendor_code(hint.split("/")[-1].split(".")[0])
    if from_code:
        return from_code

    # A connection word is not a printer, but it does pick the generic
    # Klipper slicing profile, which is all this id ever meant.
    if local in {"klipper", "moonraker", "klipper_generic"}:
        return "klipper_generic"

    from kiln.catalog_keys import resolve_printer_key
    from kiln.motion_facts import catalogue_keys

    return resolve_printer_key(hint, catalogue_keys())


def resolve_declared_model(raw: str | None) -> tuple[str | None, list[str]]:
    """``(catalogue key, close matches)`` for a printer_model a person wrote.

    The key is the row itself when the string is one (a vendor prefix
    tolerated), else the hint table's answer, else ``None`` -- the same
    resolution the motion gate performs, so a door that writes the model
    can say at write time what the gate will say at home time.  The close
    matches are catalogue keys within a loose edit distance, for the door
    to offer; empty when the key resolved, or when nothing is close.
    """
    from kiln.motion_facts import motion_facts_for

    value = str(raw or "").strip()
    if not value:
        return None, []
    facts = motion_facts_for(value)
    if facts is not None:
        return facts.printer_id, []
    # The one name rule every door reads (map_printer_hint_to_profile_id).
    mapped = map_printer_hint_to_profile_id(value)
    if mapped and motion_facts_for(mapped) is not None:
        return mapped, []
    from difflib import get_close_matches

    from kiln.motion_facts import catalogue_keys

    return None, get_close_matches(value.lower(), catalogue_keys(), n=5, cutoff=0.5)
