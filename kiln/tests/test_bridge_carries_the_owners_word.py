"""A relayed print carries the owner's word only on their own approval.

The word to print anyway an abrasive filament through a nozzle that is not
wear-resistant, and the yes to nozzles of different sizes for one print, reach
the slice from the web only on the authority block of the person's own
approval of that print.  Never as an argument, never under a delegation, and
never when someone else starts the print under that approval.
"""

from __future__ import annotations

import inspect

import pytest

from kiln.bridge_client import BRIDGE_REFUSED_CODE, handle_relay_request, owner_words
from kiln.slicer import owner_asks


def _relay(args: dict, recorded: list) -> dict:
    def call_tool(name, given):
        recorded.append((name, dict(given)))
        return {"success": True}

    return handle_relay_request(
        {"request_id": "r1", "tool_name": "slice_and_print", "args": args},
        call_tool=call_tool,
        fetch_artifact=lambda token: "/tmp/kiln-relay-test/model.stl",
    )


def _approval(**words) -> dict:
    return {"kind": "approval", "id": "pa_1", "grantor": "account:1", "said_go_by": "account:1", **words}


def _sent(authority: dict) -> dict:
    recorded: list = []
    resp = _relay({"cloud_artifact_token": "tok-1", "printer_name": "garage", "print_authority": authority}, recorded)
    assert resp["ok"] is True, resp
    return recorded[0][1]


class TestTheOwnersApproval:
    def test_print_anyway_reaches_the_slice(self):
        given = _sent(_approval(soft_nozzle_confirmed=True))
        assert given["soft_nozzle_confirmed"] is True
        assert "mixed_nozzle_experiment" not in given

    def test_the_experiment_reaches_the_slice(self):
        given = _sent(_approval(mixed_nozzle_experiment=True))
        assert given["mixed_nozzle_experiment"] is True
        assert "soft_nozzle_confirmed" not in given

    def test_an_approval_with_neither_word_adds_nothing(self):
        given = _sent(_approval())
        assert "soft_nozzle_confirmed" not in given and "mixed_nozzle_experiment" not in given

    @pytest.mark.parametrize("value", ["true", 1, "yes", None])
    def test_only_true_itself_is_a_word(self, value):
        given = _sent(_approval(soft_nozzle_confirmed=value, mixed_nozzle_experiment=value))
        assert "soft_nozzle_confirmed" not in given and "mixed_nozzle_experiment" not in given


class TestNobodyElseSaysIt:
    def test_not_when_an_agent_starts_the_print_under_the_approval(self):
        block = _approval(soft_nozzle_confirmed=True, mixed_nozzle_experiment=True)
        block["said_go_by"] = "agent:claude"
        given = _sent(block)
        assert "soft_nozzle_confirmed" not in given and "mixed_nozzle_experiment" not in given

    def test_not_under_a_delegation(self):
        block = {
            "kind": "delegation", "id": "dl_1", "grantor": "account:1", "said_go_by": "account:1",
            "soft_nozzle_confirmed": True, "mixed_nozzle_experiment": True,
        }
        given = _sent(block)
        assert "soft_nozzle_confirmed" not in given and "mixed_nozzle_experiment" not in given

    def test_not_from_a_block_with_no_grantor(self):
        assert owner_words({"kind": "approval", "id": "pa_1", "soft_nozzle_confirmed": True}) == {}

    @pytest.mark.parametrize("word", ["soft_nozzle_confirmed", "mixed_nozzle_experiment"])
    def test_never_as_an_argument(self, word):
        recorded: list = []
        resp = _relay({"cloud_artifact_token": "tok-1", word: True, "print_authority": _approval()}, recorded)
        assert resp["ok"] is False
        assert resp["error"]["code"] == BRIDGE_REFUSED_CODE
        assert recorded == []

    def test_only_a_print_takes_them(self):
        recorded: list = []

        def call_tool(name, given):
            recorded.append(dict(given))
            return {"success": True}

        handle_relay_request(
            {
                "request_id": "r2", "tool_name": "printer_status",
                "args": {"printer_name": "garage"},
            },
            call_tool=call_tool,
            fetch_artifact=lambda token: "",
        )
        assert recorded == [{"printer_name": "garage"}]


def test_the_words_are_the_print_tools_own_parameters():
    from kiln import server

    params = set(inspect.signature(server.mcp._tool_manager._tools["slice_and_print"].fn).parameters)
    assert set(owner_asks(True, None, True)) <= params
