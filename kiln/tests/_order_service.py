"""The order service as a test needs it: missing, or present.

A plain ``pip install kiln3d`` has no order service; a kiln-pro install
provides it as ``kiln.fulfillment``.  :func:`remove` makes any machine look
like the first and :func:`provide` like the second, so the ordering doors'
tests run on every machine instead of skipping wherever the module is
missing (which is everywhere but a kiln-pro install).

:func:`provide` hands back the real module where kiln-pro is installed, so
there the request types are the real ones; elsewhere it installs a stand-in
carrying only the names the doors read.  Its request types take any keyword
(``SimpleNamespace``), so the stand-in copies no field list that could
drift from the real one.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest

#: Every path the ordering doors have reached the order service through.
_PATHS = ("kiln.fulfillment", "kiln.fulfillment.base", "kiln.fulfillment.intelligence", "kiln.fulfillment_monitor")


class StandInFulfillmentError(Exception):
    """The stand-in order service's error type."""


def _no_provider(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("a test reached for a real print-service provider; inject one instead")


def remove(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make this process look like a plain install: no order service."""
    for path in _PATHS:
        monkeypatch.setitem(sys.modules, path, None)


def provide(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """The order service: the real one on a kiln-pro install, else a stand-in."""
    try:
        import kiln.fulfillment  # noqa: F401
    except ImportError:
        pass
    else:
        return sys.modules["kiln.fulfillment"]
    service = types.ModuleType("kiln.fulfillment")
    service.FulfillmentError = StandInFulfillmentError
    service.QuoteRequest = SimpleNamespace
    service.OrderRequest = SimpleNamespace
    service.get_provider = _no_provider
    service.get_order_history = _no_provider
    service.get_insurance_options = _no_provider
    for path in _PATHS[:3]:
        monkeypatch.setitem(sys.modules, path, service)
    return service
