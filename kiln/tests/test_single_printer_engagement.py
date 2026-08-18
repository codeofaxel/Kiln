"""One printer's worth of Kiln help at a time, and how it lets go.

The fleet tier sells running printers in parallel, and until now that was
enforced on ``start_print`` alone.  Every sibling command -- status, pause,
resume, cancel, temperatures, emergency stop -- is a separate adapter method
that never passed through the print-start gate, so the commands that
actually operate a second machine were open at every tier.

These tests pin the rule and, at least as importantly, pin the places it
must NOT reach: the machine Kiln is actually driving keeps every command
including emergency stop, Kiln's own internal peer reads are never gated by
the rule they exist to evaluate, and every uncertainty allows the command.
A licensing rule that blocks when it cannot prove its case is worse than no
rule, because the workaround people learn is to distrust the gate entirely.
"""

from __future__ import annotations

import json

import pytest

from kiln.printers import engagement
from kiln.printers.base import (
    JobProgress,
    PrinterCapabilities,
    PrinterEngagementError,
    PrinterState,
    PrinterStatus,
)
from kiln.registry import PrinterRegistry


class _FakeAdapter:
    """A real object, not a Mock.

    A Mock auto-creates ``serial`` and ``host``, which silently defeats the
    fingerprint's fallback chain and would make these tests pass for the
    wrong reason -- the same trap the fleet-gate tests call out.
    """

    def __init__(self, serial: str, *, state=PrinterStatus.PRINTING, job_file="bracket.gcode", elapsed=600):
        self.name = "moonraker"
        self.serial = serial
        self.host = ""
        self.capabilities = PrinterCapabilities()
        self._state = state
        self._job_file = job_file
        self._elapsed = elapsed

    def get_state(self) -> PrinterState:
        return PrinterState(connected=True, state=self._state)

    def get_job(self) -> JobProgress:
        return JobProgress(file_name=self._job_file, print_time_seconds=self._elapsed)


@pytest.fixture
def two_printers(monkeypatch):
    """Machine A and machine B, both registered, both printing."""
    a = _FakeAdapter("AAA111")
    b = _FakeAdapter("BBB222", job_file="gasket.gcode", elapsed=300)
    registry = PrinterRegistry()
    registry.register("a1", a)
    registry.register("garage", b)
    monkeypatch.setattr("kiln.registry.get_registry", lambda: registry)
    return a, b


@pytest.fixture(autouse=True)
def _single_machine_tier(monkeypatch):
    """Default every test here to the capped tier; lift it explicitly."""
    monkeypatch.setattr(engagement, "_multi_machine_tier", lambda: False)


def _engage(adapter, label):
    engagement.engage(adapter, adapter.get_job(), reason="started", label=label)


class TestTheRuleItself:
    def test_a_second_machine_is_refused_while_kiln_drives_the_first(self, two_printers):
        a, b = two_printers
        _engage(a, "a1")
        verdict = engagement.check_command(b, "pause_print")
        assert verdict is not None
        assert verdict["code"] == "TIER_SINGLE_PRINTER_LIMIT"
        assert "a1" in verdict["reason"]

    def test_the_engaged_machine_keeps_every_command(self, two_printers):
        a, _ = two_printers
        _engage(a, "a1")
        for action in sorted(engagement.GATED_ACTIONS):
            assert engagement.check_command(a, action) is None, action

    def test_nothing_is_gated_when_kiln_drives_nothing(self, two_printers):
        a, b = two_printers
        for action in sorted(engagement.GATED_ACTIONS):
            assert engagement.check_command(b, action) is None, action

    def test_status_is_refused_on_the_other_machine(self, two_printers):
        a, b = two_printers
        _engage(a, "a1")
        assert engagement.check_command(b, "get_state") is not None
        assert engagement.check_command(b, "get_job") is not None

    def test_registration_and_discovery_are_never_gated(self):
        """Owning printers is free; running them in parallel is the product."""
        for never in ("register_printer", "list_printers", "discover_printers", "upload_file"):
            assert never not in engagement.GATED_ACTIONS

    def test_a_fleet_tier_lifts_the_rule(self, two_printers, monkeypatch):
        a, b = two_printers
        _engage(a, "a1")
        monkeypatch.setattr(engagement, "_multi_machine_tier", lambda: True)
        assert engagement.check_command(b, "pause_print") is None


