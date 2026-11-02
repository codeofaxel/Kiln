"""A plain install orders through Kiln's servers, and every miss is worded.

From 2026-03-20 to 2026-10-31 every ordering door on a plain ``pip install
kiln3d`` refused: the order service had moved to kiln-pro, and the 2026-10-05
fix worded the refusal instead of serving the feature, while the servers'
``/api/fulfillment/*`` routes had been complete since 2026-10-04.  These
tests run the DOORS -- the ``fulfillment_*`` tools, ``compare_print_options``,
the ``kiln order`` commands, ``kiln compare-cost`` -- on a plain install
(``tests._order_service.remove``) signed in to an account, with the
servers stood in for at ``httpx.request``, and read what the person gets.

A/B: on the pre-fix tree every door test here goes red (the door answers
``NOT_AVAILABLE`` and never asks the servers), which is the proof these
tests test the fix and not themselves.
"""

from __future__ import annotations

import errno
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from kiln import _pro_order_bridge as bridge
from kiln.fulfillment_service import NOT_INCLUDED
from tests import _order_service as order_service
from tests._signed_in import sign_in, sign_out

ADDRESS = {
    "first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com", "phone": "555-0100",
    "street": "123 Main St", "city": "Austin", "state": "TX", "postal_code": "78701", "country": "US",
}
API = "https://kiln.test"

#: What the servers answer a quote with (``_rest/fulfillment.py`` → ``handle_quote``).
QUOTE_REPLY = {
    "success": True,
    "tell_the_person": ["1 copy of the part in grey PLA: 24.02 USD delivered.", "Customs are yours."],
    "quote": {
        "quote_id": "cc-quote-1", "provider": "craftcloud", "material": "pla-grey", "quantity": 1,
        "unit_price": 16.58, "total_price": 16.58, "currency": "USD", "lead_time_days": 4,
        "shipping_options": [{"id": "ship-1", "name": "Standard", "price": 8.85, "currency": "USD"}],
    },
    "kiln_fee": {"fee_amount": 0.0, "waived": True},
    "total_with_fee": 16.58,
    "price_sheet": {"delivered": 25.43},
    "provider_quote_id": "cc-quote-1",
    "provider_terms_url": "https://craftcloud3d.com/terms",
    "quote_token": "tok-0123456789abcdef",
}
ORDER_REPLY = {
    "success": True,
    "tell_the_person": ["Pay here: https://pay.example/1", "Checkout total 25.43 USD."],
    "order": {"order_id": "ord-1", "status": "submitted", "total_price": 25.43, "currency": "USD"},
    "kiln_fee": {"fee_amount": 0.0, "waived": True},
    "provider_terms_url": "https://craftcloud3d.com/terms",
}
STATUS_REPLY = {
    "success": True,
    "tell_the_person": ["Your order is in production."],
    "order": {"order_id": "ord-1", "status": "processing", "tracking_number": None},
}
ORDERS_REPLY = {
    "success": True, "kept": True, "count": 2,
    "orders": [
        {"order_id": "ord-1", "provider": "craftcloud", "print_price": 16.58, "currency": "USD",
         "recorded_status": "submitted", "now": {"status": "cancelled"}, "tell_the_person": ["Cancelled."]},
        {"order_id": "ord-2", "provider": "craftcloud", "print_price": 9.0, "currency": "USD",
         "recorded_status": "submitted", "now": {"status": "processing"}, "checkout_total_at_order": 17.85,
         "contents": {"material": "pla-grey"}},
    ],
}


class _Resp:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("not json")
        return self._body


def _must_not_reach(*_args, **_kwargs):
    raise AssertionError("an ordering door reached for a local print-service provider")


@pytest.fixture
def servers(monkeypatch):
    """Kiln's servers, stood in for at the one place the bridge sends:
    answers queued by the test, every request recorded."""
    import httpx

    calls: list[SimpleNamespace] = []
    replies: list[Any] = []

    def request(method, url, **kw):
        calls.append(SimpleNamespace(method=method, url=url, **kw))
        assert replies, f"no reply queued for {method} {url}"
        reply = replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    monkeypatch.setattr(httpx, "request", request)
    return SimpleNamespace(calls=calls, replies=replies)


@pytest.fixture
def plain_install(monkeypatch, tmp_path):
    """No order service, signed in, the servers at ``API``; a local
    provider is never reached."""
    import kiln.quote_cache
    import kiln.server as server

    order_service.remove(monkeypatch)
    sign_in(monkeypatch, tmp_path)
    monkeypatch.setenv("KILN_API_URL", API)
    monkeypatch.setattr(server, "_get_fulfillment", _must_not_reach)
    monkeypatch.setattr(server, "_get_fulfillment_monitor", _must_not_reach)
    monkeypatch.setattr(kiln.quote_cache, "_quote_cache", kiln.quote_cache.QuoteCache())


@pytest.fixture
def tools():
    found: dict = {}

    class _MCP:
        def tool(self, **_kwargs):
            def decorator(fn):
                found[fn.__name__] = fn
                return fn

            return decorator

    from kiln.plugins.fulfillment_tools import plugin

    plugin.register(_MCP())
    return found


def _model(tmp_path: Path) -> Path:
    model = tmp_path / "model.stl"
    model.write_bytes(b"solid test\nendsolid test\n")
    return model


