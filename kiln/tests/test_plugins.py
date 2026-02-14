"""Tests for kiln.plugins — plugin system."""

from __future__ import annotations

from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from kiln.plugins import (
    KilnPlugin,
    PluginContext,
    PluginHook,
    PluginInfo,
    PluginManager,
)


# ---------------------------------------------------------------------------
# Test plugin implementations
# ---------------------------------------------------------------------------

class SamplePlugin(KilnPlugin):
    """Minimal test plugin."""

    @property
    def name(self) -> str:
        return "sample-plugin"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "A sample plugin for testing"

    @property
    def author(self) -> str:
        return "Test Author"


class ToolPlugin(KilnPlugin):
    """Plugin that provides MCP tools."""

    @property
    def name(self) -> str:
        return "tool-plugin"

    @property
    def version(self) -> str:
        return "0.1.0"

    def get_tools(self):
        def my_tool():
            return {"hello": "world"}
        return [my_tool]


class EventPlugin(KilnPlugin):
    """Plugin that subscribes to events."""

    def __init__(self):
        self.events_received = []

    @property
    def name(self) -> str:
        return "event-plugin"

    @property
    def version(self) -> str:
        return "0.2.0"

    def _handler(self, event):
        self.events_received.append(event)

    def get_event_handlers(self):
        return {None: self._handler}  # wildcard


class BlockingPlugin(KilnPlugin):
    """Plugin that blocks prints."""

    @property
    def name(self) -> str:
        return "blocking-plugin"

    @property
    def version(self) -> str:
        return "0.3.0"

    def pre_print_hook(self, job, adapter):
        return "Print blocked by test"


class ErrorPlugin(KilnPlugin):
    """Plugin that raises on activation."""

    @property
    def name(self) -> str:
        return "error-plugin"

    @property
    def version(self) -> str:
        return "0.0.1"

    def on_activate(self, context):
        raise RuntimeError("Activation failure")


class PostPrintPlugin(KilnPlugin):
    """Plugin with post-print hook."""

    def __init__(self):
        self.post_calls = []

    @property
    def name(self) -> str:
        return "post-plugin"

    @property
    def version(self) -> str:
        return "0.4.0"

    def post_print_hook(self, job, adapter, success):
        self.post_calls.append((job, success))


# ---------------------------------------------------------------------------
# PluginHook enum tests
# ---------------------------------------------------------------------------

class TestPluginHook:
    def test_values(self):
        assert PluginHook.TOOL.value == "tool"
        assert PluginHook.EVENT.value == "event"
        assert PluginHook.CLI.value == "cli"
        assert PluginHook.ADAPTER.value == "adapter"
        assert PluginHook.PRE_PRINT.value == "pre_print"
        assert PluginHook.POST_PRINT.value == "post_print"


# ---------------------------------------------------------------------------
# PluginInfo tests
# ---------------------------------------------------------------------------

class TestPluginInfo:
    def test_creation(self):
        info = PluginInfo(name="test", version="1.0")
        assert info.name == "test"
        assert info.version == "1.0"
        assert info.active is False
        assert info.error is None

    def test_to_dict(self):
        info = PluginInfo(
            name="test", version="1.0",
            description="desc", author="auth",
            hooks=["tool"], active=True,
        )
        d = info.to_dict()
        assert d["name"] == "test"
        assert d["active"] is True
        assert d["hooks"] == ["tool"]


# ---------------------------------------------------------------------------
# PluginContext tests
# ---------------------------------------------------------------------------

class TestPluginContext:
    def test_creation(self):
        ctx = PluginContext(event_bus="bus", registry="reg")
        assert ctx.event_bus == "bus"
        assert ctx.registry == "reg"
        assert ctx.queue is None
        assert ctx.mcp is None
        assert ctx.db is None


# ---------------------------------------------------------------------------
# KilnPlugin ABC tests
# ---------------------------------------------------------------------------

class TestKilnPluginABC:
    def test_sample_plugin(self):
        p = SamplePlugin()
        assert p.name == "sample-plugin"
        assert p.version == "1.0.0"
        assert p.description == "A sample plugin for testing"
        assert p.author == "Test Author"

    def test_default_methods(self):
        p = SamplePlugin()
        assert p.get_tools() == []
        assert p.get_event_handlers() == {}
        assert p.get_cli_commands() == []
        assert p.pre_print_hook(None, None) is None
        p.post_print_hook(None, None, True)  # should not raise
        p.on_activate(PluginContext())  # should not raise
        p.on_deactivate()  # should not raise