class TestLettingGo:
    def test_handing_a_machine_back_frees_the_slot(self, two_printers):
        a, b = two_printers
        _engage(a, "a1")
        report = engagement.hand_back(a)
        assert report["released"] is True
        assert engagement.check_command(b, "pause_print") is None

    def test_one_return_to_a_handed_back_print_is_allowed(self, two_printers):
        a, b = two_printers
        _engage(a, "a1")
        engagement.hand_back(a)
        _engage(b, "garage")
        # A starts misbehaving; coming back to it is allowed exactly once.
        assert engagement.check_command(a, "pause_print") is None
        assert engagement.current().machine == engagement.machine_id(a)

    def test_the_return_moves_the_slot_rather_than_adding_one(self, two_printers):
        """A return steps OFF whatever Kiln was driving.

        This is why one return each is generous rather than a loophole: at
        no instant does a capped caller hold two machines.
        """
        a, b = two_printers
        _engage(a, "a1")
        engagement.hand_back(a)
        _engage(b, "garage")
        engagement.check_command(a, "pause_print")  # the return
        assert engagement.current().machine == engagement.machine_id(a)
        assert engagement.current().machine != engagement.machine_id(b)

    def test_alternating_between_two_live_machines_runs_out(self, two_printers):
        """The motion that IS a fleet terminates, and quickly.

        Each machine gets one return for its own print, so the bench can be
        swapped a bounded number of times and then the slot stops moving --
        which is the difference between handing a machine over and
        supervising two jobs by alternating.
        """
        a, b = two_printers
        _engage(a, "a1")
        engagement.hand_back(a)
        _engage(b, "garage")
        assert engagement.check_command(a, "pause_print") is None  # A's one return
        assert engagement.check_command(b, "pause_print") is None  # B's one return
        assert engagement.check_command(a, "pause_print") is not None  # spent
        assert engagement.check_command(b, "pause_print") is None  # B is engaged now

    def test_a_second_return_to_the_same_print_is_refused(self, two_printers):
        a, b = two_printers
        _engage(a, "a1")
        engagement.hand_back(a)
        _engage(b, "garage")
        engagement.check_command(a, "pause_print")  # return 1, allowed
        engagement.hand_back(a)
        _engage(b, "garage")
        verdict = engagement.check_command(a, "pause_print")  # return 2, refused
        assert verdict is not None
        assert verdict["returns_left"] == 0
        assert "already come back" in verdict["reason"]

    def test_handing_back_a_machine_kiln_is_not_driving_says_so(self, two_printers):
        a, b = two_printers
        _engage(a, "a1")
        report = engagement.hand_back(b)
        assert report["released"] is False
        assert "a1" in report["reason"]


class TestTheHoldEndsWithTheJob:
    def test_a_finished_print_releases_the_slot(self, two_printers):
        a, b = two_printers
        _engage(a, "a1")
        a._state = PrinterStatus.IDLE
        engagement._verify_cache.clear()
        assert engagement.check_command(b, "pause_print") is None
        assert engagement.current() is None

    def test_a_different_print_on_the_engaged_machine_releases_it(self, two_printers):
        a, b = two_printers
        _engage(a, "a1")
        a._job_file = "something-else.gcode"
        engagement._verify_cache.clear()
        assert engagement.check_command(b, "pause_print") is None

    def test_a_job_that_ended_does_not_spend_the_return(self, two_printers):
        """Ending is not handing back: nothing was given up, so nothing is spent."""
        a, b = two_printers
        _engage(a, "a1")
        a._state = PrinterStatus.IDLE
        engagement._verify_cache.clear()
        engagement.check_command(b, "pause_print")  # expires A
        a._state = PrinterStatus.PRINTING
        _engage(a, "a1")
        engagement.hand_back(a)
        _engage(b, "garage")
        assert engagement.check_command(a, "pause_print") is None


class TestItSurvivesARestart:
    def test_the_record_is_on_disk_not_in_memory(self, two_printers):
        """``restart_server`` is a tool an agent can call.

        If the engagement lived in memory, restarting would release it and
        the whole rule would be a one-call bypass.
        """
        a, b = two_printers
        _engage(a, "a1")
        # Simulate a fresh process: drop every in-process cache.
        engagement._verify_cache.clear()
        assert engagement.current() is not None
        assert engagement.check_command(b, "pause_print") is not None

    def test_an_unreadable_record_allows_commands(self, two_printers):
        a, b = two_printers
        _engage(a, "a1")
        engagement._store_path().write_text("{ this is not json")
        assert engagement.check_command(b, "pause_print") is None

    def test_a_record_from_a_future_version_allows_commands(self, two_printers):
        a, b = two_printers
        _engage(a, "a1")
        engagement._store_path().write_text(json.dumps({"version": 99, "engaged": {}}))
        assert engagement.check_command(b, "pause_print") is None


