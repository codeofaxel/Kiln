"""A printer's own name finds its row at every door that resolves printers.

Kiln resolved a printer name in five places -- the catalogue matcher behind
the compatibility and safety doors, the bed-fit candidates behind the build
volume and the motion gate, the slicer-profile hint table, and the write-time
declared-model check -- and each had grown its own rules.  They disagreed:
"Bambu Lab A1" found its build volume and its slicer profile but not its
compatibility row or its safety profile, which fell back to the generic one;
"Bambu Lab X1 Carbon" found its slicer profile and not its build volume;
"AON3D Hylo", written exactly as its own row writes it, found no door at
all.  Measured 2026-10-02 over the 201 names below: 82 wrong at the
catalogue matcher, 102 at the safety door, 37 at bed fit, 9 at the slicer
table, 4 at the declared-model check.

The corpus here is DERIVED from the catalogue -- every row's id, every model
its display name lists, and the way Bambu Lab and Prusa owners write the
maker's name -- so a printer added tomorrow is checked the day it lands, and
a door that stops agreeing with the others fails here rather than in a
customer's slice.

A/B: against the tree before the fix, every door-level test below fails
(the counts above); each mutation the docstrings name was run and failed.
"""

from __future__ import annotations

import pytest

from kiln import catalog_keys
from kiln.catalog_keys import listed_names, printer_key_candidates, resolve_printer_key
from kiln.printers.bed_fit import _load_printer_intelligence

GENERIC_ROWS = {"default", "klipper_generic"}


def _rows() -> dict[str, dict]:
    return {
        k: v
        for k, v in _load_printer_intelligence().items()
        if not k.startswith("_") and k not in GENERIC_ROWS and isinstance(v, dict)
    }


def _names_for(key: str, row: dict) -> list[str]:
    """Every spelling of one row this test holds every door to."""
    names = [key, *listed_names(row.get("display_name") or "")]
    if key.startswith("bambu_"):
        model = key.removeprefix("bambu_")
        names += [f"bambu_lab_{model}", f"Bambu Lab {model.replace('_', ' ')}", f"bambulab_{model}"]
    if key.startswith("prusa_"):
        model = key.removeprefix("prusa_")
        names += [f"original_prusa_{model}", f"Prusa Research {model}"]
    return names


def _corpus() -> list[tuple[str, str]]:
    return [(name, key) for key, row in _rows().items() for name in _names_for(key, row)]


def _misses(answer) -> list[str]:
    out = []
    for name, key in _corpus():
        got = answer(name)
        if got != key:
            out.append(f"{name!r} -> {got!r} (its row is {key!r})")
    return out


class TestEveryDoorAgrees:
    def test_the_catalogue_matcher(self):
        """The compatibility and material doors.  A/B: with the catalogue's
        own names left out of ``printer_key_candidates`` this fails."""
        from kiln.design_intelligence import _get_kb

        keys = list(_get_kb().printer_compatibility)
        assert _misses(lambda n: resolve_printer_key(n, keys)) == []

    def test_the_safety_profiles(self):
        """The ceilings every print is checked against.  A miss here was
        the generic profile -- 250C hotend, a 100C bed an A1 mini cannot
        reach, a 200mm cube -- for a printer Kiln has curated limits for.
        A/B: with the Bambu Lab vendor form removed this fails."""
        from kiln import safety_profiles

        def profile_of(name):
            profile = safety_profiles.get_profile(name)
            return getattr(profile, "printer_id", None) or getattr(profile, "id", None)

        assert _misses(profile_of) == []

    def test_the_bed_fit_and_the_motion_gate(self):
        """The build volume and the head-motion facts.  A/B: with the shared
        candidates not appended to the bed-fit candidates this fails."""
        from kiln.motion_facts import motion_facts_for
        from kiln.printers.bed_fit import resolve_build_volume_printer_id

        with_volume = {k for k, v in _rows().items() if v.get("build_volume_mm")}
        with_motion = {k for k, v in _rows().items() if isinstance(v.get("motion"), dict)}
        assert _misses(lambda n: resolve_build_volume_printer_id(n) if _key_of(n) in with_volume else _key_of(n)) == []

        def motion_row(name):
            if _key_of(name) not in with_motion:
                return _key_of(name)
            facts = motion_facts_for(name)
            return facts.printer_id if facts is not None else None

        assert _misses(motion_row) == []

    def test_the_slicer_profile(self):
        """The bundled slicer profile a slice runs with.  A/B: with the
        catalogue fallback removed from ``map_printer_hint_to_profile_id``
        this fails on five rows its hand-written table never listed: the
        AnkerMake M5 by its id, four industrial machines by id and by name."""
        from kiln.printer_profile_ids import map_printer_hint_to_profile_id

        assert _misses(map_printer_hint_to_profile_id) == []

    def test_the_declared_model_check(self):
        """The check a stated printer model passes when it is written down.
        A/B: with the catalogue's own names left out this fails on the
        industrial machines' own names."""
        from kiln.printer_profile_ids import resolve_declared_model

        assert _misses(lambda n: resolve_declared_model(n)[0]) == []