# ---------------------------------------------------------------------------
# the agent tools, happy path
# ---------------------------------------------------------------------------


class TestTheToolsOrderThroughTheServers:
    def test_materials_come_from_the_servers_filtered_as_the_local_door_filters(self, plain_install, servers, tools):
        servers.replies.append(_Resp(200, {"success": True, "provider": "craftcloud", "materials": [
            {"id": "m1", "name": "Nylon 12", "technology": "SLS", "color": "white", "finish": "standard"},
            {"id": "m2", "name": "PLA", "technology": "FDM", "color": "grey", "finish": "standard"},
            {"id": "m3", "name": "Carbonylon", "technology": "SLS", "color": "black", "finish": "standard"},
        ]}))

        reply = tools["fulfillment_materials"](search="nylon")

        assert reply["success"] is True and reply["served"] is True
        assert [m["id"] for m in reply["materials"]] == ["m1"] and reply["count"] == 1
        call = servers.calls[0]
        assert (call.method, call.url) == ("GET", f"{API}/api/fulfillment/materials")
        assert call.params == {"provider": "craftcloud"}
        assert call.headers["Authorization"] == "Bearer signed-in-account"

    def test_a_quote_uploads_the_model_and_keeps_the_token_beside_the_quote(self, plain_install, servers, tools, tmp_path):
        model = _model(tmp_path)
        servers.replies.append(_Resp(200, json.loads(json.dumps(QUOTE_REPLY))))

        reply = tools["fulfillment_quote"](file_path=str(model), material_id="pla-grey", quantity=1, shipping_country="US")

        assert reply["success"] is True
        assert list(reply)[:2] == ["success", "tell_the_person"], "the message comes before the data"
        assert reply["tell_the_person"] == QUOTE_REPLY["tell_the_person"]
        quoted = reply["quote"]
        assert quoted["quote_id"] == "cc-quote-1"
        # The fee, the price sheet and the terms are folded in where the local door puts them.
        assert quoted["kiln_fee"] == {"fee_amount": 0.0, "waived": True}
        assert quoted["price_sheet"] == {"delivered": 25.43}
        assert quoted["provider_terms_url"] == "https://craftcloud3d.com/terms"
        assert reply["quote_token"] == quoted["quote_token"] == "tok-0123456789abcdef"
        call = servers.calls[0]
        assert (call.method, call.url) == ("POST", f"{API}/api/fulfillment/quote")
        assert call.data["material_id"] == "pla-grey" and call.data["quantity"] == "1"
        assert call.files["file"][0] == "model.stl"
        assert call.timeout >= 60
        assert bridge.quote_token_for("cc-quote-1") == "tok-0123456789abcdef"

    def test_a_quote_for_a_file_that_is_not_there_is_the_doors_own_error(self, plain_install, servers, tools, tmp_path):
        reply = tools["fulfillment_quote"](file_path=str(tmp_path / "gone.stl"), material_id="pla-grey")

        assert reply["success"] is False and reply["error"]["code"] == "FILE_NOT_FOUND"
        assert servers.calls == []

    def test_an_order_takes_the_servers_two_confirmations_then_places(self, plain_install, servers, tools, tmp_path):
        from kiln.fulfillment_profiles import issue_shipping_confirmation_token, normalize_shipping_address
        from kiln.preview_gate import get_preview_gate

        model = _model(tmp_path)
        servers.replies.append(_Resp(200, json.loads(json.dumps(QUOTE_REPLY))))
        tools["fulfillment_quote"](file_path=str(model), material_id="pla-grey")
        preview_token = get_preview_gate().issue(str(model)).token
        shipping_token = issue_shipping_confirmation_token(
            quote_id="cc-quote-1", shipping_option_id="ship-1",
            shipping_address=normalize_shipping_address(ADDRESS),
        ).token
        servers.replies.extend([
            _Resp(200, {"success": True, "preview_confirmation_token": "pc_1", "expires_at": 1}),
            _Resp(200, {"success": True, "shipping_confirmation_token": "sc_1", "expires_at": 1}),
            _Resp(200, json.loads(json.dumps(ORDER_REPLY))),
        ])

        reply = tools["fulfillment_order"](
            quote_id="cc-quote-1", shipping_option_id="ship-1", shipping_address=ADDRESS,
            preview_token=preview_token, preview_file_path=str(model),
            shipping_confirmation_token=shipping_token,
        )

        assert reply["success"] is True, reply
        assert reply["tell_the_person"] == ORDER_REPLY["tell_the_person"]
        assert reply["order"]["order_id"] == "ord-1"
        assert reply["order"]["kiln_fee"] == {"fee_amount": 0.0, "waived": True}
        paths = [c.url.removeprefix(API) for c in servers.calls[1:]]
        assert paths == ["/api/fulfillment/preview-confirm", "/api/fulfillment/shipping-confirm", "/api/fulfillment/order"]
        assert servers.calls[1].json == {"quote_token": "tok-0123456789abcdef"}
        assert servers.calls[2].json["shipping_option_id"] == "ship-1"
        assert servers.calls[2].json["shipping_address"]["city"] == "Austin"
        placed = servers.calls[3].json
        assert placed["quote_token"] == "tok-0123456789abcdef" and placed["quote_id"] == "cc-quote-1"
        assert placed["preview_confirmation_token"] == "pc_1" and placed["shipping_confirmation_token"] == "sc_1"
        # The token is single-use on the servers; this computer forgets it too.
        assert bridge.quote_token_for("cc-quote-1") == ""

    def test_a_default_install_is_not_asked_for_a_local_api_key_to_order(self, plain_install, servers, tools, tmp_path, monkeypatch):
        """2026-02-12 to 2026-10-31 the order door demanded this install's own
        API-key layer be ON (KILN_AUTH_ENABLED / KILN_AUTH_KEY, a layer for a
        server reachable over a network), so every default install's first
        order was refused with "enable auth".  On the served path the servers
        own the money gate; the local layer applies only when it is on."""
        import kiln.server as server

        monkeypatch.delenv("KILN_AUTH_ENABLED", raising=False)
        monkeypatch.delenv("KILN_AUTH_KEY", raising=False)
        assert server._check_billing_auth("print") is not None, "the local wall is really off by default"
        from kiln.fulfillment_profiles import issue_shipping_confirmation_token, normalize_shipping_address
        from kiln.preview_gate import get_preview_gate

        model = _model(tmp_path)
        servers.replies.append(_Resp(200, json.loads(json.dumps(QUOTE_REPLY))))
        tools["fulfillment_quote"](file_path=str(model), material_id="pla-grey")
        preview_token = get_preview_gate().issue(str(model)).token
        shipping_token = issue_shipping_confirmation_token(
            quote_id="cc-quote-1", shipping_option_id="ship-1", shipping_address=normalize_shipping_address(ADDRESS),
        ).token
        servers.replies.extend([
            _Resp(200, {"success": True, "preview_confirmation_token": "pc_1"}),
            _Resp(200, {"success": True, "shipping_confirmation_token": "sc_1"}),
            _Resp(200, json.loads(json.dumps(ORDER_REPLY))),
        ])

        reply = tools["fulfillment_order"](
            quote_id="cc-quote-1", shipping_option_id="ship-1", shipping_address=ADDRESS,
            preview_token=preview_token, preview_file_path=str(model), shipping_confirmation_token=shipping_token,
        )

        assert reply["success"] is True, reply
        assert "AUTH_REQUIRED" not in json.dumps(reply)

    def test_an_order_still_needs_the_persons_two_approvals_here(self, plain_install, servers, tools, tmp_path):
        reply = tools["fulfillment_order"](quote_id="cc-quote-1", shipping_option_id="ship-1", shipping_address=ADDRESS)

        assert reply["success"] is False and reply["error"]["code"] == "PREVIEW_NOT_CONFIRMED"
        assert servers.calls == []

    def test_a_handed_token_for_a_quote_this_computer_never_took_is_refused(self, plain_install, servers, tools, tmp_path):
        """Fail-closed: with no record here there is nothing to bind the
        approval to, so the token is not trusted and the person quotes again."""
        from kiln.fulfillment_profiles import issue_shipping_confirmation_token, normalize_shipping_address
        from kiln.preview_gate import get_preview_gate

        model = _model(tmp_path)
        preview_token = get_preview_gate().issue(str(model)).token
        shipping_token = issue_shipping_confirmation_token(
            quote_id="other-quote", shipping_option_id="ship-1",
            shipping_address=normalize_shipping_address(ADDRESS),
        ).token

        reply = tools["fulfillment_order"](
            quote_id="other-quote", quote_token="tok-handed", shipping_option_id="ship-1",
            shipping_address=ADDRESS, preview_token=preview_token, preview_file_path=str(model),
            shipping_confirmation_token=shipping_token,
        )

        assert reply["success"] is False and reply["error"]["code"] == bridge.QUOTE_TOKEN_UNKNOWN_CODE
        assert "get the quote again" in reply["error"]["message"].lower()
        assert servers.calls == []

    def test_a_handed_token_that_matches_the_record_is_accepted(self, plain_install, servers, tools, tmp_path):
        from kiln.fulfillment_profiles import issue_shipping_confirmation_token, normalize_shipping_address
        from kiln.preview_gate import get_preview_gate

        model = _model(tmp_path)
        servers.replies.append(_Resp(200, json.loads(json.dumps(QUOTE_REPLY))))
        tools["fulfillment_quote"](file_path=str(model), material_id="pla-grey")
        preview_token = get_preview_gate().issue(str(model)).token
        shipping_token = issue_shipping_confirmation_token(
            quote_id="cc-quote-1", shipping_option_id="ship-1", shipping_address=normalize_shipping_address(ADDRESS),
        ).token
        servers.replies.extend([
            _Resp(200, {"success": True, "preview_confirmation_token": "pc_1"}),
            _Resp(200, {"success": True, "shipping_confirmation_token": "sc_1"}),
            _Resp(200, json.loads(json.dumps(ORDER_REPLY))),
        ])

        reply = tools["fulfillment_order"](
            quote_id="cc-quote-1", quote_token="tok-0123456789abcdef", shipping_option_id="ship-1",
            shipping_address=ADDRESS, preview_token=preview_token, preview_file_path=str(model),
            shipping_confirmation_token=shipping_token,
        )

        assert reply["success"] is True, reply
        assert servers.calls[1].json == {"quote_token": "tok-0123456789abcdef"}

    def test_a_handed_token_that_disagrees_with_the_remembered_one_places_nothing(self, plain_install, servers, tools, tmp_path):
        """A token names a quote; a token for another quote would place that
        other quote.  When this computer remembers the quote's token, a handed
        one must be it."""
        from kiln.fulfillment_profiles import issue_shipping_confirmation_token, normalize_shipping_address
        from kiln.preview_gate import get_preview_gate

        model = _model(tmp_path)
        servers.replies.append(_Resp(200, json.loads(json.dumps(QUOTE_REPLY))))
        tools["fulfillment_quote"](file_path=str(model), material_id="pla-grey")
        preview_token = get_preview_gate().issue(str(model)).token
        shipping_token = issue_shipping_confirmation_token(
            quote_id="cc-quote-1", shipping_option_id="ship-1", shipping_address=normalize_shipping_address(ADDRESS),
        ).token

        reply = tools["fulfillment_order"](
            quote_id="cc-quote-1", quote_token="tok-for-some-other-quote", shipping_option_id="ship-1",
            shipping_address=ADDRESS, preview_token=preview_token, preview_file_path=str(model),
            shipping_confirmation_token=shipping_token,
        )

        assert reply["success"] is False and reply["error"]["code"] == "QUOTE_TOKEN_MISMATCH"
        assert "not placed" in reply["error"]["message"]
        assert len(servers.calls) == 1, "nothing after the quote reached the servers"

    def test_the_approved_preview_must_be_of_the_quoted_file(self, plain_install, servers, tools, tmp_path):
        from kiln.fulfillment_profiles import issue_shipping_confirmation_token, normalize_shipping_address
        from kiln.preview_gate import get_preview_gate

        quoted = _model(tmp_path)
        servers.replies.append(_Resp(200, json.loads(json.dumps(QUOTE_REPLY))))
        tools["fulfillment_quote"](file_path=str(quoted), material_id="pla-grey")
        other = tmp_path / "other.stl"
        other.write_bytes(b"solid other\nendsolid other\n")
        preview_token = get_preview_gate().issue(str(other)).token
        shipping_token = issue_shipping_confirmation_token(
            quote_id="cc-quote-1", shipping_option_id="ship-1", shipping_address=normalize_shipping_address(ADDRESS),
        ).token

        reply = tools["fulfillment_order"](
            quote_id="cc-quote-1", shipping_option_id="ship-1", shipping_address=ADDRESS,
            preview_token=preview_token, preview_file_path=str(other), shipping_confirmation_token=shipping_token,
        )

        assert reply["success"] is False and reply["error"]["code"] == "PREVIEW_NOT_CONFIRMED"
        assert "different file" in reply["error"]["message"]
        assert len(servers.calls) == 1

    def test_a_file_that_is_not_a_model_is_refused_before_a_byte_is_read(self, plain_install, servers, tools, tmp_path):
        secret = tmp_path / "id_rsa"
        secret.write_text("not a model")
        link = tmp_path / "looks-like-a-model.stl"
        link.symlink_to(secret)

        for path in (secret, link):
            reply = tools["fulfillment_quote"](file_path=str(path), material_id="pla-grey")
            assert reply["success"] is False and reply["error"]["code"] == "UNSUPPORTED_FILE_TYPE", reply
            assert "nothing was sent" in reply["error"]["message"]
        assert servers.calls == []

    def test_a_stale_quote_id_spends_neither_approval(self, plain_install, servers, tools, tmp_path):
        """The lap's finding: the record checks ran after the two approvals
        were consumed, so one wrong quote id burned both and the retry with
        the right id failed PREVIEW_NOT_CONFIRMED."""
        from kiln.fulfillment_profiles import issue_shipping_confirmation_token, normalize_shipping_address
        from kiln.preview_gate import get_preview_gate

        model = _model(tmp_path)
        servers.replies.append(_Resp(200, json.loads(json.dumps(QUOTE_REPLY))))
        tools["fulfillment_quote"](file_path=str(model), material_id="pla-grey")
        preview_token = get_preview_gate().issue(str(model)).token
        stale_shipping = issue_shipping_confirmation_token(
            quote_id="stale-quote", shipping_option_id="ship-1", shipping_address=normalize_shipping_address(ADDRESS),
        ).token
        wrong = tools["fulfillment_order"](
            quote_id="stale-quote", shipping_option_id="ship-1", shipping_address=ADDRESS,
            preview_token=preview_token, preview_file_path=str(model), shipping_confirmation_token=stale_shipping,
        )
        assert wrong["error"]["code"] == bridge.QUOTE_TOKEN_UNKNOWN_CODE
        assert servers.calls[1:] == []
        servers.replies.extend([
            _Resp(200, {"success": True, "preview_confirmation_token": "pc_1"}),
            _Resp(200, {"success": True, "shipping_confirmation_token": "sc_1"}),
            _Resp(200, json.loads(json.dumps(ORDER_REPLY))),
        ])
        # the same preview approval is still live; only the shipping one is re-issued for the right quote
        shipping_token = issue_shipping_confirmation_token(
            quote_id="cc-quote-1", shipping_option_id="ship-1", shipping_address=normalize_shipping_address(ADDRESS),
        ).token
        right = tools["fulfillment_order"](
            quote_id="cc-quote-1", shipping_option_id="ship-1", shipping_address=ADDRESS,
            preview_token=preview_token, preview_file_path=str(model), shipping_confirmation_token=shipping_token,
        )
        assert right["success"] is True, right

    def test_a_quote_pins_the_tax_region_and_business_id_on_the_servers(self, plain_install, servers, tools, tmp_path):
        servers.replies.append(_Resp(200, json.loads(json.dumps(QUOTE_REPLY))))
        reply = tools["fulfillment_quote"](
            file_path=str(_model(tmp_path)), material_id="pla-grey", jurisdiction="DE", business_tax_id="DE123456789",
        )
        assert reply["success"] is True
        assert servers.calls[0].data["jurisdiction"] == "DE" and servers.calls[0].data["business_tax_id"] == "DE123456789"

    def test_an_order_with_no_token_anywhere_says_to_quote_again(self, plain_install, servers, tools, tmp_path):
        from kiln.fulfillment_profiles import issue_shipping_confirmation_token, normalize_shipping_address
        from kiln.preview_gate import get_preview_gate

        model = _model(tmp_path)
        preview_token = get_preview_gate().issue(str(model)).token
        shipping_token = issue_shipping_confirmation_token(
            quote_id="never-quoted", shipping_option_id="ship-1",
            shipping_address=normalize_shipping_address(ADDRESS),
        ).token

        reply = tools["fulfillment_order"](
            quote_id="never-quoted", shipping_option_id="ship-1", shipping_address=ADDRESS,
            preview_token=preview_token, preview_file_path=str(model), shipping_confirmation_token=shipping_token,
        )

        assert reply["success"] is False
        assert reply["error"]["code"] == bridge.QUOTE_TOKEN_UNKNOWN_CODE
        assert "not placed" in reply["error"]["message"]
        assert servers.calls == []

    def test_status_and_cancel_answer_about_the_persons_own_order(self, plain_install, servers, tools):
        servers.replies.append(_Resp(200, json.loads(json.dumps(STATUS_REPLY))))
        status = tools["fulfillment_order_status"](order_id="ord-1")
        assert status["success"] is True
        assert status["tell_the_person"] == ["Your order is in production."]
        assert status["order"]["status"] == "processing"
        assert servers.calls[0].url == f"{API}/api/fulfillment/order/ord-1/status"

        servers.replies.append(_Resp(200, {"success": True, "order": {"order_id": "ord-1", "status": "cancelled"}}))
        cancelled = tools["fulfillment_cancel"](order_id="ord-1")
        assert cancelled == {"success": True, "order": {"order_id": "ord-1", "status": "cancelled"}}
        assert (servers.calls[1].method, servers.calls[1].url) == ("POST", f"{API}/api/fulfillment/order/ord-1/cancel")

    def test_a_cancel_refusal_the_servers_worded_rides_as_it_is(self, plain_install, servers, tools):
        said = "There's nothing to cancel: an unpaid order is never made and expires on its own."
        servers.replies.append(_Resp(400, {"success": False, "error": said, "code": "NOTHING_TO_CANCEL"}))

        reply = tools["fulfillment_cancel"](order_id="ord-1")

        assert reply["success"] is False
        assert reply["error"] == {"code": "NOTHING_TO_CANCEL", "message": said} | {
            k: v for k, v in reply["error"].items() if k not in ("code", "message")
        }
        assert reply["error"]["message"] == said and reply["why"] == "refused"

    def test_alerts_are_read_off_the_persons_orders(self, plain_install, servers, tools):
        servers.replies.append(_Resp(200, json.loads(json.dumps(ORDERS_REPLY))))

        reply = tools["fulfillment_alerts"]()

        assert reply["success"] is True and reply["source"] == "kiln_servers"
        assert reply["orders_checked"] == 2 and reply["count"] == 1
        assert reply["alerts"][0]["order_id"] == "ord-1" and reply["alerts"][0]["kind"] == "cancelled"
        assert reply["alerts"][0]["tell_the_person"] == ["Cancelled."]
        assert servers.calls[0].url == f"{API}/api/fulfillment/orders"


