"""Which printers the account can tell apart, by backend.

Pinned, through the real save door, the real adapter factory and the real
courier:

* every printer type Kiln registers says how it is told apart on the
  account -- a shared code from the printer's own id, a controller code, or
  the reason it has neither -- and what the table says is what the code does;
* two printers of one model whose boards report different ids carry
  different shared codes; one printer set up on a second computer, at
  another address, carries the same one; another account gets another code;
* a printer run from a controller carries a controller code and no shared
  code; two printers on one controller carry the same controller code;
* an id too plain to name one machine, a printer known by its address only,
  and a signed-out computer carry no code;
* a shared code two printers on one computer both carry is left off both;
* what goes up holds the codes and nothing of the id, the address or the name.
"""

from __future__ import annotations

import base64
import inspect
import json
import time
from pathlib import Path
from unittest import mock

import pytest

import kiln.printers as printers_pkg
from kiln import auth_session, outbound_guard
from kiln import machine_repair_courier as courier
from kiln import machine_repairs as pr
from kiln.cli import config as cli_config
from kiln.persistence import KilnDB
from kiln.printer_backends import PRINTER_TYPES
from kiln.printers.base import MachineIdentity, PrinterAdapter

PRUSA = "prusalink_info.serial"
DUET = "rrf_object_model.boards[0].uniqueId"
KLIPPER = "moonraker_system_info.cpu_info.serial_number"

#: The adapter class behind each registered type.
ADAPTERS = {
    "bambu": "BambuAdapter",
    "elegoo": "ElegooAdapter",
    "prusalink": "PrusaLinkAdapter",
    "duet": "DuetAdapter",
    "moonraker": "MoonrakerAdapter",
    "creality": "CrealityAdapter",
    "octoprint": "OctoPrintAdapter",
    "usb": "SerialPrinterAdapter",
}


def _jwt(sub: str) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"sub": sub, "exp": time.time() + 3600}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{body}.sig"


@pytest.fixture(autouse=True)
def _own_install(monkeypatch, tmp_path):
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    monkeypatch.setenv(courier.TEST_OPT_IN_ENV, "1")
    monkeypatch.setattr(cli_config, "get_config_path", lambda: tmp_path / "config.yaml")
    # No test here reaches a printer: a model nobody declared is not asked of the machine.
    from kiln import community_autofire

    monkeypatch.setattr(community_autofire, "_probed_model", lambda adapter: None)
    pr._shared_codes.clear()
    courier._questions.clear()


@pytest.fixture
def signed_in(monkeypatch):
    monkeypatch.setattr(auth_session, "resolve_api_bearer",
                        lambda *a, **k: auth_session.ApiBearer(token=_jwt("acct-alice"), state="session"))
    return "acct-alice"


