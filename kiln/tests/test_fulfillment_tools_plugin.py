"""Tests for fulfillment MCP tool registration and safety helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests import _order_service as order_service

ADDRESS = {
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email": "ada@example.com",
    "phone": "555-0100",
    "street": "123 Main St",
    "city": "Austin",
    "state": "TX",
    "postal_code": "78701",
    "country": "US",
}


@pytest.fixture()
def registered_tools():
    tools: dict[str, callable] = {}

    class MockMCP:
        def tool(self, **_kwargs):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

    from kiln.plugins.fulfillment_tools import plugin

    plugin.register(MockMCP())
    return tools


def test_registers_shipping_profile_tools(registered_tools):
    assert "save_shipping_profile" in registered_tools
    assert "list_shipping_profiles" in registered_tools
    assert "delete_shipping_profile" in registered_tools
    assert "issue_shipping_confirmation_token" in registered_tools


def test_save_shipping_profile_requires_explicit_consent(registered_tools, monkeypatch, tmp_path):
    monkeypatch.setenv("KILN_SHIPPING_PROFILES_PATH", str(tmp_path / "profiles.json"))

    result = registered_tools["save_shipping_profile"](
        "home",
        ADDRESS,
        consent_to_store=False,
    )

    assert result["success"] is False
    assert result["error"]["code"] == "CONSENT_REQUIRED"


def test_save_shipping_profile_does_not_echo_full_address(registered_tools, monkeypatch, tmp_path):
    monkeypatch.setenv("KILN_SHIPPING_PROFILES_PATH", str(tmp_path / "profiles.json"))

    result = registered_tools["save_shipping_profile"](
        "home",
        ADDRESS,
        consent_to_store=True,
    )

    assert result["success"] is True
    assert "shipping_address" not in result["profile"]
    assert "123 Main St" not in result["profile"]["summary"]
    assert "ada@example.com" not in result["profile"]["summary"]


def test_issue_shipping_confirmation_token_resolves_saved_profile(
    registered_tools,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("KILN_SHIPPING_PROFILES_PATH", str(tmp_path / "profiles.json"))
    save_result = registered_tools["save_shipping_profile"](
        "home",
        ADDRESS,
        consent_to_store=True,
    )
    assert save_result["success"] is True

    token_result = registered_tools["issue_shipping_confirmation_token"](
        quote_id="quote-1",
        shipping_option_id="ship-1",
        shipping_profile_name="home",
    )

    assert token_result["success"] is True
    assert token_result["token"].startswith("fc_")
    assert token_result["shipping_address"]["email"] == "ada@example.com"


def test_issue_shipping_confirmation_token_requires_save_decision_for_explicit_address(
    registered_tools,
):
    result = registered_tools["issue_shipping_confirmation_token"](
        quote_id="quote-1",
        shipping_option_id="ship-1",
        shipping_address=ADDRESS,
    )

    assert result["success"] is False
    assert result["error"]["code"] == "SAVE_PROFILE_DECISION_REQUIRED"


def test_issue_shipping_confirmation_token_accepts_do_not_save_decision(
    registered_tools,
):
    result = registered_tools["issue_shipping_confirmation_token"](
        quote_id="quote-1",
        shipping_option_id="ship-1",
        shipping_address=ADDRESS,
        save_profile_decision="do_not_save",
    )

    assert result["success"] is True
    assert result["token"].startswith("fc_")
    assert result["save_profile_decision"] == "do_not_save"
    assert result["saved_profile"] is None


def test_issue_shipping_confirmation_token_can_save_after_user_decision(
    registered_tools,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("KILN_SHIPPING_PROFILES_PATH", str(tmp_path / "profiles.json"))

    result = registered_tools["issue_shipping_confirmation_token"](
        quote_id="quote-1",
        shipping_option_id="ship-1",
        shipping_address=ADDRESS,
        save_profile_decision="save",
        save_profile_name="home",
    )

    assert result["success"] is True
    assert result["saved_profile"]["name"] == "home"
    assert "shipping_address" not in result["saved_profile"]
    assert "123 Main St" not in result["saved_profile"]["summary"]


def test_fulfillment_quote_caches_provider_quote_id(registered_tools, tmp_path, monkeypatch):
    order_service.provide(monkeypatch)
    import kiln.server as server
    from kiln.quote_cache import get_cached_quote_by_id, get_quote_cache

    get_quote_cache().invalidate_all()
    model = tmp_path / "model.stl"
    model.write_bytes(b"solid test\nendsolid test\n")

    class MockProvider:
        name = "craftcloud"

        def get_quote(self, request):
            shipping = {"id": "ship-1", "name": "FedEx", "price": 21.42, "currency": "USD", "estimated_days": 5}
            quote = {
                "quote_id": "provider-quote-1",
                "provider": "craftcloud",
                "material": request.material_id,
                "quantity": request.quantity,
                "unit_price": 3.04,
                "total_price": 3.04,
                "currency": "USD",
                "lead_time_days": 5,
            }
            return SimpleNamespace(
                **quote,
                shipping_options=[SimpleNamespace(**shipping, to_dict=lambda: dict(shipping))],
                to_dict=lambda: {**quote, "shipping_options": [dict(shipping)]},
            )

    class MockBilling:
        def calculate_fee(self, price, *, currency="USD", **kwargs):
            class Fee:
                total_cost = price

                @staticmethod
                def to_dict():
                    return {"total_cost": price, "currency": currency}

            return Fee()

    class MockPayments:
        available_rails = []

    monkeypatch.setattr(server, "_get_fulfillment", lambda: MockProvider())
    monkeypatch.setattr(server, "_get_billing", lambda: MockBilling())
    monkeypatch.setattr(server, "_get_payment_mgr", lambda: MockPayments())
    monkeypatch.setattr(server, "_provider_routing_metadata", lambda provider, provider_order_id="": {"provider": provider})

    result = registered_tools["fulfillment_quote"](
        file_path=str(model),
        material_id="pla-gray",
        quantity=1,
        shipping_country="US",
    )

    assert result["success"] is True
    assert result["quote"]["quote_id"] == "provider-quote-1"
    assert get_cached_quote_by_id("provider-quote-1") is not None


def test_fulfillment_quote_leads_with_the_order_services_message(registered_tools, tmp_path, monkeypatch):
    """The quote reply opens with what the person must hear, worded by the
    order service, so an agent's summary cannot drop the terms or the
    more-copies prices (it did, live, 2026-10-12)."""
    service = order_service.provide(monkeypatch)
    import kiln.server as server
    from kiln.quote_cache import get_quote_cache

    get_quote_cache().invalidate_all()
    model = tmp_path / "model.stl"
    model.write_bytes(b"solid test\nendsolid test\n")
    quote = SimpleNamespace(
        quote_id="q-1", provider="craftcloud", material="pla", quantity=1,
        total_price=10.99, currency="USD", lead_time_days=7, shipping_options=[],
        to_dict=lambda: {"quote_id": "q-1"},
    )
    seen: dict = {}

    def fake_message(q, sheet, routing):
        seen.update(quote=q, sheet=sheet, routing=routing)
        return ["1 copy, delivered: 24.02 USD.", "Customs are yours."]

    monkeypatch.setattr(service, "price_sheet", lambda q, kiln_fee=0.0: {"total": 24.02}, raising=False)
    monkeypatch.setattr(service, "quote_message", fake_message, raising=False)
    monkeypatch.setattr(server, "_get_fulfillment", lambda: SimpleNamespace(name="craftcloud", get_quote=lambda r: quote))
    monkeypatch.setattr(server, "_get_billing", lambda: None)
    monkeypatch.setattr(server, "_get_payment_mgr", lambda: SimpleNamespace(available_rails=[]))
    routing = {"provider_terms_url": "https://terms.example", "provider_order_terms": ["Customs are yours."]}
    monkeypatch.setattr(server, "_provider_routing_metadata", lambda provider, provider_order_id="": dict(routing))

    result = registered_tools["fulfillment_quote"](file_path=str(model), material_id="pla")

    assert list(result)[:2] == ["success", "tell_the_person"], "the message comes before the data"
    assert result["tell_the_person"] == ["1 copy, delivered: 24.02 USD.", "Customs are yours."]
    assert seen["quote"] is quote and seen["sheet"] == {"total": 24.02} and seen["routing"] == routing
    assert result["quote"]["price_sheet"] == {"total": 24.02}


def test_fulfillment_order_requires_preview_confirmation(registered_tools, monkeypatch):
    order_service.provide(monkeypatch)
    result = registered_tools["fulfillment_order"](
        quote_id="quote-1",
        shipping_option_id="ship-1",
        shipping_address=ADDRESS,
        shipping_confirmation_token="fc_fake",
    )

    assert result["success"] is False
    assert result["error"]["code"] == "PREVIEW_NOT_CONFIRMED"


def test_fulfillment_order_requires_shipping_confirmation(registered_tools, tmp_path, monkeypatch):
    order_service.provide(monkeypatch)
    from kiln.preview_gate import get_preview_gate

    preview_file = tmp_path / "model.stl"
    preview_file.write_bytes(b"solid test\nendsolid test\n")
    preview_token = get_preview_gate().issue(str(preview_file)).token

    result = registered_tools["fulfillment_order"](
        quote_id="quote-1",
        shipping_option_id="ship-1",
        shipping_address=ADDRESS,
        preview_token=preview_token,
        preview_file_path=str(preview_file),
    )

    assert result["success"] is False
    assert result["error"]["code"] == "SHIPPING_NOT_CONFIRMED"


def _service_error(service, message: str, code: str) -> Exception:
    """The order service's error carrying ``code``, on either service."""
    exc = service.FulfillmentError(message)
    exc.code = code
    return exc


