"""Which physical machine a printer id means, and the other ids that mean it.

One machine answers to several ids: the server registers the active printer
as ``default`` AND under its config name; the model id (``bambu_a1``) is how
the catalogue and most agents refer to it; a person may call it ``kitchen``.
A record keyed by whichever id the caller used can only be found again by
that id -- unless every id resolves to the same machine first.

The registry already fingerprints a MACHINE (serial, else normalized
address -- :func:`kiln.registry.machine_fingerprint`), so every id that
resolves to the same fingerprint is one printer.  This module is the one
rule for that, so no door grows its own idea of who ``default`` is.

What is not guessed: a model id names a machine only when exactly one
registered machine is that model -- two of a model is a question only their
owner can answer.  A name nobody registered is never matched by prefix.
Aliases are read from the registry each time, never cached, so a printer
re-registered under an old name does not inherit the old machine's records.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def normalized(name: str) -> str:
    """The spelling every printer door matches ids by."""
    return str(name or "").casefold().replace("-", "_").strip()


def fingerprint_of(adapter: Any) -> str | None:
    """The registry's identity for the physical machine, or ``None`` for a
    machine it cannot identify -- which is then never merged with another."""
    try:
        from kiln.registry import machine_fingerprint

        return machine_fingerprint(adapter)
    except Exception:  # noqa: BLE001
        return None


def _registry() -> Any | None:
    try:
        from kiln.registry import get_printer_registry

        return get_printer_registry()
    except Exception:  # noqa: BLE001 -- no registry is no machine, never an error
        logger.debug("printer_identity: registry unavailable", exc_info=True)
        return None


def _configured_model(name: str) -> str | None:
    try:
        from kiln.printer_model_resolver import resolve_printer_model_for

        return normalized(resolve_printer_model_for(name) or "") or None
    except Exception:  # noqa: BLE001
        return None


def resolve_machine(printer_id: str) -> tuple[Any, str, str] | None:
    """The registered machine *printer_id* means: ``(adapter, name, how)``.

    A registered NAME resolves to itself (``how="name"``).  A model id
    resolves to a machine when exactly ONE registered machine is that model
    (``how="model"``), counted by fingerprint because the server registers
    one machine under two names.  ``None`` otherwise, and ``None`` is never a
    guess.
    """
    wanted = normalized(printer_id)
    registry = _registry()
    if not wanted or registry is None:
        return None
    try:
        names = list(registry.list_names())
        for name in names:
            if normalized(name) == wanted:
                return registry.get(name), name, "name"
        matches: dict[str, tuple[Any, str]] = {}
        for name in names:
            if _configured_model(name) != wanted:
                continue
            adapter = registry.get(name)
            matches.setdefault(fingerprint_of(adapter) or f"name:{name}", (adapter, name))
    except Exception:  # noqa: BLE001
        logger.debug("printer_identity: lookup failed", exc_info=True)
        return None
    if len(matches) != 1:
        return None
    adapter, name = next(iter(matches.values()))
    return adapter, name, "model"


def machine_aliases(printer_id: str) -> list[str]:
    """Every id that means the same physical machine as *printer_id*: the
    given id first, then the registered name, the machine's other registered
    names, and its configured model when that model names it uniquely.
    ``[printer_id]`` alone when it names no registered machine.
    """
    given = str(printer_id or "").strip()
    aliases = [given] if given else []
    resolved = resolve_machine(given)
    if resolved is None:
        return aliases
    adapter, name, _how = resolved
    aliases.append(name)
    fp = fingerprint_of(adapter)
    registry = _registry()
    if fp and registry is not None:
        try:
            for other in registry.list_names():
                if fingerprint_of(registry.get(other)) == fp:
                    aliases.append(other)
        except Exception:  # noqa: BLE001
            logger.debug("printer_identity: sibling names unavailable", exc_info=True)
        model = _configured_model(name)
        if model:
            by_model = resolve_machine(model)
            if by_model is not None and fingerprint_of(by_model[0]) == fp:
                aliases.append(model)
    return list(dict.fromkeys(a for a in aliases if a))


def record_home(printer_id: str, exists: Callable[[str], bool]) -> tuple[str, list[str]]:
    """Where a per-machine record keyed by id lives: ``(home, others)``.

    *home* is the first alias that already holds a record -- the given id
    first, so an exact record always wins -- or the given id when none does.
    *others* are further aliases that ALSO hold a record: a split the caller
    should report rather than silently read one half of.  An id that names
    no other machine is looked up nowhere else, so it costs no extra reads.
    """
    aliases = machine_aliases(printer_id)
    given = aliases[0] if aliases else str(printer_id or "").strip()
    if len(aliases) < 2:
        return given, []
    holding = [a for a in aliases if _safe(exists, a)]
    if not holding:
        return given, []
    return holding[0], holding[1:]


def _safe(exists: Callable[[str], bool], alias: str) -> bool:
    try:
        return bool(exists(alias))
    except Exception:  # noqa: BLE001 -- an unreadable alias holds nothing usable
        return False


__all__ = [
    "fingerprint_of",
    "machine_aliases",
    "normalized",
    "record_home",
    "resolve_machine",
]
