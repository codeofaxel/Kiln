"""Plugin system for Kiln.

Allows third-party extensions to hook into Kiln at defined extension
points.  Plugins are Python packages that register via entry points
(``kiln.plugins`` group).  The system provides hooks for event
listeners, custom MCP tools, CLI commands, and pre/post-print hooks.
"""

from __future__ import annotations

import enum
import importlib.metadata
import logging
import os
import threading
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums & dataclasses
# ---------------------------------------------------------------------------

class PluginHook(enum.Enum):
    """Extension points available to plugins."""

    TOOL = "tool"
    EVENT = "event"
    CLI = "cli"
    ADAPTER = "adapter"
    PRE_PRINT = "pre_print"
    POST_PRINT = "post_print"


@dataclass
class PluginInfo:
    """Metadata about a discovered plugin."""

    name: str
    version: str
    description: str = ""
    author: str = ""
    hooks: List[str] = field(default_factory=list)
    active: bool = False
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PluginContext:
    """Passed to plugins on activation, providing access to Kiln internals."""

    event_bus: Any = None
    registry: Any = None
    queue: Any = None
    mcp: Any = None
    db: Any = None


# ---------------------------------------------------------------------------
# Abstract plugin base class
# ---------------------------------------------------------------------------

class KilnPlugin(ABC):
    """Abstract base class that all Kiln plugins must implement.

    At minimum, plugins must provide :attr:`name` and :attr:`version`.
    All hook methods are optional and have default no-op implementations.

    Example::

        class MyPlugin(KilnPlugin):
            @property
            def name(self) -> str:
                return "my-plugin"

            @property
            def version(self) -> str:
                return "1.0.0"

            def on_activate(self, context: PluginContext) -> None:
                print("Plugin activated!")
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique plugin identifier."""

    @property
    @abstractmethod
    def version(self) -> str:
        """Semantic version string."""

    @property
    def description(self) -> str:
        """Human-readable description."""
        return ""

    @property
    def author(self) -> str:
        """Plugin author."""
        return ""

    def on_activate(self, context: PluginContext) -> None:
        """Called when the plugin is activated.

        The *context* provides access to the event bus, printer registry,
        job queue, MCP server, and database.
        """

    def on_deactivate(self) -> None:
        """Called when the plugin is deactivated."""

    def get_tools(self) -> List[Callable]:
        """Return a list of MCP tool functions to register.

        Each callable should be a function suitable for ``@mcp.tool()``.
        """
        return []

    def get_event_handlers(self) -> Dict[Any, Callable]:
        """Return event type → handler mappings to subscribe.

        Keys are :class:`~kiln.events.EventType` values (or ``None``
        for wildcard).
        """
        return {}

    def get_cli_commands(self) -> List[Any]:
        """Return Click commands to add to the CLI."""
        return []

    def pre_print_hook(self, job: Any, adapter: Any) -> Optional[str]:
        """Called before a print starts.

        Return an error string to block the print, or ``None`` to allow.
        """
        return None

    def post_print_hook(
        self, job: Any, adapter: Any, success: bool,
    ) -> None:
        """Called after a print completes or fails."""


# ---------------------------------------------------------------------------
# Plugin manager
# ---------------------------------------------------------------------------

