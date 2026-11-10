"""The code on this computer's screen answers an order or a publish the
servers hold for the person's yes.

Covers :mod:`kiln.action_yes` and the action half of :mod:`kiln.screen_code`
through the doors a person meets: the servers' wait handed back by a served
tool and by the order door, the code relayed through ``give_print_code``,
and what the servers are then told.  The servers themselves are stood in
for at :func:`kiln.bridge_client.confirm_on_screen`.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiln import action_yes, bridge_client, screen_code

_ORDER_WAIT = {
    "success": False, "code": "APPROVAL_REQUIRED",
    "error": "Nothing was ordered: only the person can place an order.",
    "question": "Order 1 copy of knight.stl from the print service, quoted at $23.10, sent to Ada, Austin TX, US?",
    "approve_at": "https://kiln3d.com/orders/approve?quote=q-1",
    "challenge_id": "sealed-order-1",
}
_PUBLISH_WAIT = {
    "success": False, "code": "APPROVAL_REQUIRED",
    "error": "Nothing has changed yet: only the person can make a design public.",
    "question": "Make “My Mug” public?\nAnyone could see it.",
    "approve_at": "https://kiln3d.com/designs/dgn-mug/settings?allow=publish",
    "challenge_id": "sealed-publish-1",
}


def _tools() -> dict[str, Any]:
    from kiln.plugins.safety_tools import _SafetyToolsPlugin

    tools: dict[str, Any] = {}

    class FakeMCP:
        def tool(self_mcp, **_kwargs):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

    _SafetyToolsPlugin().register(FakeMCP())
    return tools


@pytest.fixture
def screen(monkeypatch):
    """A machine with a screen, the relay tool, and the servers' ear."""
    import kiln.server as srv

    monkeypatch.setattr(srv, "_check_auth", lambda *_a, **_k: None)
    monkeypatch.setattr(srv, "_audit", lambda *_a, **_k: None)
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    screen_code._reset_for_tests()
    shown: list[screen_code.Issued] = []
    told: list[str] = []
    monkeypatch.setattr(screen_code, "_show_hook", lambda issued: shown.append(issued) or True)
    monkeypatch.setattr(screen_code, "screen_available", lambda: True)
    monkeypatch.setattr(screen_code, "MIN_READ_S", 0.0)
    monkeypatch.setattr(bridge_client, "confirm_on_screen", lambda ticket: told.append(ticket) or (True, "", ""))
    yield _tools()["give_print_code"], shown, told
    screen_code._reset_for_tests()


def test_a_waiting_order_shows_a_code_and_the_code_tells_the_servers(screen) -> None:
    give, shown, told = screen
    reply = action_yes.with_offer("fulfillment_order", dict(_ORDER_WAIT))
    assert reply["code_shown"] == "shown" and "give_print_code" in reply["code_next"]
    assert reply["approve_at"] == _ORDER_WAIT["approve_at"], "the page is still named"
    code = shown[0].code
    assert code not in str(reply), "the code is on the screen, never in a reply"
    assert "sealed-order-1" not in str(reply), "the servers' request stays with this install"
    title, subtitle, message = screen_code.banner_text(shown[0])
    assert code in title and "knight.stl" in subtitle and "$23.10" in subtitle and "place this order" in message

    out = give(code)
    assert out["success"] is True and "fulfillment_order again" in out["next"]
    assert told == ["sealed-order-1"]
    assert give(code)["error"]["code"] == "NO_CODE", "one code, one yes"
    assert told == ["sealed-order-1"]


def test_a_waiting_publish_shows_a_code_naming_the_design(screen) -> None:
    give, shown, told = screen
    reply = action_yes.with_offer("set_design_public", dict(_PUBLISH_WAIT))
    assert reply["code_shown"] == "shown"
    _title, subtitle, message = screen_code.banner_text(shown[0])
    assert subtitle == "Make “My Mug” public?" and "make this design public" in message
    assert give(shown[0].code)["success"] is True and told == ["sealed-publish-1"]


def test_a_wrong_code_tells_the_servers_nothing(screen) -> None:
    give, shown, told = screen
    action_yes.with_offer("fulfillment_order", dict(_ORDER_WAIT))
    wrong = "0000" if shown[0].code != "0000" else "1111"
    assert give(wrong)["error"]["code"] == "WRONG_CODE"
    assert told == []


