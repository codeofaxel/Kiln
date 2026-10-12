"""A printer's record reaches every public door, and a miss is worded once.

The bundled data files are a roster; a catalogued printer's limits, variants
and spec sheet come through ``kiln._pro_catalogue_bridge`` -- kiln-pro here,
else served and kept on this computer, else the record last kept, else the
generic floor.  Each wire is proved A/B: the same call with the wire cut
gives the floor.  ``kilntest_served`` is a roster-only row in the session
fixture: a catalogued printer whose record is not bundled, which is what
every catalogued printer looks like to an install without kiln-pro.
"""

from __future__ import annotations

import json

import pytest

from kiln import _pro_catalogue_bridge as bridge
from kiln import printer_intelligence as pi
from kiln import safety_profiles as sp
from kiln.printers import bed_fit
from kiln.served_answer import Miss

SERVED_ID = "kilntest_served"
RECORD = {
    "printer_id": SERVED_ID,
    "matched_by": "exact",
    "display_name": "Kilntest Served 200",
    "safety": {
        "max_hotend_temp": 280.0,
        "max_bed_temp": 95.0,
        "max_chamber_temp": None,
        "max_feedrate": 9000.0,
        "min_safe_z": 0.0,
        "max_volumetric_flow": 14.0,
        "build_volume": [200, 200, 210],
        "notes": "Synthetic served test printer.",
        "variants": {
            "served_kit": {
                "display_name": "Kilntest Served 200 + kit",
                "description": "Synthetic variant.",
                "max_hotend_temp": 310.0,
                "requires": ["The kit is fitted."],
            }
        },
    },
    "intel": {
        "firmware": "klipper",
        "extruder_type": "direct_drive",
        "hotend_type": "ptfe_lined",
        "has_enclosure": False,
        "has_chamber_sensor": True,
        "has_abl": True,
        "has_input_shaping": True,
        "build_volume_mm": [200, 200, 210],
        "max_hotend_temp": 280,
        "max_bed_temp": 95,
        "materials": {"PLA": {"hotend": 207, "bed": 58, "fan": 100}},
        "nozzle_clumping_probe": None,
    },
}


def _reset_loaders() -> None:
    sp._cache.clear()
    sp._roster.clear()
    sp._variant_data.clear()
    sp._loaded = False
    sp._local_override_cache.clear()
    sp._variant_selections.clear()
    sp._local_overrides_loaded = False
    sp._nudged.clear()
    sp._pending_nudges.clear()
    pi._reset_caches()
    bed_fit._printer_intelligence_cache = None
    bridge.forget()


@pytest.fixture(autouse=True)
def _no_kiln_pro_here(monkeypatch, tmp_path):
    """The install public CI is: no kiln-pro, records kept under a fresh home."""
    monkeypatch.setattr(bridge, "available", lambda: False)
    monkeypatch.setattr(bridge, "KEEP_DIR", tmp_path / "printer_catalogue")
    _reset_loaders()
    yield
    _reset_loaders()


@pytest.fixture
def served(monkeypatch):
    """The servers answer with RECORD; the fixture counts the asks."""
    calls: list[str] = []

    def answer(key: str):
        calls.append(key)
        if key == SERVED_ID:
            return json.loads(json.dumps(RECORD)), None
        return None, None  # a settled "no row"

    monkeypatch.setattr(bridge, "_served", answer)
    return calls


# ---------------------------------------------------------------------------
# The served record reaches every door
# ---------------------------------------------------------------------------


