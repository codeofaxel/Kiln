"""A material's record reaches every public door, and a miss is worded once.

The bundled ``materials.json`` is a roster: it names each family's brand
profiles.  A brand's figures come through ``kiln._pro_material_bridge`` --
kiln-pro here, else served and kept on this computer, else the record last
kept, else the floor: the family's own range, the door saying why.  Each wire
is proved A/B: the same call with the wire cut gives the floor.  A record
carries no load figures: a part's load is asked for one part at a time
(``kiln._pro_load_bridge``, tests/test_pro_load_bridge.py), and a record that
arrives or was kept with a load block is read without it.

The records below belong to no brand -- temperatures chosen to differ from
the session's synthetic record -- so a figure arriving at a door proves THIS
record was read, not the session's or a real one.
"""

from __future__ import annotations

import json

import pytest

from kiln import _pro_material_bridge as bridge
from kiln import design_intelligence as di
from kiln.cost_estimator import resolve_material
from kiln.served_answer import Miss

#: The made-up figures: temperatures no family's range gives as its midpoint.
PLA_NOZZLE = ([197, 223], 213)
PLA_BED = ([47, 63], 57)
PETG_NOZZLE = ([227, 253], 243)
PETG_BED = ([73, 87], 81)


def _roster(family: str) -> list[str]:
    """The family's brand profile ids, as the roster ships them."""
    return sorted(di._get_kb().materials[family].get("brand_profiles") or {})


def _brand_block(family: str, nozzle: tuple[list[int], int], bed: tuple[list[int], int]) -> dict:
    return {
        pid: {
            "nozzle_temp_range_c": list(nozzle[0]),
            "nozzle_temp_optimal_c": nozzle[1],
            "bed_temp_range_c": list(bed[0]),
            "bed_temp_optimal_c": bed[1],
            "enclosure_required": False,
            "hardened_nozzle_required": False,
            "ams_compatible": True,
        }
        for pid in _roster(family)
    }


def _records() -> dict[str, dict]:
    """Three families' records.  Every other family is a settled "no row"."""
    return {
        "pla": {
            "material_id": "pla", "matched_by": "exact", "display_name": "PLA",
            "brands": _brand_block("pla", PLA_NOZZLE, PLA_BED),
        },
        "petg": {
            "material_id": "petg", "matched_by": "exact", "display_name": "PETG",
            "brands": _brand_block("petg", PETG_NOZZLE, PETG_BED),
        },
        "tpu": {
            "material_id": "tpu", "matched_by": "exact", "display_name": "TPU",
            "brands": {},
        },
    }


def _pla_profile() -> str:
    return _roster("pla")[0]


def _reset_loaders() -> None:
    di._reset_knowledge_base()
    bridge.forget()


@pytest.fixture(autouse=True)
def _no_kiln_pro_here(monkeypatch, tmp_path):
    """The install public CI is: no kiln-pro, records kept under a fresh home."""
    monkeypatch.setattr(bridge, "available", lambda: False)
    monkeypatch.setattr(bridge, "KEEP_DIR", tmp_path / "material_catalogue")
    _reset_loaders()
    yield
    _reset_loaders()


@pytest.fixture
def served(monkeypatch):
    """The servers answer with the records above; the fixture counts the asks."""
    calls: list[str] = []
    records = _records()

    def answer(key: str):
        calls.append(key)
        if key in records:
            return json.loads(json.dumps(records[key])), None
        return None, None  # a settled "no row"

    monkeypatch.setattr(bridge, "_served", answer)
    return calls


# ---------------------------------------------------------------------------
# The served record reaches every door
# ---------------------------------------------------------------------------


