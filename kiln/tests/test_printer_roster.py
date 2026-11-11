"""A status read lists the printers set up on this computer, and nothing
that locates, identifies or unlocks one.

Called through the real ``printer_status`` tool and the relay handler a
Bridge answers with, because that reply is what leaves the computer.
"""
import json
import textwrap
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from kiln import printer_model_resolver as pmr
from kiln.bridge_client import handle_relay_request
from kiln.printers.octoprint import OctoPrintAdapter
from kiln.registry import PrinterRegistry
from kiln.server import printer_status

# Values chosen so each is unmistakable in a reply.
ADDRESS_A, ADDRESS_B = "192.168.77.41", "10.44.0.93"
ACCESS_CODE = "83527164"
API_KEY = "KEY-9f3c1e77a2b84d06"
SERIAL_A, SERIAL_B = "03919C4612345678", "0309DA5X98765432"
SECRETS = (ADDRESS_A, ADDRESS_B, ACCESS_CODE, API_KEY, SERIAL_A, SERIAL_B)


def _write_config(monkeypatch, tmp_path, body: str) -> None:
    from kiln.cli import config as cli_config

    cfg = tmp_path / "config.yaml"
    cfg.write_text(textwrap.dedent(body))
    monkeypatch.setattr(pmr, "_CONFIG_PATH", cfg)
    monkeypatch.setattr(cli_config, "get_config_path", lambda: cfg)
    pmr.invalidate_cache()


def _adapter(state, job, caps, *, host: str, serial: str, secret: str, model: str):
    adapter = MagicMock(spec=OctoPrintAdapter)
    adapter.get_state.return_value = state
    adapter.get_job.return_value = job
    type(adapter).capabilities = PropertyMock(return_value=caps)
    # Everything a real adapter holds about where its printer is and how to
    # unlock it, under the attribute names the adapters use.
    adapter.host = adapter._host = host
    adapter.serial = adapter._serial = serial
    adapter.api_key = adapter._api_key = adapter.access_code = adapter._access_code = secret
    adapter._printer_model = model
    adapter.declared_printer_model.return_value = model
    return adapter


@pytest.fixture
def two_printers(tmp_path, monkeypatch, mock_printer_state_idle, mock_job_progress, mock_capabilities):
    _write_config(monkeypatch, tmp_path, f"""
        active_printer: garage-a1
        printers:
          garage-a1:
            type: bambu
            host: {ADDRESS_A}
            access_code: "{ACCESS_CODE}"
            serial: {SERIAL_A}
            printer_model: bambu_a1
          office-mk4:
            type: octoprint
            host: http://{ADDRESS_B}
            api_key: {API_KEY}
            serial: {SERIAL_B}
            printer_model: prusa_mk4
    """)
    a1 = _adapter(mock_printer_state_idle, mock_job_progress, mock_capabilities,
                  host=ADDRESS_A, serial=SERIAL_A, secret=ACCESS_CODE, model="bambu_a1")
    mk4 = _adapter(mock_printer_state_idle, mock_job_progress, mock_capabilities,
                   host=ADDRESS_B, serial=SERIAL_B, secret=API_KEY, model="prusa_mk4")
    registry = PrinterRegistry()
    with patch("kiln.registry.machine_fingerprint", side_effect=lambda a: f"fp:{id(a)}"):
        registry.register("garage-a1", a1)
        registry.register("default", a1)
        registry.register("office-mk4", mk4)
        with patch("kiln.server._get_adapter", return_value=a1), \
             patch("kiln.server._get_registry", return_value=registry):
            yield registry, a1, mk4


def _relayed(args: dict) -> dict:
    """The Bridge's wire answer to one relayed ``printer_status``."""
    return handle_relay_request(
        {"request_id": "r1", "tool_name": "printer_status", "args": args},
        call_tool=lambda name, a: printer_status(**a),
        fetch_artifact=lambda token: "",
    )


