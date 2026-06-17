"""Stable per-machine id sent to the hosted Kiln API as the device fingerprint.

When this install talks to ``api.kiln3d.com`` with a *license-key* bearer
(the hosted-pro proxy in ``kiln.server._pro_api_call``, and terms-sync in
``kiln.terms``), it sends the ``X-Kiln-Device-Fingerprint`` header so the
server's per-license device-activation cap can count this machine.  A
license-bearer request that omits it is rejected once the cap is enforced.

This is deliberately a MIRROR of kiln-pro's ``kiln_pro.device_fingerprint``
resolver — same env var, same on-disk file, same value format — so that a
machine running both packages reports ONE device and never burns two of a
license's activations.  Public Kiln cannot import kiln-pro, hence the copy.

Distinct from two neighbours that look similar:
  * ``kiln.device.get_device_fingerprint`` — a one-way *hashed* hardware id
    for anonymous telemetry (different value, different purpose).
  * ``kiln.usage_ledger.device_id`` — a separate id at ``~/.kiln/device_id``
    for the JWT-only stats endpoint.
Neither is the activation-cap fingerprint; do not substitute them here.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

#: Header every license-bearer API client must send.
DEVICE_FINGERPRINT_HEADER = "X-Kiln-Device-Fingerprint"

#: Env override (highest precedence) — lets an operator pin the id.
_ENV_VAR = "KILN_DEVICE_FINGERPRINT"


def _fingerprint_path() -> Path:
    return Path.home() / ".kiln" / "device_fingerprint"


def device_fingerprint() -> str:
    """Return a stable local device fingerprint (a persisted random UUID).

    Resolution order: ``KILN_DEVICE_FINGERPRINT`` env override → the value
    cached at ``~/.kiln/device_fingerprint`` → a freshly-minted id that is
    then persisted.  On any filesystem error this returns an ephemeral id
    rather than raising — a fingerprint is never worth failing a request
    over.
    """
    explicit = os.environ.get(_ENV_VAR, "").strip()
    if explicit:
        return explicit

    path = _fingerprint_path()
    try:
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        path.parent.mkdir(parents=True, exist_ok=True)
        value = f"kiln-device-{uuid.uuid4().hex}"
        path.write_text(value, encoding="utf-8")
        if os.name != "nt":
            path.chmod(0o600)
        return value
    except Exception:
        return f"kiln-device-{uuid.uuid4().hex}"


def device_fingerprint_headers() -> dict[str, str]:
    """The one header every license-bearer API client merges into its request."""
    return {DEVICE_FINGERPRINT_HEADER: device_fingerprint()}
