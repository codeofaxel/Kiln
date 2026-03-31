"""Anonymous installation ID for per-install tracking.

Generates a stable UUID4 on first use, stored at ``~/.kiln/installation_id``.
Completely invisible to the user — no prompts, no display.

Usage::

    from kiln.installation import get_installation_id, get_installation_headers

    iid = get_installation_id()
    headers = get_installation_headers()  # {"X-Kiln-Installation-Id": "<uuid>"}
"""

from __future__ import annotations

import os

import logging
import threading
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_PATH: Path = Path.home() / ".kiln" / "installation_id"
_lock = threading.Lock()
_cached_id: str | None = None


def _is_valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError):
        return False


def get_installation_id(path: Path | None = None) -> str:
    """Return the installation UUID, generating one if needed.

    Args:
        path: Override the ID file path (for testing).

    Returns:
        A stable UUID4 string for this installation.
    """
    global _cached_id
    p = path or _DEFAULT_PATH

    # Fast path: return cached value if using default path.
    if path is None and _cached_id is not None:
        return _cached_id

    with _lock:
        # Double-check after acquiring lock.
        if path is None and _cached_id is not None:
            return _cached_id

        # Try to read existing ID.
        try:
            if p.is_file():
                stored = p.read_text(encoding="utf-8").strip()
                if _is_valid_uuid(stored):
                    if path is None:
                        _cached_id = stored
                    return stored
                logger.debug("Corrupt installation ID file, regenerating")
        except OSError as exc:
            logger.debug("Could not read installation ID: %s", exc)

        # Generate new ID.
        new_id = str(uuid.uuid4())

        try:
            p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            p.write_text(new_id, encoding="utf-8")
            os.chmod(p, 0o600)
        except OSError as exc:
            logger.warning("Could not write installation ID file: %s", exc)

        if path is None:
            _cached_id = new_id
        return new_id


def get_installation_headers(path: Path | None = None) -> dict[str, str]:
    """Return HTTP headers containing the installation ID.

    Args:
        path: Override the ID file path (for testing).

    Returns:
        Dict with ``X-Kiln-Installation-Id`` header.
    """
    return {"X-Kiln-Installation-Id": get_installation_id(path)}
