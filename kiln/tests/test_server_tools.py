"""Tests for Kiln MCP server tools: health check, webhook management, and main() wiring.

Covers:
- kiln_health() — version, uptime, module status, bambu availability
- register_webhook() — registering a webhook endpoint
- list_webhooks() — listing endpoints (empty + populated)
- delete_webhook() — removing an endpoint (found + not found)
- main() — scheduler/webhook startup and atexit handler registration
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, PropertyMock, patch, call

import pytest

from kiln.events import EventBus
from kiln.queue import PrintQueue
from kiln.registry import PrinterRegistry
from kiln.server import (
    kiln_health,
    register_webhook,
    list_webhooks,
    delete_webhook,
    _scheduler,
    _webhook_mgr,
    _registry,
    _queue,
    _event_bus,
    _start_time,
    main,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_singletons():
    """Reset module-level singletons before each test so tests are isolated."""
    # Save state
    old_printers = dict(_registry._printers)
    old_jobs = dict(_queue._jobs)
    old_history = list(_event_bus._history)

    # Save webhook endpoints
    old_endpoints = dict(_webhook_mgr._endpoints)

    yield

    # Restore state
    _registry._printers.clear()
    _registry._printers.update(old_printers)
    _queue._jobs.clear()
    _queue._jobs.update(old_jobs)
    _event_bus._history.clear()
    _event_bus._history.extend(old_history)

    # Restore webhook endpoints
    _webhook_mgr._endpoints.clear()
    _webhook_mgr._endpoints.update(old_endpoints)


@pytest.fixture(autouse=True)
def _bypass_url_validation_and_auth(monkeypatch):
    """Bypass SSRF URL validation and auth so tests with fake hostnames work."""
    monkeypatch.setattr(
        "kiln.webhooks._validate_webhook_url",
        lambda url: (True, ""),
    )
    # Disable auth so tool calls succeed without a valid token
    from kiln.server import _auth
    monkeypatch.setattr(_auth, "_enabled", False)


# ---------------------------------------------------------------------------
# TestKilnHealth
# ---------------------------------------------------------------------------


class TestKilnHealth:
    """Tests for the kiln_health() MCP tool."""

    def test_basic_response_structure(self):
        """Health check returns expected top-level keys."""
        result = kiln_health()

        assert result["success"] is True
        assert result["healthy"] is True
        assert "version" in result
        assert "uptime_seconds" in result
        assert "uptime_human" in result
        assert "printers_registered" in result
        assert "queue_depth" in result
        assert "scheduler_running" in result
        assert "webhook_endpoints" in result
        assert "modules" in result

    def test_version_matches_package(self):
        """Reported version matches kiln.__version__."""
        import kiln

        result = kiln_health()
        assert result["version"] == kiln.__version__

    def test_uptime_calculation(self):
        """Uptime should be a non-negative number with proper human format."""
        result = kiln_health()

        assert result["uptime_seconds"] >= 0
        # uptime_human is in the form "Xh Ym Zs"
        human = result["uptime_human"]
        assert "h" in human
        assert "m" in human
        assert "s" in human

    @patch("kiln.server._start_time", new=time.time() - 3661)
    def test_uptime_human_format(self):
        """Human-readable uptime is correctly formatted for ~1h 1m."""
        result = kiln_health()

        assert result["uptime_human"].startswith("1h")
        assert result["uptime_seconds"] == pytest.approx(3661.0, abs=300.0)

    def test_module_status_flags(self):
        """modules dict contains all expected boolean flags."""
        result = kiln_health()
        modules = result["modules"]

        expected_keys = {
            "scheduler",
            "webhooks",
            "persistence",
            "auth_enabled",
            "billing",
            "thingiverse",
            "bambu_available",
        }
        assert expected_keys == set(modules.keys())

        # persistence and billing are always True
        assert modules["persistence"] is True
        assert modules["billing"] is True

    def test_scheduler_status_reflected(self):
        """scheduler_running reflects _scheduler.is_running."""
        result = kiln_health()

        # The test-time scheduler is not started so it should be False
        assert result["scheduler_running"] == _scheduler.is_running
        assert result["modules"]["scheduler"] == _scheduler.is_running

    def test_webhook_status_reflected(self):
        """modules['webhooks'] reflects _webhook_mgr.is_running."""
        result = kiln_health()
        assert result["modules"]["webhooks"] == _webhook_mgr.is_running

    def test_registry_count_reflected(self):
        """printers_registered reflects the registry count."""
        result = kiln_health()
        assert result["printers_registered"] == 0

    def test_queue_depth_reflected(self):
        """queue_depth reflects _queue.total_count."""
        _queue.submit("test.gcode")
        result = kiln_health()
        assert result["queue_depth"] >= 1

    def test_webhook_endpoints_count(self):
        """webhook_endpoints reflects actual registered endpoint count."""
        _webhook_mgr.register(url="https://example.com/hook")
        result = kiln_health()
        assert result["webhook_endpoints"] >= 1

    def test_bambu_available_when_importable(self):
        """bambu_available is True when paho-mqtt (BambuAdapter) is importable."""
        mock_module = MagicMock()
        with patch.dict("sys.modules", {"kiln.printers.bambu": mock_module}):
            mock_module.BambuAdapter = MagicMock()
            result = kiln_health()
            assert result["modules"]["bambu_available"] is True

    def test_bambu_unavailable_when_not_importable(self):
        """bambu_available is False when import fails."""
        with patch.dict("sys.modules", {"kiln.printers.bambu": None}):
            result = kiln_health()
            assert result["modules"]["bambu_available"] is False

    @patch.dict("os.environ", {"KILN_THINGIVERSE_TOKEN": "test-token-123"})
    @patch("kiln.server._THINGIVERSE_TOKEN", "test-token-123")
    def test_thingiverse_enabled_when_token_set(self):
        """thingiverse module flag is True when token is present."""
        result = kiln_health()
        assert result["modules"]["thingiverse"] is True

    @patch("kiln.server._THINGIVERSE_TOKEN", "")
    def test_thingiverse_disabled_when_no_token(self):
        """thingiverse module flag is False when token is empty."""
        result = kiln_health()
        assert result["modules"]["thingiverse"] is False


# ---------------------------------------------------------------------------
# TestWebhookTools
# ---------------------------------------------------------------------------


class TestWebhookTools:
    """Tests for register_webhook(), list_webhooks(), delete_webhook() MCP tools."""

    # -- register_webhook --------------------------------------------------

    def test_register_webhook_basic(self):
        """register_webhook returns success with endpoint ID and URL."""
        result = register_webhook(
            url="https://example.com/kiln-events",
            events=["job.completed", "job.failed"],
            secret="my-secret",
            description="CI notifications",
        )

        assert result["success"] is True
        assert "endpoint_id" in result
        assert result["url"] == "https://example.com/kiln-events"
        assert sorted(result["events"]) == ["job.completed", "job.failed"]
        assert "Webhook registered" in result["message"]

    def test_register_webhook_no_events_filter(self):
        """Omitting events subscribes to all (empty set)."""
        result = register_webhook(url="https://example.com/all")

        assert result["success"] is True
        assert result["events"] == []  # empty set sorted is []

    def test_register_webhook_no_secret(self):
        """Webhook without a secret is accepted."""
        result = register_webhook(url="https://example.com/nosecret")
        assert result["success"] is True

    def test_register_webhook_endpoint_appears_in_manager(self):
        """After registration, endpoint exists in _webhook_mgr."""
        result = register_webhook(url="https://example.com/check")
        eid = result["endpoint_id"]

        ep = _webhook_mgr.get_endpoint(eid)
        assert ep is not None
        assert ep.url == "https://example.com/check"

    # -- list_webhooks -----------------------------------------------------

    def test_list_webhooks_empty(self):
        """list_webhooks with no endpoints returns empty list."""
        result = list_webhooks()

        assert result["success"] is True
        assert result["endpoints"] == []
        assert result["count"] == 0

    def test_list_webhooks_populated(self):
        """list_webhooks returns all registered endpoints."""
        register_webhook(
            url="https://one.example.com",
            events=["job.completed"],
            description="First",
        )
        register_webhook(
            url="https://two.example.com",
            description="Second",
        )

        result = list_webhooks()

        assert result["success"] is True
        assert result["count"] == 2

        urls = {ep["url"] for ep in result["endpoints"]}
        assert urls == {"https://one.example.com", "https://two.example.com"}

        # Verify endpoint structure
        for ep in result["endpoints"]:
            assert "id" in ep
            assert "url" in ep
            assert "events" in ep
            assert "description" in ep
            assert "active" in ep

    def test_list_webhooks_shows_active_true(self):
        """Newly registered webhooks are active by default."""
        register_webhook(url="https://active.example.com")
        result = list_webhooks()
        assert result["endpoints"][0]["active"] is True

    # -- delete_webhook ----------------------------------------------------

    def test_delete_webhook_found(self):
        """Deleting an existing webhook returns success."""
        reg = register_webhook(url="https://delete-me.example.com")
        eid = reg["endpoint_id"]

        result = delete_webhook(eid)

        assert result["success"] is True
        assert "deleted" in result["message"].lower()

        # Verify it's actually gone
        remaining = list_webhooks()
        remaining_ids = {ep["id"] for ep in remaining["endpoints"]}
        assert eid not in remaining_ids

    def test_delete_webhook_not_found(self):
        """Deleting a non-existent webhook returns error with NOT_FOUND code."""
        result = delete_webhook("nonexistent-id-999")

        assert result["success"] is False
        assert result["error"]["code"] == "NOT_FOUND"
        assert "not found" in result["error"]["message"].lower()

    def test_delete_webhook_idempotent(self):
        """Deleting the same webhook twice -- second attempt returns NOT_FOUND."""
        reg = register_webhook(url="https://twice.example.com")
        eid = reg["endpoint_id"]

        first = delete_webhook(eid)
        assert first["success"] is True

        second = delete_webhook(eid)
        assert second["success"] is False
        assert second["error"]["code"] == "NOT_FOUND"

    # -- error handling ----------------------------------------------------

    def test_register_webhook_exception_handling(self):
        """register_webhook catches unexpected exceptions and returns error dict."""
        with patch.object(_webhook_mgr, "register", side_effect=RuntimeError("boom")):
            result = register_webhook(url="https://broken.example.com")

        assert result["success"] is False
        assert result["error"]["code"] == "INTERNAL_ERROR"
        assert "boom" in result["error"]["message"]

    def test_list_webhooks_exception_handling(self):
        """list_webhooks catches unexpected exceptions and returns error dict."""
        with patch.object(
            _webhook_mgr, "list_endpoints", side_effect=RuntimeError("oops")
        ):
            result = list_webhooks()

        assert result["success"] is False
        assert result["error"]["code"] == "INTERNAL_ERROR"

    def test_delete_webhook_exception_handling(self):
        """delete_webhook catches unexpected exceptions and returns error dict."""
        with patch.object(
            _webhook_mgr, "unregister", side_effect=RuntimeError("fail")
        ):
            result = delete_webhook("any-id")

        assert result["success"] is False
        assert result["error"]["code"] == "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# TestMainStartup
# ---------------------------------------------------------------------------


class TestMainStartup:
    """Tests for the main() entry point wiring."""

    @patch("kiln.server.mcp")
    @patch("kiln.server._webhook_mgr")
    @patch("kiln.server._scheduler")
    def test_main_starts_scheduler(self, mock_scheduler, mock_webhook_mgr, mock_mcp):
        """main() calls _scheduler.start()."""
        with patch("kiln.server.atexit") as mock_atexit:
            main()

        mock_scheduler.start.assert_called_once()

    @patch("kiln.server.mcp")
    @patch("kiln.server._webhook_mgr")
    @patch("kiln.server._scheduler")
    def test_main_starts_webhook_mgr(self, mock_scheduler, mock_webhook_mgr, mock_mcp):
        """main() calls _webhook_mgr.start()."""
        with patch("kiln.server.atexit") as mock_atexit:
            main()

        mock_webhook_mgr.start.assert_called_once()

    @patch("kiln.server.mcp")
    @patch("kiln.server._webhook_mgr")
    @patch("kiln.server._scheduler")
    def test_main_registers_atexit_handlers(
        self, mock_scheduler, mock_webhook_mgr, mock_mcp
    ):
        """main() registers atexit handlers for scheduler and webhook manager stop."""
        with patch("kiln.server.atexit") as mock_atexit:
            main()

        # atexit.register should be called with _scheduler.stop and _webhook_mgr.stop
        atexit_calls = mock_atexit.register.call_args_list
        registered_funcs = [c[0][0] for c in atexit_calls]
        assert mock_scheduler.stop in registered_funcs
        assert mock_webhook_mgr.stop in registered_funcs

    @patch("kiln.server.mcp")
    @patch("kiln.server._webhook_mgr")
    @patch("kiln.server._scheduler")
    def test_main_calls_mcp_run(self, mock_scheduler, mock_webhook_mgr, mock_mcp):
        """main() ends by calling mcp.run()."""
        with patch("kiln.server.atexit"):
            main()

        mock_mcp.run.assert_called_once()

    @patch("kiln.server.mcp")
    @patch("kiln.server._webhook_mgr")
    @patch("kiln.server._scheduler")
    def test_main_startup_order(self, mock_scheduler, mock_webhook_mgr, mock_mcp):
        """main() starts scheduler and webhooks before calling mcp.run()."""
        call_order = []

        mock_scheduler.start.side_effect = lambda: call_order.append("scheduler.start")
        mock_webhook_mgr.start.side_effect = lambda: call_order.append(
            "webhook_mgr.start"
        )
        mock_mcp.run.side_effect = lambda: call_order.append("mcp.run")

        with patch("kiln.server.atexit"):
            main()

        assert call_order.index("scheduler.start") < call_order.index("mcp.run")
        assert call_order.index("webhook_mgr.start") < call_order.index("mcp.run")
