"""The person's choice about imported files with separate parts.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

from kiln.tool_annotations import destructive

_logger = logging.getLogger(__name__)


class _ImportMotionToolsPlugin:
    """Tools:
    - motion_for_imports
    """

    @property
    def name(self) -> str:
        return "import_motion_tools"

    @property
    def description(self) -> str:
        return "Whether an imported file with separate parts is sent to work out how they move"

    def register(self, mcp: Any) -> None:
        @mcp.tool(annotations=destructive("Choose whether imports are sent to work out how they move"))
        def motion_for_imports(on: bool | None = None) -> dict[str, Any]:
            """Whether Kiln sends a copy of an imported file with separate parts
            to its servers so it can work out how the parts move.

            Call with ``on=False`` when the person says to keep their imports
            on this computer, ``on=True`` when they ask Kiln to work out how
            their imports move, and with nothing to read the current choice.
            The choice belongs to this computer and stands until changed.  A
            file in one piece is never sent for this.  The copy is not one of
            the person's designs, and Kiln deletes it within about a month.

            Args:
                on: ``False`` to keep imports on this computer, ``True`` to
                    send a copy of each one with separate parts, left out to
                    read the current choice without changing it.

            Returns ``{"success", "sending", "chosen_here", "says"}``.  Read
            ``says`` to the person as written.
            """
            from kiln import import_motion

            if on is not None:
                import kiln.server as _srv

                # Changing what this computer sends is a setting, as the
                # other settings doors are.
                if err := _srv._check_auth("admin"):
                    return err
                try:
                    import_motion.set_sending(bool(on))
                except Exception as exc:  # noqa: BLE001 -- said, never raised
                    _logger.warning("motion_for_imports: the choice was not kept", exc_info=True)
                    return {
                        "success": False,
                        "error": f"Kiln could not keep that choice on this computer ({exc}). Nothing changed.",
                        "code": "NOT_KEPT",
                    }
            return {"success": True, **import_motion.state()}


plugin = _ImportMotionToolsPlugin()
