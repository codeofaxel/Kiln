"""A printer's name, kept free of what locates or identifies the machine.

A printer's name is the person's own label, and it goes where the printer
goes: with what Kiln asks its servers about that printer, and onto the
person's Kiln account.  A name like ``"Shop A1 @ 192.168.7.42"`` carries the
printer's network address everywhere the name goes, so every door that names
a printer asks this module first:

* :func:`name_advice` -- for a name a person or agent chose: ``None`` when it
  is clean, else what is in it, a clean name to use instead, and the one
  sentence that says why.  Kiln suggests; the person renames
  (:mod:`kiln.printer_rename`).
* :func:`default_printer_name` -- for a name Kiln picks itself (a setup
  default, the name a discovered printer advertises): always clean.

What counts as an identifier is read from :mod:`kiln.redaction`, the one
home of those rules (addresses, host names on the home network, device
paths, secrets), plus the printer's own serial, ids, access code, key and
address, matched as they are.  A person's own words ("adams-garage-x1c") are
never flagged.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

__all__ = [
    "NameAdvice",
    "default_printer_name",
    "discovered_name",
    "identifiers_of",
    "model_name",
    "name_advice",
    "saved_advice",
    "saved_advice_once",
    "saved_name",
    "slug",
]

#: What :func:`kiln.redaction.redact_places` leaves in place of each kind,
#: and how a sentence names it.
_PLACE_MARKERS: dict[str, str] = {
    "[a web address]": "a web address",
    "[an email address]": "an email address",
    "[a hardware address]": "a hardware address",
    "[a network address]": "a network address",
    "[a device]": "a device path",
}
_SECRET_MARKER = "[a secret]"

#: A saved printer's own identifying values, by config key, and how a
#: sentence names each.
_OWN_VALUES: dict[str, str] = {
    "host": "the printer's network address",
    "serial": "the printer's serial number",
    "mainboard_id": "the printer's mainboard id",
    "machine_id": "the printer's machine id",
    "access_code": "the printer's access code",
    "api_key": "the printer's API key",
}

#: An own value shorter than this is too common to recognise inside a name.
_MIN_OWN_LEN = 5

#: Words that only labelled an identifier, left at the edge of a name once
#: the identifier is gone ("A1 at 10.0.0.5" -> "bambu-a1", not "bambu-a1-at").
_LABEL_WORDS = frozenset({"at", "on", "ip", "host", "addr", "address", "sn", "serial", "mac", "code", "printer"})

_WHY = (
    "A printer's name goes with what Kiln asks its servers about that printer and shows "
    "on your Kiln account, so a name without it keeps that on this computer."
)


@dataclass(frozen=True)
class NameAdvice:
    """What a printer name carries and the clean name Kiln suggests instead."""

    name: str
    found: tuple[str, ...]
    suggested_name: str

    @property
    def sentence(self) -> str:
        """The advice in plain words, for a person or an agent to pass on."""
        return f"{self.name!r} has {_joined(self.found)} in it. {_WHY} Suggested name: {self.suggested_name!r}."

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "found": list(self.found),
            "suggested_name": self.suggested_name,
            "sentence": self.sentence,
        }


def slug(text: str) -> str:
    """*text* the way printer names are written: lowercase words joined by ``-``."""
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")


def identifiers_of(entry: Mapping[str, Any] | None = None, **values: Any) -> tuple[tuple[str, str], ...]:
    """The printer's own identifying values, as ``(phrase, value)`` pairs.

    Read from a saved config entry, keyword arguments, or both (keywords win).
    The address is reduced to its host, so ``http://10.0.0.5:7125`` is
    recognised in a name spelled ``10.0.0.5``.
    """
    merged: dict[str, Any] = dict(entry or {})
    merged.update({k: v for k, v in values.items() if v})
    out: list[tuple[str, str]] = []
    for key, phrase in _OWN_VALUES.items():
        raw = str(merged.get(key) or "").strip()
        if not raw:
            continue
        if key == "host":
            raw = _host_part(raw)
        if len(_compact(raw)) >= _MIN_OWN_LEN:
            out.append((phrase, raw))
    return tuple(out)


def model_name(printer_model: str | None) -> str:
    """A printer model the way a name says it: its maker's short name, then
    the model as the person spelled it -- ``"A1"`` -> ``"bambu-a1"``,
    ``"Prusa MK4"`` -> ``"prusa-mk4"``, ``"Voron 2.4"`` -> ``"voron-2-4"``.

    A model the catalogue does not list is written as given.  ``""`` when
    there is no model.
    """
    words = [w for w in slug(printer_model or "").split("-") if w]
    if not words:
        return ""
    maker = _maker_words(str(printer_model))
    if not maker:
        return "-".join(words)
    rest, spelled = list(words), list(maker)
    while rest and spelled and rest[0] == spelled[0]:
        rest, spelled = rest[1:], spelled[1:]
    return "-".join([_maker_short(maker), *rest]) if rest else "-".join(words)


def _maker_short(maker: list[str]) -> str:
    """The maker as its catalogue keys spell it (``Bambu Lab`` -> ``bambu``)."""
    try:
        from kiln.printer_declaration import _make_prefix

        return slug(_make_prefix(" ".join(maker))) or maker[0]
    except Exception:  # noqa: BLE001
        return maker[0]


def _catalogue_key(model: str) -> str | None:
    try:
        from kiln.printer_declaration import _catalogued

        return _catalogued(None, model)[0]
    except Exception:  # noqa: BLE001 -- a name is never worth failing a setup over
        return None


def _maker_words(model: str) -> list[str]:
    key = _catalogue_key(model)
    if not key:
        return []
    try:
        from kiln.printer_declaration import _make_of_key

        return slug(_make_of_key(key) or "").split("-")
    except Exception:  # noqa: BLE001
        return []


def name_advice(
    name: str,
    *,
    printer_model: str | None = None,
    identifiers: Iterable[tuple[str, str]] = (),
    place: str | None = None,
) -> NameAdvice | None:
    """``None`` when *name* carries nothing that locates or identifies the
    machine; else what it carries and a clean name to suggest.

    *identifiers* are the printer's own values (:func:`identifiers_of`).
    A suggestion another printer already uses is left to the save to judge:
    the same machine updates, a different one is refused with a free name.
    """
    own = tuple(identifiers)
    found = _found_in(name, own)
    if not found:
        return None
    suggestion = _clean_name(name, printer_model=printer_model, identifiers=own, place=place)
    return NameAdvice(name=name, found=found, suggested_name=suggestion)


def default_printer_name(
    network_name: str | None,
    *,
    printer_type: str,
    printer_model: str | None = None,
    identifiers: Iterable[tuple[str, str]] = (),
) -> str:
    """The name Kiln offers for a printer: the name it advertises on the
    network when that is clean, with anything identifying taken out when it
    is not, else the model's name, else the connection type."""
    own = tuple(identifiers)
    advertised = str(network_name or "").strip().rstrip(".")
    if advertised and not _found_in(advertised, own):
        base = slug(advertised)
    elif advertised:
        base = _clean_name(advertised, printer_model=printer_model, identifiers=own)
    else:
        base = model_name(printer_model)
    return base or slug(printer_type) or "printer"