def _key_of(name: str) -> str:
    return dict((n, k) for n, k in _corpus())[name]


class TestTheNamesThemselves:
    def test_no_name_is_shared_by_two_rows(self):
        """A name two rows share names neither, and is dropped -- so a new
        printer whose listed name collides with another's is caught here,
        by name, instead of silently answering for one of them."""
        shared = [name for name, key in catalog_keys._catalogue_names().items() if key is None]
        assert shared == []

    @pytest.mark.parametrize(
        ("display", "names"),
        [
            ("Prusa MK4 / MK4S", ["Prusa MK4", "Prusa MK4S"]),
            ("Creality Ender 3 / Ender 3 Pro", ["Creality Ender 3", "Creality Ender 3 Pro"]),
            (
                "Elegoo Neptune 3 / 3 Pro / 3 Plus",
                ["Elegoo Neptune 3", "Elegoo Neptune 3 Pro", "Elegoo Neptune 3 Plus"],
            ),
            ("Rat Rig V-Core 3 / V-Core 3.1", ["Rat Rig V-Core 3", "Rat Rig V-Core 3.1"]),
            ("Bambu Lab X1 Carbon", ["Bambu Lab X1 Carbon"]),
            ("", []),
        ],
    )
    def test_a_display_name_lists_whole_names(self, display, names):
        assert listed_names(display) == names

    @pytest.mark.parametrize(
        ("spelling", "row"),
        [
            ("bambu_lab_a1", "bambu_a1"),
            ("Bambu Lab P1S", "bambu_p1s"),
            ("bambulab_a1_mini", "bambu_a1_mini"),
            ("Bambu Lab X1 Carbon", "bambu_x1c"),
            ("Original Prusa MK4", "prusa_mk4"),
            ("Prusa MK4S", "prusa_mk4"),
            ("Creality K1C", "k1c"),
            ("ender 3 v3 ke", "ender3_v3_ke"),
        ],
    )
    def test_how_owners_write_it(self, spelling, row):
        keys = list(_rows())
        assert resolve_printer_key(spelling, keys) == row

    @pytest.mark.parametrize("spelling", ["Bambu Lab X1", "prusa_mk3", "voron_trident_300", "a printer", "default"])
    def test_a_name_no_row_gives_is_no_row(self, spelling):
        """Spelling, never family: the plain X1 is not the X1 Carbon, an MK3
        is not an MK3S, a Trident 300 is not the row's bed size."""
        assert resolve_printer_key(spelling, list(_rows())) is None

    def test_a_name_another_row_owns_as_its_key_stays_that_rows(self, monkeypatch):
        """A listed name that is itself another row's key belongs to that
        row, and a name two rows list names neither."""
        catalogue = {
            # Lists "Alpha One Pro", which is the next row's own key.
            "alpha_one": {"display_name": "Alpha One / Alpha One Pro"},
            "alpha_one_pro": {"display_name": "Maker Alpha One Pro"},
            # Both list "Maker Gamma".
            "beta": {"display_name": "Maker Beta / Gamma"},
            "delta": {"display_name": "Maker Delta / Gamma"},
        }
        monkeypatch.setattr("kiln.printers.bed_fit._load_printer_intelligence", lambda: catalogue)
        monkeypatch.setattr(catalog_keys, "_names_cache", None)
        assert "alpha_one" not in printer_key_candidates("Alpha One Pro")
        assert resolve_printer_key("Alpha One Pro", list(catalogue)) == "alpha_one_pro"
        assert resolve_printer_key("Maker Gamma", list(catalogue)) is None
        assert not {"beta", "delta"} & set(printer_key_candidates("Maker Gamma"))
        assert resolve_printer_key("Maker Beta", list(catalogue)) == "beta"