def test_an_order_code_never_starts_a_print_and_a_print_code_never_orders(screen) -> None:
    give, shown, told = screen
    action_yes.with_offer("fulfillment_order", dict(_ORDER_WAIT))
    order = shown[0]
    assert give(order.code + " 2h")["error"]["code"] == "VALIDATION_ERROR", "an order has no 'for a while'"
    assert told == []
    # Answered, an action's code is spent on the servers' ask, so nothing is
    # left for a print's start to take.
    assert give(order.code)["success"] is True
    assert screen_code.take_answer(order.file_name, order.printer_name) is None
    assert screen_code.take_answer(order.file_name, order.printer_name, tool="fulfillment_order") is None, (
        "the answered code is spent by telling the servers, not left to be taken again"
    )

    screen_code.issue(tool="start_print", file_name="benchy.gcode", file_sha256="f" * 64, printer_name="a1")
    out = give(shown[-1].code)
    assert out["success"] is True and out["approved"]["file"] == "benchy.gcode"
    assert told == ["sealed-order-1"], "a print's code told the servers nothing"
    assert screen_code.take_answer("benchy.gcode", "a1", tool="fulfillment_order") is None
    assert screen_code.take_answer("benchy.gcode", "a1") is not None


def test_asking_again_keeps_one_code_and_answers_the_newest_request(screen) -> None:
    give, shown, told = screen
    action_yes.with_offer("fulfillment_order", dict(_ORDER_WAIT))
    again = action_yes.with_offer("fulfillment_order", {**_ORDER_WAIT, "challenge_id": "sealed-order-2"})
    assert again["code_shown"] == "shown" and len(shown) == 1
    give(shown[0].code)
    assert told == ["sealed-order-2"]


def test_a_question_that_changed_is_a_new_code_on_a_new_banner(screen) -> None:
    """The code typed is for the words shown with it: an order whose price
    moved between two asks never rides the first banner's code."""
    give, shown, told = screen
    action_yes.with_offer("fulfillment_order", dict(_ORDER_WAIT))
    dearer = {**_ORDER_WAIT, "challenge_id": "sealed-order-dearer",
              "question": _ORDER_WAIT["question"].replace("$23.10", "$230.10")}
    action_yes.with_offer("fulfillment_order", dearer)
    assert len(shown) == 2 and shown[0].code != shown[1].code
    assert "$23.10" in shown[0].subject and "$230.10" in shown[1].subject
    assert shown[0].ticket == "sealed-order-1", "the first banner's code still answers only what it showed"
    give(shown[0].code)
    assert told == ["sealed-order-1"]


def test_a_yes_the_servers_did_not_record_is_said_and_names_what_to_do(screen, monkeypatch) -> None:
    give, shown, _told = screen
    for why, words in (("offline", "could not be reached"), ("challenge_unknown", "has run out"), ("signed_out", "not signed in")):
        monkeypatch.setattr(bridge_client, "confirm_on_screen", lambda _t, why=why: (False, why, ""))
        action_yes.with_offer("set_design_public", {**_PUBLISH_WAIT, "challenge_id": why})
        out = give(shown[-1].code)
        assert out["success"] is False and out["error"]["code"] == "YES_NOT_RECORDED" and out["why"] == why
        assert words in out["error"]["message"] and "set_design_public again" in out["error"]["message"]


def test_the_wait_is_read_in_either_shape_a_door_hands_it_back(screen) -> None:
    _give, shown, _told = screen
    nested = {
        "success": False, "error": {"code": "APPROVAL_REQUIRED", "message": "Nothing has changed yet."},
        "details": {k: _PUBLISH_WAIT[k] for k in ("question", "approve_at", "challenge_id")},
    }
    assert action_yes.with_offer("set_design_public", nested)["code_shown"] == "shown"
    assert shown[0].ticket == "sealed-publish-1"


