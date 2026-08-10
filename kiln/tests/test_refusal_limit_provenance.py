"""A refusal names whose limit it is enforcing, when that is not Kiln's.

Provenance already reached the surfaces a caller INSPECTS — the profile
payload, the roster, the safety snapshot, exports.  The refusal path was
the gap, and it is the moment the distinction actually changes what the
reader should do: told "exceeds max hotend temperature (240C)", they
cannot tell Kiln's verified ceiling from a number they typed themselves
last year.  One deserves respect; the other may be the thing to fix.

The rules these tests pin:

- silence is the common case.  A Kiln-verified limit adds nothing, so a
  refusal that carries a clause is a refusal where the clause matters.
- the note is per-FIELD, not per-profile: a refusal about the bed must
  not inherit a clause because some unrelated field was owner-set.
- the note states a provenance CLASS only — never a source, document, or
  how a verified value was established.
- both doors that quote a profile limit carry it: the G-code validator
  and the interceptor's generated block rules.
"""

from __future__ import annotations

import json

import pytest

import kiln.safety_profiles as sp
from kiln.gcode import validate_gcode_for_printer

_OWNER_NOTE = "(owner-set limit, not Kiln-verified)"
_GENERIC_NOTE = "generic fallback"

_BASE = {
    "display_name": "My Machine",
    "max_hotend_temp": 200.0,
    "max_bed_temp": 90.0,
    "max_feedrate": 5000.0,
    "build_volume": [200, 200, 200],
}


@pytest.fixture(autouse=True)
def _isolated_overrides(monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "_LOCAL_OVERRIDE_FILE", tmp_path / "local_printer_overrides.json")
    monkeypatch.setattr(sp, "_LEGACY_OVERRIDE_FILE", tmp_path / "community_profiles.json")
    monkeypatch.setattr(sp, "_LOCK_FILE", tmp_path / "locked_profiles.json")
    monkeypatch.setattr(sp, "_LOCAL_DIR", tmp_path)
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    sp._local_overrides_loaded = False
    sp._local_override_cache.clear()
    sp._variant_selections.clear()
    yield
    sp._local_overrides_loaded = False
    sp._local_override_cache.clear()
    sp._variant_selections.clear()


def _override(payload: dict) -> None:
    sp._LOCAL_OVERRIDE_FILE.write_text(json.dumps({"ender3": payload}), encoding="utf-8")
    sp._local_overrides_loaded = False
    sp._local_override_cache.clear()


class TestAVerifiedLimitRefusesQuietly:
    def test_curated_hotend_refusal_carries_no_clause(self) -> None:
        err = validate_gcode_for_printer("M104 S400", "ender3").errors[0]
        assert "exceeds" in err
        assert _OWNER_NOTE not in err
        assert _GENERIC_NOTE not in err

    def test_curated_bed_refusal_carries_no_clause(self) -> None:
        err = validate_gcode_for_printer("M140 S250", "ender3").errors[0]
        assert _OWNER_NOTE not in err


class TestAnOwnerSetLimitSaysSo:
    def test_hotend(self) -> None:
        _override(dict(_BASE))
        err = validate_gcode_for_printer("M104 S250", "ender3").errors[0]
        assert "200" in err and _OWNER_NOTE in err

    def test_bed(self) -> None:
        _override(dict(_BASE))
        err = validate_gcode_for_printer("M140 S150", "ender3").errors[0]
        assert _OWNER_NOTE in err

    def test_feedrate_warning(self) -> None:
        _override(dict(_BASE))
        res = validate_gcode_for_printer("G1 X10 Y10 F99000", "ender3")
        feed = [w for w in res.warnings if "feedrate" in w]
        assert feed and _OWNER_NOTE in feed[0]

    def test_a_clamped_away_field_refuses_as_kilns_own(self) -> None:
        """The owner tried to RAISE the hotend, so the clamp put Kiln's
        number back — the refusal is enforcing Kiln's limit and must not
        blame the owner for it."""
        curated = sp.get_profile("ender3").max_hotend_temp
        _override({**_BASE, "max_hotend_temp": curated + 100})
        err = validate_gcode_for_printer("M104 S499", "ender3").errors[0]
        assert f"{curated:g}" in err
        assert _OWNER_NOTE not in err


