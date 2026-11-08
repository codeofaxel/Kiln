"""A print refused for a filament the printer cannot melt says what to do next.

A refusal that says only no and why ("PEEK needs at least 380°C at the
nozzle") leaves a customer's install (one without kiln-pro) with nothing
else at any plan.  So, on every plan, the refusal lists the filaments the
printer runs on its stock hotend (``runs_here``, from the public
compatibility table,
filtered by the same temperature check, so nothing listed is refused next).
With Kiln Pro it also carries the swap, asked through Kiln's servers
(``kiln._pro_print_fix_bridge``, the ``material_pick`` door in refused-print
mode), with the kept version being printed so the servers can check the swap
against the part's saved goal.  Below Kiln Pro it says once what that adds;
a paying caller whose answer did not come back is told why, never pitched.

The plan is read the way a plain install reads it (``kiln.account_plan``).
The servers are a stand-in that records what was asked, so nothing reaches
the network.
"""

from __future__ import annotations

import sys
import time

import pytest

PRINTER = "bambu_a1"
REFUSED = "peek"

_ENRICHMENT = {
    "tier": "pro",
    "fixes": ["material_swap"],
    "material_swap": {
        "available": True,
        "blocked_material": REFUSED,
        "candidates": [{"material_id": "polycarbonate", "why": "Polycarbonate (PC) melts at 270C."}],
        "goal": {"goal_id": "b1", "needs": ["high-temperature use"]},
    },
    "headline": "This printer can't melt that material — here's what to print it in instead.",
}


@pytest.fixture
def plain_install(monkeypatch):
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


@pytest.fixture
def account(monkeypatch):
    """The plan this machine's saved sign-in holds, read as a plain install reads it."""
    import kiln.auth_session as auth_session
    import kiln.licensing as licensing
    from kiln import account_plan

    monkeypatch.setattr(licensing, "LicenseTier", account_plan.LicenseTier, raising=False)
    monkeypatch.setattr(licensing, "check_tier", account_plan.check_tier, raising=False)
    monkeypatch.setattr(account_plan, "_ask_account", lambda: None)
    monkeypatch.setattr(account_plan, "_last_ask_at", 0.0)
    monkeypatch.setattr(account_plan, "_last_ask_answered", True)

    def signed_in(tier: str | None) -> None:
        stored: dict = {}
        if tier is not None:
            stored = {"access_token": "tok", "refresh_token": "ref", "tier": tier,
                      "email": "maker@example.com", "plan_checked_at": time.time()}
        monkeypatch.setattr(auth_session, "_read_tokens", lambda: dict(stored))

    return signed_in


@pytest.fixture
def servers(monkeypatch):
    """Kiln's servers: ``material_pick`` answers ``answer`` (or raises it),
    every other door is offline.  ``asked`` records each material_pick call."""
    import kiln.server as server

    state = {"answer": {"success": True, "status": "ok", "known": True, "enrichment": _ENRICHMENT}, "asked": []}

    def fake(tool, **kw):
        if tool != "material_pick":
            raise ConnectionError("offline in this test")
        state["asked"].append(kw)
        if isinstance(state["answer"], BaseException):
            raise state["answer"]
        return state["answer"]

    monkeypatch.setattr(server, "_pro_api_call", fake)
    return state


def _refuse() -> dict:
    from kiln.printers.print_gate import check_material_temp

    block = check_material_temp(PRINTER, REFUSED)
    assert block is not None and block["code"] == "MATERIAL_EXCEEDS_HOTEND", block
    return block


