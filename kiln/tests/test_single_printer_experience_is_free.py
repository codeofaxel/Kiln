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
    # Deprecated in favour of printer_status(detail="lite") and kept only as
    # a shim.  Listed because it is still a live door and a live door must
    # not start charging — NOT as part of the recommended surface, which is
    # why nothing user-facing points at it any more.
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


def _force_free_tier(monkeypatch) -> None:
    """Put this install on the free tier, however it decides tier.

    There are two licensing implementations and a test that names either
    one directly only runs on the install that has it.  Naming the
    private one is what broke this file in public CI: the import is
    unconditional, so every case here errored before it could assert
    anything, and the safety floor below went unchecked on the exact
    install — no kiln-pro, free tier — whose floor it describes.

    Where kiln-pro is absent the free tier is not a state to force, it
    is the only state there is, so there is nothing to patch.
    """
    try:
        from kiln_pro.enterprise import licensing
    except ImportError:
        return
    monkeypatch.setattr(licensing, "check_tier", lambda _tier: (False, "free"))


def _refusal_code(result: object) -> str | None:
    """The refusal code a tool returned, if it refused."""
    if not isinstance(result, dict):
        return None
    error = result.get("error")
    if isinstance(error, dict):
        return error.get("code")
    return result.get("code")


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
    _force_free_tier(monkeypatch)

    fn = _unwrap(name)
    assert "printer_name" in inspect.signature(fn).parameters, (
        f"{name} cannot be aimed at a machine — 'free for one printer' would "
        "silently mean 'free for the default printer only'."
    )

    # Called with a name that resolves to nothing: the honest answers are a
    # printer-not-found or an adapter/connection failure.  A TIER_REQUIRED
    # here means a licence just cost someone sight of a hot machine.
    result = fn(printer_name="no-such-printer-in-this-test")
    assert _refusal_code(result) != "TIER_REQUIRED", (
        f"{name} refused on tier at free — this is the safety floor "
        "print_gate promises never to charge for."
    )


@pytest.mark.parametrize("name", FLEET_TOOLS_REQUIRING_BUSINESS)
def test_acting_on_machines_together_needs_business(name, monkeypatch):
    """Pro is not enough — the fleet product starts at Business.

    ``fleet_status`` is the one this was written for: it sat at Pro, whose
    printer cap is 1, while every sibling had already moved to Business.
    """
    _force_free_tier(monkeypatch)

    result = _unwrap(name)()

    assert isinstance(result, dict) and result.get("code") == "TIER_REQUIRED", (
        f"{name} did not refuse below its tier"
    )
    # Read the tier off the refusal the caller actually receives, rather
    # than off a spy on the tier check.  Both licensing implementations
    # name the tier in ``required_tier``, but only one of them reaches a
    # tier check at all — the free-tier stub refuses from the decorator
    # without consulting anything, so a spy sees nothing to report and
    # this claim would go unverified wherever kiln-pro is not installed.
    gated_at = str(result.get("required_tier", "")).lower()
    assert gated_at in ("business", "enterprise"), (
        f"{name} is gated at {gated_at!r}; acting on machines together is a "
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