class TestServed:
    def test_the_record_reaches_the_safety_profile(self, served):
        profile = sp.get_profile(SERVED_ID)
        assert profile.id == SERVED_ID
        assert profile.max_hotend_temp == 280.0 and profile.build_volume == [200, 200, 210]
        assert profile.available_variants == ("served_kit",)
        assert served == [SERVED_ID]

    def test_cut_the_wire_and_the_same_call_is_the_floor(self, served, monkeypatch):
        """A/B for the safety wire."""
        monkeypatch.setattr(sp, "_fetch_curated", lambda spelling: None)
        profile = sp.get_profile(SERVED_ID)
        assert profile.id == "default" and profile.max_hotend_temp == 240.0

    def test_the_record_reaches_the_spec_sheet(self, served):
        intel = pi.get_printer_intel(SERVED_ID)
        assert intel.id == SERVED_ID and intel.hotend_type == "ptfe_lined"
        assert intel.materials["PLA"].hotend == 207
        assert pi.chamber_sensor_for_model(SERVED_ID) is True
        assert pi._get_raw(SERVED_ID)["build_volume_mm"] == [200, 200, 210]

    def test_cut_the_wire_and_the_spec_sheet_is_generic(self, served, monkeypatch):
        """A/B for the intelligence wire."""
        monkeypatch.setattr(pi, "_fetch_row", lambda spelling: None)
        assert pi.get_printer_intel(SERVED_ID).id == "default"
        assert pi.chamber_sensor_for_model(SERVED_ID) is None

    def test_the_record_reaches_bed_fit(self, served):
        assert bed_fit.get_build_volume(SERVED_ID) == (200.0, 200.0, 210.0)
        assert bed_fit.resolve_build_volume(SERVED_ID) == (SERVED_ID, (200.0, 200.0, 210.0))

    def test_the_variant_came_along_and_resolves(self, served):
        listed = sp.list_printer_variants(SERVED_ID)
        assert listed["known"] and set(listed["variants"]) == {"served_kit"}
        assert sp.get_profile(SERVED_ID, variant="served_kit").max_hotend_temp == 310.0

    def test_one_ask_per_printer_whatever_the_spelling_of_the_door(self, served):
        sp.get_profile(SERVED_ID)
        pi.get_printer_intel(SERVED_ID)
        bed_fit.get_build_volume(SERVED_ID)
        sp.get_profile(SERVED_ID)
        assert served == [SERVED_ID]

    def test_a_roster_printer_is_a_catalogue_printer_before_its_record_arrives(self):
        assert bed_fit.catalogue_printer_id(SERVED_ID) == SERVED_ID
        assert bed_fit.catalogue_printer_id("a printer nobody makes") is None
        assert SERVED_ID in sp.list_profiles() and SERVED_ID in pi.list_intel_profiles()
        assert sp.match_display_name("Kilntest Served 200") == SERVED_ID

    def test_the_record_is_kept_on_this_computer(self, served):
        sp.get_profile(SERVED_ID)
        kept = bridge.kept_record(SERVED_ID)
        assert kept is not None and kept["safety"]["max_hotend_temp"] == 280.0
        assert (bridge.KEEP_DIR / f"{SERVED_ID}.json").exists()


# ---------------------------------------------------------------------------
# An unknown printer is a settled answer
# ---------------------------------------------------------------------------


class TestUnknown:
    def test_no_row_is_the_floor_and_is_not_asked_again(self, served):
        assert sp.get_profile("acme_zed").id == "default"
        assert bridge.why_unanswered("acme_zed") == ""
        sp.get_profile("acme_zed")
        pi.get_printer_intel("acme_zed")
        assert served == ["acme_zed"]

    def test_no_row_is_never_kept_on_disk(self, served):
        sp.get_profile("acme_zed")
        assert bridge.kept_record("acme_zed") is None


# ---------------------------------------------------------------------------
# Misses: offline, signed out
# ---------------------------------------------------------------------------


class TestOffline:
    def test_a_kept_record_answers_offline(self, served, monkeypatch):
        sp.get_profile(SERVED_ID)
        _reset_loaders()
        monkeypatch.setattr(bridge, "_served", lambda key: (_ for _ in ()).throw(ConnectionError("down")))
        profile = sp.get_profile(SERVED_ID)
        assert profile.id == SERVED_ID and profile.max_hotend_temp == 280.0
        assert bridge.why_unanswered(SERVED_ID) == ""  # fresh: nothing was asked

    def test_a_stale_kept_record_still_answers_when_the_ask_fails(self, served, monkeypatch):
        sp.get_profile(SERVED_ID)
        _reset_loaders()
        monkeypatch.setattr(bridge, "FRESH_FOR_S", 0.0)
        monkeypatch.setattr(bridge, "_served", lambda key: (None, Miss("unanswered", "SERVER_UNREACHABLE")))
        assert sp.get_profile(SERVED_ID).max_hotend_temp == 280.0
        assert bridge.why_unanswered(SERVED_ID) == "unanswered"

    def test_never_kept_and_offline_is_the_floor_with_nothing_to_say(self, monkeypatch):
        monkeypatch.setattr(bridge, "_served", lambda key: (None, Miss("offline", "SERVER_UNREACHABLE")))
        assert sp.get_profile(SERVED_ID).id == "default"
        assert bridge.why_unanswered(SERVED_ID) == "offline"
        assert sp.warm_profile(SERVED_ID) is None