# ---------------------------------------------------------------------------
# the four misses, and the one sentence kept for an address with no ordering
# ---------------------------------------------------------------------------


def _offline() -> BaseException:
    return OSError(errno.ENETUNREACH, "Network is unreachable")


class TestEveryMissIsWorded:
    def test_offline_says_reconnect_and_that_nothing_was_sent(self, plain_install, servers, tools, tmp_path):
        servers.replies.append(_offline())

        reply = tools["fulfillment_quote"](file_path=str(_model(tmp_path)), material_id="pla-grey")

        assert reply["success"] is False
        assert reply["why"] == "offline" and reply["error"]["code"] == "SERVER_UNREACHABLE"
        said = reply["error"]["message"].lower()
        assert "this computer is offline" in said and "reconnect to the internet" in said
        assert "nothing was sent to a print service" in said
        assert NOT_INCLUDED not in said

    def test_signed_out_says_sign_in_and_asks_the_servers_nothing(self, plain_install, servers, tools, monkeypatch, tmp_path):
        import kiln.server as server

        sign_out()
        monkeypatch.setattr(server, "_served_bearer", lambda: ("", None))

        reply = tools["fulfillment_quote"](file_path=str(_model(tmp_path)), material_id="pla-grey")

        assert reply["success"] is False and servers.calls == []
        assert reply["why"] == "signed_out" and reply["error"]["code"] == "KILN_ACCOUNT_NOT_PAIRED"
        said = reply["error"]["message"].lower()
        assert "kiln is signed out" in said and "sign in and try again" in said
        from kiln.tiers_and_terms import signin_hint_fields

        assert all(reply.get(k) == v for k, v in signin_hint_fields().items()), reply

    def test_an_expired_session_says_sign_back_in(self, plain_install, servers, tools, monkeypatch, tmp_path):
        import kiln.server as server

        sign_out()
        monkeypatch.setattr(server, "_served_bearer", lambda: ("", "the session ran out"))

        reply = tools["fulfillment_order_status"](order_id="ord-1")

        assert reply["why"] == "signed_out" and reply["error"]["code"] == "KILN_SESSION_EXPIRED"
        said = reply["error"]["message"].lower()
        assert "session has expired" in said and "sign back in" in said

    def test_the_servers_rejecting_the_sign_in_reads_as_signed_out(self, plain_install, servers, tools):
        servers.replies.append(_Resp(401, {"detail": "Invalid or missing auth token"}))

        reply = tools["fulfillment_materials"]()

        assert reply["why"] == "signed_out" and reply["error"]["code"] == "KILN_AUTH_REJECTED"

    def test_unanswered_says_wait_a_minute(self, plain_install, servers, tools):
        servers.replies.append(_Resp(503, {"success": False, "error": "Print partner is not answering.",
                                            "code": "PROVIDER_UNAVAILABLE", "retryable": True}))

        reply = tools["fulfillment_materials"]()

        assert reply["why"] == "unanswered" and reply["error"]["code"] == "PROVIDER_UNAVAILABLE"
        said = reply["error"]["message"].lower()
        assert "didn't answer" in said and "wait a minute" in said

    def test_an_order_the_servers_did_not_answer_is_not_called_unplaced(self, plain_install, servers, tools, tmp_path):
        """The POST may have reached the servers: Kiln says it does not know,
        and sends the person to the order's status, never "placed no order"."""
        from kiln.fulfillment_profiles import issue_shipping_confirmation_token, normalize_shipping_address
        from kiln.preview_gate import get_preview_gate

        model = _model(tmp_path)
        servers.replies.append(_Resp(200, json.loads(json.dumps(QUOTE_REPLY))))
        tools["fulfillment_quote"](file_path=str(model), material_id="pla-grey")
        preview_token = get_preview_gate().issue(str(model)).token
        shipping_token = issue_shipping_confirmation_token(
            quote_id="cc-quote-1", shipping_option_id="ship-1", shipping_address=normalize_shipping_address(ADDRESS),
        ).token
        servers.replies.extend([
            _Resp(200, {"success": True, "preview_confirmation_token": "pc_1"}),
            _Resp(200, {"success": True, "shipping_confirmation_token": "sc_1"}),
            ConnectionResetError("the link dropped mid-request"),
        ])

        reply = tools["fulfillment_order"](
            quote_id="cc-quote-1", shipping_option_id="ship-1", shipping_address=ADDRESS,
            preview_token=preview_token, preview_file_path=str(model), shipping_confirmation_token=shipping_token,
        )

        assert reply["why"] == "unanswered"
        said = reply["error"]["message"].lower()
        assert "doesn't know whether the order was placed" in said and "check the order's status" in said
        assert "placed no order" not in said
        offline = bridge.words(bridge.Miss("offline", "SERVER_UNREACHABLE"), "order").lower()
        assert "placed no order and charged nothing" in offline, "a request that never left is safely unplaced"

    def test_a_gateway_page_is_unanswered_too(self, plain_install, servers, tools):
        servers.replies.append(_Resp(502, None))

        reply = tools["fulfillment_materials"]()

        assert reply["why"] == "unanswered" and reply["error"]["code"] == "KILN_API_HTTP_ERROR"

    def test_a_refusal_rides_whole_with_its_details(self, plain_install, servers, tools, tmp_path):
        servers.replies.append(_Resp(402, {
            "success": False, "code": "PAYMENT_METHOD_REQUIRED",
            "error": "Add a card before this quote: Kiln's fee is due on it.",
            "details": {"setup_url": "https://pay.example/setup"},
        }))

        reply = tools["fulfillment_quote"](file_path=str(_model(tmp_path)), material_id="pla-grey")

        assert reply["why"] == "refused" and reply["error"]["code"] == "PAYMENT_METHOD_REQUIRED"
        said = reply["error"]["message"]
        assert "Kiln's servers said no" in said
        assert "Add a card before this quote: Kiln's fee is due on it." in said
        assert reply["details"] == {"setup_url": "https://pay.example/setup"}

    def test_an_order_refused_at_the_servers_confirmation_is_said_not_placed(self, plain_install, servers, tools, tmp_path):
        from kiln.fulfillment_profiles import issue_shipping_confirmation_token, normalize_shipping_address
        from kiln.preview_gate import get_preview_gate

        model = _model(tmp_path)
        servers.replies.append(_Resp(200, json.loads(json.dumps(QUOTE_REPLY))))
        tools["fulfillment_quote"](file_path=str(model), material_id="pla-grey")
        preview_token = get_preview_gate().issue(str(model)).token
        shipping_token = issue_shipping_confirmation_token(
            quote_id="cc-quote-1", shipping_option_id="ship-1", shipping_address=normalize_shipping_address(ADDRESS),
        ).token
        servers.replies.append(_Resp(200, {"success": True, "preview_confirmation_token": "pc_1"}))
        servers.replies.append(_Resp(400, {"success": False, "code": "SHIPPING_DETAILS_CHANGED",
                                            "error": "The shipping details changed since the quote."}))

        reply = tools["fulfillment_order"](
            quote_id="cc-quote-1", shipping_option_id="ship-1", shipping_address=ADDRESS,
            preview_token=preview_token, preview_file_path=str(model), shipping_confirmation_token=shipping_token,
        )

        assert reply["success"] is False and reply["why"] == "refused"
        assert "placed no order and charged nothing" in reply["error"]["message"]
        assert len(servers.calls) == 3, "the order step was never reached"

    def test_an_address_with_no_ordering_routes_keeps_the_one_sentence(self, plain_install, servers, tools):
        servers.replies.append(_Resp(404, {"detail": "Not Found"}))

        reply = tools["fulfillment_materials"]()

        assert reply["success"] is False
        assert reply["error"]["code"] == "NOT_AVAILABLE" and reply["error"]["message"] == NOT_INCLUDED
        assert "why" not in reply, "the one sentence stands alone, as it always did"

    def test_an_order_that_is_not_the_callers_is_a_refusal_not_the_sentence(self, plain_install, servers, tools):
        servers.replies.append(_Resp(404, {"success": False, "code": "ORDER_NOT_FOUND", "error": "No such order of yours."}))

        reply = tools["fulfillment_order_status"](order_id="ord-9")

        assert reply["why"] == "refused" and reply["error"]["code"] == "ORDER_NOT_FOUND"
        assert NOT_INCLUDED not in reply["error"]["message"]


