"""Kiln - Agentic infrastructure for physical fabrication via 3D printing."""

from __future__ import annotations

import logging
import os

__version__ = "0.1.0"

_logger = logging.getLogger(__name__)


def parse_int_env(name: str, default: int) -> int:
    """Parse an integer from an environment variable with safe fallback.

    Logs a warning and returns *default* if the value is not a valid integer.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        _logger.warning(
            "Invalid integer for %s=%r, using default %d",
            name,
            raw,
            default,
        )
        return default


def parse_float_env(name: str, default: float) -> float:
    """Parse a float from an environment variable with safe fallback.

    Logs a warning and returns *default* if the value is not a valid number.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        _logger.warning(
            "Invalid number for %s=%r, using default %s",
            name,
            raw,
            default,
        )
        return default