def discovered_name(found: Any, *, printer_model: str | None = None) -> str:
    """:func:`default_printer_name` for a printer found on the network
    (:class:`kiln.discovery.DiscoveredPrinter`): its advertised name, address
    and serial, and for a Bambu Lab printer the model its serial suggests."""
    printer_type = str(getattr(found, "printer_type", "") or "")
    serial = str(getattr(found, "serial", "") or "")
    if not printer_model and printer_type == "bambu" and serial:
        try:
            from kiln.cli.printer_model_prompt import suggest_bambu_model

            printer_model = suggest_bambu_model(serial)
        except Exception:  # noqa: BLE001
            printer_model = None
    return default_printer_name(
        getattr(found, "name", ""),
        printer_type=printer_type,
        printer_model=printer_model,
        identifiers=identifiers_of(host=getattr(found, "host", ""), serial=serial),
    )


def saved_name(name: str) -> bool:
    """Whether a printer is saved under *name* in this computer's config."""
    return _saved_entry(name) is not None


def saved_advice(name: str) -> dict[str, Any] | None:
    """:func:`name_advice` for a printer already saved under *name*, read
    against its own saved address, serial and codes, with what switching
    costs today; ``None`` when the name is clean or nothing is saved."""
    entry = _saved_entry(name)
    if entry is None:
        return None
    advice = name_advice(name, printer_model=entry.get("printer_model"), identifiers=identifiers_of(entry))
    if advice is None:
        return None
    return {
        **advice.to_dict(),
        "sentence": (
            f"{advice.sentence} To switch, rename it: `kiln rename-printer {name} {advice.suggested_name}` "
            "at a terminal, or the rename_printer tool; its history, settings and permissions follow."
        ),
    }


def saved_advice_once(name: str | None) -> dict[str, Any] | None:
    """:func:`saved_advice` the first time *name* is asked about in a
    session (:func:`kiln.tiers_and_terms.claim_once`), ``None`` after: said
    once, never on every status read.  Never raises."""
    if not name:
        return None
    try:
        from kiln.tiers_and_terms import claim_once

        if not claim_once(f"printer_name_advice:{name}"):
            return None
        return saved_advice(name)
    except Exception:  # noqa: BLE001 -- a name is never worth failing a status read
        return None