def test_order_status_leads_with_the_order_services_words(registered_tools, monkeypatch):
    """Where the order is -- and, unpaid, where to pay -- worded by the order
    service, first in the reply, the same words every other door says."""
    service = order_service.provide(monkeypatch)
    import kiln.server as server

    result = SimpleNamespace(to_dict=lambda: {"order_id": "ord-1", "status": "submitted"})
    seen: list = []

    def fake_words(r):
        seen.append(r)
        return ["Order 1528 is waiting for payment. To start it, pay on the print service's checkout: https://pay.example"]

    monkeypatch.setattr(service, "status_message", fake_words, raising=False)
    monkeypatch.setattr(
        server, "_get_fulfillment",
        lambda: SimpleNamespace(name="craftcloud", get_order_status=lambda order_id: result),
    )

    reply = registered_tools["fulfillment_order_status"](order_id="ord-1")

    assert list(reply)[:2] == ["success", "tell_the_person"], "the words come before the data"
    assert reply["tell_the_person"][0].endswith("https://pay.example")
    assert seen == [result]
    assert reply["order"]["order_id"] == "ord-1"


@pytest.mark.parametrize("code", ["NOTHING_TO_CANCEL", "CANCEL_NOT_SUPPORTED"])
def test_a_cancel_the_order_service_words_in_full_is_said_as_it_is(registered_tools, monkeypatch, code):
    """"Nothing to cancel" is not a failure to cancel something that shipped:
    the service's own sentence reaches the person, unwrapped."""
    service = order_service.provide(monkeypatch)
    import kiln.licensing
    import kiln.server as server

    sentence = "Nothing to cancel: order 1528 is waiting for payment."

    def refuse(order_id):
        raise _service_error(service, sentence, code)

    monkeypatch.setattr(server, "_check_billing_auth", lambda scope="print": None)
    monkeypatch.setattr(kiln.licensing, "check_tier", lambda *a, **k: (True, ""))
    monkeypatch.setattr(server, "_get_fulfillment", lambda: SimpleNamespace(name="craftcloud", cancel_order=refuse))

    reply = registered_tools["fulfillment_cancel"](order_id="ord-1")

    assert reply["success"] is False
    assert reply["error"]["message"] == sentence
    assert reply["error"]["code"] == code


