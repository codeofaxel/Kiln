"""Consistency checks over the bundled data catalogues, as a set.

Each catalogue used to be checked only by the test written against it, so
a file's guarantees depended on which test happened to name it.  These
tests read the manifest in :mod:`kiln.data_manifest` instead: every
bundled file is classified, the reserved-marker sweep covers every
printer-keyed file, and hazard guidance is required to stay present for
every machine the catalogues know.
"""

from __future__ import annotations

import pytest

from kiln import data_manifest

#: Material refusals that ship without the sentence explaining what the
#: machine lacks.  495 of the catalogue's 500 refusals carry one; these
#: five were missed when the machine was added.  Recorded rather than
#: waived so the gap stays visible and cannot grow -- the fix is to author
#: the reason from the machine's published chamber and hotend limits, not
#: to extend this set.


class TestBundledDataManifest:
    """The manifest is fail-closed: no file escapes the checks by being new."""

    def test_manifest_matches_what_ships(self) -> None:
        on_disk = data_manifest.discover_bundled_data_files()
        undeclared = on_disk - data_manifest.BUNDLED_DATA_FILES
        assert not undeclared, (
            "New bundled data file(s) are unclassified: "
            f"{sorted(undeclared)}. Add each to PRINTER_KEYED_FILES if it is "
            "keyed by printer id, or to REFERENCE_DATA_FILES with what keys it."
        )
        stale = data_manifest.BUNDLED_DATA_FILES - on_disk
        assert not stale, f"Manifest names files that no longer ship: {sorted(stale)}"

    def test_printer_keyed_and_reference_do_not_overlap(self) -> None:
        assert not (
            set(data_manifest.PRINTER_KEYED_FILES)
            & set(data_manifest.REFERENCE_DATA_FILES)
        )

    def test_every_reference_file_says_what_keys_it(self) -> None:
        for path, description in data_manifest.REFERENCE_DATA_FILES.items():
            assert description.strip(), f"{path} is classified without a description"

    def test_no_printer_keyed_file_is_classified_as_reference(self) -> None:
        """A file keyed by printer id belongs with the printer-keyed checks.

        The classification is written by hand, and a wrong one is
        invisible -- this catches it structurally.
        """
        printer_ids = {
            key
            for key in data_manifest.load("printer_intelligence.json")
            if not key.startswith("_")
        }
        for path in data_manifest.REFERENCE_DATA_FILES:
            catalogue = data_manifest.load(path)
            if not isinstance(catalogue, dict):
                continue
            keyed_by_printer = {
                key for key in catalogue if not key.startswith("_")
            } & printer_ids
            assert not keyed_by_printer, (
                f"{path} is keyed by printer id ({sorted(keyed_by_printer)[:5]}) "
                "but is classified as reference data"
            )


class TestPublicProfileFields:
    """The positive half of the printer catalogue's schema statement.

    ``PUBLIC_PROFILE_FIELDS`` names what a printer row carries and
    ``RESERVED_FIELD_MARKERS`` names what it must not.  A field in both
    would be a schema that contradicts itself, and each half is read by
    a different sweep, so neither would notice on its own.
    """

    def test_the_two_halves_do_not_overlap(self) -> None:
        both = (
            data_manifest.PUBLIC_PROFILE_FIELDS
            & data_manifest.RESERVED_FIELD_MARKERS
        )
        assert not both, f"field is both carried and reserved: {sorted(both)}"

    def test_every_roster_row_carries_exactly_these_fields(self) -> None:
        """The declaration is what the roster actually holds.

        ``test_printer_intelligence.py`` asserts the same thing through
        the loader; this asserts it of the constant itself, so a field
        added to the list without being added to the rows fails here
        rather than shipping as a promise the data does not keep.  The
        generic ``default`` row is exempt by name: it is the one bundled
        row that carries the full spec sheet, as the floor.
        """
        catalogue = data_manifest.load("printer_intelligence.json")
        rows = {
            key: value
            for key, value in catalogue.items()
            if not key.startswith("_") and key != "default"
        }
        assert rows, "the printer roster has no rows to check"
        for key, row in rows.items():
            assert set(row) == data_manifest.PUBLIC_PROFILE_FIELDS, key
        default = catalogue["default"]
        assert set(default) > data_manifest.PUBLIC_PROFILE_FIELDS
        assert "max_hotend_temp" in default


class TestReservedMarkersAreAbsent:
    """Reserved field names and schema ids, absent from every printer file.

    ``test_printer_intelligence.py`` checks the printer catalogue itself;
    this sweep covers the other printer-keyed files with the same markers.
    """

    def test_reserved_field_markers_are_absent(self) -> None:
        for path in data_manifest.PRINTER_KEYED_FILES:
            raw = (data_manifest.DATA_ROOT / path).read_text(encoding="utf-8")
            for field in sorted(data_manifest.RESERVED_FIELD_MARKERS):
                assert f'"{field}"' not in raw, (
                    f"{path} carries reserved field {field!r}"
                )

    def test_reserved_schema_markers_are_absent(self) -> None:
        for path in data_manifest.PRINTER_KEYED_FILES:
            raw = (data_manifest.DATA_ROOT / path).read_text(encoding="utf-8")
            for schema_id in sorted(data_manifest.RESERVED_SCHEMA_MARKERS):
                assert schema_id not in raw, (
                    f"{path} carries reserved schema {schema_id!r}"
                )

    def test_no_meta_block_advertises_a_reserved_schema(self) -> None:
        for path in data_manifest.PRINTER_KEYED_FILES:
            meta = data_manifest.load(path).get("_meta", {})
            if not isinstance(meta, dict):
                continue
            assert not {
                key
                for key in meta
                if "schema" in key.lower()
                and ("hardware" in key.lower() or "capability" in key.lower())
            }, path


