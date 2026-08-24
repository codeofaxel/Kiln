"""A nudge rides the moment; it never becomes the moment.

Two places in public Kiln already explain, in prose, that running printers in
parallel is what the fleet tier sells: ``register_printer`` when a second
machine joins a one-at-a-time plan, and the print-start concurrency gate when a
job would run a second machine at once.  Both now also carry the structured
block a surface can render instead of parsing the sentence.

What these tests hold still is everything AROUND the block:

* the free action is untouched — the registration still succeeds, the waiting
  verdict still says wait, and neither reason string moves;
* the block appears only at the moment that already explained the tier, and is
  absent for a plan the moment does not fire on;
* a physical block (bed fit, hotend ceiling) never grows one — a nudge attached
  to a safety refusal would be selling into a warning;
* and the tier is named exactly once in the line a surface prints verbatim.
"""

from __future__ import annotations

import sys
import types

import pytest

from kiln.printers.base import PrinterCapabilities, PrinterState, PrinterStatus
from kiln.printers.print_gate import _concurrent_fleet_verdict
from kiln.tiers_and_terms import upgrade_nudge_block


class _FakeAdapter:
    """A real object, not a Mock — a Mock auto-creates the attributes the
    machine fingerprint falls back to, which would make these pass for the
    wrong reason."""

    def __init__(self, host="", serial="", state=PrinterStatus.IDLE):
        self.name = "moonraker"
        self.host = host
        self.serial = serial
        self.capabilities = PrinterCapabilities()
        self._state = state

    def get_state(self):
        return PrinterState(connected=True, state=self._state)


class _FakeRegistry:
    def __init__(self, machines: dict):
        self._m = machines

    @property
    def count(self):
        return len(self._m)

    def list_machines(self):
        return sorted(self._m)

    def get(self, name):
        return self._m[name]


@pytest.fixture
def fleet(monkeypatch):
    """Install a fake registry and a controllable tier cap."""
    import kiln.registry as registry_mod

    def _install(machines: dict, cap=1):
        monkeypatch.setattr(
            registry_mod, "get_registry", lambda: _FakeRegistry(machines)
        )
        # `kiln.licensing` is an alias kiln_pro installs at import time, so it
        # exists on a dev machine and not in public CI.  The gate tolerates
        # its absence; the fixture stands one up so the cap stays controlled
        # either way.
        lic = sys.modules.get("kiln.licensing")
        if lic is None:
            lic = types.ModuleType("kiln.licensing")
            monkeypatch.setitem(sys.modules, "kiln.licensing", lic)
        monkeypatch.setattr(lic, "get_tier", lambda: "free", raising=False)
        monkeypatch.setattr(
            lic, "max_printers_for_tier", lambda _t: cap, raising=False
        )

    return _install


# ---------------------------------------------------------------------------
# The block itself
# ---------------------------------------------------------------------------


class TestUpgradeNudgeBlock:
    def test_the_tier_is_named_once_when_the_preview_already_names_it(self):
        block = upgrade_nudge_block(
            variant="second_printer",
            tier="business",
            feature="Coordinated multi-printer queue",
            headline="Put both machines to work when jobs overlap.",
            outcome_preview=(
                "This printer is registered. Kiln Business would coordinate "
                "the queue."
            ),
            free_included="Machines stay usable one at a time.",
        )
        assert block["display_text"].lower().count("business") == 1

    def test_the_tier_is_named_once_when_the_preview_does_not(self):
        block = upgrade_nudge_block(
            variant="drawing_manufacturability",
            tier="business",
            feature="Drawing manufacturability and quoting",
            headline="Turn a customer drawing into a quoting decision.",
            outcome_preview=(
                "Check extracted requirements against the selected machine "
                "and material."
            ),
            free_included="Printability checks on your own meshes stay free.",
        )
        text = block["display_text"]
        assert text.startswith(block["outcome_preview"])
        assert text.lower().count("business") == 1

    def test_the_cta_states_the_price_once_and_last(self):
        block = upgrade_nudge_block(
            variant="second_printer",
            tier="business",
            feature="Coordinated multi-printer queue",
            headline="h",
            outcome_preview="o",
            free_included="f",
        )
        assert block["cta"] == {
            "kind": "view_tier",
            "tier": "business",
            "label": "See Kiln Business",
            "url": "https://kiln3d.com/pricing",
        }
        # The price belongs to the call to action, not to the sentence a
        # surface prints — copy that repeats it reads as a pitch.
        assert "pricing" not in block["display_text"]

    def test_the_copy_version_is_derived_from_the_variant(self):
        block = upgrade_nudge_block(
            variant="concurrent_queue", tier="business", feature="f",
            headline="h", outcome_preview="o", free_included="i",
        )
        assert block["copy_version"] == "concurrent_queue_v1"

    def test_every_schema_key_is_present_so_a_reader_needs_no_guard(self):
        block = upgrade_nudge_block(
            variant="v", tier="pro", feature="f", headline="h",
            outcome_preview="o", free_included="i",
        )
        assert set(block) == {
            "schema_version", "copy_version", "moment", "variant", "feature",
            "headline", "outcome_preview", "why_this_tier", "unlocks",
            "free_included", "context", "display_text", "cta",
        }
        assert block["schema_version"] == 1
        assert block["moment"] == "resource_threshold"

    def test_the_containers_are_fresh_each_call(self):
        first = upgrade_nudge_block(
            variant="v", tier="pro", feature="f", headline="h",
            outcome_preview="o", free_included="i",
        )
        first["unlocks"].append("mutated")
        first["context"]["k"] = "v"
        second = upgrade_nudge_block(
            variant="v", tier="pro", feature="f", headline="h",
            outcome_preview="o", free_included="i",
        )
        assert second["unlocks"] == []
        assert second["context"] == {}


