"""What one part can carry is asked for one part at a time, and a miss never reads as a yes.

``estimate_structural_load`` keeps the roster check and the free answer here;
the figure for a part is kiln-pro's (``kiln._pro_load_bridge``): kiln-pro on
this computer when it is here with its catalogue, else Kiln's servers through
the ``estimate_load_figure`` wire.  Nothing about a material's own figures
comes to this computer or is kept on it.

Kiln's servers are stood in for by patching ``kiln.server._pro_api_call``, the
door every served tool uses; the figures below belong to no material.
"""

from __future__ import annotations

import errno
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from kiln import _pro_load_bridge as load_bridge
from kiln.served_answer import Miss

#: A strength answer in the wire's shape, with figures that belong to no material.
_STRENGTH = {
    "material": "petg",
    "max_load_n": 7.77,
    "safety_factor": 3.0,
    "derating_applied": 0.5,
    "reasoning": ["kilntest reasoning"],
    "caution": "",
    "safety_basis": "kilntest basis",
}
#: An elastomer's answer in the wire's shape.
_DEFLECTION = {
    "material": "tpu",
    "limit_mode": "deflection",
    "load_at_deflection_limit_n": 0.1234,
    "allowable_deflection_mm": 10.0,
    "youngs_modulus_mpa": 1.0,
    "reasoning": [],
    "safety_basis": "kilntest serviceability",
}

#: Today's free answer, word for word (tiering decision of 2026-10-16).
_FREE_PETG = (
    "Kiln doesn't check load on the free plan, so treat this part as unverified for the weight it will carry."
    " Knocks, weight left on for months, humidity and any hardware bolted on all count against it."
    " Kiln Pro sizes it for the weight, how long it's held, and the way it's printed."
)
_FREE_PLA = (
    "Kiln doesn't check load on the free plan, so treat this part as unverified for the weight it will carry."
    " PLA sags over time under a load left on it."
    " Knocks, weight left on for months, humidity and any hardware bolted on all count against it."
    " Kiln Pro sizes it for the weight, how long it's held, and the way it's printed."
)


def _tool():
    """The registered ``estimate_structural_load`` tool, off a fake MCP."""
    from kiln.plugins.design_tools import plugin

    tools: dict = {}

    class FakeMCP:
        def tool(self_mcp, **_kwargs):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

    plugin.register(FakeMCP())
    return tools["estimate_structural_load"]


@pytest.fixture
def plan(monkeypatch):
    """Sets the CALLER's plan the tool reads, through ``kiln.licensing``
    (kiln-pro's licensing when it is installed, the account's plan when not)."""
    import kiln.licensing as licensing

    def _set(paid: bool) -> None:
        monkeypatch.setattr(
            licensing, "check_tier",
            lambda required, *a, **k: (True, None) if paid else (False, "This feature requires Kiln Pro."),
        )

    return _set


@pytest.fixture
def servers(monkeypatch):
    """Kiln's servers, stood in for: answers queued by the test, every ask
    recorded.  kiln-pro on this computer is hidden, so the wire is asked."""
    import kiln.server as srv

    asks: list[tuple[str, dict[str, Any]]] = []
    replies: list[Any] = []

    def call(tool_name, /, _timeout=None, _asked_by_user=True, **kwargs):
        asks.append((tool_name, kwargs))
        reply = replies.pop(0) if replies else {"success": True, "status": "ok", "known": True, "estimate": dict(_STRENGTH)}
        if isinstance(reply, BaseException):
            raise reply
        return json.loads(json.dumps(reply))

    monkeypatch.setattr(srv, "_pro_api_call", call)
    monkeypatch.setattr(load_bridge, "_local_available", lambda: False)
    return types.SimpleNamespace(asks=asks, replies=replies)


def _files_under(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()} if root.exists() else set()


# ---------------------------------------------------------------------------
# Below Kiln Pro: today's answer, word for word, and nothing asked
# ---------------------------------------------------------------------------


