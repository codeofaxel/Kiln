"""Fulfillment provider registry.

Maintains a name→class mapping so the rest of Kiln can instantiate any
supported fulfillment provider by its machine-readable name (e.g.
``"craftcloud"``, ``"sculpteo"``).

Usage::

    from kiln.fulfillment.registry import get_provider, list_providers

    provider = get_provider("craftcloud", api_key="sk-...")
    for name in list_providers():
        print(name)
"""

from __future__ import annotations

import os
from typing import Any

from kiln.fulfillment.base import FulfillmentProvider

# Registry populated at module load via _register_builtins().
_REGISTRY: dict[str, type[FulfillmentProvider]] = {}

# Env var name → provider name, for auto-detection of which provider is
# configured when KILN_FULFILLMENT_PROVIDER is not set explicitly.
_ENV_HINTS: dict[str, str] = {
    "KILN_CRAFTCLOUD_API_KEY": "craftcloud",
    "KILN_SCULPTEO_API_KEY": "sculpteo",
}


def register(name: str, cls: type[FulfillmentProvider]) -> None:
    """Register a provider class under *name* (lowercase)."""
    _REGISTRY[name.lower()] = cls


def list_providers() -> list[str]:
    """Return sorted list of registered provider names."""
    _ensure_builtins()
    return sorted(_REGISTRY)


def get_provider_class(name: str) -> type[FulfillmentProvider]:
    """Return the class for *name*, or raise ``KeyError``."""
    _ensure_builtins()
    try:
        return _REGISTRY[name.lower()]
    except KeyError:
        available = ", ".join(sorted(_REGISTRY))
        raise KeyError(f"Unknown fulfillment provider {name!r}. Available: {available}") from None


def get_provider(
    name: str | None = None,
    **kwargs: Any,
) -> FulfillmentProvider:
    """Instantiate and return a fulfillment provider.

    Args:
        name: Provider name (e.g. ``"craftcloud"``).  If *None*, reads
            ``KILN_FULFILLMENT_PROVIDER`` env var, then falls back to
            auto-detection based on which API-key env vars are set.
        **kwargs: Passed through to the provider constructor.

    Raises:
        KeyError: If *name* is not registered.
        RuntimeError: If no provider can be determined.
    """
    if name is None:
        name = os.environ.get("KILN_FULFILLMENT_PROVIDER", "").strip()

    if not name:
        # Auto-detect from env vars
        for env_var, provider_name in _ENV_HINTS.items():
            if os.environ.get(env_var):
                name = provider_name
                break

    if not name:
        raise RuntimeError(
            "No fulfillment provider configured.  "
            "Set KILN_FULFILLMENT_PROVIDER or a provider-specific API key "
            "environment variable (e.g. KILN_CRAFTCLOUD_API_KEY)."
        )

    cls = get_provider_class(name)
    return cls(**kwargs)


# ---------------------------------------------------------------------------
# Built-in provider registration
# ---------------------------------------------------------------------------

_BUILTINS_LOADED = False


def _ensure_builtins() -> None:
    """Lazily register the built-in providers on first access."""
    global _BUILTINS_LOADED  # noqa: PLW0603
    if _BUILTINS_LOADED:
        return
    _BUILTINS_LOADED = True

    from kiln.fulfillment.craftcloud import CraftcloudProvider
    from kiln.fulfillment.sculpteo import SculpteoProvider

    register("craftcloud", CraftcloudProvider)
    register("sculpteo", SculpteoProvider)
