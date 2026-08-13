"""Fleet is Business; one machine is free — and the line holds both ways.

Two claims are pinned here, because moving either one alone breaks Kiln.

**Fleet tooling is Business+.** Seeing and driving machines TOGETHER is the
fleet product.  ``fleet_status`` was the last public fleet tool still gated
at Pro while every sibling in ``plugins/fleet_tools.py`` had moved to
Business or Enterprise — a drift, not a decision: it lives in ``server.py``
and simply missed the sweep.  It was also incoherent on its own terms,
because Pro's printer cap is 1: a fleet dashboard sold to a tier that can
run one machine.

**The single-printer experience stays free — including a user's SECOND
machine.**  Registering more than one printer is free (only running them at
once is gated), so a free user with two printers is a supported bench, and
``print_gate._concurrent_fleet_verdict`` states the floor outright: "status,
pause, cancel, emergency stop work on every machine at every tier, always. A
licensing rule must never cost a user visibility or control of a hot
machine."

That floor is what makes gating the aggregate view safe: every per-printer
reader and every stop verb takes a ``printer_name`` and none is tier-gated,
so a free user can inspect and stop any machine they own by name.  If a
future change gates one of those, this file fails — and it should, because
at that point the paywall has reached a hot machine.
"""

from __future__ import annotations

import inspect

import pytest

from kiln import server

#: Every per-machine reader and control verb that must work at ANY tier.
#: Each must also accept a printer_name, or "free for one machine" quietly
#: means "free for the DEFAULT machine" — which is not the same promise.
FREE_PER_PRINTER_TOOLS = [
    "printer_status",
    "print_status_lite",
    "printer_snapshot",
    "monitor_print",
    "printer_stats",
    "emergency_status",
    "cancel_print",
    "pause_print",
    "resume_print",
    "emergency_stop",
]

#: Tools whose whole point is acting on machines TOGETHER.
FLEET_TOOLS_REQUIRING_BUSINESS = [
    "fleet_status",
    "fleet_analytics",
    "route_print_job",
    "fleet_submit_job",
    "fleet_job_status",
    "fleet_utilization",
]


def _unwrap(name: str):
    """Resolve a tool by name, however it was registered.

    Looked up through the MCP tool manager and not just as a module
    attribute: half these tools are contributed by plugins
    (``plugins/fleet_tools.py``), so an attribute-only lookup skipped
    exactly the fleet half of this file — a boundary test that quietly
    checks one side of the boundary is worse than none.
    """
    tool = server.mcp._tool_manager._tools.get(name) or getattr(server, name, None)
    if tool is None:
        pytest.fail(
            f"{name} is not registered — this file's premise (which tools "
            "exist and what they cost) has moved; update the lists."
        )
    return getattr(tool, "fn", tool)


@pytest.mark.parametrize("name", FREE_PER_PRINTER_TOOLS)
def test_a_single_machine_can_always_be_seen_and_stopped(name, monkeypatch):
    """No tier gate, and it can be aimed at a named machine.

    The tier check is done by calling at a forced-free tier rather than by
    reading decorators: a gate added by any mechanism (decorator, inline
    ``get_tier()`` comparison, a check inside the body) has to show up here.
    """
    from kiln_pro.enterprise import licensing

    monkeypatch.setattr(licensing, "check_tier", lambda tier: (False, "free"))

    fn = _unwrap(name)
    assert "printer_name" in inspect.signature(fn).parameters, (
        f"{name} cannot be aimed at a machine — 'free for one printer' would "
        "silently mean 'free for the default printer only'."
    )

    # Called with a name that resolves to nothing: the honest answers are a
    # printer-not-found or an adapter/connection failure.  A TIER_REQUIRED
    # here means a licence just cost someone sight of a hot machine.
    result = fn(printer_name="no-such-printer-in-this-test")
    if isinstance(result, dict):
        code = (result.get("error") or {}).get("code") if isinstance(
            result.get("error"), dict
        ) else result.get("code")
        assert code != "TIER_REQUIRED", (
            f"{name} refused on tier at free — this is the safety floor "
            "print_gate promises never to charge for."
        )


@pytest.mark.parametrize("name", FLEET_TOOLS_REQUIRING_BUSINESS)
def test_acting_on_machines_together_needs_business(name, monkeypatch):
    """Pro is not enough — the fleet product starts at Business.

    ``fleet_status`` is the one this was written for: it sat at Pro, whose
    printer cap is 1, while every sibling had already moved to Business.
    """
    from kiln_pro.enterprise import licensing

    seen: list[str] = []

    def _check(tier):
        seen.append(getattr(tier, "value", str(tier)))
        return (False, "insufficient tier")

    monkeypatch.setattr(licensing, "check_tier", _check)

    result = _unwrap(name)()

    assert isinstance(result, dict) and result.get("code") == "TIER_REQUIRED", (
        f"{name} did not refuse below its tier"
    )
    assert seen and seen[0] in ("business", "enterprise"), (
        f"{name} is gated at {seen[0]!r}; acting on machines together is a "
        "Business feature. Pro's printer cap is 1, so a fleet tool sold at "
        "Pro sells a view of a fleet the tier cannot run."
    )


def test_the_smallest_tool_set_spends_no_slot_on_a_refusal():
    """A weak model's whole surface is 15 tools — none may be a locked door.

    Both curated "smallest set" lists are checked, because they are separate
    lists maintained in separate modules and drifting apart is exactly how a
    Business tool ends up in a free user's essentials again.
    """
    from kiln.agent_loop import _ESSENTIAL_TOOLS
    from kiln.tool_tiers import TIER_ESSENTIAL

    for label, names in (
        ("tool_tiers.TIER_ESSENTIAL", set(TIER_ESSENTIAL)),
        ("agent_loop._ESSENTIAL_TOOLS", set(_ESSENTIAL_TOOLS)),
    ):
        offenders = names & set(FLEET_TOOLS_REQUIRING_BUSINESS)
        assert not offenders, (
            f"{label} offers {sorted(offenders)} to every user, including "
            "free ones who can only be refused. printer_status is the "
            "single-printer experience and is already in the list."
        )

    # And the free single-printer reader IS there — removing the fleet tool
    # must not have left a weak model with no way to check a printer.
    assert "printer_status" in set(TIER_ESSENTIAL)
    assert "printer_status" in set(_ESSENTIAL_TOOLS)
