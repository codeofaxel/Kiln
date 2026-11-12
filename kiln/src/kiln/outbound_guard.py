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

Why it exists: a served check that reads a few sections of a Klipper
printer's configuration must not be sent the whole of it -- the controller's
serial port, CAN ids, macro bodies that run shell commands or carry tokens --
and a reading must not carry the printer's raw serial.  A reader's promise
never to send an address, a serial or a credential holds only where
something enforces it; this is that enforcement.
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
    "VERSION_FIELDS",
    "machine_code",
    "spool_code",
    "file_code",
    "file_hash_code",
    "job_code",
    "is_listing_link",
    "redact_text",
    "scrub",
    "scrub_relayed",
    "version_text",
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


def spool_code(value: Any) -> str | None:
    """A stable one-way code for a recorded spool's id on this computer,
    made as :func:`machine_code` is, under its own label so a spool and a
    machine never share a code.  The same spool gives the same code every
    time here; the code reveals nothing about the id."""
    text = str(value or "").strip().lower()
    if not text:
        return None
    return "sp:" + hmac.new(_key(), b"spool|" + text.encode("utf-8"), hashlib.sha256).hexdigest()[:24]


def _labelled_code(prefix: str, label: bytes, text: str) -> str | None:
    if not text:
        return None
    return prefix + hmac.new(_key(), label + b"|" + text.encode("utf-8"), hashlib.sha256).hexdigest()[:24]


def file_code(value: Any) -> str | None:
    """A stable one-way code for a printed file's name on this computer,
    made as :func:`machine_code` is, under its own label.  The name is read
    without its folder; the same file name gives the same code every time
    here, and the code reveals nothing about the name."""
    text = str(value or "").strip().replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    return _labelled_code("fc:", b"file", text)


def file_hash_code(value: Any) -> str | None:
    """A stable one-way code for a printed file's fingerprint on this
    computer, under its own label: two prints of the same bytes share it,
    and the fingerprint itself never leaves."""
    return _labelled_code("fh:", b"file-hash", str(value or "").strip().lower())


def job_code(value: Any) -> str | None:
    """A stable one-way code for a print's job id on this computer, under
    its own label.  A job id is often a file's name; the code is not."""
    return _labelled_code("pj:", b"job", str(value or "").strip())


def redact_text(text: str) -> str:
    """*text* read off a machine, as it may leave for Kiln's servers
    (:func:`kiln.redaction.redact_machine_text`).  A report keeps the links
    the person wrote; nothing a printer says keeps one."""
    from kiln.redaction import redact_machine_text

    return redact_machine_text(text)


#: The one key a link to a model's public listing page travels under.
LISTING_LINK_KEY = "listing_url"


def is_listing_link(value: Any) -> bool:
    """Whether *value* is an https link to a public listing page on a site a
    marketplace Kiln downloads from declares
    (``MarketplaceAdapter.listing_hosts``).  Such a page is public by
    nature; any other link says where something is, and is not sent."""
    if not isinstance(value, str) or len(value) > 500:
        return False
    try:
        from urllib.parse import urlsplit

        from kiln.marketplaces.base import listing_hosts
        import kiln.marketplaces  # noqa: F401 -- the adapters declare their sites as they load

        parts = urlsplit(value.strip())
        return (
            parts.scheme == "https"
            and not parts.username
            and not parts.password
            and (parts.hostname or "").lower() in listing_hosts()
        )
    except Exception:  # noqa: BLE001 -- a link that cannot be judged is not sent
        return False


#: A string this long made only of the letters a file's bytes are written in
#: is a file in text form (a camera frame), never a sentence.  Only this
#: computer's longer printer values are looked for inside it: a short one
#: can occur in a picture by chance, and cutting it out breaks the picture.
_BLOB_MIN = 2048
_BLOB = re.compile(r"(?:data:[\w/+.-]+;base64,)?[A-Za-z0-9+/_=-]+\Z")
_BLOB_OWN_MIN = 12
_LINK_LOGIN = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/\s@]+@", re.IGNORECASE)
#: What a saved printer's address is called (``kiln.printer_names``).
_ADDRESS_PHRASE = "the printer's network address"

#: The printer settings an install may hold in its environment, by the
#: saved-printer key each one stands for.
_ENV_PRINTER_VALUES = {
    "host": "KILN_PRINTER_HOST",
    "api_key": "KILN_PRINTER_API_KEY",
    "serial": "KILN_PRINTER_SERIAL",
    "mainboard_id": "KILN_PRINTER_MAINBOARD_ID",
}
#: Addresses of a camera the environment may name beside the printer's own,
#: and the saved-printer keys that hold one.
_ENV_PRINTER_ADDRESSES = ("KILN_PRINTER_CAMERA_SNAPSHOT_URL", "KILN_PRINTER_CAMERA_STREAM_URL")
_SAVED_ADDRESS_KEYS = re.compile(r"(?:_url|_host|_address)$|^(?:url|address|ip)$", re.IGNORECASE)


