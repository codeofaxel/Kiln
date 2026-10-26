"""What this computer reads off its own printers never reaches Kiln's servers raw.

Some of what an install sends to Kiln's servers is the person's own words: a
prompt, a file they chose.  The rest is read off their machines by Kiln on
their behalf: a printer's facts, its configuration, a nozzle reading, a fault
log, a count at print start.  That second kind is where the address of a
printer, its serial number, an access code, a controller's serial port or a
home folder can ride along without anyone choosing to send it.  Every value
of that kind passes through :func:`scrub` before it leaves, at the doors that
send it (:func:`kiln.server._pro_api_call` and the senders named in
:data:`SENDERS`), so a reader that forgets to cut its own output still sends
nothing that locates, identifies or unlocks a machine.

What :func:`scrub` does, at any depth:

* a key that names a secret (an API key, a token, an access code, a password)
  or a place (a host, an address, a URL) is dropped with its value;
* a key that names a machine's identity (a serial, a fingerprint, a CAN id, a
  MAC) keeps its role but not its value: the value becomes a stable one-way
  code (:func:`machine_code`), the same for the same machine on this computer
  and meaningless anywhere else, so a reading history can still be kept per
  machine without the serial being known;
* inside any text, a secret, an address, a URL, a host on the local
  network, a device path, a home folder's owner or an email address is
  replaced (:mod:`kiln.redaction`, the one home of these rules).

Why it exists: 2026-10-17, a served power-loss check was sent the whole of a
Klipper printer's configuration -- the controller's serial port, CAN ids,
macro bodies that run shell commands or carry tokens -- although it reads six
sections.  The facts reader promised in its docstring never to send an
address, a serial or a credential, and nothing enforced the promise.  The
same review found a nozzle reading carrying the printer's raw serial.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "SENDERS",
    "machine_code",
    "redact_text",
    "scrub",
]

#: Keys whose value is a secret, or where a machine is: dropped.  Exact
#: names, so a server-minted handle (``source_artifact_token``) the install
#: hands back is never mistaken for a credential.
_SECRET_OR_PLACE = re.compile(
    r"^(?:api_?key|apikey|access_?code|lan_?code|access_?token|refresh_?token|auth_?token|"
    r"api_?token|token|secret|client_?secret|password|passwd|authorization|bearer|cookie|"
    r"credentials?|private_?key|host|hostname|ip|ip_?address|address|url|uri|endpoint|"
    r"base_?url|api_?url|printer_?url|webhook_?url|email)$",
    re.IGNORECASE,
)
#: Keys whose value says WHICH machine: kept as a one-way code.
_IDENTITY = re.compile(
    r"^(?:serial|serial_?number|printer_?serial|fingerprint|machine_?fingerprint|"
    r"canbus_?uuid|mac|mac_?address)$",
    re.IGNORECASE,
)
#: How deep a value is walked; deeper is not data a reader produces.
_MAX_DEPTH = 24

_code_key: bytes | None = None


def _key() -> bytes:
    """A secret only this computer holds: its own machine id, never sent."""
    global _code_key
    if _code_key is None:
        try:
            from kiln.device import _read_machine_id

            seed = _read_machine_id() or ""
        except Exception:  # noqa: BLE001 -- no id is still a key, just not a stable one
            seed = ""
        if not seed:
            import secrets

            seed = secrets.token_hex(32)
        _code_key = hashlib.sha256(f"kiln-outbound-machine-code-v1|{seed}".encode()).digest()
    return _code_key


def machine_code(value: Any) -> str | None:
    """A stable one-way code for a machine's identity on this computer.

    The same serial gives the same code every time here, so a served
    history can follow one machine; the code reveals nothing about the
    serial, and another computer gives another code.
    """
    text = str(value or "").strip().lower()
    if not text:
        return None
    return "id:" + hmac.new(_key(), text.encode("utf-8"), hashlib.sha256).hexdigest()[:24]


def redact_text(text: str) -> str:
    """*text* read off a machine, as it may leave for Kiln's servers
    (:func:`kiln.redaction.redact_machine_text`).  A report keeps the links
    the person wrote; nothing a printer says keeps one."""
    from kiln.redaction import redact_machine_text

    return redact_machine_text(text)


def scrub(value: Any, _depth: int = 0) -> Any:
    """*value* with nothing that locates, identifies or unlocks a machine.

    Never raises.  Numbers, flags and ordinary words pass unchanged.
    """
    if _depth > _MAX_DEPTH:
        return None
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            name = str(key)
            if _SECRET_OR_PLACE.match(name):
                continue
            if _IDENTITY.match(name):
                out[key] = machine_code(item) if isinstance(item, (str, int)) and not isinstance(item, bool) else None
                continue
            out[key] = scrub(item, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [scrub(item, _depth + 1) for item in value]
    return value


#: Every place in public Kiln that sends something to Kiln's servers, and what
#: it sends.  ``scrubbed``: machine-read values pass through :func:`scrub`
#: there.  ``person's_own``: what is sent is what the person chose to send (a
#: file, a sign-in), named in the reason.  ``own_schema``: a fixed set of
#: fields built by hand, each one checked here by name.  The gate
#: (``tests/test_outbound_guard.py``) finds every sender in the source and
#: fails for one this table does not name, so a new way out is decided the
#: day it is written.
SENDERS: dict[str, tuple[str, str]] = {
    "kiln/server.py": (
        "scrubbed",
        "the served door: machine-read call arguments, the facts and answers a "
        "served step sends, and a bug report's log tail pass through scrub",
    ),
    "kiln/terms.py": ("own_schema", "the terms version and when it was accepted"),
    "kiln/community_sync.py": (
        "own_schema",
        "shared print outcomes, by design anonymous: a geometry signature, model, "
        "material, settings hash and outcome, built field by field; its signature must "
        "match across installs, so it is never rewritten here",
    ),
    "kiln/usage_ledger.py": ("own_schema", "tool names and counts"),
    "kiln/auth_session.py": ("person's_own", "the sign-in's own refresh token, to Kiln's sign-in service"),
    "kiln/cli/auth_commands.py": ("person's_own", "the sign-in and pairing codes the person types"),
    "kiln/stage_link.py": ("person's_own", "the model file the person asked to view on the 3D stage"),
    "kiln/heartbeat.py": (
        "own_schema",
        "the daily heartbeat, built field by field: an install id and a salted device "
        "hash that must stay the same across days, version, printer models, counts",
    ),
    "kiln/bridge_client.py": (
        "person's_own",
        "the answers of tools the person's own web session ran on their own printers, "
        "relayed to that session",
    ),
    "kiln/monitor_twin.py": ("person's_own", "the sliced file of the print the person is watching on the web"),
    "kiln/cli/served_commands.py": (
        "person's_own",
        "the manual file the person asks Kiln's public verifier to check (kiln manual verify), "
        "and the release they name; the file is checked and not kept",
    ),
    "kiln/history_courier.py": (
        "person's_own",
        "the person's own design history (records and models) with each history call they "
        "make, and what they push, pull or clone with their own account; its request "
        "helper also carries kiln/outcome_courier.py's print endings, built field by field "
        "(a random id, version ids, the machine as machine_code(), model and material "
        "words, nozzle, ending, who settled it, a failure word, two times) and scrubbed; "
        "and kiln/machine_repair_courier.py's repair marks and printer announcements "
        "(a random id, the machine as machine_code(), a status word, a kind word, two "
        "times; per printer its code, model word and a one-way shared code -- never the "
        "note, the name, the serial or the access code) and the person's answer to an "
        "identity question (a question id and same/different), scrubbed",
    ),
    "kiln/account_plan.py": ("own_schema", "asks which plan the signed-in account holds; sends only the sign-in"),
    "kiln/plugins/spend_cap_tools.py": (
        "person's_own", "the spending limits the person reads, sets or approves on their own account",
    ),
    "kiln/served_makes.py": (
        "person's_own",
        "the files a served call names (a model, an image) going up, and what the servers "
        "made coming back; machine-read values ride the call through the served door",
    ),
    "kiln/stage_cache.py": ("own_schema", "fetches the 3D stage's own page; sends nothing of the person's"),
    "kiln/served_public.py": (
        "person's_own",
        "a public tool's own call run on Kiln's servers, with the files it names; the "
        "printer facts read first ride the served door, which scrubs them, and a made "
        "file comes back by its server-minted token",
    ),
}