class PluginManager:
    """Discovers, loads, and manages Kiln plugins.

    Plugins are discovered via ``importlib.metadata.entry_points``
    using the ``kiln.plugins`` group.
    """

    ENTRY_POINT_GROUP = "kiln.plugins"

    def __init__(self) -> None:
        self._plugins: Dict[str, KilnPlugin] = {}
        self._infos: Dict[str, PluginInfo] = {}
        self._lock = threading.Lock()
        self._context: Optional[PluginContext] = None

    def discover(self) -> List[PluginInfo]:
        """Discover installed plugins via entry points.

        Returns:
            List of :class:`PluginInfo` for all discovered plugins.
        """
        infos: List[PluginInfo] = []

        try:
            eps = importlib.metadata.entry_points()
            # Python 3.12+ returns a SelectableGroups, earlier returns dict
            if hasattr(eps, "select"):
                group_eps = eps.select(group=self.ENTRY_POINT_GROUP)
            elif isinstance(eps, dict):
                group_eps = eps.get(self.ENTRY_POINT_GROUP, [])
            else:
                group_eps = [
                    ep for ep in eps
                    if getattr(ep, "group", None) == self.ENTRY_POINT_GROUP
                ]
        except Exception:
            logger.debug("No plugins found", exc_info=True)
            return []

        # Only load plugins explicitly allowed in config
        _allowed = os.environ.get("KILN_ALLOWED_PLUGINS", "").split(",")
        _allowed = {p.strip() for p in _allowed if p.strip()}

        for ep in group_eps:
            if _allowed and ep.name not in _allowed:
                logger.warning(
                    "Plugin %s not in KILN_ALLOWED_PLUGINS allow-list, skipping. "
                    "Set KILN_ALLOWED_PLUGINS=%s to enable it.",
                    ep.name, ep.name,
                )
                continue

            try:
                plugin_cls = ep.load()
                if not (isinstance(plugin_cls, type)
                        and issubclass(plugin_cls, KilnPlugin)):
                    logger.warning(
                        "Plugin entry point %s does not point to a "
                        "KilnPlugin subclass, skipping",
                        ep.name,
                    )
                    continue

                plugin = plugin_cls()
                hooks: List[str] = []
                if plugin.get_tools():
                    hooks.append(PluginHook.TOOL.value)
                if plugin.get_event_handlers():
                    hooks.append(PluginHook.EVENT.value)
                if plugin.get_cli_commands():
                    hooks.append(PluginHook.CLI.value)

                info = PluginInfo(
                    name=plugin.name,
                    version=plugin.version,
                    description=plugin.description,
                    author=plugin.author,
                    hooks=hooks,
                    active=False,
                )

                with self._lock:
                    self._plugins[plugin.name] = plugin
                    self._infos[plugin.name] = info

                infos.append(info)
                logger.info("Discovered plugin: %s v%s", plugin.name, plugin.version)

            except Exception as exc:
                error_info = PluginInfo(
                    name=ep.name,
                    version="unknown",
                    error=str(exc),
                )
                with self._lock:
                    self._infos[ep.name] = error_info
                infos.append(error_info)
                logger.warning("Failed to load plugin %s: %s", ep.name, exc)

        return infos

    def activate_all(self, context: PluginContext) -> None:
        """Activate all discovered plugins."""
        self._context = context
        with self._lock:
            names = list(self._plugins.keys())

        for name in names:
            self.activate(name, context)

    def activate(self, name: str, context: Optional[PluginContext] = None) -> bool:
        """Activate a single plugin by name.

        Returns ``True`` if activation succeeded.
        """
        ctx = context or self._context
        if ctx is None:
            logger.error("Cannot activate plugin %s: no context", name)
            return False

        with self._lock:
            plugin = self._plugins.get(name)
            info = self._infos.get(name)

        if plugin is None or info is None:
            logger.error("Plugin %s not found", name)
            return False

        try:
            logger.info(
                "Activating plugin %s -- plugin has full system access",
                plugin.name,
            )
            plugin.on_activate(ctx)

            # Register MCP tools
            if ctx.mcp is not None:
                for tool_fn in plugin.get_tools():
                    ctx.mcp.tool()(tool_fn)

            # Subscribe event handlers
            if ctx.event_bus is not None:
                for event_type, handler in plugin.get_event_handlers().items():
                    ctx.event_bus.subscribe(event_type, handler)

            with self._lock:
                info.active = True
                info.error = None

            if ctx.event_bus is not None:
                from kiln.events import EventType
                ctx.event_bus.publish(
                    EventType.PLUGIN_LOADED,
                    data={"name": name, "version": plugin.version},
                    source="plugin_manager",
                )

            logger.info("Activated plugin: %s", name)
            return True

        except Exception as exc:
            with self._lock:
                info.error = str(exc)
            logger.error("Failed to activate plugin %s: %s", name, exc)

            if ctx.event_bus is not None:
                from kiln.events import EventType
                ctx.event_bus.publish(
                    EventType.PLUGIN_ERROR,
                    data={"name": name, "error": str(exc)},
                    source="plugin_manager",
                )

            return False

    def deactivate(self, name: str) -> bool:
        """Deactivate a plugin by name."""
        with self._lock:
            plugin = self._plugins.get(name)
            info = self._infos.get(name)

        if plugin is None or info is None:
            return False

        try:
            plugin.on_deactivate()
            with self._lock:
                info.active = False
            logger.info("Deactivated plugin: %s", name)
            return True
        except Exception as exc:
            logger.error("Error deactivating plugin %s: %s", name, exc)
            return False

    def list_plugins(self) -> List[PluginInfo]:
        """Return info for all discovered plugins."""
        with self._lock:
            return list(self._infos.values())

    def get_plugin_info(self, name: str) -> Optional[PluginInfo]:
        """Return info for a specific plugin."""
        with self._lock:
            return self._infos.get(name)

    def run_pre_print_hooks(self, job: Any, adapter: Any) -> Optional[str]:
        """Run all pre-print hooks.

        Returns the first error string (blocks the print), or ``None``
        if all plugins allow the print.
        """
        with self._lock:
            active = [
                (n, p) for n, p in self._plugins.items()
                if self._infos.get(n, PluginInfo(name=n, version="")).active
            ]

        for name, plugin in active:
            try:
                result = plugin.pre_print_hook(job, adapter)
                if result is not None:
                    return f"[{name}] {result}"
            except Exception as exc:
                logger.error(
                    "Plugin %s pre_print_hook error: %s", name, exc,
                )
        return None

    def run_post_print_hooks(
        self, job: Any, adapter: Any, success: bool,
    ) -> None:
        """Run all post-print hooks."""
        with self._lock:
            active = [
                (n, p) for n, p in self._plugins.items()
                if self._infos.get(n, PluginInfo(name=n, version="")).active
            ]

        for name, plugin in active:
            try:
                plugin.post_print_hook(job, adapter, success)
            except Exception as exc:
                logger.error(
                    "Plugin %s post_print_hook error: %s", name, exc,
                )

    def register_cli_hooks(self, cli_group: Any) -> None:
        """Register CLI commands from all active plugins."""
        with self._lock:
            active = [
                (n, p) for n, p in self._plugins.items()
                if self._infos.get(n, PluginInfo(name=n, version="")).active
            ]

        for name, plugin in active:
            try:
                for cmd in plugin.get_cli_commands():
                    cli_group.add_command(cmd)
            except Exception as exc:
                logger.error(
                    "Plugin %s CLI registration error: %s", name, exc,
                )