class TestItRefusesToGuess:
    def test_an_unidentifiable_machine_is_never_blocked(self, two_printers):
        """No serial and no address: nothing that outlives this process."""
        a, _ = two_printers
        _engage(a, "a1")
        nameless = _FakeAdapter("")
        nameless.host = ""
        assert engagement.machine_id(nameless) == ""
        assert engagement.check_command(nameless, "pause_print") is None

    def test_an_unreachable_engaged_peer_releases_the_slot(self, two_printers):
        a, b = two_printers
        _engage(a, "a1")

        def _boom():
            raise RuntimeError("printer unplugged")

        a.get_state = _boom
        engagement._verify_cache.clear()
        assert engagement.check_command(b, "pause_print") is None

    def test_kilns_own_peer_reads_are_not_gated(self, two_printers):
        """The check is built out of reads of other machines.

        Without the exemption the gate would refuse its own evidence and
        then read that refusal as proof.
        """
        a, b = two_printers
        _engage(a, "a1")
        with engagement.internal_read():
            assert engagement.check_command(b, "get_state") is None

    def test_an_ungated_action_passes_through(self, two_printers):
        a, b = two_printers
        _engage(a, "a1")
        assert engagement.check_command(b, "list_files") is None


class TestWhatAPersonReads:
    def test_the_refusal_says_kiln_is_not_watching_the_other_machine(self, two_printers):
        a, b = two_printers
        _engage(a, "a1")
        verdict = engagement.check_command(b, "get_state")
        assert "not watching" in verdict["reason"]

    def test_emergency_stop_leads_with_how_to_stop_it_yourself(self, two_printers):
        a, b = two_printers
        _engage(a, "a1")
        verdict = engagement.check_command(b, "emergency_stop")
        first_suggestion = verdict["suggestions"][0]
        assert first_suggestion.startswith("To stop")
        assert "power switch" in first_suggestion

    def test_emergency_stop_always_works_on_the_machine_kiln_drives(self, two_printers):
        a, _ = two_printers
        _engage(a, "a1")
        assert engagement.check_command(a, "emergency_stop") is None

    def test_the_refusal_names_the_tier_exactly_once(self, two_printers):
        a, b = two_printers
        _engage(a, "a1")
        verdict = engagement.check_command(b, "pause_print")
        text = verdict["upgrade_nudge"]["display_text"]
        assert text.lower().count("business") == 1, text

    def test_the_refusal_offers_the_free_way_out(self, two_printers):
        a, b = two_printers
        _engage(a, "a1")
        verdict = engagement.check_command(b, "pause_print")
        assert "hand_back_printer" in " ".join(verdict["suggestions"])


class TestTheAdapterChokepoint:
    def test_a_gated_command_raises_a_catchable_printer_error(self, two_printers):
        """Every caller already handles PrinterError, so the refusal reaches
        a person as a message rather than a traceback, on every surface."""
        from kiln.printers import PrinterError

        verdict = {"reason": "Kiln is working with a1 right now.", "code": "X"}
        err = PrinterEngagementError(verdict)
        assert isinstance(err, PrinterError)
        assert err.verdict is verdict
        assert "a1" in str(err)

    def test_every_control_method_is_covered(self):
        """The list is the contract: a command absent here is ungated."""
        assert engagement.GATED_ACTIONS == frozenset(
            {
                "get_state",
                "get_job",
                "pause_print",
                "resume_print",
                "cancel_print",
                "emergency_stop",
                "set_tool_temp",
                "set_bed_temp",
                "send_gcode",
            }
        )


