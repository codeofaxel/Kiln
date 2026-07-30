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

from collections.abc import Sequence
from dataclasses import dataclass


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


PRINTER_BACKENDS: tuple[PrinterBackend, ...] = (
    PrinterBackend("bambu", "Bambu Lab"),
    PrinterBackend("creality", "Creality (Klipper/Moonraker)"),
    PrinterBackend("prusalink", "Prusa Link"),
    PrinterBackend("elegoo", "Elegoo (SDCP)"),
    PrinterBackend("moonraker", "Moonraker (Klipper)"),
    PrinterBackend("octoprint", "OctoPrint"),
    PrinterBackend("duet", "Duet (RepRapFirmware)"),
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
