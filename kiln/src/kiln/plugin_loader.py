"""Internal tool-plugin loader for Kiln.

Provides auto-discovery and registration of internal tool modules from a
plugin directory.  This complements the third-party entry-point plugin
system in :mod:`kiln.plugins` by enabling server.py's monolithic tool
definitions to be split into focused modules without changing the public
API.

Each tool-plugin module must define a :data:`plugin` module-level variable
that implements the :class:`ToolPlugin` protocol.

Usage::

    from kiln.plugin_loader import register_all_plugins

    register_all_plugins(mcp, plugin_dir=Path(__file__).parent / "plugins")
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Any, Protocol, runtime_checkable

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ToolPlugin protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ToolPlugin(Protocol):
    """Protocol that internal tool-plugin modules must implement.

    Each plugin module should expose a module-level ``plugin`` variable
    that satisfies this interface.  The :func:`register_all_plugins`
    loader will call :meth:`register` for each discovered plugin.

    Example::

        class _MyPlugin:
            name = "my_tools"
            description = "A set of tools for X."

            def register(self, mcp: Any) -> None:
                @mcp.tool()
                def my_tool() -> dict:
                    return {"success": True}

        plugin = _MyPlugin()
    """

    @property
    def name(self) -> str:
        """Short identifier for this plugin (e.g. ``"marketplace_tools"``)."""
        ...

    @property
    def description(self) -> str:
        """Human-readable summary of the tools provided."""
        ...

    def register(self, mcp: Any) -> None:
        """Register all tools with the given FastMCP server instance.

        This method should call ``mcp.tool()`` for each tool function
        this plugin provides.
        """
        ...


# ---------------------------------------------------------------------------
# Discovery and registration
# ---------------------------------------------------------------------------


def discover_plugins(plugin_package: str) -> list[ToolPlugin]:
    """Import all modules from *plugin_package* and collect their plugins.

    Each module is expected to expose a ``plugin`` attribute that satisfies
    the :class:`ToolPlugin` protocol.  Modules without a ``plugin``
    attribute or whose ``plugin`` does not satisfy the protocol are
    silently skipped with a debug log.

    If a module fails to import, the error is logged and that module is
    skipped — other plugins continue loading (graceful degradation).

    :param plugin_package: Dotted import path of the plugin package
        (e.g. ``"kiln.plugins"``).
    :returns: List of :class:`ToolPlugin` instances found.
    """
    plugins: list[ToolPlugin] = []

    try:
        package = importlib.import_module(plugin_package)
    except Exception:
        _logger.warning(
            "Could not import plugin package %s — no tool plugins loaded",
            plugin_package,
            exc_info=True,
        )
        return plugins

    package_path = getattr(package, "__path__", None)
    if package_path is None:
        _logger.debug(
            "%s is not a package (no __path__); skipping tool-plugin discovery",
            plugin_package,
        )
        return plugins

    for _finder, module_name, _is_pkg in pkgutil.iter_modules(package_path):
        fqn = f"{plugin_package}.{module_name}"
        try:
            mod = importlib.import_module(fqn)
        except Exception:
            _logger.error(
                "Failed to import tool-plugin module %s — skipping",
                fqn,
                exc_info=True,
            )
            continue

        candidate = getattr(mod, "plugin", None)
        if candidate is None:
            _logger.debug("Module %s has no 'plugin' attribute — skipping", fqn)
            continue

        if not isinstance(candidate, ToolPlugin):
            _logger.warning(
                "Module %s has a 'plugin' attribute but it does not satisfy the ToolPlugin protocol — skipping",
                fqn,
            )
            continue

        plugins.append(candidate)
        _logger.debug("Discovered tool plugin: %s", candidate.name)

    return plugins


def register_all_plugins(
    mcp: Any,
    *,
    plugin_package: str = "kiln.plugins",
) -> int:
    """Discover and register all internal tool plugins.

    Imports every module in *plugin_package*, collects those that expose
    a :class:`ToolPlugin`-compatible ``plugin`` attribute, and calls
    ``plugin.register(mcp)`` for each one.

    If a plugin fails to register, the error is logged and the remaining
    plugins continue loading.

    :param mcp: The :class:`~mcp.server.fastmcp.FastMCP` server instance.
    :param plugin_package: Dotted import path of the plugin package.
    :returns: Number of plugins successfully registered.
    """
    plugins = discover_plugins(plugin_package)
    registered = 0

    for p in plugins:
        try:
            p.register(mcp)
            registered += 1
            _logger.info(
                "Registered tool plugin: %s (%s)",
                p.name,
                p.description,
            )
        except Exception:
            _logger.error(
                "Failed to register tool plugin %s — skipping",
                p.name,
                exc_info=True,
            )

    if registered:
        _logger.info(
            "Loaded %d/%d tool plugin(s) from %s",
            registered,
            len(plugins),
            plugin_package,
        )

    return registered