def _place_order_through_the_tool(registered_tools, tmp_path, monkeypatch, placed, drift, **order_args):
    """``fulfillment_order`` end to end with a provider that answers ``placed``.

    The quote priced 16.58 to print plus 8.85 for the chosen shipping;
    ``drift`` stands in for the order service's price check and records what
    it was asked to compare.  The pay step is worded by the order service as
    "Pay here: <link>".
    """
    import sys

    service = order_service.provide(monkeypatch)
    import kiln.licensing
    import kiln.quote_cache
    import kiln.server as server
    from kiln.fulfillment_profiles import issue_shipping_confirmation_token, normalize_shipping_address
    from kiln.preview_gate import get_preview_gate

    model = tmp_path / "model.stl"
    model.write_bytes(b"solid test\nendsolid test\n")
    preview_token = get_preview_gate().issue(str(model)).token
    shipping_token = issue_shipping_confirmation_token(
        quote_id="quote-1", shipping_option_id="ship-1",
        shipping_address=normalize_shipping_address(ADDRESS),
    ).token

    provider = SimpleNamespace(name="proxy", place_order=lambda request: placed)
    monkeypatch.setattr(server, "_check_billing_auth", lambda scope="print": None)
    monkeypatch.setattr(kiln.licensing, "check_tier", lambda *a, **k: (True, ""))
    monkeypatch.setattr(server, "_get_fulfillment", lambda: provider)
    monkeypatch.setattr(
        server, "_validate_quote_for_order",
        lambda quote_id, provider_name=None: SimpleNamespace(quoted_price=16.58, warnings=[]),
    )
    monkeypatch.setattr(
        kiln.quote_cache, "get_cached_quote_by_id",
        lambda quote_id: SimpleNamespace(metadata={"shipping_options": [{"id": "ship-1", "price": 8.85}]}),
    )
    monkeypatch.setattr(service, "order_message", lambda r, routing: ["Pay here: https://pay.example"], raising=False)
    monkeypatch.setattr(sys.modules["kiln.fulfillment.intelligence"], "_check_price_drift", drift, raising=False)

    return registered_tools["fulfillment_order"](
        quote_id="quote-1",
        shipping_option_id="ship-1",
        shipping_address=ADDRESS,
        preview_token=preview_token,
        preview_file_path=str(model),
        shipping_confirmation_token=shipping_token,
        **order_args,
    )