# ---------------------------------------------------------------------------
# PluginManager discovery tests
# ---------------------------------------------------------------------------

class TestPluginManagerDiscovery:
    def test_initial_state(self):
        mgr = PluginManager()
        assert mgr.list_plugins() == []

    @patch("kiln.plugins.importlib.metadata.entry_points")
    def test_discover_no_plugins(self, mock_eps):
        mock_eps.return_value = {}
        mgr = PluginManager()
        result = mgr.discover()
        assert result == []

    @patch("kiln.plugins.importlib.metadata.entry_points")
    def test_discover_with_plugin(self, mock_eps, monkeypatch):
        monkeypatch.setenv("KILN_PLUGIN_POLICY", "permissive")
        ep = MagicMock()
        ep.name = "sample"
        ep.load.return_value = SamplePlugin

        # Simulate dict-style return
        mock_eps.return_value = {"kiln.plugins": [ep]}

        mgr = PluginManager()
        result = mgr.discover()
        assert len(result) == 1
        assert result[0].name == "sample-plugin"
        assert result[0].version == "1.0.0"

    @patch("kiln.plugins.importlib.metadata.entry_points")
    def test_discover_bad_entry_point(self, mock_eps, monkeypatch):
        monkeypatch.setenv("KILN_PLUGIN_POLICY", "permissive")
        ep = MagicMock()
        ep.name = "bad"
        ep.load.side_effect = ImportError("not found")

        mock_eps.return_value = {"kiln.plugins": [ep]}

        mgr = PluginManager()
        result = mgr.discover()
        assert len(result) == 1
        assert result[0].error is not None

    @patch("kiln.plugins.importlib.metadata.entry_points")
    def test_discover_non_plugin_class(self, mock_eps, monkeypatch):
        monkeypatch.setenv("KILN_PLUGIN_POLICY", "permissive")
        ep = MagicMock()
        ep.name = "notaplugin"
        ep.load.return_value = str  # not a KilnPlugin subclass

        mock_eps.return_value = {"kiln.plugins": [ep]}

        mgr = PluginManager()
        result = mgr.discover()
        assert len(result) == 0  # skipped

    @patch("kiln.plugins.importlib.metadata.entry_points")
    def test_discover_strict_default_blocks_unallowlisted(self, mock_eps, monkeypatch):
        monkeypatch.delenv("KILN_PLUGIN_POLICY", raising=False)
        monkeypatch.delenv("KILN_ALLOWED_PLUGINS", raising=False)
        ep = MagicMock()
        ep.name = "sample"
        ep.load.return_value = SamplePlugin
        mock_eps.return_value = {"kiln.plugins": [ep]}

        mgr = PluginManager()
        result = mgr.discover()
        assert len(result) == 1
        assert result[0].name == "sample"
        assert result[0].version == "blocked"
        assert result[0].error is not None

    @patch("kiln.plugins.importlib.metadata.entry_points")
    def test_discover_strict_with_allowlist_loads_plugin(self, mock_eps, monkeypatch):
        monkeypatch.setenv("KILN_PLUGIN_POLICY", "strict")
        monkeypatch.setenv("KILN_ALLOWED_PLUGINS", "sample")
        ep = MagicMock()
        ep.name = "sample"
        ep.load.return_value = SamplePlugin
        mock_eps.return_value = {"kiln.plugins": [ep]}

        mgr = PluginManager()
        result = mgr.discover()
        assert len(result) == 1
        assert result[0].name == "sample-plugin"
        assert result[0].version == "1.0.0"


# ---------------------------------------------------------------------------
# PluginManager activation tests
# ---------------------------------------------------------------------------