class TestTheFreeAnswer:
    @pytest.mark.parametrize(("material", "message"), [("petg", _FREE_PETG), ("pla", _FREE_PLA), ("PLA", _FREE_PLA)])
    def test_free_is_told_the_load_was_not_checked_word_for_word(self, plan, servers, material, message):
        plan(False)
        out = _tool()(material, 24.0, 100.0)
        assert out["success"] is True and out["load_checked"] is False
        assert out["material"] == material and out["message"] == message
        assert "max_load_n" not in out and "load_at_deflection_limit_n" not in out
        assert out["upgrade_recommendation"]["code"] == "LOAD_BEARING_DETECTED"
        assert servers.asks == [], "a free caller's part went to the servers"

    def test_an_unknown_material_is_refused_before_any_tier_or_ask(self, plan, servers):
        plan(True)
        out = _tool()("unobtanium", 24.0, 100.0)
        assert out["success"] is False and out["error"].startswith("Unknown load table material: unobtanium.")
        assert servers.asks == []


# ---------------------------------------------------------------------------
# Kiln Pro: the figure for THIS part, from the servers, kept nowhere
# ---------------------------------------------------------------------------


class TestTheProAnswer:
    def test_pro_gets_the_figure_for_the_part_it_described(self, plan, servers):
        plan(True)
        out = _tool()("petg", 24.0, 100.0, load_across_layers=False)
        assert out["success"] is True and "load_checked" not in out
        assert {k: out[k] for k in _STRENGTH} == _STRENGTH
        assert out["upgrade_recommendation"]["code"] == "LOAD_BEARING_DETECTED"
        assert servers.asks == [(
            "estimate_load_figure",
            {"material": "petg", "cross_section_mm2": 24.0, "cantilever_length_mm": 100.0, "load_across_layers": False},
        )]

    def test_only_the_part_is_sent_and_nothing_is_kept(self, plan, servers, tmp_path, monkeypatch):
        """No material figure arrives and nothing lands on this computer:
        the answer is relayed and dropped."""
        import kiln._pro_material_bridge as material_bridge

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(material_bridge, "KEEP_DIR", tmp_path / ".kiln" / "material_catalogue")
        before = _files_under(tmp_path)
        plan(True)
        _tool()("pla", 50.0, 40.0)
        _tool()("pla", 60.0, 40.0)
        assert _files_under(tmp_path) == before
        assert [set(kwargs) for _, kwargs in servers.asks] == [
            {"material", "cross_section_mm2", "cantilever_length_mm", "load_across_layers"}
        ] * 2

    def test_an_elastomer_answer_is_a_serviceability_limit_with_no_strength_nudge(self, plan, servers):
        plan(True)
        servers.replies.append({"success": True, "status": "ok", "known": True, "estimate": dict(_DEFLECTION)})
        out = _tool()("tpu", 24.0, 100.0)
        assert out == {**_DEFLECTION, "success": True}

    def test_a_material_kiln_has_no_figures_for_is_unknown(self, plan, servers):
        plan(True)
        servers.replies.append({"success": True, "status": "ok", "known": False, "estimate": None})
        out = _tool()("petg", 24.0, 100.0)
        assert out["success"] is False and out["error"].startswith("Unknown load table material: petg.")

    def test_the_gate_is_the_callers_plan(self, plan, servers, monkeypatch):
        """A/B: the gate used to read ``kiln.account_plan`` -- this machine's
        own sign-in, which on Kiln's servers is the server's, so a paying
        caller there was told load is not checked on the free plan.  Here
        the account plan says Free and the caller's plan says Pro."""
        import kiln.account_plan as account_plan

        monkeypatch.setattr(account_plan, "check_tier", lambda *a, **k: (False, "free"))
        monkeypatch.setattr(account_plan, "get_tier", lambda: account_plan.LicenseTier.FREE)
        plan(True)
        out = _tool()("petg", 24.0, 100.0)
        assert out.get("max_load_n") == _STRENGTH["max_load_n"], out


# ---------------------------------------------------------------------------
# A miss: unchecked, and why -- never a figure, never a yes
# ---------------------------------------------------------------------------


