"""A machine is a machine, and a fleet is what you run in parallel.

Two defects, one root.  The server registers the active printer as
``"default"`` AND again under its config.yaml name, so ONE printer filled
two registry slots: ``printer_count`` telemetry read 2 for a single
machine (18 of 19 production installs, which read as "most free users run
fleets" until the alias was found), the user saw their printer twice, and
the tier cap's arithmetic was wrong before it was ever consulted.

And the cap only ever existed on one door — the ``register_printer``
tool — while ``kiln config add-printer`` and config.yaml auto-load
registered printers uncapped.  Enforcement now sits at the print-start
chokepoint every entry point funnels through, and gates *parallel use*
rather than possession: owning two printers and using them one at a time
is honest single-machine use and stays free.
"""

from __future__ import annotations

import sys
import types

import pytest

from kiln.printers.base import (
    PrinterCapabilities,
    PrinterState,
    PrinterStatus,
)
from kiln.printers.print_gate import _concurrent_fleet_verdict
from kiln.registry import PrinterRegistry, machine_fingerprint


class _FakeAdapter:
    """A real object, not a Mock — a Mock auto-creates ``_serial`` and
    ``_host``, which silently defeats the fingerprint's fallback chain and
    would make these tests pass for the wrong reason."""

    def __init__(
        self, family="moonraker", host="", serial="", state=PrinterStatus.IDLE,
    ):
        self.name = family
        self.host = host
        self.serial = serial
        self.capabilities = PrinterCapabilities()
        self._state = state
        self.state_calls = 0
        self.state_error: Exception | None = None

    def get_state(self):
        self.state_calls += 1
        if self.state_error is not None:
            raise self.state_error
        return PrinterState(connected=True, state=self._state)


def _adapter(family="moonraker", host="", serial="", state=PrinterStatus.IDLE):
    return _FakeAdapter(family=family, host=host, serial=serial, state=state)


# ---------------------------------------------------------------------------
# Machine identity
# ---------------------------------------------------------------------------


class TestMachineIdentity:
    def test_two_names_for_one_machine_count_once(self):
        """THE production bug: config.yaml names a printer "my-voron", the
        bootstrap also registers it as "default", and the install reported
        two printers while owning one."""
        reg = PrinterRegistry()
        reg.register("default", _adapter(host="http://192.0.2.10:7125"))
        reg.register("my-voron", _adapter(host="http://192.0.2.10:7125"))

        assert reg.count == 1, "one machine, one count"
        assert reg.name_count == 2, "both names still resolvable"
        assert sorted(reg.list_names()) == ["default", "my-voron"]

    def test_the_users_name_wins_over_the_default_alias(self):
        reg = PrinterRegistry()
        reg.register("default", _adapter(host="192.0.2.10:7125"))
        reg.register("my-voron", _adapter(host="192.0.2.10:7125"))
        assert reg.list_machines() == ["my-voron"]

    def test_genuinely_distinct_machines_count_separately(self):
        reg = PrinterRegistry()
        reg.register("voron", _adapter(host="192.0.2.10:7125"))
        reg.register("ender", _adapter(host="192.0.2.11:7125"))
        assert reg.count == 2
        assert reg.list_machines() == ["ender", "voron"]

    @pytest.mark.parametrize("a,b", [
        ("http://192.0.2.10:7125", "192.0.2.10:7125"),
        ("https://Printer.local/", "printer.local"),
        ("http://printer.local", "PRINTER.LOCAL/"),
    ])
    def test_address_spellings_of_one_machine_agree(self, a, b):
        assert machine_fingerprint(_adapter(host=a)) == machine_fingerprint(
            _adapter(host=b)
        )

    def test_two_print_servers_on_one_box_stay_distinct(self):
        """Same host, different port really is two printers."""
        reg = PrinterRegistry()
        reg.register("left", _adapter(host="192.0.2.10:7125"))
        reg.register("right", _adapter(host="192.0.2.10:7126"))
        assert reg.count == 2

    def test_serial_identifies_a_machine_across_an_address_change(self):
        """A Bambu that took a new DHCP lease is the same printer."""
        before = _adapter(family="bambu", host="192.0.2.50", serial="01P00A1")
        after = _adapter(family="bambu", host="192.0.2.77", serial="01P00A1")
        assert machine_fingerprint(before) == machine_fingerprint(after)

    def test_unidentifiable_adapters_are_never_merged_on_a_guess(self):
        reg = PrinterRegistry()
        reg.register("a", _adapter(host="", serial=""))
        reg.register("b", _adapter(host="", serial=""))
        assert reg.count == 2

    def test_aliases_are_discoverable(self):
        reg = PrinterRegistry()
        reg.register("default", _adapter(host="192.0.2.10"))
        reg.register("my-voron", _adapter(host="192.0.2.10"))
        assert reg.aliases_of("default") == ["default", "my-voron"]