class TestPluginManagerActivation:
    def _make_manager_with(self, plugin_cls):
        mgr = PluginManager()
        plugin = plugin_cls()
        mgr._plugins[plugin.name] = plugin
        mgr._infos[plugin.name] = PluginInfo(
            name=plugin.name, version=plugin.version,
        )
        return mgr

    def test_activate_sets_active(self):
        mgr = self._make_manager_with(SamplePlugin)
        ctx = PluginContext()
        result = mgr.activate("sample-plugin", ctx)
        assert result is True
        info = mgr.get_plugin_info("sample-plugin")
        assert info.active is True

    def test_activate_calls_on_activate(self):
        mgr = self._make_manager_with(SamplePlugin)
        plugin = mgr._plugins["sample-plugin"]
        plugin.on_activate = MagicMock()
        ctx = PluginContext()
        mgr.activate("sample-plugin", ctx)
        plugin.on_activate.assert_called_once_with(ctx)

    def test_activate_registers_tools(self):
        mgr = self._make_manager_with(ToolPlugin)
        mcp_mock = MagicMock()
        mcp_mock.tool.return_value = lambda f: f
        ctx = PluginContext(mcp=mcp_mock)
        mgr.activate("tool-plugin", ctx)
        mcp_mock.tool.assert_called()

    def test_activate_subscribes_events(self):
        mgr = self._make_manager_with(EventPlugin)
        bus_mock = MagicMock()
        ctx = PluginContext(event_bus=bus_mock)
        mgr.activate("event-plugin", ctx)
        bus_mock.subscribe.assert_called_once()

    def test_activate_publishes_loaded_event(self):
        mgr = self._make_manager_with(SamplePlugin)
        bus_mock = MagicMock()
        ctx = PluginContext(event_bus=bus_mock)
        mgr.activate("sample-plugin", ctx)
        assert bus_mock.publish.called

    def test_activate_failure_sets_error(self):
        mgr = self._make_manager_with(ErrorPlugin)
        ctx = PluginContext()
        result = mgr.activate("error-plugin", ctx)
        assert result is False
        info = mgr.get_plugin_info("error-plugin")
        assert info.error is not None
        assert "Activation failure" in info.error

    def test_activate_failure_publishes_error(self):
        mgr = self._make_manager_with(ErrorPlugin)
        bus_mock = MagicMock()
        ctx = PluginContext(event_bus=bus_mock)
        mgr.activate("error-plugin", ctx)
        assert bus_mock.publish.called

    def test_activate_not_found(self):
        mgr = PluginManager()
        ctx = PluginContext()
        result = mgr.activate("nonexistent", ctx)
        assert result is False

    def test_activate_no_context(self):
        mgr = self._make_manager_with(SamplePlugin)
        result = mgr.activate("sample-plugin")
        assert result is False

    def test_activate_all(self):
        mgr = PluginManager()
        for cls in [SamplePlugin, ToolPlugin]:
            p = cls()
            mgr._plugins[p.name] = p
            mgr._infos[p.name] = PluginInfo(name=p.name, version=p.version)

        ctx = PluginContext()
        mgr.activate_all(ctx)
        for info in mgr.list_plugins():
            assert info.active is True


# ---------------------------------------------------------------------------
# PluginManager deactivation tests
# ---------------------------------------------------------------------------

class TestPluginManagerDeactivation:
    def test_deactivate(self):
        mgr = PluginManager()
        p = SamplePlugin()
        mgr._plugins[p.name] = p
        mgr._infos[p.name] = PluginInfo(name=p.name, version=p.version, active=True)

        result = mgr.deactivate("sample-plugin")
        assert result is True
        assert mgr.get_plugin_info("sample-plugin").active is False

    def test_deactivate_not_found(self):
        mgr = PluginManager()
        result = mgr.deactivate("nonexistent")
        assert result is False

    def test_deactivate_calls_on_deactivate(self):
        mgr = PluginManager()
        p = SamplePlugin()
        p.on_deactivate = MagicMock()
        mgr._plugins[p.name] = p
        mgr._infos[p.name] = PluginInfo(name=p.name, version=p.version, active=True)

        mgr.deactivate("sample-plugin")
        p.on_deactivate.assert_called_once()


# ---------------------------------------------------------------------------
# PluginManager listing tests
# ---------------------------------------------------------------------------

