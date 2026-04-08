"""Cloud sync MCP tools — manage cloud synchronization.

Extracted from server.py for maintainability.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger("kiln.plugins.cloud_sync_tools")


class _CloudSyncToolsPlugin:
    """Plugin providing cloud sync management tools."""

    @property
    def name(self) -> str:
        return "cloud_sync_tools"

    @property
    def description(self) -> str:
        return "Cloud sync management (status, trigger, configure)"

    def register(self, mcp) -> None:  # noqa: ANN001
        import kiln.server as _srv

        @mcp.tool()
        @_srv.requires_tier(_srv.LicenseTier.PRO)
        def cloud_sync_status() -> dict:
            """Get the current cloud sync status."""
            cs = _srv._get_cloud_sync()
            if cs is None:
                return {"success": True, "status": {"enabled": False, "last_sync_status": "not_configured"}}
            return {"success": True, "status": cs.status().to_dict()}

        @mcp.tool()
        @_srv.requires_tier(_srv.LicenseTier.PRO)
        def cloud_sync_now() -> dict:
            """Trigger an immediate cloud sync cycle."""
            if err := _srv._check_auth("admin"):
                return err
            cs = _srv._get_cloud_sync()
            if cs is None:
                return _srv._error_dict("Cloud sync not configured.", code="NOT_CONFIGURED")
            try:
                result = cs.sync_now()
                return {"success": True, **result}
            except Exception as exc:
                logger.exception("Unexpected error in cloud_sync_now")
                return _srv._error_dict(f"Unexpected error in cloud_sync_now: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        @_srv.requires_tier(_srv.LicenseTier.PRO)
        def cloud_sync_configure(
            cloud_url: str,
            api_key: str,
            interval: float = 60.0,
        ) -> dict:
            """Configure and start cloud sync.

            Args:
                cloud_url: Base URL of the cloud sync endpoint.
                api_key: API key for authentication.
                interval: Sync interval in seconds (default 60).
            """
            if err := _srv._check_auth("admin"):
                return err
            try:
                from kiln.cloud_sync import CloudSyncManager, SyncConfig

                config = SyncConfig(
                    cloud_url=cloud_url,
                    api_key=api_key,
                    sync_interval_seconds=interval,
                )
                new_mgr = CloudSyncManager(
                    db=_srv.get_db(),
                    event_bus=_srv._get_event_bus(),
                    config=config,
                )
                _srv._set_cloud_sync(new_mgr)
                new_mgr.start()
                return {"success": True, "config": config.to_dict()}
            except Exception as exc:
                logger.exception("Unexpected error in cloud_sync_configure")
                return _srv._error_dict(f"Unexpected error in cloud_sync_configure: {exc}", code="INTERNAL_ERROR")


plugin = _CloudSyncToolsPlugin()