class TestTheNoteIsPerFieldNotPerProfile:
    def test_bed_refusal_is_clean_when_only_hotend_was_owner_set(self) -> None:
        """The sharp case: an owner-set hotend must not put a clause on a
        refusal about the BED, whose limit is still Kiln's."""
        curated = sp.get_profile("ender3")
        _override(
            {
                "display_name": "Mixed",
                "max_hotend_temp": curated.max_hotend_temp - 40,
                "max_bed_temp": curated.max_bed_temp + 100,  # raised -> clamped back
                "max_feedrate": curated.max_feedrate,
                "build_volume": [200, 200, 200],
            }
        )
        got = sp.get_profile("ender3")
        assert "max_hotend_temp" in got.owner_supplied
        assert "max_bed_temp" not in got.owner_supplied

        hot = validate_gcode_for_printer("M104 S250", "ender3").errors[0]
        bed = validate_gcode_for_printer("M140 S250", "ender3").errors[0]
        assert _OWNER_NOTE in hot
        assert _OWNER_NOTE not in bed


class TestAnUnrecognisedPrinterSaysItIsGeneric:
    def test_generic_fallback_is_named(self) -> None:
        err = validate_gcode_for_printer("M104 S400", "machine_kiln_never_heard_of").errors[0]
        assert _GENERIC_NOTE in err

    def test_generic_note_is_not_owner_blame(self) -> None:
        err = validate_gcode_for_printer("M104 S400", "machine_kiln_never_heard_of").errors[0]
        assert _OWNER_NOTE not in err


class TestTheNoteLeaksNothing:
    def test_no_source_or_method_in_any_suffix(self) -> None:
        """A refusal is a user-facing surface: it may name the provenance
        CLASS and must never carry a source, a document, or the account
        of how a verified value was established."""
        forbidden = ("http", "tds", "datasheet", "source", "manufacturer", "spec sheet")
        _override(dict(_BASE))
        for pid, cmd in (
            ("ender3", "M104 S250"),
            ("ender3", "M140 S150"),
            ("machine_kiln_never_heard_of", "M104 S400"),
        ):
            err = validate_gcode_for_printer(cmd, pid).errors[0].lower()
            for token in forbidden:
                assert token not in err, f"{token!r} leaked into: {err}"


class TestTheInterceptorDoorCarriesItToo:
    """The second door: rules the interceptor generates from a profile
    carry their own block message, built independently of the validator's
    — so it needs its own proof, through the real public entry point."""

    def _hotend_rule(self, printer_name: str):
        from kiln.gcode_interceptor import GcodeInterceptor

        rules = GcodeInterceptor().load_safety_rules(printer_name)
        hot = [r for r in rules if r.name.startswith("max_hotend_temp")]
        assert hot, f"no hotend rule generated for {printer_name!r}"
        return hot[0]

    def test_owner_set_hotend_reaches_the_block_rule(self) -> None:
        _override(dict(_BASE))
        assert _OWNER_NOTE in self._hotend_rule("ender3").message

    def test_curated_profile_block_rule_stays_clean(self) -> None:
        msg = self._hotend_rule("ender3").message
        assert _OWNER_NOTE not in msg
        assert _GENERIC_NOTE not in msg

    def test_owner_set_bed_reaches_its_own_rule(self) -> None:
        from kiln.gcode_interceptor import GcodeInterceptor

        _override(dict(_BASE))
        rules = GcodeInterceptor().load_safety_rules("ender3")
        bed = [r for r in rules if r.name.startswith("max_bed_temp")]
        assert bed and _OWNER_NOTE in bed[0].message

    def test_unrecognised_printer_rule_says_generic(self) -> None:
        """The threshold is then the generic fallback's, so the message
        must not present it as a ceiling measured for this machine."""
        assert _GENERIC_NOTE in self._hotend_rule("machine_kiln_never_heard_of").message
