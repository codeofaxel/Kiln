"""A printer the person already set up answers to a mention of it.

"Print this on my bambu", "my a1", "the bambu": Kiln knows the person's own
machines, so a mention resolves against THEIR fleet in the registry's one
lookup, which every door that names a printer reaches.  One printer it could
mean is an answer; two is a question that names them; a printer the person
did not set up is never an answer.  Setting a printer UP is the other
question (kiln.printer_declaration) and is never guessed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock

import pytest

from kiln.printers.base import PrinterAdapter, PrinterCapabilities
from kiln.registry import PrinterNotFoundError, PrinterRegistry


def _adapter(backend: str, model: str | None) -> MagicMock:
    adapter = MagicMock(spec=PrinterAdapter)
    type(adapter).name = PropertyMock(return_value=backend)
    type(adapter).capabilities = PropertyMock(return_value=PrinterCapabilities())
    adapter.declared_printer_model.return_value = model or ""
    return adapter


@pytest.fixture
def fleet() -> PrinterRegistry:
    registry = PrinterRegistry()
    registry.register("shelf", _adapter("bambu", "bambu_a1"))
    registry.register("desk", _adapter("moonraker", "Voron 2.4"))
    registry.register("garage", _adapter("octoprint", "prusa_core_one_l"))
    return registry


class TestAMentionFindsTheirPrinter:
    def test_the_registered_name_still_wins_exactly(self, fleet):
        assert fleet.get("shelf") is fleet.list_all()["shelf"]

    @pytest.mark.parametrize("mention", ["my bambu", "the bambu", "Bambu", "my a1", "A1", "bambu lab a1", "my shelf", "Shelf"])
    def test_one_bambu_answers_to_its_maker_model_or_name(self, fleet, mention):
        assert fleet.get(mention) is fleet.list_all()["shelf"]

    @pytest.mark.parametrize("mention", ["my voron", "voron 2.4", "the voron", "v2.4"])
    def test_a_voron_answers_to_its_model_spellings(self, fleet, mention):
        assert fleet.get(mention) is fleet.list_all()["desk"]

    @pytest.mark.parametrize("mention", ["my prusa", "the core one", "prusa core one l"])
    def test_a_printer_kiln_has_no_record_for_still_answers_to_its_label(self, fleet, mention):
        assert fleet.get(mention) is fleet.list_all()["garage"]

    def test_a_printer_the_person_did_not_set_up_is_never_an_answer(self, fleet):
        with pytest.raises(PrinterNotFoundError) as raised:
            fleet.get("my creality")
        assert raised.value.candidates == []
        assert "desk, garage, shelf" in str(raised.value)

    def test_a_word_inside_a_name_is_not_a_mention(self, fleet):
        # "a" is in "bambu lab a1"; a mention is whole words.
        with pytest.raises(PrinterNotFoundError):
            fleet.get("a")


class TestTwoPrintersIsAQuestion:
    def test_two_bambus_are_named_never_picked(self, fleet):
        fleet.register("bench", _adapter("bambu", "bambu_p1s"))
        with pytest.raises(PrinterNotFoundError) as raised:
            fleet.get("my bambu")
        assert raised.value.candidates == ["bench", "shelf"]
        assert "could mean any of bench, shelf" in str(raised.value)

    def test_the_model_still_tells_them_apart(self, fleet):
        fleet.register("bench", _adapter("bambu", "bambu_p1s"))
        assert fleet.get("my p1s") is fleet.list_all()["bench"]
        assert fleet.get("the a1") is fleet.list_all()["shelf"]


class TestEveryDoor:
    def test_the_shared_resolver_and_a_direct_registry_door_agree(self, fleet, monkeypatch):
        import kiln.server as srv

        monkeypatch.setattr(srv, "_registry", fleet)
        assert srv._resolve_adapter("my a1") is fleet.list_all()["shelf"]
        assert srv._get_registry().get("the voron") is fleet.list_all()["desk"]

    def test_a_tool_door_words_the_question(self, fleet, monkeypatch):
        import kiln.server as srv

        fleet.register("bench", _adapter("bambu", "bambu_p1s"))
        monkeypatch.setattr(srv, "_registry", fleet)
        out = srv.printer_status(printer_name="my bambu")
        assert out["success"] is False
        assert "bench" in out["error"]["message"] and "shelf" in out["error"]["message"]


class TestAMentionCannotSlipPastASafetyGate:
    """2026-10-27, an automated review of the mention lookup: a safety gate
    asked about the TYPED name ("my bambu") checked a name nothing was latched
    under, while the action went to the printer that was.  Every gate asked
    through the name resolver now sees the registered name the mention means,
    the same key the action uses."""

    def test_the_name_resolver_answers_the_registered_name(self, fleet, monkeypatch):
        import kiln.server as srv

        monkeypatch.setattr(srv, "_registry", fleet)
        assert srv._resolve_effective_printer_name("my bambu") == "shelf"
        assert srv._resolve_effective_printer_name("shelf") == "shelf"
        # Nothing, or several: the typed string, for the lookup to refuse.
        assert srv._resolve_effective_printer_name("my creality") == "my creality"
        fleet.register("bench", _adapter("bambu", "bambu_p1s"))
        assert srv._resolve_effective_printer_name("my bambu") == "my bambu"

    def test_a_latched_printer_is_refused_by_its_mention(self, fleet, monkeypatch):
        import kiln.server as srv
        from kiln.emergency import get_emergency_coordinator

        monkeypatch.setattr(srv, "_registry", fleet)
        coordinator = get_emergency_coordinator()
        coordinator._stopped_printers.add("shelf")
        try:
            out = srv.resume_print(printer_name="my bambu")
        finally:
            coordinator._stopped_printers.discard("shelf")
        assert out["success"] is False, out
        assert out["error"]["code"] == "E_STOP_LATCHED"
        fleet.list_all()["shelf"].resume_print.assert_not_called()

    def test_the_per_printer_lock_is_the_printers_not_the_mentions(self, fleet):
        assert fleet.printer_lock("my bambu") is fleet.printer_lock("shelf")
        with pytest.raises(PrinterNotFoundError):
            fleet.printer_lock("my creality")
