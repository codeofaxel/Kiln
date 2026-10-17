"""Kiln - Agentic infrastructure for physical fabrication via 3D printing."""

from __future__ import annotations

import logging
import os
import re
import sys as _sys
from pathlib import Path

try:
    from importlib.metadata import PackageNotFoundError, version
except ImportError:  # pragma: no cover - py310+ ships importlib.metadata
    from importlib_metadata import PackageNotFoundError, version  # type: ignore[no-redef]


_logger = logging.getLogger(__name__)


def _resolve_version() -> str:
    """Resolve the installed package version with a source-tree fallback."""
    # Source-tree first: avoids stale installed metadata when running from git.
    try:
        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        if pyproject.is_file():
            content = pyproject.read_text(encoding="utf-8")
            match = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"\s*$', content)
            if match:
                return match.group(1)
    except Exception as exc:
        _logger.debug("Local pyproject version fallback failed: %s", exc)

    for pkg in ("kiln3d", "kiln"):
        try:
            return version(pkg)
        except PackageNotFoundError:
            continue
        except Exception as exc:
            _logger.debug("Package version lookup failed for %s: %s", pkg, exc)
            break

    return "unknown"


__version__ = _resolve_version()


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


# ---------------------------------------------------------------------------
# Lazy attribute lookup for kiln-pro compat shims.
# kiln-pro registers shimmed submodules (e.g. kiln.licensing) in
# sys.modules but doesn't set them as attributes on this package.
# __getattr__ bridges that gap so attribute-based traversal works.
#
# A submodule another thread is still importing is in sys.modules too, half
# built.  ``from kiln import x`` asks this hook first, so handing that module
# out skipped the wait an import statement makes: on a cold start, slices run
# in parallel read the melt-rate bridge before its functions existed, and each
# one quietly lost its material settings.  Such a module goes back through the
# import system, which waits for the other thread and accepts a half-built
# module only where waiting would deadlock, as an import statement does.
# ---------------------------------------------------------------------------


def __getattr__(name: str):
    fqn = f"{__name__}.{name}"
    mod = _sys.modules.get(fqn)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if getattr(getattr(mod, "__spec__", None), "_initializing", False):
        __import__(fqn)
        if fqn not in _sys.modules:
            # The other thread's import failed: run it here, so its error
            # is the one raised.
            __import__(fqn)
        mod = _sys.modules[fqn]
    setattr(_sys.modules[__name__], name, mod)
    return mod