class TestServed:
    def test_the_figures_reach_the_brand_profile(self, served):
        pid = _pla_profile()
        profile = di.get_brand_filament_profile(pid)
        assert profile is not None and profile.has_figures()
        assert profile.nozzle_temp_range_c == PLA_NOZZLE[0] and profile.nozzle_temp_optimal_c == PLA_NOZZLE[1]
        assert profile.bed_temp_range_c == PLA_BED[0] and profile.bed_temp_optimal_c == PLA_BED[1]
        assert profile.ams_compatible is True
        assert served == ["pla"]

    def test_cut_the_wire_and_the_profile_is_names_only(self, served, monkeypatch):
        """A/B for the brand wire."""
        monkeypatch.setattr(di, "_material_record", lambda material_id: None)
        profile = di.get_brand_filament_profile(_pla_profile())
        assert profile is not None and not profile.has_figures()
        assert profile.nozzle_temp_optimal_c is None and profile.brand
        assert served == []

    def test_the_figures_reach_the_family_list(self, served):
        profiles = di.list_brand_filament_profiles(parent_material="petg")
        assert [p.profile_id for p in profiles] == _roster("petg")
        assert all(p.has_figures() and p.nozzle_temp_optimal_c == PETG_NOZZLE[1] for p in profiles)
        assert served == ["petg"]

    def test_the_figures_reach_resolve_filament(self, served):
        resolved = di.resolve_filament(_pla_profile())
        assert resolved.is_brand_specific is True
        assert resolved.material_id == "pla" and resolved.brand_profile_id == _pla_profile()
        assert resolved.nozzle_temp_optimal_c == PLA_NOZZLE[1] and resolved.nozzle_temp_range_c == PLA_NOZZLE[0]
        assert resolved.bed_temp_optimal_c == PLA_BED[1] and resolved.bed_temp_range_c == PLA_BED[0]
        assert not any("not in hand" in w for w in resolved.warnings)
        assert served == ["pla"]

    def test_cut_the_wire_and_the_filament_is_the_family_range_with_a_warning(self, served, monkeypatch):
        """A/B for the filament wire: the brand's NAME stays, the figures are
        the family's, and the warning says so."""
        monkeypatch.setattr(di, "_material_record", lambda material_id: None)
        resolved = di.resolve_filament(_pla_profile())
        nozzle_range, nozzle_opt, bed_range, bed_opt = di._family_floor_ranges("pla")
        assert resolved.is_brand_specific is False
        assert resolved.brand_profile_id == _pla_profile()
        # The FAMILY's own public range, never the cost table's generic default.
        assert resolved.nozzle_temp_optimal_c == nozzle_opt and resolved.nozzle_temp_range_c == nozzle_range
        assert resolved.bed_temp_optimal_c == bed_opt and resolved.bed_temp_range_c == bed_range
        [warning] = [w for w in resolved.warnings if "not in hand" in w]
        assert "range is used" in warning

    def test_one_ask_per_family_whatever_the_spelling_of_the_door(self, served):
        """A brand id and its family id are one fetch."""
        di.get_brand_filament_profile(_pla_profile())
        di.list_brand_filament_profiles(parent_material="pla")
        di.resolve_filament(_pla_profile().upper())
        di.get_brand_filament_profile(_pla_profile())
        assert served == ["pla"]

    def test_an_unfiltered_list_never_asks_the_servers(self, served):
        """A listing that walked the roster through the per-material wire
        would ask for every family at once."""
        profiles = di.list_brand_filament_profiles()
        assert profiles and all(not p.has_figures() for p in profiles)
        assert served == []

    def test_the_record_is_kept_on_this_computer(self, served):
        di.get_brand_filament_profile(_pla_profile())
        kept = bridge.kept_record("pla")
        assert kept is not None and kept["brands"][_pla_profile()]["nozzle_temp_optimal_c"] == PLA_NOZZLE[1]
        assert (bridge.KEEP_DIR / "pla.json").exists()


# ---------------------------------------------------------------------------
# No load figure arrives in a record, or stays in one kept on disk
# ---------------------------------------------------------------------------

#: A load block in the shape the servers sent until 2026-10-17, with figures
#: that belong to no material.
_OLD_LOAD_BLOCK = {"kilntest_grid": [[1, 2], [3, 4]], "kilntest_capacity": 1.5}


