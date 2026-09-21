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
    """Map a free-form model hint to a bundled slicer profile id.

    Returns ``None`` when nothing matches — the caller then slices with
    the slicer's own defaults rather than a profile for some other
    machine.
    """
    if not raw:
        return None
    hint = raw.strip().lower().replace("-", "_").replace(" ", "_")
    if not hint:
        return None
    hint_compact = hint.replace("_", "")

    # A vendor model code, bare or as the machine spells it in a config
    # directory or firmware file name, names the row outright.
    from_code = profile_id_from_vendor_code(raw.strip().split("/")[-1].split(".")[0] if raw else None)
    if from_code:
        return from_code

    if (
        hint in {"prusa_mini", "prusamini"}
        or hint_compact.startswith("prusamini")
        or ("prusa" in hint and "mini" in hint)
    ):
        return "prusa_mini"
    if "mk4" in hint:
        return "prusa_mk4"
    if "mk3" in hint:
        return "prusa_mk3s"
    if "prusa_xl" in hint or hint.endswith("_xl") or hint == "xl" or ("prusa" in hint and "xl" in hint):
        return "prusa_xl"
    if "sparkxi7" in hint_compact or "sparkx" in hint_compact:
        return "sparkx_i7"
    if "ender3" in hint_compact:
        # The S1 family before the bare Ender 3: an S1 Pro homes on a probe
        # and parks with G27; the original homes on a switch and has no G27.
        if "s1pro" in hint_compact:
            return "ender3_s1_pro"
        if "s1" in hint_compact:
            return "ender3_s1"
        if "v4" in hint_compact:
            return "ender3_v4"
        if "v3ke" in hint_compact:
            return "ender3_v3_ke"
        if "v3se" in hint_compact:
            return "ender3_v3_se"
        # Before the bare "v3": a V3 Plus is a 300mm bed, and answering
        # "ender3_v3" for one certifies geometry against the wrong volume.
        if "v3plus" in hint_compact:
            return "ender3_v3_plus"
        if "v3" in hint_compact:
            return "ender3_v3"
        if "v2" in hint_compact:
            return "ender3_v2"
        return "ender3"
    if "k1max" in hint_compact:
        return "k1_max"
    if "k1c" in hint_compact:
        return "k1c"
    if "k1se" in hint_compact:
        return "k1_se"
    if hint_compact == "k1" or "crealityk1" in hint_compact:
        return "k1"
    if "k2plus" in hint_compact:
        return "k2_plus"
    if "k2pro" in hint_compact:
        return "k2_pro"
    if "k2se" in hint_compact:
        return "k2_se"
    if hint_compact == "k2" or "crealityk2" in hint_compact:
        return "k2"
    if hint_compact in {"hi", "crealityhi"}:
        return "creality_hi"
    if "ender5max" in hint_compact:
        return "ender5_max"
    if "cr10se" in hint_compact:
        return "cr10_se"
    if "ender5max" in hint_compact:
        return "ender5_max"
    if "ender5" in hint_compact:
        return "ender5"
    if "cr10se" in hint_compact:
        return "cr10_se"
    if "cr10" in hint_compact:
        return "cr10"
    if hint in {"klipper", "moonraker"}:
        return "klipper_generic"

    # Voron -- the Trident has its own row (its bed moves in Z; the 2.4's
    # gantry does), so "voron" alone is not enough to name one.
    if "trident" in hint_compact:
        return "voron_trident"
    if "voron0" in hint_compact or "v0.2" in hint or "v0.1" in hint:
        return "voron_0"
    if "voron2" in hint_compact or "voron24" in hint_compact or "v2.4" in hint:
        return "voron_2"
    if "ratrig" in hint_compact or "vcore3" in hint_compact or "v-core3" in hint or "vcore" in hint_compact:
        return "ratrig_vcore3"

    # QIDI
    if "qidi" in hint_compact or hint_compact.startswith(("xplus", "xmax", "xsmart", "q1pro", "q2c", "q2", "plus4", "plus5", "max4")):
        if "xplus3" in hint_compact or "plus3" in hint_compact:
            return "qidi_x_plus3"
        if "xmax3" in hint_compact or "max3" in hint_compact:
            return "qidi_x_max3"
        if "xsmart3" in hint_compact or "smart3" in hint_compact:
            return "qidi_x_smart3"
        if "q1pro" in hint_compact:
            return "qidi_q1_pro"
        if "q2c" in hint_compact:
            return "qidi_q2c"
        if "q2" in hint_compact:
            return "qidi_q2"
        if "plus4" in hint_compact:
            return "qidi_plus4"
        if "plus5" in hint_compact:
            return "qidi_plus5"
        if "max4" in hint_compact:
            return "qidi_max4"
        return None

    # Elegoo
    if "neptune4" in hint_compact or "neptune 4" in raw.lower():
        return "elegoo_neptune4"
    if "neptune3" in hint_compact or "neptune 3" in raw.lower():
        return "elegoo_neptune3"
    # The Carbon 2 family (Carbon 2 and Carbon 2 Combo) is its own row: a
    # load-cell Z home, a 350C nozzle and a LAN protocol the first Carbon does
    # not speak.  Tested before the bare "centauri" so it cannot fall through
    # to the older machine.
    if "carbon2" in hint_compact or hint_compact in {"cc2", "cc2c", "elegoocc2", "elegoocc2c"}:
        return "elegoo_centauri_carbon_2"
    if "centauri" in hint_compact:
        return "elegoo_centauri_carbon"
    if "orangestorm" in hint_compact or "giga" in hint_compact:
        return "elegoo_orangestorm_giga"

    # Sovol, FlashForge, AnkerMake, Artillery
    if "sv06plus" in hint_compact:
        return "sovol_sv06_plus"
    if "sv06" in hint_compact:
        return "sovol_sv06"
    if "sv07plus" in hint_compact:
        return "sovol_sv07_plus"
    if "sv07" in hint_compact:
        return "sovol_sv07"
    if "adventurer5m" in hint_compact or "ad5m" in hint_compact or "adventurer 5m" in raw.lower():
        return "flashforge_adventurer5m"
    if "ankermake" in hint_compact or hint_compact in {"m5", "m5c"}:
        return "anker_m5"
    if "sidewinderx3plus" in hint_compact or ("x3" in hint_compact and "plus" in hint_compact and "artillery" in hint_compact):
        return "artillery_sw_x3_plus"
    if "sidewinderx3" in hint_compact or ("artillery" in hint_compact and "x3" in hint_compact):
        return "artillery_sw_x3"

    # Bambu Lab printers
    if "a1" in hint and "mini" in hint:
        return "bambu_a1_mini"
    if hint in {"bambu_a1", "a1", "a1_combo"} or ("bambu" in hint and "a1" in hint):
        return "bambu_a1"
    if "a2l" in hint:
        return "bambu_a2l"
    # "h2d_pro" contains "h2d", so the Pro must be tested first or every Pro
    # resolves to the base H2D.  They are separate machines with separate
    # spec pages, not a trim level.
    if "h2d" in hint and ("pro" in hint or "pro" in hint_compact):
        return "bambu_h2d_pro"
    if "h2d" in hint:
        return "bambu_h2d"
    if "h2c" in hint:
        return "bambu_h2c"
    if "h2s" in hint:
        return "bambu_h2s"
    if "x2d" in hint:
        return "bambu_x2d"
    if "x1e" in hint or "x1e" in hint_compact:
        return "bambu_x1e"
    if "x1c" in hint or "x1_carbon" in hint_compact or ("bambu" in hint and "x1" in hint):
        return "bambu_x1c"
    if "p2s" in hint:
        return "bambu_p2s"
    if "p1s" in hint or ("bambu" in hint and "p1" in hint and "s" in hint):
        return "bambu_p1s"
    if "p1p" in hint or ("bambu" in hint and "p1" in hint):
        return "bambu_p1p"

    return None


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
    mapped = map_printer_hint_to_profile_id(value)
    if mapped and motion_facts_for(mapped) is not None:
        return mapped, []
    from difflib import get_close_matches

    from kiln.motion_facts import catalogue_keys

    return None, get_close_matches(value.lower(), catalogue_keys(), n=5, cutoff=0.5)