def _real_adapter_class():
    """A genuine PrinterAdapter subclass, for tests that need the WRAPPER.

    This distinction matters more than it looks.  The gate is installed by
    ``__init_subclass__``, so a plain stand-in that merely LOOKS like an
    adapter is never wrapped, and every call sails past the rule untouched.
    A test written against such a stand-in passes whether the gate works or
    not — which is exactly what happened to the registry tests below: the
    exemption they were meant to prove was switched off and the result did
    not move.
    """
    from kiln.printers.base import (
        PrinterAdapter,
        PrinterCapabilities,
        PrinterState,
        PrinterStatus,
        PrintResult,
    )

    class _Real(PrinterAdapter):
        def __init__(self, serial, state=PrinterStatus.PRINTING, job="bracket.gcode"):
            self.serial = serial
            self.host = ""
            self._state = state
            self._job = job
            self.state_calls = 0

        @property
        def name(self):
            return "probe"

        @property
        def capabilities(self):
            return PrinterCapabilities()

        def get_state(self):
            self.state_calls += 1
            return PrinterState(connected=True, state=self._state)

        def get_job(self):
            return JobProgress(file_name=self._job, print_time_seconds=60)

        def list_files(self):
            return []

        def upload_file(self, path):
            return None

        def delete_file(self, path):
            return True

        def _start_print_impl(self, file_name, **kwargs):
            return PrintResult(success=True, message="")

        def _resume_print_impl(self):
            return PrintResult(success=True, message="")

        def cancel_print(self):
            return PrintResult(success=True, message="")

        def pause_print(self):
            return PrintResult(success=True, message="")

        def emergency_stop(self):
            return PrintResult(success=True, message="")

        def send_gcode(self, commands):
            return True

        def set_bed_temp(self, target):
            return True

        def set_tool_temp(self, target):
            return True

    return _Real


class TestThroughTheRealAdapterMethods:
    """The wrapper, not the verdict function.

    Everything above calls ``check_command`` directly, which tests the
    reasoning and not the wiring.  These go through the actual adapter
    methods, because the wiring is where a rule like this historically
    fails: the gate existed on start_print and every sibling method walked
    past it for months.
    """

    @staticmethod
    def _adapter_class():
        return _real_adapter_class()

    def test_the_gate_actually_fires_through_a_real_method(self, monkeypatch):
        cls = self._adapter_class()
        a, b = cls("AAA111"), cls("BBB222")
        registry = PrinterRegistry()
        registry.register("a1", a)
        registry.register("garage", b)
        monkeypatch.setattr("kiln.registry.get_registry", lambda: registry)

        a.get_state()  # claims the free slot for A
        assert engagement.current() is not None

        with pytest.raises(PrinterEngagementError) as caught:
            b.pause_print()
        assert caught.value.verdict["code"] == "TIER_SINGLE_PRINTER_LIMIT"

    def test_handing_back_does_not_open_the_whole_bench(self, monkeypatch):
        """The escape hatch must not be the bypass.

        Hand back, and the next machine commanded becomes Kiln's -- otherwise
        a user empties the slot on purpose and drives every printer they own
        with nothing engaged, which is the fleet experience for free.
        """
        cls = self._adapter_class()
        a, b = cls("AAA111"), cls("BBB222", job="gasket.gcode")
        registry = PrinterRegistry()
        registry.register("a1", a)
        registry.register("garage", b)
        monkeypatch.setattr("kiln.registry.get_registry", lambda: registry)

        a.get_state()
        engagement.hand_back(a)
        assert engagement.current() is None

        b.get_state()  # B is printing, so B becomes the machine Kiln works with
        assert engagement.current() is not None
        with pytest.raises(PrinterEngagementError):
            a.pause_print()

    def test_an_idle_machine_claims_nothing(self, monkeypatch):
        from kiln.printers.base import PrinterStatus

        cls = self._adapter_class()
        idle = cls("CCC333", state=PrinterStatus.IDLE)
        registry = PrinterRegistry()
        registry.register("spare", idle)
        monkeypatch.setattr("kiln.registry.get_registry", lambda: registry)

        idle.get_state()
        assert engagement.current() is None

    def test_the_gate_costs_no_extra_round_trips(self, monkeypatch):
        """Measured, because the first version cost one per engagement.

        Claiming from inside the gate meant the first status call on every
        engagement asked the printer twice.  The claim reads the answer the
        command already returned instead.
        """
        cls = self._adapter_class()
        a = cls("AAA111")
        registry = PrinterRegistry()
        registry.register("a1", a)
        monkeypatch.setattr("kiln.registry.get_registry", lambda: registry)

        for _ in range(10):
            a.get_state()
        assert a.state_calls == 10