class TestNoLoadBlock:
    def test_a_served_answer_is_read_and_kept_without_its_load_block(self, monkeypatch, tmp_path):
        """The real served path (the session replaces ``_served``, so the
        bridge is loaded fresh from its source), its answer carrying a load
        block.  A/B: before 2026-10-17 the block was read and kept on disk."""
        import importlib.util

        import kiln.server as srv

        answer = {"success": True, "status": "ok", "known": True, **_records()["petg"], "load": dict(_OLD_LOAD_BLOCK)}
        monkeypatch.setattr(srv, "_pro_api_call", lambda *a, **k: json.loads(json.dumps(answer)))
        spec = importlib.util.spec_from_file_location("_fresh_material_bridge", bridge.__file__)
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)
        fresh.available = lambda: False
        fresh.KEEP_DIR = tmp_path / "kept"
        record = fresh.material_record("petg")
        assert record is not None and set(record) == {"material_id", "matched_by", "display_name", "brands"}
        assert record["brands"] == _records()["petg"]["brands"]
        on_disk = (fresh.KEEP_DIR / "petg.json").read_text(encoding="utf-8")
        assert "kilntest_grid" not in on_disk and '"load"' not in on_disk

    def test_a_record_kept_before_is_rewritten_without_its_load_block(self):
        import time

        old = {**_records()["pla"], "load": dict(_OLD_LOAD_BLOCK)}
        bridge.KEEP_DIR.mkdir(parents=True, exist_ok=True)
        kept_at = time.time() - 3600
        bridge._keep_path("pla").write_text(json.dumps({"kept_at": kept_at, "record": old}), encoding="utf-8")
        record = bridge.material_record("pla")
        assert record is not None and "load" not in record
        on_disk = json.loads(bridge._keep_path("pla").read_text(encoding="utf-8"))
        assert "load" not in on_disk["record"] and "kilntest_grid" not in json.dumps(on_disk)
        assert on_disk["kept_at"] == pytest.approx(kept_at)  # same age: asked again when it would have been


# ---------------------------------------------------------------------------
# An unknown material is a settled answer
# ---------------------------------------------------------------------------


class TestUnknown:
    def test_no_row_is_settled_and_is_not_asked_again(self, served):
        assert bridge.material_record("acme_resin") is None
        assert bridge.why_unanswered("acme_resin") == "" and bridge.unanswered("acme_resin") is None
        bridge.material_record("acme_resin")
        bridge.material_record("ACME_RESIN")
        assert served == ["acme_resin"]

    def test_no_row_is_never_kept_on_disk(self, served):
        bridge.material_record("acme_resin")
        assert bridge.kept_record("acme_resin") is None
        assert not (bridge.KEEP_DIR / "acme_resin.json").exists()

    def test_a_family_with_no_row_is_names_only_with_nothing_to_say(self, served):
        """abs is on the roster and the catalogue has no row for it: the
        names, no figures, and no miss to word."""
        profiles = di.list_brand_filament_profiles(parent_material="abs")
        assert profiles and all(not p.has_figures() for p in profiles)
        assert di.brand_figures_unanswered("abs") is None
        assert served == ["abs"]

    def test_a_material_off_the_roster_never_reaches_the_wire(self, served):
        assert di.get_brand_filament_profile("nobody_makes_this") is None
        assert served == []


# ---------------------------------------------------------------------------
# Misses: offline, signed out
# ---------------------------------------------------------------------------