def _own_printer_values() -> list[tuple[str, str]]:
    """``(what it is, the value)`` for every address, serial and code this
    computer holds for its printers, longest value first."""
    import os

    from kiln.printer_names import identifiers_of

    found: dict[str, str] = {}
    entries: list[dict[str, Any]] = []
    try:
        from kiln.cli.config import _read_config_file, get_config_path

        saved = (_read_config_file(get_config_path()) or {}).get("printers") or {}
        for entry in saved.values():
            if not isinstance(entry, dict):
                continue
            entries.append(entry)
            entries.extend(
                {"host": str(value)} for key, value in entry.items()
                if value and _SAVED_ADDRESS_KEYS.search(str(key))
            )
    except Exception:  # noqa: BLE001 -- no saved printers is no values to find
        logger.debug("outbound guard: saved printers unreadable", exc_info=True)
    entries.append({key: os.environ.get(env) or "" for key, env in _ENV_PRINTER_VALUES.items()})
    entries.extend({"host": os.environ.get(env) or ""} for env in _ENV_PRINTER_ADDRESSES)
    for entry in entries:
        for phrase, value in identifiers_of(entry):
            found.setdefault(value, phrase)
    return sorted(((phrase, value) for value, phrase in found.items()), key=lambda pair: -len(pair[1]))


def _own_pattern(phrase: str, value: str) -> str:
    """How one of this computer's printer values is looked for in text.

    A serial, a key or a code is matched anywhere it appears.  An address is
    matched as a whole word, and a bare one-word host name (``voron``) only
    where it is used as an address, so the same word as a printer's name or
    model is left alone.
    """
    found = re.escape(value)
    if phrase != _ADDRESS_PHRASE or len(value) >= _BLOB_OWN_MIN:
        return found
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", value):
        return rf"(?:(?<=//)|(?<=@)){found}(?![A-Za-z0-9-])|(?<![A-Za-z0-9-]){found}(?=:\d)"
    return rf"(?<![A-Za-z0-9.]){found}(?![A-Za-z0-9])"


def _relayed_text(text: str, own: list[tuple[str, str]]) -> str:
    blob = len(text) >= _BLOB_MIN and _BLOB.match(text) is not None
    lowered = text.lower()
    for phrase, value in own:
        if blob and len(value) < _BLOB_OWN_MIN:
            continue
        if value.lower() in lowered:
            text = re.sub(_own_pattern(phrase, value), f"[{phrase}]", text, flags=re.IGNORECASE)
            lowered = text.lower()
    if blob:
        return text
    from kiln.redaction import redact_local_network, redact_secrets

    # A name and password written into a link.
    text = _LINK_LOGIN.sub(r"\1[a secret]@", text)
    return redact_local_network(redact_secrets(text))


def _relayed_key(key: Any, own: list[tuple[str, str]], taken: dict[Any, Any]) -> Any:
    """A field name as it leaves: text is read like any other text, and two
    names that read the same afterwards are kept apart by a count."""
    if not isinstance(key, str):
        return key
    try:
        name = _relayed_text(key, own)
    except Exception:  # noqa: BLE001 -- a name that cannot be read is not sent
        name = "[withheld]"
    if name == key or name not in taken:
        return name
    count = 2
    while f"{name} ({count})" in taken:
        count += 1
    return f"{name} ({count})"