class TestTheStatusReadListsThePrintersHere:
    @pytest.mark.parametrize("detail", ["lite", "full"])
    def test_each_printer_once_with_its_model(self, two_printers, detail):
        result = printer_status(detail=detail)

        assert result["success"] is True
        rows = result["known_printers"]
        assert [row["name"] for row in rows] == ["garage-a1", "office-mk4"]
        assert [row["printer_model"] for row in rows] == ["bambu_a1", "prusa_mk4"]

    def test_listing_asks_no_printer_anything(self, two_printers):
        _registry, _a1, mk4 = two_printers

        printer_status(detail="lite")

        # The printer the read was not about is never contacted: a list is
        # read from what this computer already knows.
        mk4.get_state.assert_not_called()
        mk4.get_job.assert_not_called()
        mk4.get_printer_info.assert_not_called()

    def test_a_row_holds_only_what_names_the_machine(self, two_printers):
        for row in printer_status(detail="lite")["known_printers"]:
            assert set(row) <= {"name", "printer_model", "kiln_is_working_with"}


class TestNothingThatLocatesOrUnlocksAPrinterLeaves:
    @pytest.mark.parametrize("detail", ["lite", "full"])
    def test_the_relayed_reply_carries_no_address_code_key_or_serial(self, two_printers, detail):
        wire = json.dumps(_relayed({"detail": detail}))

        assert '"known_printers"' in wire
        for secret in SECRETS:
            assert secret not in wire

    def test_a_name_that_holds_its_own_serial_is_not_listed_by_name(
        self, tmp_path, monkeypatch, mock_printer_state_idle, mock_job_progress, mock_capabilities
    ):
        leaky = f"a1-{SERIAL_A}"
        _write_config(monkeypatch, tmp_path, f"""
            active_printer: other
            printers:
              other: {{type: octoprint, host: http://{ADDRESS_B}, api_key: {API_KEY}, printer_model: prusa_mk4}}
              {leaky}: {{type: bambu, host: {ADDRESS_A}, access_code: "{ACCESS_CODE}", serial: {SERIAL_A}, printer_model: bambu_a1}}
        """)
        other = _adapter(mock_printer_state_idle, mock_job_progress, mock_capabilities,
                         host=ADDRESS_B, serial=SERIAL_B, secret=API_KEY, model="prusa_mk4")
        a1 = _adapter(mock_printer_state_idle, mock_job_progress, mock_capabilities,
                      host=ADDRESS_A, serial=SERIAL_A, secret=ACCESS_CODE, model="bambu_a1")
        registry = PrinterRegistry()
        with patch("kiln.registry.machine_fingerprint", side_effect=lambda a: f"fp:{id(a)}"):
            registry.register("other", other)
            registry.register(leaky, a1)
            with patch("kiln.server._get_adapter", return_value=other), \
                 patch("kiln.server._get_registry", return_value=registry):
                result = printer_status(detail="lite")

        rows = result["known_printers"]
        assert len(rows) == 2
        withheld = [row for row in rows if row.get("name_withheld")]
        assert len(withheld) == 1 and "name" not in withheld[0]
        assert withheld[0]["printer_model"] == "bambu_a1"
        assert SERIAL_A not in json.dumps(rows)


class TestAListIsNeverWorthAReading:
    def test_an_unreadable_registry_leaves_the_status_read_whole(
        self, two_printers, monkeypatch
    ):
        registry, _a1, _mk4 = two_printers
        monkeypatch.setattr(registry, "list_machines", MagicMock(side_effect=OSError("nope")))

        result = printer_status(detail="lite")

        assert result["success"] is True
        assert "known_printers" not in result


class TestAPrinterThatIsOffIsStillListed:
    def test_an_unreachable_default_printer_still_lists_both(self, two_printers):
        from kiln.printers.base import PrinterError

        _registry, a1, _mk4 = two_printers
        a1.get_state.side_effect = PrinterError("connection refused")

        result = printer_status(detail="lite")

        assert result["success"] is True and result["printer"]["connected"] is False
        assert [row["name"] for row in result["known_printers"]] == ["garage-a1", "office-mk4"]
        wire = json.dumps(result)
        for secret in SECRETS:
            assert secret not in wire