class TestOffline:
    def test_a_kept_record_answers_offline(self, served, monkeypatch):
        di.get_brand_filament_profile(_pla_profile())
        _reset_loaders()
        monkeypatch.setattr(bridge, "_served", lambda key: (_ for _ in ()).throw(ConnectionError("down")))
        profile = di.get_brand_filament_profile(_pla_profile())
        assert profile is not None and profile.nozzle_temp_optimal_c == PLA_NOZZLE[1]
        assert bridge.why_unanswered("pla") == ""  # fresh: nothing was asked

    def test_a_stale_kept_record_still_answers_when_the_ask_fails(self, served, monkeypatch):
        di.get_brand_filament_profile(_pla_profile())
        _reset_loaders()
        monkeypatch.setattr(bridge, "FRESH_FOR_S", 0.0)
        monkeypatch.setattr(bridge, "_served", lambda key: (None, Miss("unanswered", "SERVER_UNREACHABLE")))
        profile = di.get_brand_filament_profile(_pla_profile())
        assert profile is not None and profile.nozzle_temp_optimal_c == PLA_NOZZLE[1]
        assert bridge.why_unanswered("pla") == "unanswered"

    def test_never_kept_and_offline_is_the_floor_and_says_so(self, monkeypatch):
        monkeypatch.setattr(bridge, "_served", lambda key: (None, Miss("offline", "SERVER_UNREACHABLE")))
        resolved = di.resolve_filament(_pla_profile())
        assert resolved.is_brand_specific is False
        assert resolved.nozzle_temp_optimal_c == di._family_floor_ranges("pla")[1]
        [warning] = [w for w in resolved.warnings if "not in hand" in w]
        assert "because Kiln can't get this material's figures" in warning
        assert bridge.why_unanswered("pla") == "offline"


class TestSignedOut:
    @pytest.fixture(autouse=True)
    def _signed_out(self, signed_out_material_install):
        """The posture under test, requested on purpose: records are the
        session default."""
        _reset_loaders()
        yield
        _reset_loaders()

    def test_a_brand_is_names_only_and_the_warning_names_the_sign_out(self):
        resolved = di.resolve_filament(_pla_profile())
        assert resolved.is_brand_specific is False and resolved.brand_profile_id == _pla_profile()
        [warning] = [w for w in resolved.warnings if "not in hand" in w]
        assert "signed out" in warning and "sign in" in warning
        assert bridge.why_unanswered("pla") == "signed_out"
        assert "signed out" in (di.brand_figures_unanswered("pla") or "")

    def test_a_signed_out_miss_is_not_asked_again_on_every_call(self, monkeypatch):
        calls: list[str] = []

        def refused(key):
            calls.append(key)
            return None, Miss("signed_out", "KILN_ACCOUNT_NOT_PAIRED")

        monkeypatch.setattr(bridge, "_served", refused)
        for _ in range(5):
            di.get_brand_filament_profile(_pla_profile())
            di.resolve_filament(_pla_profile())
        assert calls == ["pla"]

    def test_the_bridge_is_on_the_hosted_doors_roster(self):
        from kiln.served_answer import HOSTED_DOORS

        assert HOSTED_DOORS["kiln._pro_material_bridge"][0] == "served_answer"


# ---------------------------------------------------------------------------
# With kiln-pro on this computer, nothing is asked and nothing is kept
# ---------------------------------------------------------------------------