def _relayed(value: Any, own: list[tuple[str, str]], depth: int) -> Any:
    if depth > _MAX_DEPTH:
        return None
    if isinstance(value, str):
        try:
            return _relayed_text(value, own)
        except Exception:  # noqa: BLE001 -- text that cannot be read is not sent
            return "[withheld]"
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            out[_relayed_key(key, own, out)] = _relayed(item, own, depth + 1)
        return out
    if isinstance(value, int) and not isinstance(value, bool):
        # A code or a serial written as a number is still that code.
        digits = str(value)
        for phrase, own_value in own:
            if own_value == digits:
                return f"[{phrase}]"
        return value
    if isinstance(value, bytes):
        try:
            return _relayed_text(value.decode("utf-8", "replace"), own)
        except Exception:  # noqa: BLE001
            return "[withheld]"
    if isinstance(value, list):
        return [_relayed(item, own, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_relayed(item, own, depth + 1) for item in value)
    return value


def scrub_relayed(value: Any) -> Any:
    """The answer of a tool a person ran on their own printers from the web
    or a cloud agent, as it leaves this computer.

    Unlike :func:`scrub`, nothing is dropped by its key: the answer is the
    person's own and is shown back to them, so its fields, handles, links
    and numbers arrive whole.  What is taken out of every piece of text and
    every field name, at any depth, is what says where a printer is or how
    to unlock it: each address, serial and code this computer holds for its
    printers (as text or as a number), a labelled secret, and any address
    or device on the local network.  A printer's name is not touched.
    Never raises.
    """
    try:
        own = _own_printer_values()
    except Exception:  # noqa: BLE001 -- the general rules still apply
        logger.debug("outbound guard: own printer values unreadable", exc_info=True)
        own = []
    return _relayed(value, own, 0)


#: A version as firmware states one: an optional short word, an optional
#: ``v``, two to five dot-separated numbers, and up to three short suffixes
#: each led by a dash, an underscore or a space (``01.07.00.00``,
#: ``Marlin 2.1.2.1``, ``v0.12.0-85-gd785b396``).
_VERSION_TEXT = re.compile(
    r"(?:[A-Za-z]{1,12}[ _-])?[vV]?\d{1,6}(?:\.\d{1,6}){1,4}(?:[-_ ][A-Za-z0-9]{1,12}){0,3}"
)
_VERSION_MAX_CHARS = 40
#: A run of letters and digits this long inside a version is checked for
#: being a piece of one of this computer's printer values.
_OWN_PIECE_MIN = 6

#: The fields a sender may pass through :func:`scrub` as a version, by the
#: sender's module: named here, and nowhere else.  Such a field is sent only
#: when :func:`version_text` accepts it, and is dropped otherwise.
VERSION_FIELDS: dict[str, frozenset[str]] = {
    "kiln/outcome_courier.py": frozenset({"firmware"}),
    "kiln/served_steps.py": frozenset({"firmware"}),
    "kiln/_pro_printer_checkin_bridge.py": frozenset({"firmware"}),
}


def version_text(value: Any) -> str | None:
    """*value* as a version that may leave as written, or ``None``.

    It must be nothing but a version (:data:`_VERSION_TEXT`, at most
    :data:`_VERSION_MAX_CHARS` characters) and must hold none of the
    addresses, serials or codes this computer keeps for its printers.  A
    value that fails either test, or one that cannot be checked against this
    computer's printers, is ``None``: it is dropped, never cleaned.  A piece
    of one of those values (:data:`_OWN_PIECE_MIN` characters or more) inside
    the version fails it too.
    """
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > _VERSION_MAX_CHARS or _VERSION_TEXT.fullmatch(text) is None:
        return None
    try:
        own = _own_printer_values()
    except Exception:  # noqa: BLE001 -- a value that cannot be checked is not sent
        logger.debug("outbound guard: own printer values unreadable", exc_info=True)
        return None
    lowered = text.lower()
    squeezed = re.sub(r"[^a-z0-9]", "", lowered)
    pieces = [run for run in re.findall(r"[a-z0-9]+", lowered) if len(run) >= _OWN_PIECE_MIN]
    for _phrase, held in own:
        mine = str(held).strip().lower()
        mine_squeezed = re.sub(r"[^a-z0-9]", "", mine)
        if mine and (mine in lowered or (mine_squeezed and mine_squeezed in squeezed)):
            return None
        if any(piece in mine_squeezed for piece in pieces):
            return None
    return text


def scrub(value: Any, _depth: int = 0, *, version_keys: frozenset[str] = frozenset()) -> Any:
    """*value* with nothing that locates, identifies or unlocks a machine.

    *version_keys* names the fields a sender declared in
    :data:`VERSION_FIELDS`: each is kept as written when
    :func:`version_text` accepts it and dropped with its value otherwise, at
    any depth.  A version of four numbers reads as a network address to the
    text rules, so it is judged whole here and never passed through them.

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
            if name in version_keys:
                version = version_text(item)
                if version is not None:
                    out[key] = version
                continue
            if name == LISTING_LINK_KEY:
                # Judged by key AND value: a marketplace's public page goes
                # as it is, anything else under this key goes nowhere.
                if is_listing_link(item):
                    out[key] = item.strip()
                continue
            if _IDENTITY.match(name):
                out[key] = machine_code(item) if isinstance(item, (str, int)) and not isinstance(item, bool) else None
                continue
            out[key] = scrub(item, _depth + 1, version_keys=version_keys)
        return out
    if isinstance(value, (list, tuple)):
        return [scrub(item, _depth + 1, version_keys=version_keys) for item in value]
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
        "served step sends, and a bug report's log tail pass through scrub; the "
        "print records kiln/served_steps.py reads for a served learning tool ride "
        "it too (what was printed in, how it went, settings and conditions, and "
        "when held: set_aside, a one-way code of the sliced settings, the spool as "
        "spool_code(), how far the print got, and the firmware version, declared in "
        "VERSION_FIELDS so it leaves only when version_text() accepts it; never a "
        "file, a spool id or a note); so "
        "does every value of an ask the person never made, which is how the words "
        "of a model Kiln made here that names no goal leave (kiln/make_words.py, "
        "sent by kiln/_pro_goal_bridge.py: the tool, the model's fingerprint, the "
        "kept id, the call's naming arguments and the made file's own name, each "
        "cleaned and capped there first; never the model, its source or a folder, "
        "and nothing at all from a signed-out install)",
    ),
    "kiln/terms.py": ("own_schema", "the terms version and when it was accepted"),
    "kiln/community_sync.py": (
        "own_schema",
        "shared print outcomes, by design anonymous: printer model, material, nozzle "
        "size, outcome, grade, failure word and firmware code, built through one "
        "allowlist that also checks each value's kind",
    ),
    "kiln/usage_ledger.py": ("own_schema", "tool names, plan refusals by tool name, and counts"),
    "kiln/auth_session.py": ("person's_own", "the sign-in's own refresh token, to Kiln's sign-in service"),
    "kiln/cli/auth_commands.py": ("person's_own", "the sign-in and pairing codes the person types"),
    "kiln/stage_link.py": ("person's_own", "the model file the person asked to view on the 3D stage"),
    "kiln/heartbeat.py": (
        "own_schema",
        "the daily heartbeat, built field by field: an install id and a salted device "
        "hash that must stay the same across days, version, printer models, counts; "
        "after it, for a printer outside the catalogue, its bed's three numbers, the "
        "source's class and a maker page's web host (kiln.printer_bed_report)",
    ),
    "kiln/bridge_client.py": (
        "scrubbed",
        "the answers of tools the person's own web session ran on their own printers, "
        "relayed to that session through scrub_relayed",
    ),
    "kiln/monitor_twin.py": ("person's_own", "the sliced file of the print the person is watching on the web"),
    "kiln/_pro_order_bridge.py": (
        "person's_own",
        "the model file, material, quantity, shipping details and order choices the "
        "person asked to quote, order or cancel",
    ),
    "kiln/cli/served_commands.py": (
        "person's_own",
        "the manual file the person asks Kiln's public verifier to check (kiln manual verify), "
        "and the release they name; the file is checked and not kept",
    ),
    "kiln/history_courier.py": (
        "person's_own",
        "the person's own design history (records and models) with each history call they "
        "make, and what they push, pull or clone with their own account; the manual files "
        "(a PDF or a 3MF, nothing else) a manual check names, sent whole with that call and "
        "not kept; its request "
        "helper also carries kiln/outcome_courier.py's print endings, built field by field "
        "(a random id, version ids, the machine as machine_code(), model and material "
        "words, nozzle, ending, who settled it, a failure word, two times, a one-way code "
        "of the sliced file's settings, how far the print got, the spool as spool_code() "
        "and the firmware version) and scrubbed; the firmware field is declared in "
        "VERSION_FIELDS, so it leaves only when version_text() accepts it (a bare "
        "version holding none of this computer's printer addresses, serials or codes) "
        "and is dropped otherwise; "
        "and kiln/machine_repair_courier.py's repair marks and printer announcements "
        "(a random id, the machine as machine_code(), a status word, a kind word, two "
        "times; per printer its code, model word and a one-way shared code -- never the "
        "note, the name, the serial or the access code) and the person's answer to an "
        "identity question (a question id and same/different), scrubbed; "
        "and kiln/_pro_printer_checkin_bridge.py's check-in on one printer after a "
        "print ends there, built field by field and scrubbed: the machine as "
        "machine_code(), the name the person gave the printer (cut to 40 characters, and "
        "left out when it holds one of this computer's printer addresses, serials or codes), "
        "its repair mark (a state word, a kind word, times), the episode keys the "
        "person has answered and when, and per print record a one-way code of the job "
        "id (job_code()), the ending, who settled it, a failure word, two times, the "
        "design id, the file's name as file_code() and its fingerprint as "
        "file_hash_code(), the material word, the nozzle size, a one-way code of the "
        "sliced settings, the spool as spool_code(), how far the print got, and the "
        "firmware version, declared in VERSION_FIELDS -- never a file's name, path or "
        "fingerprint, a job id, a serial, an address, a note, or settings",
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