class Computer:
    """One computer: its own saved printers, built the way the server builds them."""

    def __init__(self, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / "config.yaml"
        self.adapters: dict = {}

    def save(self, name, printer_type, host, *, board_id=None, source=None, mainboard_id=None, model=None,
             api_key="key"):
        import kiln.server as srv

        identity = MachineIdentity(value=board_id, source=source, kind="id") if board_id else None
        cli_config.save_printer(
            name, printer_type, host, api_key=api_key, serial=mainboard_id, machine_identity=identity,
            printer_model=model, config_path=self.path, probe=False,
        )
        entry = cli_config._read_config_file(self.path)["printers"][name]
        from kiln.printers.creality import CrealityAdapter

        with mock.patch.object(CrealityAdapter, "_resolve_moonraker_url", lambda self: self._input_host):
            adapter = srv._build_adapter_from_config_entry(name, entry)
        adapter._kiln_registered_name = name
        self.adapters[name] = adapter
        return adapter

    def use(self, monkeypatch):
        monkeypatch.setattr(cli_config, "get_config_path", lambda: self.path)
        return self

    def list_all(self):
        return dict(self.adapters)

    def get(self, name):
        return self.adapters[name]


@pytest.fixture
def computer(tmp_path, monkeypatch):
    return Computer(tmp_path / "mac").use(monkeypatch)


# ---------------------------------------------------------------------------
# Every backend says how it is told apart
# ---------------------------------------------------------------------------


class TestEveryBackendIsDecided:
    def test_every_registered_type_has_a_row_and_no_row_is_stale(self):
        assert set(pr.IDENTITY_BY_TYPE) == set(PRINTER_TYPES)
        assert set(ADAPTERS) == set(PRINTER_TYPES)

    @pytest.mark.parametrize("slug", PRINTER_TYPES)
    def test_the_row_is_what_the_backend_offers(self, slug):
        kind, detail = pr.IDENTITY_BY_TYPE[slug]
        assert kind in ("printer", "controller", "none")
        cls = getattr(printers_pkg, ADAPTERS[slug])
        if cls is None:
            pytest.skip(f"{slug}: its adapter's own dependency is not installed here")
        reads_an_id = cls.read_machine_identity is not PrinterAdapter.read_machine_identity
        if kind == "none":
            assert len(detail) >= 40, "a type with no code says why in a sentence"
            assert not reads_an_id, (
                f"{slug} reads an id off the machine: give it a row in PRINTER_ID_SOURCES or "
                "CONTROLLER_ID_SOURCES, or say here why that id cannot tell two printers apart"
            )
            return
        sources = pr.PRINTER_ID_SOURCES if kind == "printer" else pr.CONTROLLER_ID_SOURCES
        if slug == "bambu":
            assert detail == "serial"
            return
        assert detail in sources
        if slug == "elegoo":
            assert "mainboard_id" in cli_config._STATED_IDENTITY_KEYS
            return
        assert reads_an_id
        owner = cls
        if slug == "creality":
            from kiln.printers.moonraker import MoonrakerAdapter as owner
        assert detail in inspect.getsource(owner.read_machine_identity)

    @pytest.mark.parametrize("slug,host,saved", [
        ("prusalink", "http://192.168.1.21", {"board_id": "CZPX4720X004XK70123", "source": PRUSA}),
        ("duet", "http://192.168.1.22", {"board_id": "08DJM-9P63L-DJ3T8-6J1DG-3SJ6M-T8ZHG", "source": DUET}),
        ("elegoo", "192.168.1.23", {"mainboard_id": "5a2b0c7d9e11f3a4"}),
        ("moonraker", "http://192.168.1.24:7125", {"board_id": "10000000a1b2c3d4", "source": KLIPPER}),
        ("creality", "http://192.168.1.25:7125", {"board_id": "20000000a1b2c3d4", "source": KLIPPER}),
        ("octoprint", "http://192.168.1.26", {}),
    ])
    def test_a_saved_printer_of_each_type_carries_what_its_row_says(self, slug, host, saved, computer, signed_in):
        if getattr(printers_pkg, ADAPTERS[slug]) is None:
            pytest.skip(f"{slug}: its adapter's own dependency is not installed here")
        adapter = computer.save("p", slug, host, **saved)
        kind = pr.IDENTITY_BY_TYPE[slug][0]
        shared, controller = pr.shared_code(adapter), pr.controller_code(adapter)
        assert (shared is not None) == (kind == "printer")
        assert (controller is not None) == (kind == "controller")
        if shared:
            assert shared.startswith("pc:") and len(shared) == 27
        if controller:
            assert controller.startswith("cc:") and len(controller) == 27


    @pytest.mark.parametrize("slug,host,saved", [
        ("prusalink", "http://192.168.1.21", {}),
        ("duet", "http://192.168.1.22", {}),
        ("moonraker", "http://192.168.1.24:7125", {}),
        ("creality", "http://192.168.1.25:7125", {}),
        ("octoprint", "http://192.168.1.26", {}),
    ])
    def test_every_networked_type_has_an_identity_on_its_own_computer(self, slug, host, saved, computer):
        from kiln.registry import stable_machine_fingerprint

        adapter = computer.save("p", slug, host, **saved)
        assert stable_machine_fingerprint(adapter) == f"{slug}:host:{host[len('http://'):]}"


# ---------------------------------------------------------------------------
# The codes
# ---------------------------------------------------------------------------


class TestTheSharedCode:
    def test_two_printers_of_one_model_differ_and_one_printer_on_two_computers_agrees(
            self, tmp_path, monkeypatch, signed_in):
        mac = Computer(tmp_path / "mac").use(monkeypatch)
        one = mac.save("left", "prusalink", "http://192.168.1.21", board_id="CZPX4720X004XK70123", source=PRUSA)
        two = mac.save("right", "prusalink", "http://192.168.1.22", board_id="CZPX4720X004XK70999", source=PRUSA)
        left, right = pr.shared_code(one), pr.shared_code(two)
        assert left and right and left != right
        pr._shared_codes.clear()
        pc = Computer(tmp_path / "pc").use(monkeypatch)
        again = pc.save("mk4", "prusalink", "http://mk4-left.local", board_id="czpx4720x004xk70123", source=PRUSA)
        assert pr.shared_code(again) == left
        assert pr.shared_code(again, account_id="acct-bob") not in (None, left)

    def test_the_code_holds_nothing_of_the_id(self, computer, signed_in):
        adapter = computer.save("left", "prusalink", "http://192.168.1.21", board_id="CZPX4720X004XK70123",
                                source=PRUSA)
        code = pr.shared_code(adapter)
        for piece in ("czpx", "70123", "4720"):
            assert piece not in code.lower()

    @pytest.mark.parametrize("plain", ["0000000000000000", "1234", "unknown-serial", "FFFFFFFFFFFF", "0123456789"])
    def test_an_id_too_plain_to_name_one_machine_gives_no_code(self, plain, computer, signed_in):
        adapter = computer.save("left", "prusalink", "http://192.168.1.21", board_id=plain, source=PRUSA)
        assert pr.shared_code(adapter) is None
        other = computer.save("host", "moonraker", "http://192.168.1.24:7125", board_id=plain, source=KLIPPER)
        assert pr.controller_code(other) is None

    def test_an_id_from_a_source_nobody_decided_gives_no_code(self, computer, signed_in):
        adapter = computer.save("left", "prusalink", "http://192.168.1.21", board_id="CZPX4720X004XK70123",
                                source="some_new_endpoint.id")
        assert pr.shared_code(adapter) is None and pr.controller_code(adapter) is None

    def test_signed_out_gives_no_code(self, computer):
        adapter = computer.save("left", "prusalink", "http://192.168.1.21", board_id="CZPX4720X004XK70123",
                                source=PRUSA)
        assert pr.shared_code(adapter) is None
        host = computer.save("host", "moonraker", "http://192.168.1.24:7125", board_id="10000000a1b2c3d4",
                             source=KLIPPER)
        assert pr.controller_code(host) is None

    def test_a_printer_saved_with_no_id_gives_no_code(self, computer, signed_in):
        adapter = computer.save("left", "prusalink", "http://192.168.1.21")
        assert pr.shared_code(adapter) is None

    def test_two_saved_entries_at_one_address_that_disagree_give_no_code(self, computer, signed_in):
        computer.save("left", "prusalink", "http://192.168.1.21", board_id="CZPX4720X004XK70123", source=PRUSA)
        adapter = computer.save("also-left", "prusalink", "http://192.168.1.21", board_id="CZPX4720X004XK70999",
                                source=PRUSA)
        assert pr.shared_code(adapter) is None


class TestTheControllerCode:
    def test_two_controllers_differ_and_two_printers_on_one_controller_agree(self, computer, signed_in):
        a = computer.save("voron-a", "moonraker", "http://192.168.1.24:7125", board_id="10000000a1b2c3d4",
                          source=KLIPPER)
        b = computer.save("voron-b", "moonraker", "http://192.168.1.25:7125", board_id="10000000ffee0011",
                          source=KLIPPER)
        second_on_a = computer.save("voron-a2", "moonraker", "http://192.168.1.24:7126",
                                    board_id="10000000a1b2c3d4", source=KLIPPER)
        assert pr.controller_code(a) != pr.controller_code(b)
        assert pr.controller_code(a) == pr.controller_code(second_on_a)
        assert pr.shared_code(a) is None and pr.shared_code(second_on_a) is None

    def test_a_controller_id_never_makes_a_shared_code_of_the_same_digits(self, computer, signed_in):
        host = computer.save("voron-a", "moonraker", "http://192.168.1.24:7125", board_id="10000000a1b2c3d4",
                             source=KLIPPER)
        board = computer.save("mk4", "prusalink", "http://192.168.1.21", board_id="10000000a1b2c3d4", source=PRUSA)
        assert pr.controller_code(host)[3:] != pr.shared_code(board)[3:]


# ---------------------------------------------------------------------------
# What the courier announces and sends
# ---------------------------------------------------------------------------


class TestWhatIsAnnounced:
    def test_one_shared_code_on_two_printers_here_is_left_off_both(self, computer, signed_in):
        computer.save("left", "prusalink", "http://192.168.1.21", board_id="CZPX4720X004XK70123", source=PRUSA)
        computer.save("same-id", "prusalink", "http://192.168.1.22", board_id="CZPX4720X004XK70123", source=PRUSA)
        computer.save("right", "prusalink", "http://192.168.1.23", board_id="CZPX4720X004XK70999", source=PRUSA)
        said = {p["name"]: p for p in courier.announcements(computer)}
        assert "shared_code" not in said["left"] and "shared_code" not in said["same-id"]
        assert said["right"]["shared_code"].startswith("pc:")

    def test_what_goes_up_holds_the_codes_and_nothing_of_the_id_or_the_address(
            self, computer, signed_in, monkeypatch, tmp_path):
        computer.save("left", "prusalink", "http://192.168.1.21", board_id="CZPX4720X004XK70123", source=PRUSA,
                      model="prusa_mk4")
        computer.save("right", "prusalink", "http://192.168.1.22", board_id="CZPX4720X004XK70999", source=PRUSA,
                      model="prusa_mk4")
        computer.save("voron", "moonraker", "http://192.168.1.24:7125", board_id="10000000a1b2c3d4",
                      source=KLIPPER, model="voron_2")
        computer.save("ender", "octoprint", "http://192.168.1.26", model="ender3")
        calls: list[dict] = []

        def fake_request(tool, method, path, body=None, **kw):
            calls.append(body)
            return {"status": "success", "success": True, "kept": [], "refused": 0, "machines": {}, "questions": []}

        import kiln.history_courier as hc

        monkeypatch.setattr(hc, "_request", fake_request)
        db = KilnDB(str(tmp_path / "kiln.db"))
        try:
            assert courier.sync(db=db, registry=computer)["synced"] is True
        finally:
            db.close()
        (body,) = calls
        by_code = {p["machine_code"]: p for p in body["machines"]}
        assert len(by_code) == 4
        shapes = sorted(tuple(sorted(set(p) - {"machine_code", "model"})) for p in by_code.values())
        assert shapes == [(), ("controller_code",), ("shared_code",), ("shared_code",)]
        shared = [p["shared_code"] for p in by_code.values() if "shared_code" in p]
        assert len(set(shared)) == 2
        flat = json.dumps(body).lower()
        for leak in ("czpx", "70123", "70999", "a1b2c3d4", "192.168", "7125", "left", "right", "voron\"", "key",
                     PRUSA, KLIPPER):
            assert leak.lower() not in flat, leak
        assert body == outbound_guard.scrub(body)
