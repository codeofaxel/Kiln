"""The one answer every ordering door gives on an install without the order service.

Covers: the helper's two answers (missing, present); what the sentence says
(where ordering lives, that nothing was sent, no package to go and install);
that the terminal and the agent tools say the identical sentence; and that
the cost comparison keeps its local half when the outsourced half cannot
answer.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from click.testing import CliRunner

from tests import _order_service as order_service

_CONNECTOR_DOCS = "https://kiln3d.com/docs/connector"


def _must_not_reach(*_args, **_kwargs):
    raise AssertionError("an ordering door reached for a print-service provider")


def _agent_tools() -> dict:
    tools: dict = {}

    class _MCP:
        def tool(self, **_kwargs):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

    from kiln.plugins.fulfillment_tools import plugin

    plugin.register(_MCP())
    return tools


class TestTheHelper:
    def test_a_plain_install_has_no_order_service(self, monkeypatch):
        order_service.remove(monkeypatch)
        from kiln.fulfillment_service import order_service as installed

        assert installed() is None

    def test_an_install_with_the_order_service_gets_it_back(self, monkeypatch):
        service = order_service.provide(monkeypatch)
        from kiln.fulfillment_service import order_service as installed

        assert installed() is service


class TestTheSentence:
    def test_says_where_ordering_lives_and_that_nothing_was_sent(self):
        from kiln.fulfillment_service import NOT_INCLUDED

        assert _CONNECTOR_DOCS in NOT_INCLUDED
        assert "nothing was sent" in NOT_INCLUDED

    def test_sends_nobody_to_a_package_they_cannot_install(self):
        from kiln.fulfillment_service import NOT_INCLUDED

        assert "kiln-pro" not in NOT_INCLUDED


class TestEveryDoorSaysIt:
    def test_the_terminal_and_the_agent_tools_say_the_identical_sentence(self, monkeypatch):
        order_service.remove(monkeypatch)
        import kiln.server as server
        from kiln.cli.main import cli

        monkeypatch.setattr(server, "_get_fulfillment", _must_not_reach)

        result = CliRunner().invoke(cli, ["order", "status", "ord-1", "--json"])
        terminal = json.loads(result.output)["error"]["message"]
        agent = _agent_tools()["fulfillment_order_status"](order_id="ord-1")["error"]["message"]

        assert terminal == agent
        assert _CONNECTOR_DOCS in terminal

    def test_compare_print_options_keeps_its_local_half_and_says_it_for_the_outsourced_half(
        self,
        monkeypatch,
        tmp_path,
    ):
        order_service.remove(monkeypatch)
        import kiln.server as server

        monkeypatch.setattr(server, "_get_fulfillment", _must_not_reach)
        estimator = MagicMock()
        estimator.estimate_from_file.side_effect = ValueError("no local estimate in this test")
        monkeypatch.setattr(server, "_get_cost_estimator", lambda: estimator)
        gcode = tmp_path / "part.gcode"
        gcode.write_text("G28\n")

        result = server.compare_print_options(file_path=str(gcode), fulfillment_material_id="pla-white")

        assert result["success"] is True
        assert result["local"]["error"] == "no local estimate in this test"
        assert result["fulfillment"]["available"] is False
        assert _CONNECTOR_DOCS in result["fulfillment"]["error"]