class TestSignedOut:
    """The session fixture seals the served door as a signed-out install."""

    def test_a_catalogued_printer_is_the_floor(self):
        profile = sp.get_profile(SERVED_ID)
        assert profile.id == "default" and profile.max_hotend_temp == 240.0
        assert bridge.why_unanswered(SERVED_ID) == "signed_out"
        assert "signed out" in (bridge.unanswered(SERVED_ID) or "")

    def test_the_nudge_names_the_printer_and_is_said_once(self):
        sentence = sp.warm_profile(SERVED_ID)
        assert sentence and "Kiln knows the Kilntest Served 200 well" in sentence
        assert "240 C hotend, 80 C bed" in sentence and "Sign in free" in sentence
        assert sp.pending_limits_nudge() == sentence
        assert sp.pending_limits_nudge() is None
        assert sp.warm_profile(SERVED_ID) is None

    @pytest.mark.parametrize("signed_in", [False, True])
    def test_an_unknown_printer_gets_the_other_sentence_with_no_sign_in_clause(self, signed_in, request):
        """Signing in changes nothing for a printer Kiln has no row for today
        (custom limits are a Business feature; a bed from a slicer file needs
        no account), so the sentence asks for none."""
        if signed_in:
            request.getfixturevalue("served")
        sentence = sp.warm_profile("Acme Zed")
        assert sentence and "doesn't know the 'Acme Zed' yet" in sentence
        assert "Sign in" not in sentence
        assert "set_printer_model" in sentence and "add_safety_profile" in sentence and "code" in sentence

    def test_a_bundled_printer_gets_no_nudge(self):
        assert sp.warm_profile("kilntest_open") is None
        assert sp.warm_profile("default") is None and sp.warm_profile("") is None

    def test_a_signed_out_miss_is_not_asked_again_on_every_command(self, monkeypatch):
        calls: list[str] = []

        def refused(key):
            calls.append(key)
            return None, Miss("signed_out", "KILN_ACCOUNT_NOT_PAIRED")

        monkeypatch.setattr(bridge, "_served", refused)
        for _ in range(5):
            sp.get_profile(SERVED_ID)
        assert calls == [SERVED_ID]

    def test_an_override_saved_under_a_catalogue_name_is_clamped_to_the_floor_without_the_record(
        self, monkeypatch, tmp_path
    ):
        """State drift the save-time check cannot see: an override saved while
        the record was held was exempt from a person's confirmation because
        it would be clamped against that record.  In a process that cannot
        get the record it must not stand unclamped (A/B: before the clamp
        target fell back to the floor, this read 500)."""
        monkeypatch.setattr(sp, "_LOCAL_OVERRIDE_FILE", tmp_path / "overrides.json")
        monkeypatch.setattr(sp, "_LEGACY_OVERRIDE_FILE", tmp_path / "legacy.json")
        monkeypatch.setattr(sp, "_LOCAL_DIR", tmp_path)
        sp._local_override_cache.clear()
        sp._local_overrides_loaded = False
        sp.set_local_printer_override(
            SERVED_ID,
            {"max_hotend_temp": 500, "max_bed_temp": 150, "max_feedrate": 9000, "build_volume": [200, 200, 210]},
            confirmed=True,
        )
        profile = sp.get_profile(SERVED_ID)
        assert profile.max_hotend_temp == 240.0 and profile.max_bed_temp == 80.0
        assert profile.curated_base is False
        # Reached by prefix under a spelling the roster does not list, the
        # override's OWN key still names the catalogue printer: still the floor.
        assert sp.get_profile(SERVED_ID + "x").max_hotend_temp == 240.0
        # An unknown name a person confirmed keeps standing, as it always has.
        sp.set_local_printer_override(
            "acme_zed",
            {"max_hotend_temp": 320, "max_bed_temp": 110, "max_feedrate": 9000, "build_volume": [200, 200, 210]},
            confirmed=True,
        )
        assert sp.get_profile("acme_zed").max_hotend_temp == 320.0

    def test_a_roster_name_with_no_record_is_held_to_the_floor_for_overrides(self):
        """A typed 500 C under a catalogue name stands unclamped only if the
        name's record is held; signed out, it is a number Kiln cannot
        vouch for, and a person confirms it like any unknown printer's."""
        assert sp.limits_above_floor(SERVED_ID, {"max_hotend_temp": 500}) == ("max_hotend_temp",)
        assert sp.limits_above_floor("kilntest_open", {"max_hotend_temp": 500}) == ()


# ---------------------------------------------------------------------------
# The setup chokepoint and the doors that speak
# ---------------------------------------------------------------------------