class TestPluginManagerListing:
    def test_list_plugins(self):
        mgr = PluginManager()
        p = SamplePlugin()
        mgr._infos[p.name] = PluginInfo(name=p.name, version=p.version)
        plugins = mgr.list_plugins()
        assert len(plugins) == 1
        assert plugins[0].name == "sample-plugin"

    def test_get_plugin_info(self):
        mgr = PluginManager()
        p = SamplePlugin()
        mgr._infos[p.name] = PluginInfo(name=p.name, version=p.version)
        info = mgr.get_plugin_info("sample-plugin")
        assert info is not None
        assert info.version == "1.0.0"

    def test_get_plugin_info_not_found(self):
        mgr = PluginManager()
        assert mgr.get_plugin_info("nope") is None


# ---------------------------------------------------------------------------
# Pre/post-print hook tests
# ---------------------------------------------------------------------------

class TestPluginHooks:
    def test_pre_print_all_pass(self):
        mgr = PluginManager()
        p = SamplePlugin()
        mgr._plugins[p.name] = p
        mgr._infos[p.name] = PluginInfo(name=p.name, version=p.version, active=True)

        result = mgr.run_pre_print_hooks(MagicMock(), MagicMock())
        assert result is None

    def test_pre_print_blocks(self):
        mgr = PluginManager()
        p = BlockingPlugin()
        mgr._plugins[p.name] = p
        mgr._infos[p.name] = PluginInfo(name=p.name, version=p.version, active=True)

        result = mgr.run_pre_print_hooks(MagicMock(), MagicMock())
        assert result is not None
        assert "blocked" in result.lower()

    def test_pre_print_only_active(self):
        mgr = PluginManager()
        p = BlockingPlugin()
        mgr._plugins[p.name] = p
        mgr._infos[p.name] = PluginInfo(name=p.name, version=p.version, active=False)

        result = mgr.run_pre_print_hooks(MagicMock(), MagicMock())
        assert result is None  # not active, so not run

    def test_post_print_calls_all(self):
        mgr = PluginManager()
        p = PostPrintPlugin()
        mgr._plugins[p.name] = p
        mgr._infos[p.name] = PluginInfo(name=p.name, version=p.version, active=True)

        job, adapter = MagicMock(), MagicMock()
        mgr.run_post_print_hooks(job, adapter, True)
        assert len(p.post_calls) == 1
        assert p.post_calls[0] == (job, True)

    def test_post_print_exception_doesnt_propagate(self):
        mgr = PluginManager()
        p = SamplePlugin()
        p.post_print_hook = MagicMock(side_effect=RuntimeError("boom"))
        mgr._plugins[p.name] = p
        mgr._infos[p.name] = PluginInfo(name=p.name, version=p.version, active=True)

        # Should not raise
        mgr.run_post_print_hooks(MagicMock(), MagicMock(), True)

    def test_pre_print_exception_doesnt_propagate(self):
        mgr = PluginManager()
        p = SamplePlugin()
        p.pre_print_hook = MagicMock(side_effect=RuntimeError("boom"))
        mgr._plugins[p.name] = p
        mgr._infos[p.name] = PluginInfo(name=p.name, version=p.version, active=True)

        result = mgr.run_pre_print_hooks(MagicMock(), MagicMock())
        assert result is None  # exception caught, doesn't block


# ---------------------------------------------------------------------------
# CLI hooks tests
# ---------------------------------------------------------------------------

class TestPluginCLIHooks:
    def test_register_cli_hooks(self):
        mgr = PluginManager()
        p = SamplePlugin()
        mgr._plugins[p.name] = p
        mgr._infos[p.name] = PluginInfo(name=p.name, version=p.version, active=True)

        cli_group = MagicMock()
        mgr.register_cli_hooks(cli_group)
        # SamplePlugin has no CLI commands, so add_command not called
        cli_group.add_command.assert_not_called()

    def test_register_cli_hooks_with_commands(self):
        mgr = PluginManager()
        p = SamplePlugin()
        cmd = MagicMock()
        p.get_cli_commands = MagicMock(return_value=[cmd])
        mgr._plugins[p.name] = p
        mgr._infos[p.name] = PluginInfo(name=p.name, version=p.version, active=True)

        cli_group = MagicMock()
        mgr.register_cli_hooks(cli_group)
        cli_group.add_command.assert_called_once_with(cmd)
