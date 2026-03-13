"""Tests for kiln.plugin_loader — internal tool-plugin discovery and registration.

Covers:
- discover_plugins: package import, module iteration, protocol validation
- register_all_plugins: registration delegation, error handling, counting
- Edge cases: import failures, missing plugin attr, bad protocol, empty package
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock, patch

import kiln.plugin_loader as _loader_mod
from kiln.plugin_loader import ToolPlugin, discover_plugins, register_all_plugins

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _GoodPlugin:
    """Satisfies the ToolPlugin protocol."""

    name = "test_tools"
    description = "Test plugin for unit tests."

    def register(self, mcp):
        pass


class _BadPlugin:
    """Missing the register method — does NOT satisfy ToolPlugin."""

    name = "bad_tools"
    description = "Missing register."


# ---------------------------------------------------------------------------
# ToolPlugin protocol
# ---------------------------------------------------------------------------


class TestToolPluginProtocol:
    """Verify runtime_checkable protocol behavior."""

    def test_good_plugin_satisfies_protocol(self):
        assert isinstance(_GoodPlugin(), ToolPlugin)

    def test_bad_plugin_does_not_satisfy_protocol(self):
        assert not isinstance(_BadPlugin(), ToolPlugin)

    def test_plain_object_does_not_satisfy_protocol(self):
        assert not isinstance("hello", ToolPlugin)


# ---------------------------------------------------------------------------
# discover_plugins
# ---------------------------------------------------------------------------


class TestDiscoverPlugins:
    """Tests for discover_plugins()."""

    def test_returns_empty_when_package_import_fails(self):
        orig = _loader_mod.importlib.import_module
        _loader_mod.importlib.import_module = MagicMock(side_effect=ImportError("nope"))
        try:
            result = discover_plugins("nonexistent.package")
        finally:
            _loader_mod.importlib.import_module = orig
        assert result == []

    def test_returns_empty_when_package_has_no_path(self):
        """Non-package modules (plain .py files) have no __path__."""
        fake_module = types.ModuleType("fake_pkg")
        orig = _loader_mod.importlib.import_module
        _loader_mod.importlib.import_module = MagicMock(return_value=fake_module)
        try:
            result = discover_plugins("fake_pkg")
        finally:
            _loader_mod.importlib.import_module = orig
        assert result == []

    def _run_discover(self, import_map, iter_modules_return):
        """Helper: patch importlib.import_module and pkgutil.iter_modules on the loader module."""
        orig_import = _loader_mod.importlib.import_module
        orig_iter = _loader_mod.pkgutil.iter_modules

        def _import_side_effect(name):
            if name in import_map:
                return import_map[name]
            raise ImportError(name)

        _loader_mod.importlib.import_module = _import_side_effect
        _loader_mod.pkgutil.iter_modules = lambda *a, **kw: iter_modules_return
        try:
            return discover_plugins("fake_pkg")
        finally:
            _loader_mod.importlib.import_module = orig_import
            _loader_mod.pkgutil.iter_modules = orig_iter

    def test_discovers_valid_plugin(self):
        fake_pkg = types.ModuleType("fake_pkg")
        fake_pkg.__path__ = ["/fake/path"]
        good_mod = types.ModuleType("fake_pkg.good")
        good_mod.plugin = _GoodPlugin()  # type: ignore[attr-defined]

        plugins = self._run_discover(
            {"fake_pkg": fake_pkg, "fake_pkg.good": good_mod},
            [("finder", "good", False)],
        )
        assert len(plugins) == 1
        assert plugins[0].name == "test_tools"

    def test_skips_module_without_plugin_attr(self):
        fake_pkg = types.ModuleType("fake_pkg")
        fake_pkg.__path__ = ["/fake/path"]
        empty_mod = types.ModuleType("fake_pkg.empty")

        plugins = self._run_discover(
            {"fake_pkg": fake_pkg, "fake_pkg.empty": empty_mod},
            [("finder", "empty", False)],
        )
        assert plugins == []

    def test_skips_module_with_bad_protocol(self):
        fake_pkg = types.ModuleType("fake_pkg")
        fake_pkg.__path__ = ["/fake/path"]
        bad_mod = types.ModuleType("fake_pkg.bad")
        bad_mod.plugin = _BadPlugin()  # type: ignore[attr-defined]

        plugins = self._run_discover(
            {"fake_pkg": fake_pkg, "fake_pkg.bad": bad_mod},
            [("finder", "bad", False)],
        )
        assert plugins == []

    def test_skips_module_that_fails_to_import(self):
        fake_pkg = types.ModuleType("fake_pkg")
        fake_pkg.__path__ = ["/fake/path"]
        # "fake_pkg.broken" not in the map → ImportError

        plugins = self._run_discover(
            {"fake_pkg": fake_pkg},
            [("finder", "broken", False)],
        )
        assert plugins == []

    def test_discovers_multiple_plugins(self):
        fake_pkg = types.ModuleType("fake_pkg")
        fake_pkg.__path__ = ["/fake/path"]

        mod_a = types.ModuleType("fake_pkg.a")
        plugin_a = _GoodPlugin()
        plugin_a.name = "plugin_a"
        mod_a.plugin = plugin_a  # type: ignore[attr-defined]

        mod_b = types.ModuleType("fake_pkg.b")
        plugin_b = _GoodPlugin()
        plugin_b.name = "plugin_b"
        mod_b.plugin = plugin_b  # type: ignore[attr-defined]

        plugins = self._run_discover(
            {"fake_pkg": fake_pkg, "fake_pkg.a": mod_a, "fake_pkg.b": mod_b},
            [("finder", "a", False), ("finder", "b", False)],
        )
        assert len(plugins) == 2
        names = {p.name for p in plugins}
        assert names == {"plugin_a", "plugin_b"}


# ---------------------------------------------------------------------------
# register_all_plugins
# ---------------------------------------------------------------------------


class TestRegisterAllPlugins:
    """Tests for register_all_plugins()."""

    def test_registers_discovered_plugins(self):
        plugin = _GoodPlugin()
        mock_mcp = MagicMock()

        with patch("kiln.plugin_loader.discover_plugins", return_value=[plugin]):
            count = register_all_plugins(mock_mcp, plugin_package="fake")

        assert count == 1

    def test_returns_zero_when_no_plugins(self):
        mock_mcp = MagicMock()

        with patch("kiln.plugin_loader.discover_plugins", return_value=[]):
            count = register_all_plugins(mock_mcp, plugin_package="fake")

        assert count == 0

    def test_continues_on_registration_error(self):
        """If one plugin's register() raises, others still get registered."""
        good = _GoodPlugin()

        failing = _GoodPlugin()
        failing.name = "failing_plugin"
        failing.register = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore[assignment]

        mock_mcp = MagicMock()

        with patch("kiln.plugin_loader.discover_plugins", return_value=[failing, good]):
            count = register_all_plugins(mock_mcp, plugin_package="fake")

        assert count == 1  # only the good one succeeded

    def test_passes_mcp_to_register(self):
        plugin = MagicMock(spec=_GoodPlugin)
        plugin.name = "mock_plugin"
        plugin.description = "mock"

        # Make it satisfy the protocol check
        plugin.register = MagicMock()
        mock_mcp = MagicMock()

        with patch("kiln.plugin_loader.discover_plugins", return_value=[plugin]):
            register_all_plugins(mock_mcp, plugin_package="fake")

        plugin.register.assert_called_once_with(mock_mcp)
