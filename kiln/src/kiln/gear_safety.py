"""What Kiln tells a person about meshing printed gears, at every tier.

These sentences are safety floors: free everywhere, said wherever Kiln
answers about gear teeth, and written once here so every door says the same
thing.  kiln-pro's gear answers read them from this module.
"""

from __future__ import annotations

import re

#: Said for every gear pair.
GEAR_LOAD_HOLDING_CAUTION = (
    "A printed gear train is never the thing that holds a load up: a load "
    "that must not move or fall needs a stop or brake of its own."
)

#: Said for a PLA gear.
PLA_GEAR_HEAT_CAUTION = (
    "PLA gear teeth soften above about 60 °C, and teeth heat up with "
    "torque: keep a PLA gear at low torque and away from motors, cars and "
    "sun. A gear that works again after a rest overheated."
)

#: Said for a gear in ABS, ASA or polycarbonate.
GREASE_ON_STYRENICS_CAUTION = (
    "Do not grease ABS, ASA or polycarbonate gears with just any grease: "
    "ester-based oils crack ABS and polycarbonate, and ASA is treated the "
    "same way. Use a grease whose maker rates it for that plastic."
)


def _words(material: str | None) -> set[str]:
    return {w for w in re.split(r"[^A-Z0-9]+", (material or "").upper()) if w}


def gear_cautions(*materials: str | None) -> list[str]:
    """The cautions for a gear pair in *materials*, most important first.

    A material counts by any word of its name, so "Tough PLA" and "PLA-CF"
    are PLA and "PC/ABS" is both: saying a caution once too often is the
    safe direction.
    """
    words: set[str] = set()
    for material in materials:
        words |= _words(material)
    out = [GEAR_LOAD_HOLDING_CAUTION]
    if "PLA" in words:
        out.append(PLA_GEAR_HEAT_CAUTION)
    if words & {"ABS", "ASA", "PC", "POLYCARBONATE"}:
        out.append(GREASE_ON_STYRENICS_CAUTION)
    return out