def _placed(**amounts):
    return SimpleNamespace(
        order_id="ord-1", currency="USD",
        to_dict=lambda: {"order_id": "ord-1", "status": "submitted", "checkout_url": "https://pay.example"},
        **amounts,
    )


#: The order service's price check at its harshest: it flags a change past
#: its limit, in words that say "blocked".
_BLOCKING = ("Price changed significantly. Order blocked — please request a new quote.", True)


def test_an_order_whose_checkout_adds_shipping_and_tax_is_not_a_price_change(
    registered_tools, tmp_path, monkeypatch,
):
    """A quote's price is the printing alone; the placed order states the
    checkout's total, which adds shipping and sales tax.  Compared as they
    were, a 16.58 quote and a 27.55 checkout read as a 66% price jump and the
    order -- already placed -- was reported blocked, its pay link never
    shown.  Like with like: 16.58 + 8.85 shipping against 25.43 before tax."""
    compared: list[tuple[float, float]] = []

    def record_drift(quoted, placed):
        compared.append((quoted, placed))
        return None, False

    reply = _place_order_through_the_tool(
        registered_tools, tmp_path, monkeypatch,
        _placed(total_price=27.55, checkout_total=27.55, checkout_net=25.43), record_drift,
    )

    assert reply["success"] is True, reply
    assert compared == [(pytest.approx(25.43), pytest.approx(25.43))], "printing + shipping vs before tax"
    assert reply["tell_the_person"] == ["Pay here: https://pay.example"]


def test_a_real_price_change_on_a_placed_order_is_said_plainly_never_blocked(
    registered_tools, tmp_path, monkeypatch,
):
    """The order exists whatever the check finds: the person hears the new
    price and keeps the choice -- pay, or don't and it expires -- with the
    link right after, never "blocked" and never an error."""
    import json

    reply = _place_order_through_the_tool(
        registered_tools, tmp_path, monkeypatch,
        _placed(total_price=34.50, checkout_total=34.50, checkout_net=32.00), lambda q, p: _BLOCKING,
    )

    assert reply["success"] is True, reply
    assert reply["tell_the_person"] == [
        "Your order was placed, but the checkout total before tax is 32.00 USD, not the quoted "
        "25.43 USD. Pay only if that's fine; if not, just don't pay: an unpaid order is never "
        "made and expires on its own. If you don't pay, any fee Kiln charged for it comes back "
        "automatically once the order expires.",
        "Pay here: https://pay.example",
    ]
    assert reply["order"]["price_change"] == {"quoted": 25.43, "checkout_before_tax": 32.0, "currency": "USD"}
    assert "blocked" not in json.dumps(reply).lower()