# ---------------------------------------------------------------------------
# the other doors: the comparison tool, the terminal
# ---------------------------------------------------------------------------


class TestTheOtherDoors:
    def test_compare_print_options_quotes_through_the_servers(self, plain_install, servers, monkeypatch, tmp_path):
        import kiln.server as server

        estimator = SimpleNamespace(estimate_from_file=lambda *a, **k: (_ for _ in ()).throw(ValueError("no local")))
        monkeypatch.setattr(server, "_get_cost_estimator", lambda: estimator)
        servers.replies.append(_Resp(200, json.loads(json.dumps(QUOTE_REPLY))))

        result = server.compare_print_options(file_path=str(_model(tmp_path)), fulfillment_material_id="pla-grey")

        assert result["fulfillment"]["available"] is True
        assert result["fulfillment"]["quote"]["quote_id"] == "cc-quote-1"
        assert result["fulfillment"]["quote"]["total_with_fee"] == 16.58

    def test_compare_print_options_says_the_miss_for_the_outsourced_half(self, plain_install, servers, monkeypatch, tmp_path):
        import kiln.server as server

        estimator = SimpleNamespace(estimate_from_file=lambda *a, **k: (_ for _ in ()).throw(ValueError("no local")))
        monkeypatch.setattr(server, "_get_cost_estimator", lambda: estimator)
        servers.replies.append(_offline())

        result = server.compare_print_options(file_path=str(_model(tmp_path)), fulfillment_material_id="pla-grey")

        assert result["success"] is True and result["local"]["error"] == "no local"
        assert result["fulfillment"]["available"] is False
        assert "this computer is offline" in result["fulfillment"]["error"]

    def test_kiln_order_materials_and_history_run_through_the_servers(self, plain_install, servers):
        from kiln.cli.main import cli

        servers.replies.append(_Resp(200, {"success": True, "provider": "craftcloud", "materials": [
            {"id": "m2", "name": "PLA", "technology": "FDM", "color": None, "finish": "standard"},
        ]}))
        out = CliRunner().invoke(cli, ["order", "materials", "--json"])
        assert out.exit_code == 0, out.output
        assert json.loads(out.output)["data"]["materials"][0]["id"] == "m2"

        servers.replies.append(_Resp(200, json.loads(json.dumps(ORDERS_REPLY))))
        out = CliRunner().invoke(cli, ["order", "history", "--json"])
        assert out.exit_code == 0, out.output
        rows = json.loads(out.output)["data"]
        assert [r["order_id"] for r in rows] == ["ord-1", "ord-2"]
        assert rows[1]["status"] == "processing" and rows[1]["total_price"] == 17.85 and rows[1]["material_id"] == "pla-grey"

        servers.replies.append(_Resp(200, json.loads(json.dumps(ORDERS_REPLY))))
        out = CliRunner().invoke(cli, ["order", "history"])
        assert out.exit_code == 0 and "ord-2" in out.output and "processing" in out.output

    def test_kiln_order_quote_shows_the_servers_price_sheet(self, plain_install, servers, tmp_path):
        from kiln.cli.main import cli

        servers.replies.append(_Resp(200, json.loads(json.dumps(QUOTE_REPLY))))
        out = CliRunner().invoke(cli, ["order", "quote", str(_model(tmp_path)), "--material", "pla-grey", "--json"])

        assert out.exit_code == 0, out.output
        quoted = json.loads(out.output)["data"]["quote"]
        assert quoted["quote_id"] == "cc-quote-1" and quoted["price_sheet"] == {"delivered": 25.43}
        assert quoted["kiln_fee"] == {"fee_amount": 0.0, "waived": True}
        assert bridge.quote_token_for("cc-quote-1") == "tok-0123456789abcdef"

    def test_kiln_order_status_offline_says_the_four_cause_sentence_not_the_commands_wrapping(self, plain_install, servers):
        from kiln.cli.main import cli

        servers.replies.append(_offline())
        out = CliRunner().invoke(cli, ["order", "status", "ord-1", "--json"])

        assert out.exit_code == 1
        error = json.loads(out.output)["error"]
        assert error["code"] == "SERVER_UNREACHABLE" and error["why"] == "offline"
        assert "this computer is offline" in error["message"]
        assert "Failed to get order status" not in error["message"]

    def test_kiln_order_status_signed_out_says_sign_in(self, plain_install, servers, monkeypatch):
        import kiln.server as server
        from kiln.cli.main import cli

        sign_out()
        monkeypatch.setattr(server, "_served_bearer", lambda: ("", None))
        out = CliRunner().invoke(cli, ["order", "status", "ord-1", "--json"])

        error = json.loads(out.output)["error"]
        assert error["code"] == "KILN_ACCOUNT_NOT_PAIRED" and error["why"] == "signed_out"
        assert "sign in and try again" in error["message"].lower() and servers.calls == []

    def test_kiln_order_place_places_through_the_servers_and_charges_nothing_here(self, plain_install, servers, tmp_path):
        from kiln.cli.main import cli

        model = _model(tmp_path)
        servers.replies.append(_Resp(200, json.loads(json.dumps(QUOTE_REPLY))))
        assert CliRunner().invoke(cli, ["order", "quote", str(model), "--material", "pla-grey", "--json"]).exit_code == 0
        servers.replies.extend([
            _Resp(200, {"success": True, "preview_confirmation_token": "pc_1"}),
            _Resp(200, {"success": True, "shipping_confirmation_token": "sc_1"}),
            _Resp(200, json.loads(json.dumps(ORDER_REPLY))),
        ])

        out = CliRunner().invoke(cli, [
            "order", "place", "cc-quote-1", "--shipping", "ship-1",
            "--first-name", "Ada", "--last-name", "Lovelace", "--email", "ada@example.com", "--phone", "555-0100",
            "--street", "123 Main St", "--city", "Austin", "--state", "TX", "--postal-code", "78701",
            "--preview-file", str(model), "--confirm-preview", "--confirm-shipping", "--do-not-save-shipping-profile",
            "--json",
        ])

        assert out.exit_code == 0, out.output
        data = json.loads(out.output)["data"]["order"]
        assert data["order_id"] == "ord-1"
        assert data["kiln_fee"] == {"fee_amount": 0.0, "waived": True}
        assert data["payment"] == {"status": "charged_by_kiln_server"}
        assert data["tell_the_person"] == ORDER_REPLY["tell_the_person"]
        assert [c.url.removeprefix(API) for c in servers.calls[1:]] == [
            "/api/fulfillment/preview-confirm", "/api/fulfillment/shipping-confirm", "/api/fulfillment/order",
        ]

    def test_kiln_compare_cost_quotes_through_the_servers(self, plain_install, servers, tmp_path):
        from kiln.cli.main import cli

        servers.replies.append(_Resp(200, json.loads(json.dumps(QUOTE_REPLY))))
        out = CliRunner().invoke(cli, [
            "compare-cost", str(_model(tmp_path)), "--fulfillment-material", "a0000000-0000-0000-0000-000000000001", "--json",
        ])

        assert out.exit_code == 0, out.output
        data = json.loads(out.output)["data"]
        assert data["fulfillment"]["available"] is True
        assert data["fulfillment"]["quote"]["quote_id"] == "cc-quote-1"

    def test_kiln_fulfillment_materials_runs_through_the_servers(self, plain_install, servers):
        from kiln.cli.main import cli

        servers.replies.append(_Resp(200, {"success": True, "provider": "craftcloud", "materials": [
            {"id": "m2", "name": "PLA", "technology": "FDM", "color": "grey", "finish": "standard"},
        ]}))
        out = CliRunner().invoke(cli, ["fulfillment-materials", "--json"])

        assert out.exit_code == 0, out.output
        assert json.loads(out.output)["data"]["materials"][0]["name"] == "PLA"