class TestCopyIsWrittenForWhoIsReadingIt:
    """A refusal is the only part of this most people will ever read."""

    def test_a_free_caller_is_told_it_is_the_free_plan(self, two_printers, monkeypatch):
        a, b = two_printers
        monkeypatch.setattr(engagement, "_tier_name", lambda: "free")
        _engage(a, "a1")
        verdict = engagement.check_command(b, "pause_print")
        assert "on the free plan" in verdict["reason"]

    def test_a_paying_caller_is_not_told_they_are_on_the_free_plan(
        self, two_printers, monkeypatch,
    ):
        """Pro is capped here too, and reads the same refusal.

        Telling a subscriber they are "on the free plan" is the product they
        pay for informing them they did not pay.
        """
        a, b = two_printers
        monkeypatch.setattr(engagement, "_tier_name", lambda: "pro")
        _engage(a, "a1")
        verdict = engagement.check_command(b, "pause_print")
        assert "free plan" not in verdict["reason"]
        assert "Kiln Pro" in verdict["reason"]

    def test_the_upgrade_line_still_names_business_once_for_either(
        self, two_printers, monkeypatch,
    ):
        a, b = two_printers
        for tier in ("free", "pro"):
            monkeypatch.setattr(engagement, "_tier_name", lambda t=tier: t)
            _engage(a, "a1")
            verdict = engagement.check_command(b, "pause_print")
            text = verdict["upgrade_nudge"]["display_text"]
            assert text.lower().count("business") == 1, (tier, text)

    def test_handing_back_reports_whether_a_return_is_left(self, two_printers):
        """The consequence is stated at the moment of the release."""
        a, b = two_printers
        _engage(a, "a1")
        first = engagement.hand_back(a)
        assert first["returns_left"] == 1

        _engage(b, "garage")
        engagement.check_command(a, "pause_print")  # spends A's return
        spent = engagement.hand_back(a)
        assert spent["returns_left"] == 0


class TestAFleetStopNeverLiesAboutWhatItStopped:
    """The sharpest edge in the whole rule.

    A fleet-wide stop loops every known printer and calls the same adapter
    methods the engagement gate guards.  The coordinator catches delivery
    failures and latches the printer anyway, which is right for a real
    failure -- the machine's state is unknown, so treat it as halted.  A
    tier refusal is the opposite situation: nothing was sent, and the
    machine is definitely still running.  Taking the same path would tell
    an operator a running printer was halted, and would then block starting
    prints on a printer that never stopped.
    """

    @staticmethod
    def _coordinator():
        from kiln.emergency import EmergencyCoordinator

        return EmergencyCoordinator()

    def test_a_refused_machine_is_not_recorded_as_stopped(self):
        from kiln.printers.base import PrinterEngagementError

        coord = self._coordinator()
        refusal = PrinterEngagementError(
            {
                "reason": "Kiln is working with a1 right now.",
                "code": "TIER_SINGLE_PRINTER_LIMIT",
                "suggestions": ["To stop this printer right now, use its own controls."],
            }
        )

        def _send(printer_id):
            if printer_id == "garage":
                raise refusal
            return ([], [])

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(coord, "_send_emergency_gcode", _send)
            stopped = coord.emergency_stop("a1")
            refused = coord.emergency_stop("garage")

        assert stopped.success is True
        assert refused.success is False
        # The machine Kiln drives really is latched.
        assert "a1" in coord._stopped_printers
        # The one it refused is NOT, because it never stopped.
        assert "garage" not in coord._stopped_printers, (
            "a printer Kiln declined to command must never be recorded as "
            "halted — the operator would read a running machine as stopped"
        )

    def test_the_refusal_reaches_the_report_with_its_own_words(self):
        """So a fleet-stop report names what the operator still has to do."""
        from kiln.printers.base import PrinterEngagementError

        coord = self._coordinator()

        def _send(printer_id):
            raise PrinterEngagementError(
                {"reason": "Kiln is working with a1 right now.", "code": "X"}
            )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(coord, "_send_emergency_gcode", _send)
            record = coord.emergency_stop("garage")

        assert "Not stopped by Kiln" in (record.error or "")
        assert "a1" in (record.error or "")
        assert record.actions_taken == []
        assert record.gcode_sent == []

    def test_a_real_delivery_failure_still_latches(self):
        """The safe behaviour for the indeterminate case is unchanged."""
        coord = self._coordinator()

        def _send(printer_id):
            raise RuntimeError("printer unplugged mid-stop")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(coord, "_send_emergency_gcode", _send)
            record = coord.emergency_stop("a1")

        assert record.success is False
        assert "G-code delivery failed" in (record.error or "")
        assert "a1" in coord._stopped_printers, (
            "an attempted stop that failed leaves the machine in an unknown "
            "state, which must still be treated as halted"
        )


