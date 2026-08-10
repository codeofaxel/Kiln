"""``kiln doctor`` reports when sources disagree about the printer's model.

The identity self-report (2026-08) deliberately reports NOTHING when an
adapter's channels contradict each other — naming a model on a coin flip is
what got printer-model inference scrapped.  That silence made the most
diagnostic state Kiln can observe invisible to the user; this check is where
it surfaces.

A conflict FAILS the run rather than warning: the config-declared model is
what temperature ceilings and bed-fit key off, so declaring an X1C on a
machine that is really an A1 applies all-metal limits to a PTFE hotend.
"""
import json

from click.testing import CliRunner

from kiln.cli.main import cli
from kiln.printers.base import PrinterInfo


def _model_check(output: str) -> dict | None:
    data = json.loads(output)
    return next((c for c in data["checks"] if c["name"] == "printer_model"), None)


class _Adapter:
    """Minimal stand-in shaped like a real adapter."""

    def __init__(self, *, declared=None, channels=None, probed=None):
        if declared is not None:
            self.printer_model = declared
        self._channels = channels or {}
        self._probed = probed

    def get_state(self):
        from kiln.printers.base import PrinterState, PrinterStatus

        return PrinterState(connected=True, state=PrinterStatus.IDLE)

    def get_identity_channels(self):
        return dict(self._channels)

    def get_printer_info(self):
        return PrinterInfo(model=self._probed) if self._probed else None


def _run(monkeypatch, tmp_path, adapter):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "kiln.cli.main.load_printer_config",
        lambda *a, **k: {"name": "default", "type": "bambu", "host": "192.168.1.6"},
    )
    monkeypatch.setattr("kiln.cli.main._make_adapter", lambda *a, **k: adapter)
    return CliRunner().invoke(cli, ["doctor", "--json"])


def test_disagreement_fails_the_run_and_names_every_claim(monkeypatch, tmp_path):
    adapter = _Adapter(
        declared="bambu_a1",
        channels={"serial_prefix": "bambu_a1", "firmware_product_name": "bambu_x1c"},
    )
    check = _model_check(_run(monkeypatch, tmp_path, adapter).output)
    assert check is not None
    assert check["ok"] is False  # a wrong ceiling is not a warning
    assert check["claims"] == {
        "config": "bambu_a1",
        "serial_prefix": "bambu_a1",
        "firmware_product_name": "bambu_x1c",
    }
    assert "bambu_x1c" in check["detail"]
    assert "config.yaml" in check["detail"]


def test_agreement_reports_the_model_and_passes(monkeypatch, tmp_path):
    adapter = _Adapter(
        declared="bambu_a1", channels={"serial_prefix": "bambu_a1"}
    )
    check = _model_check(_run(monkeypatch, tmp_path, adapter).output)
    assert check is not None
    assert check["ok"] is True
    assert check["detail"] == "bambu_a1"
    assert "warn" not in check


def test_self_reported_model_alone_passes(monkeypatch, tmp_path):
    """Nothing declared, but the printer said what it is — that is the
    whole point of the self-report, and it is not a problem."""
    adapter = _Adapter(channels={"serial_prefix": "bambu_a1"}, probed="bambu_a1")
    check = _model_check(_run(monkeypatch, tmp_path, adapter).output)
    assert check is not None
    assert check["ok"] is True
    assert check["detail"] == "bambu_a1"


def test_no_model_anywhere_warns_without_failing(monkeypatch, tmp_path):
    """Common on Klipper/OctoPrint rigs — worth telling the user that
    model-specific checks are running on defaults, but it must not start
    failing doctor for every install that never set the field."""
    check = _model_check(_run(monkeypatch, tmp_path, _Adapter()).output)
    assert check is not None
    assert check["ok"] is True
    assert check["warn"] is True
    assert "printer_model" in check["detail"]


def test_a_broken_adapter_never_breaks_doctor(monkeypatch, tmp_path):
    class _Angry(_Adapter):
        def get_identity_channels(self):
            raise RuntimeError("printer exploded")

        def get_printer_info(self):
            raise RuntimeError("printer exploded")

    check = _model_check(_run(monkeypatch, tmp_path, _Angry()).output)
    assert check is not None
    assert check["ok"] is True  # diagnostics never fail the run on their own
