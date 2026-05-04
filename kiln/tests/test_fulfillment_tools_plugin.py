"""Tests for fulfillment MCP tool registration and safety helpers."""

from __future__ import annotations

import pytest

try:
    import kiln.fulfillment  # noqa: F401

    _has_fulfillment = True
except ImportError:
    _has_fulfillment = False

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
        def tool(self):
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


@pytest.mark.skipif(not _has_fulfillment, reason="kiln-pro fulfillment module not available")
def test_fulfillment_quote_caches_provider_quote_id(registered_tools, tmp_path, monkeypatch):
    from kiln.fulfillment import Quote, ShippingOption

    import kiln.server as server
    from kiln.quote_cache import get_cached_quote_by_id, get_quote_cache

    get_quote_cache().invalidate_all()
    model = tmp_path / "model.stl"
    model.write_bytes(b"solid test\nendsolid test\n")

    class MockProvider:
        name = "craftcloud"

        def get_quote(self, request):
            return Quote(
                quote_id="provider-quote-1",
                provider="craftcloud",
                material=request.material_id,
                quantity=request.quantity,
                unit_price=3.04,
                total_price=3.04,
                currency="USD",
                lead_time_days=5,
                shipping_options=[
                    ShippingOption(
                        id="ship-1",
                        name="FedEx",
                        price=21.42,
                        currency="USD",
                        estimated_days=5,
                    )
                ],
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


@pytest.mark.skipif(not _has_fulfillment, reason="kiln-pro fulfillment module not available")
def test_fulfillment_order_requires_preview_confirmation(registered_tools):
    result = registered_tools["fulfillment_order"](
        quote_id="quote-1",
        shipping_option_id="ship-1",
        shipping_address=ADDRESS,
        shipping_confirmation_token="fc_fake",
    )

    assert result["success"] is False
    assert result["error"]["code"] == "PREVIEW_NOT_CONFIRMED"


@pytest.mark.skipif(not _has_fulfillment, reason="kiln-pro fulfillment module not available")
def test_fulfillment_order_requires_shipping_confirmation(registered_tools, tmp_path):
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