def test_a_price_change_past_the_limit_leaves_kilns_fee_with_the_order(
    registered_tools, tmp_path, monkeypatch,
):
    """The order stays payable, so the fee is not handed back on the spot:
    a person could take the refund and still pay, and the part would be made
    with Kiln's fee returned.  The fee follows the order -- back only when it
    is cancelled or expires unpaid -- and the reply says exactly that."""
    import json

    import kiln.server as server

    refunds: list = []
    charges: list = []
    fee = SimpleNamespace(to_dict=lambda: {"fee_amount": 1.27, "waived": False}, total_cost=1.27, waived=False, fee_amount=1.27)
    held = SimpleNamespace(
        payment_id="pi_hold_1", rail=SimpleNamespace(value="stripe"), status=SimpleNamespace(value="completed"),
        to_dict=lambda: {"payment_id": "pi_hold_1", "status": "completed"},
    )
    monkeypatch.setattr(server, "_refund_after_order_failure", lambda *a, **k: refunds.append(a))
    monkeypatch.setattr(server, "_get_billing", lambda: SimpleNamespace(
        calculate_fee=lambda *a, **k: fee,
        record_charge=lambda *a, **k: charges.append(k),
    ))
    monkeypatch.setattr(server, "_get_payment_mgr", lambda: SimpleNamespace(
        available_rails=["stripe"], capture_fee=lambda hold, quote_id, fee_calc: held,
    ))
    reply = _place_order_through_the_tool(
        registered_tools, tmp_path, monkeypatch,
        _placed(total_price=34.50, checkout_total=34.50, checkout_net=32.00), lambda q, p: _BLOCKING,
        payment_hold_id="pi_hold_1",
    )

    assert reply["success"] is True, reply
    assert refunds == [], "no refund while the order can still be paid"
    assert reply["order"]["payment"]["status"] == "completed", "the fee is held, as charged"
    assert [c.get("payment_status") for c in charges] == ["completed"], "the billing record says charged"
    said = json.dumps(reply).lower()
    assert "refunded" not in said
    assert "comes back automatically once the order expires" in reply["tell_the_person"][0]


def test_prices_that_cannot_be_compared_are_stated_not_judged(registered_tools, tmp_path, monkeypatch):
    """No total before tax came back (an older service, another provider):
    nothing fair to compare, so nothing is judged and nothing is blocked --
    the person hears what is known, after the pay step."""
    import json

    asked: list = []

    def drift(quoted, placed):
        asked.append((quoted, placed))
        return _BLOCKING

    reply = _place_order_through_the_tool(
        registered_tools, tmp_path, monkeypatch, _placed(total_price=27.55), drift,
    )

    assert reply["success"] is True, reply
    assert asked == [], "a print price is never compared with a total that adds shipping and tax"
    assert reply["tell_the_person"] == [
        "Pay here: https://pay.example",
        "The print service's total for this order is 27.55 USD; the quoted 16.58 USD was the "
        "print price alone, before shipping and tax. Check the total on its checkout before you pay.",
    ]
    assert "blocked" not in json.dumps(reply).lower()


def _must_not_reach(*_args, **_kwargs):
    raise AssertionError("an order tool reached for a print-service provider")


@pytest.mark.parametrize(
    "tool, kwargs",
    [
        ("fulfillment_materials", {}),
        ("fulfillment_quote", {"file_path": "model.stl", "material_id": "pla-white"}),
        ("fulfillment_order", {"quote_id": "quote-1"}),
        ("fulfillment_order_status", {"order_id": "ord-1"}),
        ("fulfillment_cancel", {"order_id": "ord-1"}),
        ("fulfillment_alerts", {}),
    ],
)
def test_without_the_order_service_each_order_tool_says_where_ordering_lives(
    registered_tools, monkeypatch, tool, kwargs,
):
    order_service.remove(monkeypatch)
    import kiln.server as server

    monkeypatch.setattr(server, "_get_fulfillment", _must_not_reach)
    monkeypatch.setattr(server, "_get_fulfillment_monitor", _must_not_reach)

    result = registered_tools[tool](**kwargs)

    assert result["success"] is False
    assert result["error"]["code"] == "NOT_AVAILABLE"
    message = result["error"]["message"]
    assert "https://kiln3d.com/docs/connector" in message
    assert "nothing was sent" in message
    assert "kiln-pro" not in message


def test_the_order_status_tool_tells_the_agent_to_hand_over_the_pay_link_unasked(registered_tools):
    """Kiln cannot pay for the person; it can make paying one click.  On any
    sign they want to pay, the agent fetches the order and leads with the link."""
    said = " ".join((registered_tools["fulfillment_order_status"].__doc__ or "").split())
    assert "When the person wants to pay, asks how to pay, or asks about an order that is not paid yet" in said
    assert "give them the link to pay first" in said
