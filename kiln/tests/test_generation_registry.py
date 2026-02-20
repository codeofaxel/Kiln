"""Tests for kiln.generation.registry -- universal provider registry."""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

from kiln.generation.base import (
    GenerationError,
    GenerationJob,
    GenerationProvider,
    GenerationResult,
    GenerationStatus,
)
from kiln.generation.registry import GenerationRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeProvider(GenerationProvider):
    """Minimal provider for testing."""

    def __init__(self, provider_name: str = "fake", **kwargs):
        self._name = provider_name

    @property
    def name(self) -> str:
        return self._name

    @property
    def display_name(self) -> str:
        return self._name.title()

    def generate(self, prompt, *, format="stl", style=None, **kwargs):
        return GenerationJob(id="fake-1", provider=self.name, prompt=prompt, status=GenerationStatus.PENDING)

    def get_job_status(self, job_id):
        return GenerationJob(id=job_id, provider=self.name, prompt="", status=GenerationStatus.PENDING)

    def download_result(self, job_id, output_dir="/tmp"):
        return GenerationResult(
            job_id=job_id,
            provider=self.name,
            local_path="/tmp/fake.stl",
            format="stl",
            file_size_bytes=100,
            prompt="",
        )


# ---------------------------------------------------------------------------
# TestGenerationRegistryRegister
# ---------------------------------------------------------------------------


class TestGenerationRegistryRegister:
    def test_register_adds_provider(self):
        reg = GenerationRegistry()
        p = _FakeProvider("test_provider")
        reg.register(p)
        assert reg.get("test_provider") is p

    def test_register_duplicate_raises(self):
        reg = GenerationRegistry()
        reg.register(_FakeProvider("dup"))
        with pytest.raises(ValueError, match="already registered"):
            reg.register(_FakeProvider("dup"))

    def test_register_multiple_providers(self):
        reg = GenerationRegistry()
        reg.register(_FakeProvider("alpha"))
        reg.register(_FakeProvider("beta"))
        assert set(reg.list_providers()) == {"alpha", "beta"}


# ---------------------------------------------------------------------------
# TestGenerationRegistryGet
# ---------------------------------------------------------------------------


class TestGenerationRegistryGet:
    def test_get_existing_provider(self):
        reg = GenerationRegistry()
        p = _FakeProvider("existing")
        reg.register(p)
        assert reg.get("existing") is p

    def test_get_missing_provider_raises(self):
        reg = GenerationRegistry()
        with pytest.raises(GenerationError, match="not found"):
            reg.get("nonexistent")

    def test_get_missing_shows_available(self):
        reg = GenerationRegistry()
        reg.register(_FakeProvider("avail"))
        with pytest.raises(GenerationError, match="avail"):
            reg.get("missing")


# ---------------------------------------------------------------------------
# TestGenerationRegistryListProviders
# ---------------------------------------------------------------------------


class TestGenerationRegistryListProviders:
    def test_list_empty_registry(self):
        reg = GenerationRegistry()
        assert reg.list_providers() == []

    def test_list_returns_all_names(self):
        reg = GenerationRegistry()
        reg.register(_FakeProvider("a"))
        reg.register(_FakeProvider("b"))
        reg.register(_FakeProvider("c"))
        result = reg.list_providers()
        assert sorted(result) == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# TestGenerationRegistryAutoDiscover
# ---------------------------------------------------------------------------


class TestGenerationRegistryAutoDiscover:
    @patch.dict("os.environ", {"KILN_MESHY_API_KEY": "test-key-123"})
    @patch("kiln.generation.meshy.MeshyProvider.__init__", return_value=None)
    @patch("kiln.generation.meshy.MeshyProvider.name", new_callable=lambda: property(lambda self: "meshy"))
    def test_auto_discover_meshy(self, mock_name, mock_init):
        reg = GenerationRegistry()
        discovered = reg.auto_discover()
        assert "meshy" in discovered

    @patch.dict("os.environ", {}, clear=True)
    def test_auto_discover_no_env_vars(self):
        reg = GenerationRegistry()
        discovered = reg.auto_discover()
        assert discovered == []

    @patch.dict("os.environ", {"KILN_TRIPO3D_API_KEY": "test-tripo-key"})
    @patch("kiln.generation.tripo3d.Tripo3DProvider.__init__", return_value=None)
    @patch("kiln.generation.tripo3d.Tripo3DProvider.name", new_callable=lambda: property(lambda self: "tripo3d"))
    def test_auto_discover_tripo3d(self, mock_name, mock_init):
        reg = GenerationRegistry()
        discovered = reg.auto_discover()
        assert "tripo3d" in discovered

    @patch.dict("os.environ", {"KILN_STABILITY_API_KEY": "test-stab-key"})
    @patch("kiln.generation.stability.StabilityProvider.__init__", return_value=None)
    @patch("kiln.generation.stability.StabilityProvider.name", new_callable=lambda: property(lambda self: "stability"))
    def test_auto_discover_stability(self, mock_name, mock_init):
        reg = GenerationRegistry()
        discovered = reg.auto_discover()
        assert "stability" in discovered

    @patch.dict("os.environ", {"KILN_MESHY_API_KEY": "key"})
    @patch("kiln.generation.meshy.MeshyProvider.__init__", return_value=None)
    @patch("kiln.generation.meshy.MeshyProvider.name", new_callable=lambda: property(lambda self: "meshy"))
    def test_auto_discover_skips_already_registered(self, mock_name, mock_init):
        reg = GenerationRegistry()
        reg.register(_FakeProvider("meshy"))
        discovered = reg.auto_discover()
        assert "meshy" not in discovered


# ---------------------------------------------------------------------------
# TestGenerationRegistryThreadSafety
# ---------------------------------------------------------------------------


class TestGenerationRegistryThreadSafety:
    def test_concurrent_register_and_list(self):
        reg = GenerationRegistry()
        errors: list[str] = []

        def register_provider(idx: int):
            try:
                reg.register(_FakeProvider(f"provider_{idx}"))
            except Exception as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=register_provider, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(reg.list_providers()) == 20

    def test_concurrent_get(self):
        reg = GenerationRegistry()
        reg.register(_FakeProvider("shared"))
        results: list[GenerationProvider] = []

        def get_provider():
            results.append(reg.get("shared"))

        threads = [threading.Thread(target=get_provider) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 10
        assert all(p.name == "shared" for p in results)