# ---------------------------------------------------------------------------
# Fleet concurrency gate
# ---------------------------------------------------------------------------


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
        # `kiln.licensing` is an ALIAS kiln_pro installs at import time
        # (kiln_pro/__init__.py maps it to kiln_pro.enterprise.licensing).
        # It therefore exists on a dev machine with kiln-pro installed and
        # does NOT exist in public Kiln's CI — where a bare import made all
        # nine of these fail. The gate itself already tolerates that
        # (`except Exception: cap = 1`), so the test must too: stand up a
        # stub when the alias is absent, and the cap stays controlled by
        # this fixture either way.
        lic = sys.modules.get("kiln.licensing")
        if lic is None:
            lic = types.ModuleType("kiln.licensing")
            monkeypatch.setitem(sys.modules, "kiln.licensing", lic)
        monkeypatch.setattr(lic, "get_tier", lambda: "free", raising=False)
        monkeypatch.setattr(
            lic, "max_printers_for_tier", lambda _t: cap, raising=False
        )
    return _install


class TestFleetConcurrencyGate:
    def test_single_machine_never_blocks_and_never_touches_the_network(
        self, fleet,
    ):
        """The case nearly every install is in: it must cost nothing."""
        only = _adapter(host="192.0.2.10")
        fleet({"only": only}, cap=1)
        assert _concurrent_fleet_verdict(only) is None
        assert only.state_calls == 0

    def test_second_machine_blocked_while_the_first_prints(self, fleet):
        busy = _adapter(host="192.0.2.10", state=PrinterStatus.PRINTING)
        idle = _adapter(host="192.0.2.11")
        fleet({"voron": busy, "ender": idle}, cap=1)

        verdict = _concurrent_fleet_verdict(idle)
        assert verdict is not None
        assert verdict["code"] == "TIER_CONCURRENT_PRINT_LIMIT"
        assert "voron" in verdict["reason"], "name the printer that's busy"
        assert "pricing" in verdict["override_hint"]

    def test_serial_use_of_two_owned_printers_stays_free(self, fleet):
        """Own two, use one at a time — the friction that is CORRECT for
        free tier.  Nothing here may block."""
        first = _adapter(host="192.0.2.10", state=PrinterStatus.IDLE)
        second = _adapter(host="192.0.2.11", state=PrinterStatus.IDLE)
        fleet({"voron": first, "ender": second}, cap=1)
        assert _concurrent_fleet_verdict(first) is None
        assert _concurrent_fleet_verdict(second) is None

    def test_restarting_the_machine_that_is_already_printing_is_allowed(
        self, fleet,
    ):
        """The gate counts OTHER machines — never the one being started."""
        busy = _adapter(host="192.0.2.10", state=PrinterStatus.PRINTING)
        idle = _adapter(host="192.0.2.11")
        fleet({"voron": busy, "ender": idle}, cap=1)
        assert _concurrent_fleet_verdict(busy) is None

    def test_business_cap_allows_parallel_machines(self, fleet):
        busy = _adapter(host="192.0.2.10", state=PrinterStatus.PRINTING)
        idle = _adapter(host="192.0.2.11")
        fleet({"voron": busy, "ender": idle}, cap=50)
        assert _concurrent_fleet_verdict(idle) is None

    def test_enterprise_uncapped_allows_everything(self, fleet):
        busy = _adapter(host="192.0.2.10", state=PrinterStatus.PRINTING)
        idle = _adapter(host="192.0.2.11")
        fleet({"voron": busy, "ender": idle}, cap=None)
        assert _concurrent_fleet_verdict(idle) is None

    def test_unreachable_peer_never_blocks_a_valid_print(self, fleet):
        """A network hiccup must not cost the user a print."""
        broken = _adapter(host="192.0.2.10")
        broken.state_error = OSError("printer offline")
        idle = _adapter(host="192.0.2.11")
        fleet({"voron": broken, "ender": idle}, cap=1)
        assert _concurrent_fleet_verdict(idle) is None

    def test_a_broken_registry_soft_passes(self, monkeypatch):
        import kiln.registry as registry_mod

        def _boom():
            raise RuntimeError("registry exploded")

        monkeypatch.setattr(registry_mod, "get_registry", _boom)
        assert _concurrent_fleet_verdict(_adapter()) is None

    def test_paused_machine_counts_as_occupied(self, fleet):
        paused = _adapter(host="192.0.2.10", state=PrinterStatus.PAUSED)
        idle = _adapter(host="192.0.2.11")
        fleet({"voron": paused, "ender": idle}, cap=1)
        assert _concurrent_fleet_verdict(idle) is not None

    def test_aliases_of_the_same_machine_do_not_block_each_other(self, fleet):
        """One printer under two names is not two printers — the alias bug
        would otherwise paywall a single-printer free user."""
        printing = _adapter(host="192.0.2.10", state=PrinterStatus.PRINTING)
        fleet({"default": printing, "my-voron": printing}, cap=1)
        assert _concurrent_fleet_verdict(printing) is None