class TestEveryPlanIsToldWhatThePrinterRuns:
    def test_the_list_is_the_compatible_filaments_the_gate_also_passes(self, plain_install, account, servers):
        from kiln.design_intelligence import check_printer_material_compatibility
        from kiln.printers.print_gate import material_temp_blocks

        account(None)
        runs = _refuse()["runs_here"]
        table = check_printer_material_compatibility(PRINTER).materials
        assert runs["materials"], runs
        for material_id in runs["materials"]:
            assert table[material_id]["status"] == "compatible", material_id
            assert not material_temp_blocks(PRINTER, material_id), material_id
        assert REFUSED not in runs["materials"]
        # Upgrade-needing filaments are not "runs on its stock hotend".
        assert not any(table[m]["status"] == "needs_upgrade" for m in runs["materials"])

    def test_the_sentence_names_families_not_the_catalogue(self, plain_install, account, servers):
        account(None)
        summary = _refuse()["runs_here"]["summary"]
        assert summary.startswith("On its stock hotend this printer runs PLA, PETG"), summary
        assert "PLA+" not in summary and "Silk PLA" not in summary, summary
        assert "variants" in summary

    def test_signed_out_a_known_printer_is_told_the_floors_list_as_the_floors(self, plain_install, monkeypatch):
        """No record in hand (signed out): the printer is held to the generic
        floor, and the refusal lists what the FLOOR runs, said as such --
        never a dead end, never the floor passed off as this printer's own."""
        from kiln import _pro_catalogue_bridge as bridge
        from kiln import design_intelligence as di
        from kiln import printer_intelligence as pi
        from kiln import safety_profiles as sp
        from kiln.printers.print_gate import _filaments_that_run
        from kiln.served_answer import Miss

        def forget_everything():
            sp._cache.clear()
            sp._loaded = False
            pi._reset_caches()
            di._reset_knowledge_base()
            bridge.forget()

        monkeypatch.setattr(bridge, "available", lambda: False)
        monkeypatch.setattr(bridge, "_served", lambda key: (None, Miss("signed_out", "KILN_ACCOUNT_NOT_PAIRED")))
        monkeypatch.setattr(bridge, "_kept", lambda key: None)
        forget_everything()
        try:
            runs = _filaments_that_run(PRINTER, REFUSED)
        finally:
            monkeypatch.undo()
            forget_everything()
        assert runs is not None and runs["materials"], runs
        assert runs["held_to_floor"] is True
        assert runs["summary"].startswith(
            "Until Kiln has this printer's record (sign in free), it is held to the generic floor, "
            "which runs PLA, PETG"
        ), runs["summary"]

    def test_an_unknown_printer_gets_no_list(self):
        from kiln.printers.print_gate import _filaments_that_run

        assert _filaments_that_run("no_such_printer_xyz", REFUSED) is None


class TestBelowKilnPro:
    @pytest.mark.parametrize("tier", [None, "free"])
    def test_it_says_once_what_kiln_pro_adds_and_asks_nothing(self, plain_install, account, servers, tier):
        account(tier)
        block = _refuse()
        assert block.get("enrichment") is None
        note = block["pro_adds"]
        assert "closest to PEEK" in note and "goal" in note, note
        assert note.count("Kiln Pro") == 1, note
        assert "pro_fix_missed" not in block
        assert servers["asked"] == []


class TestWithKilnPro:
    def test_the_swap_comes_from_the_servers_with_what_names_the_goal(self, plain_install, account, servers, tmp_path, monkeypatch):
        import kiln.auto_record_hook as hook
        from kiln.printers.print_gate import check_material_temp

        account("pro")
        part = tmp_path / "mount.stl"
        part.write_text("solid x\nendsolid x\n")
        monkeypatch.setattr(hook, "version_of_print_file", lambda path: "v-mount-2" if path == str(part) else None)
        block = check_material_temp(PRINTER, REFUSED, job_path=str(part))
        assert block["enrichment"] == _ENRICHMENT
        assert "pro_adds" not in block and "pro_fix_missed" not in block
        assert block["runs_here"]["materials"]
        (asked,) = servers["asked"]
        assert asked["blocked_material"] == REFUSED
        assert asked["printer_model"] == PRINTER
        assert asked["version_id"] == "v-mount-2"

    def test_an_answer_that_did_not_come_back_is_said_never_pitched(self, plain_install, account, servers):
        account("pro")
        servers["answer"] = ConnectionError("offline")
        block = _refuse()
        assert block.get("enrichment") is None
        assert "pro_adds" not in block
        missed = block["pro_fix_missed"]
        assert "Kiln's servers" in missed and "Kiln Pro" in missed, missed
        assert block["runs_here"]["materials"], "the free answer still stands"

    def test_nothing_to_add_adds_nothing(self, plain_install, account, servers):
        account("pro")
        servers["answer"] = {"success": True, "status": "ok", "known": False, "enrichment": None}
        block = _refuse()
        assert len(servers["asked"]) == 1, "a Pro plan's refusal asks the servers"
        assert block.get("enrichment") is None
        assert "pro_adds" not in block and "pro_fix_missed" not in block
        assert block["runs_here"]["materials"], "the free answer still stands"


class TestTheSliceDoorCarriesIt:
    def test_the_slice_refusal_keeps_every_part_of_the_fix(self, plain_install, account, servers):
        from kiln.plugins.slicer_tools import _gate_error_response, _material_temp_block

        account(None)
        response = _gate_error_response(_material_temp_block(PRINTER, REFUSED, "/tmp/mount.stl"))
        assert response["runs_here"]["materials"]
        assert "closest to PEEK" in response["pro_adds"]


class TestTooBig:
    def test_a_model_is_cut_and_a_sliced_file_goes_back_to_its_model(self):
        from kiln.printers.print_gate import _suggestions

        (model,) = _suggestions("EXCEEDS_BED", None, "/x/bracket.stl")
        (sliced,) = _suggestions("EXCEEDS_BED", None, "/x/bracket.gcode")
        assert "split_mesh_to_fit" in model
        assert "split_mesh_to_fit" in sliced and "model it came from" in sliced
