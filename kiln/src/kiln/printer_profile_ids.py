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
    if hint in {"klipper", "moonraker"}:
        return "klipper_generic"

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