class TestGateNeverTouchesSafety:
    def test_resume_prints_are_never_fleet_gated(self, monkeypatch):
        """A mid-print continuation is committed work — blocking it would
        strand a hot machine."""
        from kiln.printers import print_gate

        monkeypatch.setattr(
            print_gate, "_concurrent_fleet_verdict",
            lambda _a: {"blocked": True, "reason": "should never be reached"},
        )
        assert print_gate.run_adapter_gate(
            _adapter(), "transformed_resume_ab12.3mf", {},
        ) is None

    def test_control_paths_still_bypass_the_print_start_gate(self):
        """A control command must never run the PRINT-START gate.

        This half of the original assertion is unchanged and still matters:
        pausing a running print must not be re-judged for bed fit, hotend
        ceiling or concurrency-at-start.  What changed is the other half —
        see the test below.
        """
        import inspect

        from kiln.printers.base import PrinterAdapter

        for method in (
            "cancel_print", "pause_print", "emergency_stop",
            "get_state", "get_job",
        ):
            src = inspect.getsource(getattr(PrinterAdapter, method))
            assert "run_adapter_gate" not in src, (
                f"{method} must never run the print-start gate — its checks "
                f"are about starting a job, not operating a running one"
            )

    def test_control_paths_consult_the_single_printer_engagement(self):
        """Below the fleet tier, Kiln works with one machine at a time.

        This test used to assert the opposite: that control paths were never
        tier-gated at all, on the reasoning that a licensing rule must never
        cost a user control of a hot machine.  That was overruled
        deliberately on 2026-08-17, and the reasoning is recorded so it is
        not quietly reversed by whoever reads this next.

        Kiln is not the safety system.  Thermal runaway protection lives in
        the printer's own firmware and the machine has its own controls and
        power switch.  And Kiln does not refuse to STOP for free what it
        never agreed to START for free: below the fleet tier it drives one
        machine, so it is one machine it takes responsibility for.  The
        floors that remain are pinned by the two tests after this one.

        Asserted behaviourally rather than by reading source: ``functools.
        wraps`` sets ``__wrapped__``, so ``inspect.getsource`` follows a
        wrapper straight back to the original and would report the gate
        missing while it is installed and working.
        """
        from kiln.printers.base import PrinterAdapter
        from kiln.printers.engagement import GATED_ACTIONS

        for action in sorted(GATED_ACTIONS):
            method = getattr(PrinterAdapter, action, None)
            if method is None or getattr(method, "__isabstractmethod__", False):
                # Abstract here; the implementing adapter carries the wrapper.
                continue
            assert getattr(method, "_kiln_engagement_wrapped", False), (
                f"{action} is not gated — every printer-directed command has "
                f"to ask which machine Kiln is working with, or the rule only "
                f"covers whichever doors somebody remembered"
            )

    def test_the_machine_kiln_drives_keeps_every_command(self):
        """The floor: nothing is ever taken away on the engaged machine."""
        from kiln.printers.engagement import GATED_ACTIONS, check_command, engage

        adapter = _adapter(serial="AAA111", state=PrinterStatus.PRINTING)
        engage(adapter, None, reason="started", label="a1")
        for action in sorted(GATED_ACTIONS):
            assert check_command(adapter, action) is None, action

    def test_an_unknown_tier_never_costs_anyone_a_command(self):
        """Soft-pass is the whole posture: prove it, or allow it."""
        from kiln.printers import engagement

        adapter = _adapter(serial="AAA111", state=PrinterStatus.PRINTING)
        engagement.engage(adapter, None, reason="started", label="a1")
        other = _adapter(serial="BBB222", state=PrinterStatus.PRINTING)
        # kiln-pro absent is the common install, and it reads as "unknown".
        assert engagement._multi_machine_tier() is False
        # ...but an unreachable engaged peer still releases rather than blocks.
        engagement._verify_cache.clear()
        assert engagement.check_command(other, "emergency_stop") is None


