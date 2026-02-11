"""Tests for kiln.fulfillment.registry — provider registry and factory."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiln.fulfillment.base import FulfillmentProvider
from kiln.fulfillment.craftcloud import CraftcloudProvider
from kiln.fulfillment.registry import (
    _REGISTRY,
    get_provider,
    get_provider_class,
    list_providers,
    register,
)
from kiln.fulfillment.sculpteo import SculpteoProvider
from kiln.fulfillment.shapeways import ShapewaysProvider


# ---------------------------------------------------------------------------
# list_providers
# ---------------------------------------------------------------------------


class TestListProviders:
    def test_builtins_registered(self):
        providers = list_providers()
        assert "craftcloud" in providers
        assert "shapeways" in providers
        assert "sculpteo" in providers

    def test_sorted_order(self):
        providers = list_providers()
        assert providers == sorted(providers)


# ---------------------------------------------------------------------------
# get_provider_class
# ---------------------------------------------------------------------------


class TestGetProviderClass:
    def test_get_craftcloud(self):
        assert get_provider_class("craftcloud") is CraftcloudProvider

    def test_get_shapeways(self):
        assert get_provider_class("shapeways") is ShapewaysProvider

    def test_get_sculpteo(self):
        assert get_provider_class("sculpteo") is SculpteoProvider

    def test_case_insensitive(self):
        assert get_provider_class("Craftcloud") is CraftcloudProvider
        assert get_provider_class("SHAPEWAYS") is ShapewaysProvider

    def test_unknown_provider_raises(self):
        with pytest.raises(KeyError, match="Unknown fulfillment provider"):
            get_provider_class("nonexistent")

    def test_unknown_provider_lists_available(self):
        with pytest.raises(KeyError, match="craftcloud"):
            get_provider_class("nonexistent")


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_custom_provider(self):
        class DummyProvider(FulfillmentProvider):
            @property
            def name(self): return "dummy"
            @property
            def display_name(self): return "Dummy"
            @property
            def supported_technologies(self): return []
            def list_materials(self): return []
            def get_quote(self, request): pass
            def place_order(self, request): pass
            def get_order_status(self, order_id): pass
            def cancel_order(self, order_id): pass

        register("dummy", DummyProvider)
        try:
            assert get_provider_class("dummy") is DummyProvider
            assert "dummy" in list_providers()
        finally:
            # Clean up
            _REGISTRY.pop("dummy", None)


# ---------------------------------------------------------------------------
# get_provider (factory)
# ---------------------------------------------------------------------------


class TestGetProvider:
    def test_explicit_name(self, monkeypatch):
        monkeypatch.setenv("KILN_CRAFTCLOUD_API_KEY", "test-key")
        provider = get_provider(name="craftcloud")
        assert isinstance(provider, CraftcloudProvider)

    def test_env_var_selection(self, monkeypatch):
        monkeypatch.setenv("KILN_FULFILLMENT_PROVIDER", "craftcloud")
        monkeypatch.setenv("KILN_CRAFTCLOUD_API_KEY", "test-key")
        provider = get_provider()
        assert isinstance(provider, CraftcloudProvider)

    def test_auto_detect_craftcloud(self, monkeypatch):
        monkeypatch.delenv("KILN_FULFILLMENT_PROVIDER", raising=False)
        monkeypatch.setenv("KILN_CRAFTCLOUD_API_KEY", "test-key")
        monkeypatch.delenv("KILN_SHAPEWAYS_CLIENT_ID", raising=False)
        monkeypatch.delenv("KILN_SCULPTEO_API_KEY", raising=False)
        provider = get_provider()
        assert isinstance(provider, CraftcloudProvider)

    def test_auto_detect_shapeways(self, monkeypatch):
        monkeypatch.delenv("KILN_FULFILLMENT_PROVIDER", raising=False)
        monkeypatch.delenv("KILN_CRAFTCLOUD_API_KEY", raising=False)
        monkeypatch.setenv("KILN_SHAPEWAYS_CLIENT_ID", "test-id")
        monkeypatch.setenv("KILN_SHAPEWAYS_CLIENT_SECRET", "test-secret")
        monkeypatch.delenv("KILN_SCULPTEO_API_KEY", raising=False)
        provider = get_provider()
        assert isinstance(provider, ShapewaysProvider)

    def test_auto_detect_sculpteo(self, monkeypatch):
        monkeypatch.delenv("KILN_FULFILLMENT_PROVIDER", raising=False)
        monkeypatch.delenv("KILN_CRAFTCLOUD_API_KEY", raising=False)
        monkeypatch.delenv("KILN_SHAPEWAYS_CLIENT_ID", raising=False)
        monkeypatch.setenv("KILN_SCULPTEO_API_KEY", "test-key")
        provider = get_provider()
        assert isinstance(provider, SculpteoProvider)

    def test_no_provider_configured(self, monkeypatch):
        monkeypatch.delenv("KILN_FULFILLMENT_PROVIDER", raising=False)
        monkeypatch.delenv("KILN_CRAFTCLOUD_API_KEY", raising=False)
        monkeypatch.delenv("KILN_SHAPEWAYS_CLIENT_ID", raising=False)
        monkeypatch.delenv("KILN_SCULPTEO_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="No fulfillment provider configured"):
            get_provider()

    def test_unknown_provider_name(self, monkeypatch):
        monkeypatch.setenv("KILN_FULFILLMENT_PROVIDER", "nonexistent")
        with pytest.raises(KeyError, match="Unknown fulfillment provider"):
            get_provider()

    def test_kwargs_passed_through(self, monkeypatch):
        monkeypatch.delenv("KILN_FULFILLMENT_PROVIDER", raising=False)
        monkeypatch.delenv("KILN_CRAFTCLOUD_API_KEY", raising=False)
        provider = get_provider(name="craftcloud", api_key="custom-key")
        assert isinstance(provider, CraftcloudProvider)
        assert provider._api_key == "custom-key"

    def test_env_provider_overrides_autodetect(self, monkeypatch):
        """Explicit KILN_FULFILLMENT_PROVIDER takes precedence."""
        monkeypatch.setenv("KILN_FULFILLMENT_PROVIDER", "sculpteo")
        monkeypatch.setenv("KILN_CRAFTCLOUD_API_KEY", "test-key")
        monkeypatch.setenv("KILN_SCULPTEO_API_KEY", "test-key")
        provider = get_provider()
        assert isinstance(provider, SculpteoProvider)