# ---------------------------------------------------------------------------
# the bridge's own contract
# ---------------------------------------------------------------------------


class TestTheBridge:
    def test_the_hosted_default_and_the_operators_own_address(self, monkeypatch):
        monkeypatch.delenv("KILN_API_URL", raising=False)
        assert bridge.api_base() == "https://api.kiln3d.com"
        monkeypatch.setenv("KILN_API_URL", "https://kiln.example/ ")
        assert bridge.api_base() == "https://kiln.example"

    def test_a_200_that_says_no_is_a_refusal(self, plain_install, servers):
        servers.replies.append(_Resp(200, {"success": False, "error": "No.", "code": "SPEND_CAP_REACHED",
                                            "details": {"approval_url": "https://kiln3d.com/approve/1"}}))

        answer = bridge.materials()

        assert not answer.ok and answer.miss.cause == "refused" and answer.miss.code == "SPEND_CAP_REACHED"
        assert answer.details == {"approval_url": "https://kiln3d.com/approve/1"}

    def test_an_order_id_is_url_safe(self, plain_install, servers):
        servers.replies.append(_Resp(200, json.loads(json.dumps(STATUS_REPLY))))
        bridge.order_status("ord/1 x")
        assert servers.calls[0].url.endswith("/api/fulfillment/order/ord%2F1%20x/status")