class TestPossessionIsFree:
    """Owning printers is free; running them in parallel is the product.

    The cap used to refuse the REGISTRATION, which was simultaneously too
    strict (a user who owns two machines and alternates between them was
    blocked from even adding the second) and too loose (the CLI and
    config.yaml auto-load registered printers with no check at all).
    """

    def test_registering_past_the_cap_succeeds_with_an_honest_note(
        self, monkeypatch,
    ):
        import kiln.server as srv

        monkeypatch.setattr(srv, "_check_auth", lambda _s: None)
        monkeypatch.setattr(srv, "get_tier", lambda: "free", raising=False)
        monkeypatch.setattr(
            srv, "max_printers_for_tier", lambda _t: 1, raising=False,
        )
        reg = srv._get_registry()
        for name in list(reg.list_names()):
            reg.unregister(name)
        reg.register("first", _adapter(host="192.0.2.10"))

        result = srv.register_printer(
            name="second",
            printer_type="moonraker",
            host="192.0.2.11",
            persist=False,
            verify_connection=False,
        )

        assert result["success"] is True, "possession is free at every tier"
        assert "fleet_note" in result
        assert "1 printer at a time" in result["fleet_note"]
        assert "pricing" in result["upgrade_url"]

    def test_the_old_registration_paywall_is_gone(self):
        """A hard refusal here would make the concurrency gate
        unreachable — the two rules would contradict each other."""
        import inspect

        import kiln.server as srv

        src = inspect.getsource(srv.register_printer)
        assert "TIER_PRINTER_LIMIT" not in src


# ---------------------------------------------------------------------------
# The declared model must reach the adapter — every construction door
# ---------------------------------------------------------------------------


class TestModelReachesAdapter:
    """A printer's declared model is identity, not just a safety hint.

    Bed-aware planners (split-to-fit) resolve a registered machine's usable
    envelope from its adapter's model.  The named-config door threaded it,
    but the default-printer door and the register_printer tool dropped it —
    so the one printer most users have reported no known bed (2026-07-28).
    """

    def test_register_printer_threads_model_into_bambu_adapter(
        self, monkeypatch,
    ):
        pytest.importorskip("paho.mqtt")
        import kiln.server as srv

        monkeypatch.setattr(srv, "_check_auth", lambda _s: None)
        reg = srv._get_registry()
        for name in list(reg.list_names()):
            reg.unregister(name)

        result = srv.register_printer(
            name="bench-a1",
            printer_type="bambu",
            host="192.0.2.20",
            api_key="12345678",
            serial="TESTSERIAL01",
            printer_model="bambu_a1",
            persist=False,
            verify_connection=False,
        )
        assert result["success"] is True
        adapter = reg.get("bench-a1")
        assert getattr(adapter, "_printer_model", "") == "bambu_a1"
        reg.unregister("bench-a1")

    def test_model_reaches_modeless_adapters_via_safety_profile(
        self, monkeypatch,
    ):
        import kiln.server as srv

        monkeypatch.setattr(srv, "_check_auth", lambda _s: None)
        reg = srv._get_registry()
        for name in list(reg.list_names()):
            reg.unregister(name)

        result = srv.register_printer(
            name="bench-neptune",
            printer_type="moonraker",
            host="192.0.2.21",
            printer_model="neptune4_plus",
            persist=False,
            verify_connection=False,
        )
        assert result["success"] is True
        adapter = reg.get("bench-neptune")
        assert getattr(adapter, "_safety_profile_id", "") == "neptune4_plus"
        reg.unregister("bench-neptune")

    def test_every_bambu_construction_site_passes_the_model(self):
        """Structural pin for the doors a behavioural test can't reach
        (the module-global default-printer path): no BambuAdapter may be
        constructed in server.py without threading printer_model."""
        import inspect
        import re

        import kiln.server as srv

        src = inspect.getsource(srv)
        sites = [
            m for m in re.finditer(r"BambuAdapter\((?:[^()]|\([^()]*\))*\)", src)
        ]
        assert sites, "expected BambuAdapter construction sites in server.py"
        missing = [
            m.group(0) for m in sites if "printer_model" not in m.group(0)
        ]
        assert not missing, (
            "BambuAdapter constructed without printer_model= — the adapter "
            f"loses its model identity: {missing}"
        )