def test_no_code_is_offered_where_there_is_nothing_to_answer_or_no_screen(screen, monkeypatch) -> None:
    _give, shown, _told = screen
    no_request = {k: v for k, v in _ORDER_WAIT.items() if k != "challenge_id"}
    assert action_yes.with_offer("fulfillment_order", dict(no_request)) == no_request
    other_refusal = {**_ORDER_WAIT, "code": "SHIPPING_NOT_CONFIRMED"}
    assert action_yes.with_offer("fulfillment_order", dict(other_refusal)) == other_refusal
    assert action_yes.with_offer("generate_coaster", dict(_ORDER_WAIT)) == _ORDER_WAIT, "only the two doors"
    monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
    assert action_yes.with_offer("fulfillment_order", dict(_ORDER_WAIT)) == _ORDER_WAIT
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT")
    monkeypatch.setattr(screen_code, "screen_available", lambda: False)
    assert action_yes.with_offer("fulfillment_order", dict(_ORDER_WAIT)) == _ORDER_WAIT
    assert shown == []


def test_the_order_door_offers_the_code_beside_the_page(screen) -> None:
    _give, shown, _told = screen
    from kiln import _pro_order_bridge as served
    from kiln.plugins import fulfillment_tools

    waiting = {k: _ORDER_WAIT[k] for k in ("question", "approve_at", "challenge_id")}
    answer = served.Answer(
        miss=served.Miss("refused", "APPROVAL_REQUIRED", _ORDER_WAIT["error"]),
        details=dict(waiting), waiting=waiting, said=_ORDER_WAIT["error"],
    )
    reply = fulfillment_tools._served_waiting(answer)
    assert reply["code_shown"] == "shown" and len(shown) == 1
    assert any("code Kiln just showed" in line for line in reply["tell_the_person"])
    assert any(_ORDER_WAIT["approve_at"] in line for line in reply["tell_the_person"])
    assert shown[0].code not in str(reply) and "sealed-order-1" not in str(reply)


def test_the_servers_are_asked_with_the_request_alone(monkeypatch) -> None:
    sent: list[tuple] = []
    monkeypatch.setattr(bridge_client, "account_bearer", lambda: "tok")
    monkeypatch.setattr(
        bridge_client, "_account_call",
        lambda method, path, bearer, **kw: sent.append((method, path, kw.get("json"))) or (200, {"approval": {"id": "apv-1"}}),
    )
    assert bridge_client.confirm_on_screen("sealed-1") == (True, "", "")
    assert sent == [("POST", "/approve/confirm-on-screen", {"challenge_id": "sealed-1"})]
    monkeypatch.setattr(bridge_client, "_account_call", lambda *a, **k: (409, {"error": "challenge_spent", "message": "Already answered."}))
    assert bridge_client.confirm_on_screen("sealed-1") == (False, "challenge_spent", "Already answered.")
    monkeypatch.setattr(bridge_client, "_account_call", lambda *a, **k: None)
    assert bridge_client.confirm_on_screen("sealed-1")[1] == "offline"
    monkeypatch.setattr(bridge_client, "account_bearer", lambda: "")
    assert bridge_client.confirm_on_screen("sealed-1")[1] == "signed_out"


def test_the_real_publish_stub_offers_the_code_when_the_servers_wait(screen, tmp_path, monkeypatch) -> None:
    """The door a plain install has for making a design public is the stub
    built from the shipped manifest; the servers' wait comes back through it
    with the code offer beside it, and any other answer comes back as it was."""
    import asyncio
    import inspect

    from kiln import server

    _give, shown, _told = screen
    tools: dict[str, Any] = {}

    class FakeMCP:
        def tool(self, **_kwargs):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn

            return deco

    server._register_pro_tool_stubs(FakeMCP())
    stub = tools["set_design_public"]

    def call() -> Any:
        out = stub(design_id="dgn-mug", public=True, preview_token="t")
        return asyncio.run(out) if inspect.isawaitable(out) else out

    monkeypatch.setattr(server, "_pro_api_call", lambda *_a, **_k: dict(_PUBLISH_WAIT))
    reply = call()
    assert reply["code_shown"] == "shown" and reply["approve_at"] == _PUBLISH_WAIT["approve_at"]
    assert len(shown) == 1 and shown[0].tool == "set_design_public" and shown[0].code not in str(reply)

    done = {"success": True, "public": True}
    monkeypatch.setattr(server, "_pro_api_call", lambda *_a, **_k: dict(done))
    assert call() == done and len(shown) == 1
