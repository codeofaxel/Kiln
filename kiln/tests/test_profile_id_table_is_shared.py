"""One model→profile table, read by every door.

THE DRIFT (found 2026-08-28)
----------------------------
``_map_printer_hint_to_profile_id`` existed twice — once in the MCP
server, once in the ``kiln`` CLI.  The CLI's copy was written on
2026-02-13 for a Prusa feature; the Bambu pipeline landed three weeks
later and taught only the server's copy.  ``git log -S'return
"bambu_a1"' -- src/kiln/cli/main.py`` has no commits: the CLI copy never
knew a single Bambu model.

So ``kiln --printer <a Bambu> slice`` resolved NO bundled profile and
sliced with PrusaSlicer's generic defaults — absolute extrusion,
PrusaSlicer's own start block — and ``kiln print`` then wrapped exactly
that into a Bambu 3MF, which assumes the opposite.  A wrong file, not
untuned settings.  The identical MCP call resolved ``bambu_a1``.

The server's copy had its own gap in the other direction: an Ender 3 V3
Plus answered ``ender3_v3``, a different (250mm vs 300mm) bed, though
the ``ender3_v3_plus`` profile has shipped all along.

WHAT THIS PINS
--------------
Both doors resolve through :mod:`kiln.printer_profile_ids`, they cannot
answer differently, and every id the table emits is a profile that
actually exists.
"""
from __future__ import annotations

import pytest

from kiln.printer_profile_ids import map_printer_hint_to_profile_id

# Every hint the two copies used to disagree on, plus the families they
# shared.  Written as the ANSWER, not as "whatever the code says".
CANONICAL = {
    # Bambu — the whole family the CLI never knew.
    "bambu_a1": "bambu_a1",
    "bambu a1": "bambu_a1",
    "a1_combo": "bambu_a1",
    "Bambu Lab A1 mini": "bambu_a1_mini",
    "bambu_a2l": "bambu_a2l",
    "BAMBU-X1C": "bambu_x1c",
    "bambu x1 carbon": "bambu_x1c",
    "x1e": "bambu_x1e",
    "p1s": "bambu_p1s",
    "p1p": "bambu_p1p",
    "p2s": "bambu_p2s",
    "h2s": "bambu_h2s",
    # Ender 3 variants — the specific one must beat the family.
    "ender3": "ender3",
    "ender3 v2": "ender3_v2",
    "Ender-3 V3 Plus": "ender3_v3_plus",
    "ender3v3se": "ender3_v3_se",
    "ender3_v3_ke": "ender3_v3_ke",
    "ender3 v3": "ender3_v3",
    "ender3_v4": "ender3_v4",
    # Prusa + Creality, which both copies already agreed on.
    "prusa mini": "prusa_mini",
    "mk4s": "prusa_mk4",
    "mk3s+": "prusa_mk3s",
    "prusa xl": "prusa_xl",
    "k1 max": "k1_max",
    "k1c": "k1c",
    "k2 plus": "k2_plus",
    "klipper": "klipper_generic",
    # Nothing recognisable resolves to nothing — never to a default.
    "my-printer": None,
    "unknown-thing": None,
    "": None,
    "   ": None,
    None: None,
}


@pytest.mark.parametrize("hint,expected", sorted(CANONICAL.items(), key=lambda kv: str(kv[0])))
def test_the_table_answers_canonically(hint, expected):
    assert map_printer_hint_to_profile_id(hint) == expected


def test_both_doors_read_the_same_table():
    """The server and the CLI must not be able to answer differently.

    Compares the two module-level functions the doors actually call, not
    the shared helper — a future copy pasted back into either file fails
    here rather than in a user's print.
    """
    from kiln.cli.main import _map_printer_hint_to_profile_id as cli_map
    from kiln.server import _map_printer_hint_to_profile_id as server_map

    disagreements = {
        hint: (server_map(hint), cli_map(hint))
        for hint in CANONICAL
        if server_map(hint) != cli_map(hint)
    }
    assert not disagreements, (
        f"the MCP server and the kiln CLI answer differently: {disagreements}"
    )


def test_every_id_the_table_emits_is_a_real_profile():
    """A mapping to a profile that doesn't exist is a slice with none."""
    from kiln.slicer_profiles import list_slicer_profiles

    available = set(list_slicer_profiles())
    emitted = {v for v in CANONICAL.values() if v}
    missing = sorted(emitted - available)
    assert not missing, f"table maps to profiles that don't exist: {missing}"
