"""Every kind of printer Kiln drives can hand a print file back, or says why
it need not.

A print started by its name on the printer is checked against the bytes the
printer holds: the safety checks read them, the approval card shows them,
and a yes given for other bytes does not start them.  A backend that cannot
hand a file back leaves all three blind, so a new backend implements
``read_print_file`` and ``print_file_identity`` (the contract in
:mod:`kiln.printers.read_back`), or is named in
:data:`kiln.printers.read_back.NO_READ_BACK` with the reason the bytes that
print are known without them.  The adapters are found by walking every
module in :mod:`kiln.printers`, never from a list.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import kiln.printers as printers_pkg
from kiln.printers.base import PrinterAdapter
from kiln.printers.read_back import NO_READ_BACK


def _adapters() -> dict[str, type]:
    found: dict[str, type] = {}
    for info in pkgutil.iter_modules(printers_pkg.__path__):
        module = importlib.import_module(f"kiln.printers.{info.name}")
        for name, cls in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(cls, PrinterAdapter)
                and cls is not PrinterAdapter
                and cls.__module__ == module.__name__
                and not inspect.isabstract(cls)
            ):
                found[name] = cls
    return found


def _overrides(cls: type, method: str) -> bool:
    return getattr(cls, method) is not getattr(PrinterAdapter, method)


def test_the_walk_finds_the_adapters():
    found = _adapters()
    # The backends this test exists for, so a walk that found nothing
    # cannot pass as every backend covered.
    for name in ("BambuAdapter", "OctoPrintAdapter", "MoonrakerAdapter", "PrusaLinkAdapter", "DuetAdapter"):
        assert name in found, name


def test_every_adapter_hands_its_file_back_or_says_why_not():
    missing = sorted(
        name
        for name, cls in _adapters().items()
        if name not in NO_READ_BACK
        and not (_overrides(cls, "read_print_file") and _overrides(cls, "print_file_identity"))
    )
    assert missing == [], (
        "these printers cannot hand a print file back, so a print started by name there is never checked "
        f"against the bytes it prints: {missing}. Implement read_print_file and print_file_identity "
        "(kiln.printers.read_back), or name the adapter in NO_READ_BACK with why its bytes are known."
    )


def test_every_reason_is_said_and_names_a_real_adapter():
    found = _adapters()
    for name, why in NO_READ_BACK.items():
        assert name in found, f"{name} is in NO_READ_BACK but no such adapter exists"
        assert why.strip(), name
        assert not (_overrides(found[name], "read_print_file") and _overrides(found[name], "print_file_identity")), (
            f"{name} hands its file back now: remove it from NO_READ_BACK"
        )