class TestOwningPrintersIsStillFree:
    """The floor the rule must never cross.

    Kiln enumerates its own registry to build the printer listing, to find
    idle machines, and to answer status queries — all by calling get_state
    on every printer at once, on a thread pool.  That is Kiln asking about
    its own hardware, not a person commanding a machine.  If the rule
    refused those, someone with two printers would open the listing, see
    the second one as an error, and conclude Kiln had lost their hardware.

    These use a REAL PrinterAdapter subclass on purpose.  The gate is
    installed by ``__init_subclass__``, so a look-alike stand-in is never
    wrapped and the test would pass with the exemption switched off — which
    is precisely how the first version of this class fooled itself.
    """

    @staticmethod
    def _two_registered(monkeypatch, second_state=None):
        from kiln.printers.base import PrinterStatus

        cls = _real_adapter_class()
        a = cls("AAA111")
        b = cls("BBB222", state=second_state or PrinterStatus.PRINTING, job="gasket.gcode")
        registry = PrinterRegistry()
        registry.register("a1", a)
        registry.register("garage", b)
        monkeypatch.setattr("kiln.registry.get_registry", lambda: registry)
        engagement.engage(a, a.get_job(), reason="started", label="a1")
        engagement._verify_cache.clear()
        return a, b, registry

    def test_the_gate_really_is_in_the_path_for_these_adapters(self, monkeypatch):
        """Guard against the whole class passing for the wrong reason."""
        a, b, _ = self._two_registered(monkeypatch)
        with pytest.raises(PrinterEngagementError):
            b.get_state()

    def test_the_fleet_listing_shows_every_printer_while_engaged(self, monkeypatch):
        a, b, registry = self._two_registered(monkeypatch)
        listing = registry.get_fleet_status()
        assert sorted(e["name"] for e in listing) == ["a1", "garage"], listing
        for entry in listing:
            assert entry.get("connected") is True, (
                f"{entry['name']} came back disconnected — the rule refused "
                f"Kiln's own registry read, which is not a user command"
            )

    def test_idle_lookup_still_sees_other_machines(self, monkeypatch):
        from kiln.printers.base import PrinterStatus

        a, b, registry = self._two_registered(monkeypatch, second_state=PrinterStatus.IDLE)
        assert registry.get_idle_printers() == ["garage"]

    def test_status_lookup_still_sees_other_machines(self, monkeypatch):
        from kiln.printers.base import PrinterStatus

        a, b, registry = self._two_registered(monkeypatch)
        assert registry.get_printers_by_status(PrinterStatus.PRINTING) == ["a1", "garage"]

    def test_the_marker_does_not_leak_to_a_real_command(self, monkeypatch):
        """An internal read must not excuse the next user command.

        The exemption is thread-local and scoped to its block, so a listing
        that just ran cannot leave the gate open behind it.
        """
        a, b, registry = self._two_registered(monkeypatch)
        registry.get_fleet_status()
        assert engagement.check_command(b, "pause_print") is not None
        with pytest.raises(PrinterEngagementError):
            b.pause_print()


class TestTheRefusalNamesThingsProperly:
    """It says the machine's name, and says it the way the user wrote it."""

    def test_the_refused_machine_is_named_not_called_this_printer(self, two_printers):
        a, b = two_printers
        _engage(a, "a1")
        verdict = engagement.check_command(b, "pause_print")
        assert "garage" in verdict["reason"].lower()
        assert "this printer" not in verdict["reason"].lower()

    @pytest.mark.parametrize(
        ("given", "expected"),
        [("garage", "Garage"), ("MK4S", "MK4S"), ("X1C", "X1C"), ("a1", "A1"), ("", "")],
    )
    def test_a_printer_name_is_never_rewritten(self, given, expected):
        """str.capitalize would render MK4S as "Mk4s".

        Silently restyling the name someone gave their own machine is not a
        detail — it is the product telling them they named it wrong.
        """
        assert engagement._starts_sentence(given) == expected