class TestAMiss:
    @pytest.mark.parametrize(
        ("reply", "why", "says"),
        [
            (OSError(errno.ENETUNREACH, "Network is unreachable"), "offline", "reconnect to the internet"),
            ({"status": "error", "success": False, "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "x"}, "signed_out", "sign in"),
            ({"status": "error", "success": False, "code": "SERVER_UNREACHABLE", "error": "x"}, "unanswered", "wait a minute"),
            ({"status": "error", "success": False, "code": "TIER_REQUIRED", "error": "Needs Kiln Pro."}, "refused", "Needs Kiln Pro."),
            ({"success": True, "status": "ok"}, "unanswered", "wait a minute"),
        ],
    )
    def test_every_miss_says_unchecked_and_why(self, plan, servers, reply, why, says):
        plan(True)
        servers.replies.append(reply)
        out = _tool()("petg", 24.0, 100.0)
        assert out["success"] is False and out["load_checked"] is False
        assert out["code"] == "LOAD_FIGURES_UNAVAILABLE" and out["why"] == why
        assert "unchecked for the weight it will carry" in out["error"]
        assert says.lower() in out["error"].lower()
        assert "petg" in out["error"]
        for figure in ("max_load_n", "load_at_deflection_limit_n", "estimate"):
            assert figure not in out
        assert out["retryable"] is (why != "refused")


# ---------------------------------------------------------------------------
# The bridge: kiln-pro here first, else the servers
# ---------------------------------------------------------------------------


class TestTheBridge:
    def _local_module(self, monkeypatch, answer):
        module = types.ModuleType("kiln_pro.load_capacity")
        calls: list[tuple] = []

        def local_answer(material, cross_section_mm2, cantilever_length_mm, *, load_across_layers=True):
            calls.append((material, cross_section_mm2, cantilever_length_mm, load_across_layers))
            if isinstance(answer, BaseException):
                raise answer
            return answer

        module.answer = local_answer
        package = types.ModuleType("kiln_pro")
        package.__path__ = []  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "kiln_pro", package)
        monkeypatch.setitem(sys.modules, "kiln_pro.load_capacity", module)
        monkeypatch.setattr(load_bridge, "_local_available", lambda: True)
        return calls

    def test_kiln_pro_here_answers_and_the_servers_are_not_asked(self, monkeypatch):
        import kiln.server as srv

        monkeypatch.setattr(srv, "_pro_api_call", lambda *a, **k: pytest.fail("asked the servers"))
        calls = self._local_module(monkeypatch, {"success": True, "status": "ok", "known": True, "estimate": dict(_STRENGTH)})
        estimate, miss = load_bridge.load_figure("petg", 24.0, 100.0, load_across_layers=False)
        assert miss is None and estimate == _STRENGTH
        assert calls == [("petg", 24.0, 100.0, False)]

    def test_a_refusal_on_kiln_s_servers_is_a_refusal(self, monkeypatch):
        class HostedIntelligencePolicyError(Exception):
            pass

        self._local_module(monkeypatch, HostedIntelligencePolicyError("caller not entitled to material_catalogue"))
        estimate, miss = load_bridge.load_figure("petg", 24.0, 100.0)
        assert estimate is None and miss == Miss("refused", "TIER_REQUIRED", load_bridge._PRO_REQUIRED)

    def test_kiln_pro_failing_here_is_a_miss_not_a_crash(self, monkeypatch):
        """The exception's text stays in this computer's log, never in the
        miss (tests/test_bridge_faults_stay_local.py walks every bridge)."""
        self._local_module(monkeypatch, RuntimeError("boom"))
        estimate, miss = load_bridge.load_figure("petg", 24.0, 100.0)
        assert estimate is None and miss.cause == "unanswered"
        assert miss.detail == "kiln-pro on this computer stopped" and "boom" not in miss.detail

    def test_kiln_pro_with_no_load_module_asks_the_servers(self, monkeypatch, servers):
        monkeypatch.setitem(sys.modules, "kiln_pro.load_capacity", None)
        monkeypatch.setattr(load_bridge, "_local_available", lambda: True)
        estimate, miss = load_bridge.load_figure("petg", 24.0, 100.0)
        assert miss is None and estimate == _STRENGTH and len(servers.asks) == 1

    def test_the_bridge_is_on_the_hosted_doors_roster(self):
        from kiln.served_answer import HOSTED_DOORS

        assert HOSTED_DOORS["kiln._pro_load_bridge"][0] == "served_answer"

    def test_the_bridge_holds_no_state(self):
        """Nothing kept between calls, in memory or on disk: no module-level
        container, no path."""
        source = Path(load_bridge.__file__).read_text(encoding="utf-8")
        assert "KEEP_DIR" not in source and ".kiln" not in source
        assert not [name for name, value in vars(load_bridge).items() if isinstance(value, (dict, list, set)) and not name.startswith("__")]