def _saved_entry(name: str) -> dict[str, Any] | None:
    try:
        from kiln.cli.config import _read_config_file, get_config_path

        entry = (_read_config_file(get_config_path()).get("printers") or {}).get(name)
    except Exception:  # noqa: BLE001 -- no config yet is no saved printer
        return None
    return entry if isinstance(entry, dict) else None


# ---------------------------------------------------------------------------
# Reading a name
# ---------------------------------------------------------------------------


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _host_part(host: str) -> str:
    raw = host.strip()
    if raw.startswith("/") or re.match(r"^[A-Za-z]:\\|^COM\d+$", raw, re.IGNORECASE):
        return raw  # a serial port; redaction names device paths itself
    try:
        parsed = urlsplit(raw if "://" in raw else f"//{raw}")
        return parsed.hostname or raw
    except ValueError:
        return raw


def _as_written(name: str) -> str:
    """*name* with digits joined by ``-`` or ``_`` read as joined by ``.``:
    Kiln's own setup once wrote ``192.168.7.42`` as ``192-168-7-42``."""
    return re.sub(r"(?<=\d)[-_](?=\d)", ".", name)


def _marked(name: str) -> str:
    from kiln.redaction import redact_places, redact_secrets

    return redact_secrets(redact_places(_as_written(name)), marker=_SECRET_MARKER)


def _found_in(name: str, own: tuple[tuple[str, str], ...]) -> tuple[str, ...]:
    """What *name* carries, in sentence words, each once, in a stable order."""
    if not str(name or "").strip():
        return ()
    marked = _marked(name)
    found: list[str] = []
    compact_name = _compact(name)
    for phrase, value in sorted(own, key=lambda item: -len(_compact(item[1]))):
        value_compact = _compact(value)
        if value_compact in compact_name:
            compact_name = compact_name.replace(value_compact, " ")
            if phrase not in found:
                found.append(phrase)
    for marker, phrase in _PLACE_MARKERS.items():
        if marker in marked and phrase not in found:
            if phrase == "a network address" and "the printer's network address" in found:
                continue
            found.append(phrase)
    if _SECRET_MARKER in marked:
        found.append("a password or code")
    return tuple(found)


def _clean_name(
    name: str,
    *,
    printer_model: str | None,
    identifiers: tuple[tuple[str, str], ...],
    place: str | None = None,
) -> str:
    """*name* with what identifies the machine taken out: the model's name
    first when it is known, then the person's own words that it does not
    already say ("Shop A1 @ 192.168.7.42" -> ``bambu-a1-shop``)."""
    text = _keep_host_labels(name)
    for _, value in identifiers:
        text = re.sub(re.escape(value), " ", text, flags=re.IGNORECASE)
    text = _marked(text)
    for marker in (*_PLACE_MARKERS, _SECRET_MARKER):
        text = text.replace(marker, " ")
    words = slug(text).split("-")
    while words and (not words[0] or words[0] in _LABEL_WORDS):
        words.pop(0)
    while words and (not words[-1] or words[-1] in _LABEL_WORDS):
        words.pop()
    if place:
        words += [w for w in slug(place).split("-") if w and w not in words]
    model = model_name(printer_model)
    if model:
        words = [*model.split("-"), *(w for w in words if not _names_model(w, model, printer_model))]
    cleaned = "-".join(words)
    if not cleaned or _found_in(cleaned, identifiers):
        cleaned = "-".join(part for part in (model, slug(place or "")) if part)
    return cleaned or "printer"


def _names_model(word: str, model: str, printer_model: str | None) -> bool:
    """Whether one of the person's own words only repeats the model: a word
    of the model's name ("a1", "voron"), or one the catalogue reads as the
    same printer ("x1c" for an X1 Carbon)."""
    if word in model.split("-"):
        return True
    key = _catalogue_key(str(printer_model or ""))
    return bool(key) and _catalogue_key(word) == key


def _keep_host_labels(name: str) -> str:
    """A host name on the home network (``voron.local``) reduced to its
    first label, which is the word the person gave the machine."""
    from kiln.redaction import redact_places

    out: list[str] = []
    for token in re.split(r"(\s+)", name):
        bare = token.strip().rstrip(".")
        if "://" in bare:
            try:
                bare = urlsplit(bare).hostname or ""
            except ValueError:
                bare = ""
        bare = bare.split(":")[0]
        label = bare.split(".")[0]
        if (
            "." in bare
            and redact_places(bare) == "[a network address]"
            and re.search(r"[a-z]", label, re.IGNORECASE)
            and redact_places(label) == label
        ):
            out.append(label)
        else:
            out.append(token)
    return "".join(out)


def _joined(phrases: tuple[str, ...]) -> str:
    if len(phrases) <= 1:
        return "".join(phrases)
    return ", ".join(phrases[:-1]) + " and " + phrases[-1]