class TestProseExtraction:
    """A shared helper nobody exercises is the same bug with extra steps."""

    @pytest.mark.parametrize(
        "path",
        [
            "slicer_profiles.json",
            "safety_profiles.json",
            "design_knowledge/printer_material_compatibility.json",
        ],
    )
    def test_the_scanner_reaches_the_files_that_carry_notes(self, path: str) -> None:
        found = [
            entry
            for entry in data_manifest.iter_printer_prose()
            if entry.source == path
        ]
        assert found, f"prose scan returned nothing for {path}"

    def test_slicer_settings_are_not_read_as_prose(self) -> None:
        """Slicer settings are configuration; their numbers are not claims."""
        keys = {
            entry.key
            for entry in data_manifest.iter_printer_prose()
            if entry.source == "slicer_profiles.json"
        }
        assert "start_gcode" not in keys
        assert "bed_shape" not in keys

    def test_published_figures_exclude_prose(self) -> None:
        """A number that appears only in a note is not 'published as data'.

        The generic row is the one bundled safety row with a figure in it;
        a roster row publishes none, and the sweep still lists it."""
        published = data_manifest.printer_published_figures()
        profile = data_manifest.load("safety_profiles.json")["default"]
        assert (
            data_manifest.canonical_figure(profile["max_hotend_temp"])
            in published["default"]
        )
        roster = [
            key for key in data_manifest.load("safety_profiles.json")
            if not key.startswith("_") and key != "default"
        ]
        assert roster and all(key in published for key in roster)

    def test_a_marked_hazard_passage_is_recognised(self) -> None:
        assert data_manifest.is_safety_marked(
            "Ventilation note: this enclosure has no filter."
        )
        assert not data_manifest.is_safety_marked(
            "Enclosed CoreXY with a 370 C nozzle."
        )

    def test_redaction_splits_a_mixed_note_at_the_marker(self) -> None:
        mixed = (
            "Enclosed CoreXY with a large bed. Ventilation note: the air "
            "filter is switched off whenever chamber heating is on."
        )
        scanned = data_manifest.redact_safety_passages(mixed)
        assert "large bed" in scanned
        assert "air filter" not in scanned


class TestSafetyNotesAreRequired:
    """Hazard guidance must exist for every machine the catalogues know.

    The safety profile and the per-material refusal note are where a user
    meets a hazard, so they must be populated for the whole fleet.
    """

    def test_every_printer_has_a_safety_profile(self) -> None:
        known = {
            key
            for key in data_manifest.load("printer_intelligence.json")
            if not key.startswith("_")
        }
        safety = data_manifest.load("safety_profiles.json")
        missing = known - set(safety)
        assert not missing, f"no safety profile for {sorted(missing)}"

    def test_the_generic_safety_profile_carries_notes(self) -> None:
        """The one bundled row with limits says what its numbers are.  That
        every served record's notes are populated is pinned beside the
        records, in kiln-pro's ``tests/test_printer_catalogue_numbers.py``."""
        profile = data_manifest.load("safety_profiles.json")["default"]
        assert profile.get("notes", "").strip(), "the generic safety profile has no notes"

    def test_every_printer_has_material_refusals(self) -> None:
        known = {
            key
            for key in data_manifest.load("printer_intelligence.json")
            if not key.startswith("_")
        }
        compatibility = data_manifest.load(
            "design_knowledge/printer_material_compatibility.json"
        )
        missing = known - set(compatibility)
        assert not missing, f"no material compatibility for {sorted(missing)}"

    def test_a_refused_material_explains_itself(self) -> None:
        """The refusal note is the 'do not try this' door -- it needs a why.

        Since 2026-10-12 the per-printer notes are the Pro overlay's, and
        that every verdict has one is kiln-pro's material-note coverage
        gate; the one bundled row whose notes are still public is the
        generic ``default`` row, judged here."""
        compatibility = data_manifest.load(
            "design_knowledge/printer_material_compatibility.json"
        )
        unexplained = {
            f"default.{material}"
            for material, record in compatibility["default"].items()
            if record.get("status") == "not_compatible"
            and not record.get("notes", "").strip()
        }
        assert not unexplained, (
            f"{len(unexplained)} generic-row refusal(s) ship with no reason: "
            f"{sorted(unexplained)[:5]}. Every other refused material explains "
            "what the machine lacks; write the same sentence."
        )
        noted_elsewhere = {
            f"{printer_id}.{material}"
            for printer_id, materials in compatibility.items()
            if not printer_id.startswith("_") and printer_id != "default"
            for material, record in materials.items()
            if record.get("notes") and record.get("upgrades_needed")
        }
        assert not noted_elsewhere, (
            "a per-printer compatibility note is the Pro overlay's since "
            f"2026-10-12, never the public matrix's: {sorted(noted_elsewhere)[:5]}"
        )
        # The one exception: a refusal with no upgrade path keeps a public
        # reason, or the user is told "no" and nothing else (the ceiling it
        # names stays unquoted; test_registry_mirror pins that).
        bare = {
            f"{printer_id}.{material}"
            for printer_id, materials in compatibility.items()
            if not printer_id.startswith("_")
            for material, record in materials.items()
            if record.get("status") == "not_compatible" and not record.get("upgrades_needed") and not record.get("notes")
        }
        assert not bare, f"refusals with no upgrade path and no reason: {sorted(bare)[:5]}"