class TestLocal:
    def test_a_reader_with_no_file_is_not_available(self, monkeypatch):
        """A pip-installed kiln-pro has the reader and no catalogue file; the
        bridge must go to the served door, not hand every material the floor."""
        import importlib.util
        import sys
        import types

        reader = types.ModuleType("kiln_pro.material_catalogue")
        reader.catalogue_present = lambda: False
        monkeypatch.setitem(sys.modules, "kiln_pro.material_catalogue", reader)
        # The module fixture stubs ``available``; read the real one off a
        # fresh load of the bridge's source.
        spec = importlib.util.spec_from_file_location("_fresh_material_bridge", bridge.__file__)
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)
        assert fresh.available() is False
        reader.catalogue_present = lambda: True
        assert fresh.available() is True

    def test_the_local_record_is_read_and_never_kept(self, monkeypatch):
        monkeypatch.setattr(bridge, "available", lambda: True)
        records = _records()
        monkeypatch.setattr(bridge, "_local", lambda key: (json.loads(json.dumps(records.get(key))) if key in records else None, None))
        monkeypatch.setattr(bridge, "_served", lambda key: (_ for _ in ()).throw(AssertionError("asked the servers")))
        profile = di.get_brand_filament_profile(_pla_profile())
        assert profile is not None and profile.nozzle_temp_optimal_c == PLA_NOZZLE[1]
        assert di.resolve_filament(_roster("petg")[0]).nozzle_temp_optimal_c == PETG_NOZZLE[1]
        assert bridge.kept_record("pla") is None and bridge.kept_record("petg") is None

    def test_with_kiln_pro_here_every_family_is_listed_with_figures(self, monkeypatch):
        """The unfiltered list reads every family when the catalogue is on
        this computer: no server is asked, so nothing is spent."""
        monkeypatch.setattr(bridge, "available", lambda: True)
        records = _records()
        monkeypatch.setattr(bridge, "_local", lambda key: (json.loads(json.dumps(records.get(key))) if key in records else None, None))
        # Keyed by family AND id: one product can sit in two families' rosters.
        profiles = {(p.parent_material, p.profile_id): p for p in di.list_brand_filament_profiles()}
        assert all(profiles[("pla", pid)].has_figures() for pid in _roster("pla"))
        assert all(profiles[("petg", pid)].has_figures() for pid in _roster("petg"))
        assert all(not profiles[("abs", pid)].has_figures() for pid in _roster("abs"))

    def test_a_local_reader_that_fails_is_a_miss_not_a_crash(self, monkeypatch):
        monkeypatch.setattr(bridge, "available", lambda: True)
        monkeypatch.setattr(bridge, "_local", lambda key: (None, Miss("unanswered", "NOT_ASKED", "boom")))
        profile = di.get_brand_filament_profile(_pla_profile())
        assert profile is not None and not profile.has_figures()
        assert "can't read this material's record" in (bridge.unanswered("pla") or "")


class TestARefusalDropsTheKeptRecord:
    """A lapsed plan does not keep the figures: a kept record is read on an
    offline or silent miss, never on a refusal (A/B: before, a 30-day-old
    kept record answered a TIER_REQUIRED refusal with the figures)."""

    def _keep_old(self, bridge, key: str, days: float) -> None:
        import json
        import time

        record = {"material_id": key, "matched_by": "exact", "display_name": key.upper(),
                  "brands": {"kilntest_old": {"nozzle_temp_optimal_c": 999}}}
        bridge.KEEP_DIR.mkdir(parents=True, exist_ok=True)
        bridge._keep_path(key).write_text(
            json.dumps({"kept_at": time.time() - days * 86400, "record": record}), encoding="utf-8"
        )

    def test_a_tier_refusal_drops_a_stale_kept_record(self, monkeypatch, tmp_path):
        from kiln import _pro_material_bridge as bridge
        from kiln.served_answer import Miss

        monkeypatch.setattr(bridge, "available", lambda: False)
        monkeypatch.setattr(bridge, "KEEP_DIR", tmp_path / "material_catalogue")
        monkeypatch.setattr(bridge, "_served", lambda key: (None, Miss("refused", "TIER_REQUIRED", "needs Kiln Pro")))
        bridge.forget()
        self._keep_old(bridge, "petg", days=30)
        assert bridge.material_record("petg") is None
        assert bridge.kept_record("petg") is None, "the kept record outlived the refusal"
        assert bridge.unanswered("petg") == "the brand's own tune is part of Kiln Pro"

    def test_an_offline_miss_still_reads_a_stale_kept_record(self, monkeypatch, tmp_path):
        from kiln import _pro_material_bridge as bridge
        from kiln.served_answer import Miss

        monkeypatch.setattr(bridge, "available", lambda: False)
        monkeypatch.setattr(bridge, "KEEP_DIR", tmp_path / "material_catalogue")
        monkeypatch.setattr(bridge, "_served", lambda key: (None, Miss("offline", "OFFLINE")))
        bridge.forget()
        self._keep_old(bridge, "petg", days=30)
        assert bridge.material_record("petg")["brands"]["kilntest_old"]["nozzle_temp_optimal_c"] == 999
