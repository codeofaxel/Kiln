"""A printer's name never carries its address, serial or codes unasked.

A printer's name goes wherever the printer goes: with what Kiln asks its
servers about that printer and onto the person's Kiln account.  Kiln's own
setup used to make names like ``192-168-7-42`` from what the network said, and
nothing looked at a name a person or agent typed.  Every door that names a
printer now reads :mod:`kiln.printer_names`: Kiln's own defaults are clean, a
chosen name holding an identifier is held with a clean one offered, and a
saved one is said once.  Kiln suggests; it never renames.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from kiln.cli.main import cli
from kiln.printer_names import (
    default_printer_name,
    discovered_name,
    identifiers_of,
    model_name,
    name_advice,
    saved_advice,
)

_SERIAL = "01P00A123456789"
_OWN = identifiers_of(host="http://192.168.7.42", serial=_SERIAL, access_code="87654321")


def _found(name: str, *, host: str = "192.168.7.42", printer_type: str = "bambu", serial: str = _SERIAL):
    from kiln.discovery import DiscoveredPrinter

    return DiscoveredPrinter(host=host, port=8883, printer_type=printer_type, name=name, serial=serial)


# ---------------------------------------------------------------------------
# What a name carries
# ---------------------------------------------------------------------------


class TestWhatANameCarries:
    @pytest.mark.parametrize(
        ("name", "phrase"),
        [
            ("Shop A1 @ 192.168.7.42", "the printer's network address"),
            ("192-168-7-42", "the printer's network address"),  # Kiln's own old spelling
            ("garage 10.0.0.5", "a network address"),
            ("voron.local", "a network address"),
            ("http://octopi.local:5000", "a web address"),
            ("k1 aa:bb:cc:dd:ee:ff", "a hardware address"),
            ("me@example.com printer", "an email address"),
            (f"x1c {_SERIAL}", "the printer's serial number"),
            ("a1 87654321", "the printer's access code"),
        ],
    )
    def test_an_identifier_in_any_spelling_is_found(self, name: str, phrase: str) -> None:
        advice = name_advice(name, identifiers=_OWN)
        assert advice is not None
        assert phrase in advice.found

    @pytest.mark.parametrize("name", ["adams-garage-x1c", "voron-350", "mk4", "bambu-a1-bay-3", "Shop A1"])
    def test_a_persons_own_words_are_never_flagged(self, name: str) -> None:
        assert name_advice(name, identifiers=_OWN) is None

    def test_a_code_inside_the_serial_is_named_once(self) -> None:
        own = identifiers_of(serial="01P00A123456789", access_code="12345678")
        advice = name_advice("x1c 01P00A123456789", identifiers=own)
        assert advice is not None
        assert advice.found == ("the printer's serial number",)

    def test_what_is_an_address_is_read_from_redaction(self, monkeypatch) -> None:
        # kiln.redaction is the one home of these rules: with its place rule
        # switched off, a bare address is no longer seen.
        import kiln.redaction

        assert name_advice("garage 10.0.0.5") is not None
        monkeypatch.setattr(kiln.redaction, "redact_places", lambda text: text)
        assert name_advice("garage 10.0.0.5") is None


# ---------------------------------------------------------------------------
# What Kiln suggests
# ---------------------------------------------------------------------------


class TestTheSuggestion:
    @pytest.mark.parametrize(
        ("name", "model", "expected"),
        [
            ("Shop A1 @ 192.168.7.42", "A1", "bambu-a1-shop"),
            ("192-168-7-42", "bambu_a1", "bambu-a1"),
            ("A1 at 192.168.7.42", "A1", "bambu-a1"),
            ("garage @ 10.0.0.5", "P1S", "bambu-p1s-garage"),
            (f"x1c {_SERIAL}", "X1C", "bambu-x1c"),
            ("MK4 192.168.1.9", "Prusa MK4", "prusa-mk4"),
            ("voron.local", "Voron 2.4", "voron-2-4"),
            ("voron.local", None, "voron"),
            ("octopi-192-168-1-5", None, "octopi"),
            ("10.0.0.5", None, "printer"),
        ],
    )
    def test_the_model_leads_and_the_persons_words_stay(self, name: str, model: str | None, expected: str) -> None:
        advice = name_advice(name, printer_model=model, identifiers=_OWN)
        assert advice is not None
        assert advice.suggested_name == expected
        assert name_advice(advice.suggested_name, identifiers=_OWN) is None

    def test_the_sentence_says_what_and_why_and_the_name(self) -> None:
        advice = name_advice("Shop A1 @ 192.168.7.42", printer_model="A1", identifiers=_OWN)
        assert advice is not None
        assert advice.sentence == (
            "'Shop A1 @ 192.168.7.42' has the printer's network address in it. A printer's name goes "
            "with what Kiln asks its servers about that printer and shows on your Kiln account, so a "
            "name without it keeps that on this computer. Suggested name: 'bambu-a1-shop'."
        )

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("A1", "bambu-a1"),
            ("bambu_a1", "bambu-a1"),
            ("Prusa MK4", "prusa-mk4"),
            ("K1 Max", "creality-k1-max"),
            ("Voron 2.4", "voron-2-4"),
            ("prusa_core_one_l", "prusa-core-one-l"),
            (None, ""),
        ],
    )
    def test_a_model_is_said_with_its_maker(self, model: str | None, expected: str) -> None:
        assert model_name(model) == expected

    def test_kilns_own_default_is_clean(self) -> None:
        assert default_printer_name("192.168.1.5", printer_type="octoprint", printer_model="Ender 3") == "creality-ender-3"
        assert default_printer_name("voron.local.", printer_type="moonraker") == "voron"
        assert default_printer_name("", printer_type="moonraker") == "moonraker"
        assert default_printer_name("Creality K1C", printer_type="moonraker") == "creality-k1c"
        assert discovered_name(_found("192.168.7.42")) == "bambu-p1s"  # the serial names the model


# ---------------------------------------------------------------------------
# Every door that names a printer
# ---------------------------------------------------------------------------


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.yaml"
    with patch("kiln.cli.config.get_config_path", return_value=path):
        yield path


class TestRegisterPrinterTool:
    """The agent door: a name holding an identifier is held before anything
    is asked, built or saved, with the clean name offered."""

    @pytest.fixture
    def server(self, monkeypatch, config):
        import kiln.bench
        import kiln.server as mod
        from kiln.registry import PrinterRegistry

        registry = PrinterRegistry()
        monkeypatch.setattr(mod, "_registry", registry)
        monkeypatch.setattr(mod, "_adapter", None)
        monkeypatch.setattr(kiln.bench, "offer_after_registration", lambda *_a, **_k: None)
        built: list = []

        class FakeBambuAdapter:
            name = "bambu"

            def __init__(self, *, host, access_code, serial, tls_mode, printer_model=None):
                self.printer_model = printer_model
                built.append(self)

            def set_safety_profile(self, profile_id):
                self.profile_id = profile_id

            def set_external_camera(self, *, snapshot_url=None, stream_url=None):
                pass

        monkeypatch.setattr(mod, "BambuAdapter", FakeBambuAdapter)
        return mod, registry, built

    def _register(self, mod, name: str, **extra):
        return mod.register_printer(
            name=name, printer_type="bambu", host="192.168.7.42", api_key="87654321",
            serial=_SERIAL, printer_model="bambu_a1", verify_connection=False, **extra,
        )

    def test_a_name_with_its_address_is_held_and_nothing_is_saved(self, server, config) -> None:
        mod, registry, built = server
        result = self._register(mod, "Shop A1 @ 192.168.7.42")
        assert result["success"] is False
        assert result["error"]["code"] == "NAME_HOLDS_IDENTIFIER"
        assert result["suggested_name"] == "bambu-a1-shop"
        assert result["name_advice"]["found"] == ["the printer's network address"]
        assert "keep_name=true" in result["error"]["message"]
        assert built == [] and registry.list_names() == []
        assert not config.exists()

    def test_the_person_keeps_their_name_when_they_say_so(self, server, config) -> None:
        mod, registry, _ = server
        result = self._register(mod, "Shop A1 @ 192.168.7.42", keep_name=True)
        assert result["success"] is True, result
        assert "Shop A1 @ 192.168.7.42" in registry.list_names()

    def test_the_suggested_name_registers(self, server, config) -> None:
        mod, registry, _ = server
        result = self._register(mod, "bambu-a1-shop")
        assert result["success"] is True, result
        assert yaml.safe_load(config.read_text())["printers"]["bambu-a1-shop"]["serial"] == _SERIAL

    def test_a_printer_already_saved_under_the_name_is_updated_not_held(self, server, config) -> None:
        from kiln.cli.config import save_printer

        mod, _, _ = server
        save_printer(
            "192-168-7-42", "bambu", "192.168.7.42", access_code="87654321", serial=_SERIAL,
            printer_model="bambu_a1", config_path=config, probe=False,
        )
        result = self._register(mod, "192-168-7-42")
        assert result["success"] is True, result


class TestAuthCommand:
    """``kiln auth --name``: the same hold, with ``--keep-name``."""

    def _auth(self, name: str, *extra: str):
        return CliRunner().invoke(cli, [
            "auth", "--name", name, "--host", "192.168.7.42", "--type", "bambu",
            "--access-code", "87654321", "--serial", _SERIAL, "--printer-model", "A1", *extra,
        ])

    def test_a_name_with_its_address_is_held(self, config) -> None:
        result = self._auth("a1-192-168-7-42")
        assert result.exit_code == 2, result.output
        assert "--name bambu-a1" in result.output
        assert "--keep-name" in result.output
        assert not config.exists()

    def test_keep_name_saves_it(self, config) -> None:
        with patch("kiln.cli.main._machine_identity_for_save", return_value=None):
            result = self._auth("a1-192-168-7-42", "--keep-name", "--json")
        assert result.exit_code == 0, result.output
        assert "a1-192-168-7-42" in yaml.safe_load(config.read_text())["printers"]


class TestSetupWizard:
    """``kiln setup``: the default is clean, and a typed name holding an
    identifier is said once with a clean one offered."""

    def _run(self, found, typed: str):
        with (
            patch("kiln.terms.is_current", return_value=True),
            patch("kiln.cli.discovery.discover_printers", return_value=[found]),
            patch("kiln.cli.printer_model_prompt.prompt_for_printer_model", return_value="bambu_a1"),
            patch("kiln.cli.main._make_adapter", return_value=MagicMock()),
        ):
            return CliRunner().invoke(cli, ["setup"], input=f"1\n{typed}87654321\n\n" + "\n" * 8)

    def test_a_printer_advertising_its_address_gets_a_clean_default(self, config) -> None:
        result = self._run(_found("192.168.7.42"), "\n")
        names = list(yaml.safe_load(config.read_text())["printers"])
        assert names == ["bambu-p1s"], result.output

    def test_a_typed_address_is_said_and_the_clean_name_offered(self, config) -> None:
        result = self._run(_found("Garage"), "shop-192-168-7-42\n\n")
        assert "has the printer's network address in it" in result.output
        assert list(yaml.safe_load(config.read_text())["printers"]) == ["shop"], result.output

    def test_typing_their_own_name_again_keeps_it(self, config) -> None:
        result = self._run(_found("Garage"), "shop-192-168-7-42\nshop-192-168-7-42\n")
        assert list(yaml.safe_load(config.read_text())["printers"]) == ["shop-192-168-7-42"], result.output


class TestQuickstartAndBridge:
    def test_quickstart_hands_over_a_clean_name(self, config) -> None:
        with (
            patch("kiln.cli.main._list_printers", return_value=[]),
            patch("kiln.cli.discovery.discover_printers", return_value=[_found("192.168.7.42")]),
        ):
            result = CliRunner().invoke(cli, ["quickstart"])
        assert "kiln auth --name bambu-p1s " in result.output, result.output
        assert "--name 192-168-7-42" not in result.output

    def test_bridge_setup_saves_under_a_clean_name(self) -> None:
        from kiln.cli.bridge_commands import _suggest_printer_name

        assert _suggest_printer_name(_found("192.168.7.42"), printer_model="bambu_a1") == "bambu-a1"
        assert _suggest_printer_name(_found("Garage")) == "garage"


class TestDiscoverTool:
    def test_each_found_printer_carries_a_clean_suggested_name(self) -> None:
        from kiln.plugins.printer_management_tools import plugin

        tools: dict = {}

        class FakeMCP:
            def tool(self, *_a, **_k):
                def register(fn):
                    tools[fn.__name__] = fn
                    return fn

                return register

        plugin.register(FakeMCP())
        with patch("kiln.discovery.discover_printers", return_value=[_found("192.168.7.42")]):
            result = tools["discover_printers"](timeout=0.1)
        assert result["printers"][0]["suggested_name"] == "bambu-p1s"


# ---------------------------------------------------------------------------
# A printer already saved under such a name
# ---------------------------------------------------------------------------


@pytest.fixture
def saved_dirty(config):
    from kiln.cli.config import save_printer

    save_printer(
        "192-168-7-42", "bambu", "192.168.7.42", access_code="87654321", serial=_SERIAL,
        printer_model="bambu_a1", config_path=config, probe=False,
    )
    return config


class TestASavedName:
    def test_the_advice_says_what_switching_costs_today(self, saved_dirty) -> None:
        note = saved_advice("192-168-7-42")
        assert note is not None
        assert note["suggested_name"] == "bambu-a1"
        assert "Kiln can't rename a saved printer yet" in note["sentence"]
        assert "its past prints stay listed under '192-168-7-42'" in note["sentence"]

    def test_printer_status_says_it_once_per_session(self, saved_dirty, monkeypatch) -> None:
        import kiln.server as mod
        from kiln.printers.base import JobProgress, PrinterState, PrinterStatus
        from kiln.registry import PrinterRegistry
        from kiln.tiers_and_terms import reset_spoken_keys

        reset_spoken_keys()
        adapter = MagicMock()
        adapter.name = "bambu"
        adapter.get_state.return_value = PrinterState(state=PrinterStatus.IDLE, connected=True)
        adapter.get_job.return_value = JobProgress()
        registry = PrinterRegistry()
        registry.register("192-168-7-42", adapter)
        monkeypatch.setattr(mod, "_registry", registry)

        first = mod.printer_status(printer_name="192-168-7-42", detail="lite")
        second = mod.printer_status(printer_name="192-168-7-42", detail="lite")
        assert first["name_advice"]["suggested_name"] == "bambu-a1", first
        assert "name_advice" not in second

    def test_doctor_says_it_every_run(self, saved_dirty) -> None:
        for _ in range(2):
            result = CliRunner().invoke(cli, ["doctor", "--json"])
            checks = json.loads(result.output)["checks"]
            named = [c for c in checks if c["name"] == "printer_name"]
            assert len(named) == 1, result.output
            assert named[0]["warn"] is True
            assert "Suggested name: 'bambu-a1'" in named[0]["detail"]

    def test_a_clean_saved_name_says_nothing(self, config) -> None:
        from kiln.cli.config import save_printer

        save_printer(
            "bambu-a1", "bambu", "192.168.7.42", access_code="87654321", serial=_SERIAL,
            printer_model="bambu_a1", config_path=config, probe=False,
        )
        assert saved_advice("bambu-a1") is None
