"""A Bambu printer's file server, faked at the FTPS client the adapter opens.

Serves the files it holds from ``/sdcard`` the way the printer does: ``RETR``
streams the bytes, ``SIZE`` and ``MDTM`` answer for a file it holds, and
``MDTM`` can be switched off, as some firmware leaves it out.  Every command
the adapter sends is logged, so a test can count reads.
"""

from __future__ import annotations

import datetime
import ftplib
import types
from typing import Any
from unittest import mock

from kiln.printers.bambu import BambuAdapter

STORAGE = "/sdcard"


class FakeBambuFTPS:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.stamps: dict[str, int] = {}
        self.answers_mdtm = True
        self.commands: list[str] = []
        self.sock = types.SimpleNamespace(getpeercert=lambda binary_form=True: b"printer-certificate")

    # -- what a test sets -----------------------------------------------
    def hold(self, name: str, data: bytes, stamp: int | None = None) -> None:
        self.files[f"{STORAGE}/{name}"] = data
        if stamp is not None:
            self.stamps[f"{STORAGE}/{name}"] = stamp

    def reads(self) -> list[str]:
        return [c for c in self.commands if c.startswith("RETR ")]

    # -- the client surface the adapter uses ------------------------------
    def __call__(self, *args: Any, **kwargs: Any) -> FakeBambuFTPS:
        return self

    def connect(self, host: str, port: int, timeout: float | None = None) -> str:
        return "220 ready"

    def login(self, user: str, passwd: str) -> str:
        return "230 logged in"

    def prot_p(self) -> str:
        return "200 protected"

    def cwd(self, path: str) -> str:
        if path != STORAGE:
            raise ftplib.error_perm("550 No such directory")
        return "250 ok"

    def voidcmd(self, cmd: str) -> str:
        self.commands.append(cmd)
        return "200 ok"

    def size(self, path: str) -> int:
        self.commands.append(f"SIZE {path}")
        if path not in self.files:
            raise ftplib.error_perm("550 No such file")
        return len(self.files[path])

    def sendcmd(self, cmd: str) -> str:
        self.commands.append(cmd)
        verb, _, path = cmd.partition(" ")
        if verb == "MDTM":
            if not self.answers_mdtm:
                raise ftplib.error_perm("502 Command not implemented")
            if path not in self.stamps:
                raise ftplib.error_perm("550 No such file")
            when = datetime.datetime.fromtimestamp(self.stamps[path], tz=datetime.timezone.utc)
            return "213 " + when.strftime("%Y%m%d%H%M%S")
        raise ftplib.error_perm("502 Command not implemented")

    def retrbinary(self, cmd: str, callback: Any, blocksize: int = 8192, rest: Any = None) -> str:
        self.commands.append(cmd)
        path = cmd.split(" ", 1)[1]
        if path not in self.files:
            raise ftplib.error_perm("550 No such file")
        data = self.files[path]
        for start in range(0, len(data), blocksize):
            callback(data[start : start + blocksize])
        return "226 Transfer complete"

    def quit(self) -> str:
        return "221 bye"

    def close(self) -> None:
        return None


def bambu_on(ftps: FakeBambuFTPS, monkeypatch: Any, tmp_path: Any) -> BambuAdapter:
    """A Bambu adapter whose file server is *ftps*."""
    monkeypatch.setenv("KILN_BAMBU_TLS_PIN_FILE", str(tmp_path / "bambu_tls_pins.json"))
    monkeypatch.setattr("kiln.printers.bambu._ImplicitFTP_TLS", ftps)
    adapter = BambuAdapter(host="192.0.2.10", access_code="12345678", serial="01S00A000000001", timeout=2)
    adapter._mqtt_client = mock.MagicMock()
    return adapter