class TestSetupChokepoint:
    def test_set_safety_profile_warms_and_mints_once(self):
        from kiln.printers.base import PrinterAdapter

        class _Bare:  # the one method under test, no backend behind it
            pass

        adapter = _Bare()
        PrinterAdapter.set_safety_profile(adapter, SERVED_ID)
        assert adapter._safety_profile_id == SERVED_ID
        note = sp.pending_limits_nudge()
        assert note and "Kilntest Served 200" in note
        PrinterAdapter.set_safety_profile(adapter, SERVED_ID)
        assert sp.pending_limits_nudge() is None

    def test_register_printer_speaks_the_note_once(self, monkeypatch, tmp_path):
        pytest.importorskip("kiln.server")
        from kiln import bench, server

        monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
        monkeypatch.setattr("kiln.cli.config.get_config_path", lambda: tmp_path / "config.yaml")
        monkeypatch.setattr(bench, "offer_after_registration", lambda _adapter, name: None)
        result = server.register_printer(
            name="bench", printer_type="moonraker", host="192.0.2.21",
            printer_model=SERVED_ID, persist=False, verify_connection=False,
        )
        assert result["success"], result
        assert "Kilntest Served 200" in result["limits_note"] and result["limits_note"] in result["message"]
        again = server.register_printer(
            name="bench2", printer_type="moonraker", host="192.0.2.22",
            printer_model=SERVED_ID, persist=False, verify_connection=False,
        )
        assert again["success"] and "limits_note" not in again

    def test_set_printer_model_speaks_the_note(self, monkeypatch, tmp_path):
        from kiln import printer_setup
        from kiln.cli import config as cli_config

        path = tmp_path / "config.yaml"
        path.write_text(
            "printers:\n  bench:\n    type: moonraker\n    host: 192.0.2.21\nactive_printer: bench\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(cli_config, "get_config_path", lambda: path)
        result = printer_setup.set_printer_model(SERVED_ID, printer_name="bench", config_path=path)
        assert result["success"] and result["applied"], result
        assert "Kilntest Served 200" in result["limits_note"] and result["limits_note"] in result["message"]

    def test_get_started_carries_a_note_minted_at_boot(self):
        pytest.importorskip("kiln.server")
        from kiln.server import get_started

        sp.warm_profile(SERVED_ID)
        account = get_started()["account"]
        assert "Kilntest Served 200" in account["limits_note"]
        assert "limits_note" not in get_started()["account"]

    def test_the_bridge_is_on_the_hosted_doors_roster(self):
        from kiln.served_answer import HOSTED_DOORS

        assert HOSTED_DOORS["kiln._pro_catalogue_bridge"][0] == "served_answer"


# ---------------------------------------------------------------------------
# With kiln-pro on this computer, nothing is asked and nothing is kept
# ---------------------------------------------------------------------------


class TestLocal:
    def test_with_kiln_pro_here_the_local_answer_is_final(self, monkeypatch):
        """On a kiln-pro install (the hosted server is one) the local reader
        IS the resolver, so a spelling it answers nothing for gets the floor
        even when a row it would prefix-match is already held: the coverage
        guard and the loaders then resolve a spelling identically, and no
        spelling reaches a held row uncounted.  A/B: with the local prefix
        fallback still running here, the second call returned the held row."""
        monkeypatch.setattr(bridge, "available", lambda: True)
        answers = {SERVED_ID: json.loads(json.dumps(RECORD))}
        monkeypatch.setattr(bridge, "_local", lambda key: (answers.get(key), None))
        assert sp.get_profile(SERVED_ID).max_hotend_temp == 280.0
        assert pi.get_printer_intel(SERVED_ID).hotend_type == "ptfe_lined"
        assert sp.get_profile(SERVED_ID + "x").id == "default"
        assert pi.get_printer_intel(SERVED_ID + "x").id == "default"

    def test_the_local_record_is_read_and_never_kept(self, monkeypatch):
        monkeypatch.setattr(bridge, "available", lambda: True)
        monkeypatch.setattr(bridge, "_local", lambda key: (json.loads(json.dumps(RECORD)), None))
        monkeypatch.setattr(bridge, "_served", lambda key: (_ for _ in ()).throw(AssertionError("asked the servers")))
        assert sp.get_profile(SERVED_ID).max_hotend_temp == 280.0
        assert pi.get_printer_intel(SERVED_ID).hotend_type == "ptfe_lined"
        assert bridge.kept_record(SERVED_ID) is None

    def test_a_local_reader_that_fails_is_a_miss_not_a_crash(self, monkeypatch):
        monkeypatch.setattr(bridge, "available", lambda: True)
        monkeypatch.setattr(bridge, "_local", lambda key: (None, Miss("unanswered", "NOT_ASKED", "boom")))
        assert sp.get_profile(SERVED_ID).id == "default"
        assert "can't read this printer's record" in (bridge.unanswered(SERVED_ID) or "")