# ---------------------------------------------------------------------------
# Second printer — registration is free, and stays free
# ---------------------------------------------------------------------------


class TestSecondPrinterMoment:
    def _register_second(self, monkeypatch, cap):
        import kiln.server as srv

        monkeypatch.setattr(srv, "_check_auth", lambda _s: None)
        monkeypatch.setattr(srv, "get_tier", lambda: "free", raising=False)
        monkeypatch.setattr(
            srv, "max_printers_for_tier", lambda _t: cap, raising=False,
        )
        reg = srv._get_registry()
        for name in list(reg.list_names()):
            reg.unregister(name)
        reg.register("first", _FakeAdapter(host="192.0.2.10"))
        try:
            return srv.register_printer(
                name="second",
                printer_type="moonraker",
                host="192.0.2.11",
                persist=False,
                verify_connection=False,
            )
        finally:
            for name in list(reg.list_names()):
                reg.unregister(name)

    def test_the_registration_still_succeeds_and_carries_the_block(
        self, monkeypatch,
    ):
        result = self._register_second(monkeypatch, cap=1)

        assert result["success"] is True, "possession is free at every tier"
        assert "1 printer at a time" in result["fleet_note"]
        block = result["upgrade_nudge"]
        assert block["variant"] == "second_printer"
        assert block["moment"] == "resource_threshold"
        assert block["cta"]["tier"] == "business"
        # What they keep without paying is part of the copy, not an omission.
        assert "one at a time" in block["free_included"]

    def test_a_plan_that_already_runs_them_in_parallel_gets_nothing(
        self, monkeypatch,
    ):
        result = self._register_second(monkeypatch, cap=50)

        assert result["success"] is True
        assert "fleet_note" not in result
        assert "upgrade_nudge" not in result


# ---------------------------------------------------------------------------
# Concurrent start — the verdict is the verdict
# ---------------------------------------------------------------------------


class TestConcurrentQueueMoment:
    def test_the_waiting_verdict_is_unchanged_and_carries_the_block(
        self, fleet,
    ):
        busy = _FakeAdapter(host="192.0.2.10", state=PrinterStatus.PRINTING)
        idle = _FakeAdapter(host="192.0.2.11")
        fleet({"voron": busy, "ender": idle}, cap=1)

        verdict = _concurrent_fleet_verdict(idle)

        assert verdict["blocked"] is True
        assert verdict["code"] == "TIER_CONCURRENT_PRINT_LIMIT"
        assert "voron" in verdict["reason"], "still names the busy printer"
        assert "pricing" in verdict["override_hint"]
        block = verdict["upgrade_nudge"]
        assert block["variant"] == "concurrent_queue"
        assert block["cta"]["tier"] == "business"
        assert "has not started" in block["free_included"]

    def test_serial_use_of_two_owned_printers_is_never_nudged(self, fleet):
        """Nothing was refused, so there is nothing to explain."""
        first = _FakeAdapter(host="192.0.2.10")
        second = _FakeAdapter(host="192.0.2.11")
        fleet({"voron": first, "ender": second}, cap=1)
        assert _concurrent_fleet_verdict(first) is None

    def test_a_plan_that_runs_them_in_parallel_is_never_nudged(self, fleet):
        busy = _FakeAdapter(host="192.0.2.10", state=PrinterStatus.PRINTING)
        idle = _FakeAdapter(host="192.0.2.11")
        fleet({"voron": busy, "ender": idle}, cap=50)
        assert _concurrent_fleet_verdict(idle) is None


class TestNudgesStayOffSafetyAndControl:
    def test_a_physical_block_carries_no_upgrade_copy(self):
        """A hotend that cannot reach the material is a fact about the world.
        Selling into it would put a price next to a warning."""
        from kiln.printers.print_gate import check_material_temp

        verdict = check_material_temp("bambu_a1", "peek")

        assert verdict is not None, "expected a real block, not a soft-pass"
        assert verdict["code"] == "MATERIAL_EXCEEDS_HOTEND"
        assert "upgrade_nudge" not in verdict

    def test_only_the_tier_refusal_builds_one(self):
        """Structural pin: the block is attached in exactly one function, so a
        later edit cannot quietly grow one on a physical or control path."""
        import inspect

        from kiln.printers import print_gate

        holders = [
            name
            for name, fn in vars(print_gate).items()
            if inspect.isfunction(fn)
            and fn.__module__ == print_gate.__name__
            and "upgrade_nudge_block(" in inspect.getsource(fn)
        ]
        assert holders == ["_concurrent_fleet_verdict"]

    def test_control_paths_never_reach_the_gate_at_all(self):
        """Status, pause, cancel and emergency stop must work on every machine
        at every tier — they never call start_print, so no nudge can reach
        them.  Pinned here too because that is the property being relied on."""
        import inspect

        from kiln.printers.base import PrinterAdapter

        for method in (
            "cancel_print", "pause_print", "emergency_stop", "get_state",
        ):
            src = inspect.getsource(getattr(PrinterAdapter, method))
            assert "run_adapter_gate" not in src
            assert "upgrade_nudge" not in src
