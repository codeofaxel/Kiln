"""Canonical registry of the printer backends Kiln can drive.

One list, one owner.  Adapter dispatch, config validation, the CLI's
``--type`` choices, and every "supported types are ..." message a user
sees after a typo all read the backends from here instead of restating
them.  Hand-maintained copies drift: ``duet`` shipped in 1.2 and was
accepted by the dispatcher and by ``validate_printer_config`` while four
separate user-facing strings still told people it did not exist.

Adding a backend is therefore one edit here plus its dispatch branch;
``tests/test_printer_backends_canonical.py`` fails when the two disagree.

Declaration order is the order users see everywhere, and matches the
Supported Printers table in ``README.md``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ConnectionNeed:
    """One thing a person supplies to connect a printer of some backend."""

    #: The key it is saved under in config.yaml.
    key: str
    #: What a person calls it.
    name: str
    #: Where a person finds it, said so they can go and look.
    where: str
    #: False when the printer connects without it.
    required: bool = True
    #: The ``register_printer`` argument that carries it, when that is not
    #: ``key`` (the tool takes a Bambu access code as ``api_key``).
    param: str = ""
    #: True when finding the printer on the network reads it, so a setup
    #: flow asks for it only when discovery did not.
    found_by_discovery: bool = False
    #: For an optional need: True when a printer that answered discovery
    #: without it has shown it is not needed.  False when the printer
    #: answers a status probe either way but will not take a print without
    #: it, so setup asks even for a printer it found.
    skip_if_found: bool = True

    @property
    def argument(self) -> str:
        """The ``register_printer`` argument that carries it."""
        return self.param or self.key


@dataclass(frozen=True)
class PrinterBackend:
    """One printer backend, as users name it and as we show it."""

    #: The ``type:`` in config.yaml, ``KILN_PRINTER_TYPE``, and the
    #: ``printer_type`` argument of ``register_printer``.
    slug: str
    #: Human-readable name for menus and probe results.
    label: str
    #: False for a backend reached over a cable rather than the network.
    #: Its "host" is a serial port path, so the discovery and setup
    #: surfaces that ask for an IP address leave it out.
    networked: bool = True
    #: What a person supplies to connect one, beyond its address, in the
    #: order a setup flow asks.  Every setup door reads this: the
    #: ``register_printer`` tool and its refusals, ``discover_printers``,
    #: ``kiln setup``, ``kiln quickstart``, ``kiln auth``, the bridge's
    #: first-printer offer, the check a saved config passes, and what an
    #: agent is told when no printer is set up.
    needs: tuple[ConnectionNeed, ...] = ()
    #: What to switch on in the printer before Kiln can connect, in a
    #: person's words, in order.
    first: tuple[str, ...] = ()


#: Moonraker's own login, which Creality and plain Klipper printers share.
#: Moonraker always checks it, and lets in the computers on its trusted list
#: without one (moonraker.readthedocs.io: configuration, installation).
_MOONRAKER_KEY = ConnectionNeed(
    "api_key",
    "Moonraker API key",
    "only needed if Kiln's computer is not on Moonraker's trusted list; to get one, open "
    "http://<printer address>/access/api_key from a computer that is, or run "
    "~/moonraker/scripts/fetch-apikey.sh on the printer",
    required=False,
)

PRINTER_BACKENDS: tuple[PrinterBackend, ...] = (
    PrinterBackend(
        "bambu",
        "Bambu Lab",
        # Menu paths are Bambu's own (wiki.bambulab.com: enable-lan-mode,
        # enable-developer-mode, find-sn), by the maker's model families.
        # An X1 on firmware before 01.09.00.00 still shows its older menus.
        needs=(
            ConnectionNeed(
                "serial",
                "serial number",
                "on the printer's screen under Settings > Device "
                "(Device and Serial Number on H2, P2S, X1 and X2D models)",
                found_by_discovery=True,
            ),
            ConnectionNeed(
                "access_code",
                "LAN access code",
                "on the printer's screen, on the LAN Only page in Settings "
                "(on a P1P or P1S: Settings > WLAN; on an X1 with older firmware: Settings > General)",
                param="api_key",
            ),
        ),
        first=(
            "On the page that shows the access code, turn on LAN Only Mode, then Developer Mode. "
            "Without them Kiln can read the printer's status but cannot start or "
            "control a print; with them, Bambu Handy and Bambu's cloud stop "
            "working with this printer.",
        ),
    ),
    PrinterBackend(
        "creality",
        "Creality (Klipper/Moonraker)",
        needs=(_MOONRAKER_KEY,),
        # Creality's own wiki and its K1 Series Annex.
        first=(
            "Kiln reaches a Creality printer through its Moonraker service. The K2, "
            "K2 Plus, K2 Pro, Ender-3 V3 and Ender-3 V3 Plus have it as shipped; a "
            "K1, K1 Max or K1C needs root access turned on and Creality's Fluidd "
            "install first, and the 2025 K1C and K1 Max cannot be rooted. "
            "`kiln doctor-creality` checks a specific printer.",
        ),
    ),
    PrinterBackend(
        "prusalink",
        "Prusa Link",
        needs=(
            # Prusa's firmware uses the PrusaLink password as the API key
            # (Prusa-Firmware-Buddy, lib/WUI/nhttp/req_parser.cpp).  Optional
            # for loading a config, so no saved printer stops loading; asked
            # even for a printer discovery found, which answers a status
            # probe either way but takes no print without it.
            ConnectionNeed(
                "api_key",
                "PrusaLink password",
                "on the printer's screen under Settings > Network > PrusaLink; an "
                "MK3S running PrusaLink on a Raspberry Pi makes its key in PrusaLink's "
                "web page instead, under Settings",
                required=False,
                skip_if_found=False,
            ),
        ),
    ),
    PrinterBackend("elegoo", "Elegoo (SDCP)"),
    PrinterBackend("moonraker", "Moonraker (Klipper)", needs=(_MOONRAKER_KEY,)),
    PrinterBackend(
        "octoprint",
        "OctoPrint",
        # docs.octoprint.org: the per-user Application Keys replace the
        # global key, which OctoPrint has announced it will remove.
        needs=(
            ConnectionNeed(
                "api_key",
                "OctoPrint API key",
                "in OctoPrint under User Settings > Application Keys, where you can make "
                "one for Kiln (the older key under Settings > API also works for now)",
            ),
        ),
    ),
    PrinterBackend(
        "duet",
        "Duet (RepRapFirmware)",
        needs=(
            ConnectionNeed(
                "api_key",
                "machine password",
                "only needed if one was set with M551 in config.g",
                required=False,
            ),
        ),
    ),
    PrinterBackend("usb", "Direct USB", networked=False),
)

#: Every accepted ``printer_type``.
PRINTER_TYPES: tuple[str, ...] = tuple(b.slug for b in PRINTER_BACKENDS)

#: The types reachable at an IP address — what network discovery can find,
#: and what the setup wizard offers once it has asked for a host.
NETWORK_PRINTER_TYPES: tuple[str, ...] = tuple(
    b.slug for b in PRINTER_BACKENDS if b.networked
)

#: Printer type -> display label.
PRINTER_TYPE_LABELS: dict[str, str] = {b.slug: b.label for b in PRINTER_BACKENDS}

#: Baud rate assumed for a USB printer that does not declare one.  Standard
#: for most Marlin builds; boards flashed for 250000 must say so, which is
#: why every door that creates a serial adapter has to carry the setting
#: rather than assume this.
DEFAULT_SERIAL_BAUDRATE = 115200


def format_printer_types(
    *,
    quote: str = "'",
    conjunction: str | None = None,
    types: Sequence[str] | None = None,
) -> str:
    """Render the supported printer types as one human-readable list.

    Args:
        quote: Wrapped around each type.  Pass ``""`` for bare words.
        conjunction: Placed before the final item (``"and"``, ``"or"``).
            ``None`` leaves a plain comma-separated list.
        types: The types to render; defaults to every supported type.

    Returns:
        e.g. ``"'bambu', 'creality', ..., and 'serial'"``.
    """
    names = PRINTER_TYPES if types is None else types
    items = [f"{quote}{slug}{quote}" for slug in names]
    if conjunction and len(items) > 1:
        items[-1] = f"{conjunction} {items[-1]}"
    return ", ".join(items)


# ---------------------------------------------------------------------------
# What it takes to connect one
# ---------------------------------------------------------------------------


def backend_for(slug: str) -> PrinterBackend | None:
    """The backend called *slug*, or ``None`` for a type Kiln does not drive."""
    return _BY_SLUG.get(slug)


def _supplied(value: object) -> bool:
    return bool(str(value or "").strip())


def missing_needs(
    slug: str, values: Mapping[str, object], *, by_argument: bool = False
) -> list[ConnectionNeed]:
    """The required needs *values* leaves empty.

    *values* is a config.yaml entry, keyed by config key -- or, with
    *by_argument*, a ``register_printer`` call keyed by its arguments.
    """
    backend = backend_for(slug)
    if backend is None:
        return []
    return [
        need
        for need in backend.needs
        if need.required and not _supplied(values.get(need.argument if by_argument else need.key))
    ]


def needs_to_ask(
    slug: str, *, found: Mapping[str, object] | None = None, discovered: bool
) -> list[ConnectionNeed]:
    """What a setup flow asks a person for.

    Every required need that *found* (what discovery read off the printer,
    by config key) does not fill -- and the optional ones too, except where
    a printer found on the network has already shown, by answering without
    one, that it is not needed (``skip_if_found``).
    """
    backend = backend_for(slug)
    if backend is None:
        return []
    found = found or {}
    return [
        need
        for need in backend.needs
        if not _supplied(found.get(need.key))
        and (need.required or not discovered or not need.skip_if_found)
    ]


def _need_clause(need: ConnectionNeed) -> str:
    return f"its {need.name} ({need.where})"


def needs_sentence(slug: str, needs: Sequence[ConnectionNeed]) -> str:
    """One sentence naming *needs* and where a person finds each.

    e.g. "To connect this Bambu Lab printer, Kiln also needs its LAN access
    code (...)."
    """
    backend = backend_for(slug)
    label = backend.label if backend else slug
    clauses = [_need_clause(need) for need in needs]
    if not clauses:
        return ""
    joined = clauses[0] if len(clauses) == 1 else ", ".join(clauses[:-1]) + " and " + clauses[-1]
    return f"To connect this {label} printer, Kiln also needs {joined}."


def connection_guide(
    slug: str, *, found: Mapping[str, object] | None = None, discovered: bool = True
) -> dict[str, object]:
    """What a reply carries so an agent can finish setting a printer up:
    what is still needed and where the person finds each, and what to
    switch on in the printer first."""
    backend = backend_for(slug)
    if backend is None:
        return {"still_needed": [], "first": []}
    return {
        "still_needed": [
            {"name": need.name, "where": need.where, "required": need.required}
            for need in needs_to_ask(slug, found=found, discovered=discovered)
        ],
        "first": list(backend.first),
    }


def setup_summary() -> str:
    """One line per backend -- what it needs and where it is found -- for an
    agent helping someone set up a printer Kiln could not find."""
    lines = []
    for backend in PRINTER_BACKENDS:
        parts = [f"{need.name}: {need.where}" for need in backend.needs]
        needs = "; ".join(parts) if parts else "nothing beyond its address"
        first = f" First: {' '.join(backend.first)}" if backend.first else ""
        lines.append(f"- {backend.label}: {needs}.{first}")
    return "\n".join(lines)


_BY_SLUG: dict[str, PrinterBackend] = {b.slug: b for b in PRINTER_BACKENDS}


def need_where(slug: str, key: str) -> str:
    """Where a person finds *key* for *slug* -- for a refusal or a recovery
    message that has to send them back to the same place setup did."""
    backend = backend_for(slug)
    for need in backend.needs if backend else ():
        if need.key == key:
            return need.where
    raise KeyError(f"{slug} has no connection need {key!r}")
