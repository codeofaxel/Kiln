"""Universal generation provider registry.

Normalizes output from any AI text-to-3D backend (Meshy, Google Deep Think,
OpenAI Shap-E, Stability AI, Tripo3D) into Kiln's generation pipeline.
Auto-discovers providers from env vars.
"""

from __future__ import annotations

import logging
import os
import threading

from kiln.generation.base import GenerationError, GenerationProvider

logger = logging.getLogger(__name__)

# Env var → (provider module path, provider class name)
_ENV_PROVIDER_MAP: dict[str, tuple[str, str]] = {
    "KILN_MESHY_API_KEY": ("kiln.generation.meshy", "MeshyProvider"),
    "KILN_TRIPO3D_API_KEY": ("kiln.generation.tripo3d", "Tripo3DProvider"),
    "KILN_STABILITY_API_KEY": ("kiln.generation.stability", "StabilityProvider"),
    "KILN_GEMINI_API_KEY": ("kiln.generation.gemini", "GeminiDeepThinkProvider"),
}


class GenerationRegistry:
    """Thread-safe registry for generation providers.

    Stores provider instances keyed by name and supports auto-discovery
    from environment variables.

    Example::

        registry = GenerationRegistry()
        registry.auto_discover()
        provider = registry.get("meshy")
        job = provider.generate("a small vase")
    """

    def __init__(self) -> None:
        self._providers: dict[str, GenerationProvider] = {}
        self._lock = threading.Lock()

    def register(self, provider: GenerationProvider) -> None:
        """Register a provider instance.

        :param provider: A :class:`GenerationProvider` implementation.
        :raises ValueError: If a provider with the same name is already
            registered.
        """
        with self._lock:
            if provider.name in self._providers:
                raise ValueError(f"Provider {provider.name!r} is already registered.")
            self._providers[provider.name] = provider
            logger.info("Registered generation provider: %s", provider.name)

    def get(self, name: str) -> GenerationProvider:
        """Retrieve a registered provider by name.

        :param name: The provider's machine-readable name.
        :raises GenerationError: If no provider with that name is registered.
        """
        with self._lock:
            provider = self._providers.get(name)
        if provider is None:
            available = ", ".join(self.list_providers()) or "(none)"
            raise GenerationError(
                f"Generation provider {name!r} not found.  Available: {available}.",
                code="UNKNOWN_PROVIDER",
            )
        return provider

    def list_providers(self) -> list[str]:
        """Return the names of all registered providers."""
        with self._lock:
            return list(self._providers.keys())

    def auto_discover(self) -> list[str]:
        """Auto-register providers whose API key env vars are set.

        Checks ``KILN_MESHY_API_KEY``, ``KILN_TRIPO3D_API_KEY``,
        ``KILN_STABILITY_API_KEY``, and ``KILN_GEMINI_API_KEY``.
        For each env var that has a non-empty
        value, the corresponding provider class is imported and instantiated.

        :returns: List of provider names that were successfully registered.
        """
        import importlib

        registered: list[str] = []

        for env_var, (module_path, class_name) in _ENV_PROVIDER_MAP.items():
            api_key = os.environ.get(env_var, "").strip()
            if not api_key:
                continue

            # Skip if already registered.
            with self._lock:
                # Check by class name prefix (e.g. "meshy" from MeshyProvider).
                existing_names = set(self._providers.keys())

            try:
                mod = importlib.import_module(module_path)
                cls = getattr(mod, class_name)
                instance = cls(api_key=api_key)

                if instance.name in existing_names:
                    logger.debug(
                        "Provider %s already registered, skipping auto-discover",
                        instance.name,
                    )
                    continue

                self.register(instance)
                registered.append(instance.name)
                logger.info(
                    "Auto-discovered provider %s from %s",
                    instance.name,
                    env_var,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to auto-discover provider from %s: %s",
                    env_var,
                    exc,
                )

        return registered
