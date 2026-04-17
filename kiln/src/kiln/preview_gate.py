"""Preview confirmation gate.

Jony Ive / Steve Jobs tier safety: ``start_print`` refuses to execute
unless the calling agent has demonstrated — via a fresh confirmation
token — that a preview was rendered and the user approved it.

Without this gate the Kiln A1 crashed on 2026-04-15 (incident #0):
the agent sliced, wrapped, uploaded, and started a print without
ever showing the user a preview.  Had a preview been rendered, the
missing start-gcode / off-bed centering would have been caught by
eye before bytes reached the printer.

Enforcement model (two-step):

    1. Agent calls ``render_preview_for_print(file_path, printer_id)``.
       This renders multi-angle previews (visualize_model), serialises
       them for display, and returns a token like ``pg_<hex>_<ttl>``.
       The user sees the preview in their chat and approves.

    2. Agent passes that token to ``start_print(confirmation_token=...)``.
       Server validates the token (matches file hash + printer, within
       TTL, not yet used).  If valid, start_print proceeds.  If not,
       start_print refuses with ``PREVIEW_NOT_CONFIRMED``.

Escape hatch: ``KILN_SKIP_PREVIEW_GATE=1`` environment variable bypasses
the gate for advanced users / CI.  Logged on every bypass.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Token format: pg_<32 hex chars>.  10-minute TTL by default.
_TOKEN_PREFIX = "pg_"
_DEFAULT_TTL_SEC = 600


def _looks_like_path(s: str) -> bool:
    """Decide whether ``s`` is a filesystem path or a bare identifier.

    The old heuristic was ``"/" in s or "\\" in s or "." in s`` — the
    dot check was wrong because every slicer output filename has a dot
    (``coaster.3mf``, ``plate_1.gcode``).  That caused ``validate()``
    to try to hash ``"coaster.3mf"`` as a file path from the server's
    cwd; the file didn't exist there, ``hash_file()`` returned the
    sentinel ``"NO_FILE:..."``, and the token check rejected every
    real start_print with ``token_file_hash_mismatch``.

    A path must contain a directory separator.  A bare filename (even
    ``"coaster.3mf"``) has no separator and should NOT be treated as
    a path.  Absolute paths start with ``/`` on POSIX and ``C:\\``-
    style on Windows; relative paths always contain ``os.sep``.
    """
    return "/" in s or "\\" in s


@dataclass
class PreviewToken:
    token: str
    file_hash: str            # SHA-256 of the file bytes at issue time.
    filename_key: str         # basename(file_path) — the stable key
                              # start_print can match against, since it
                              # doesn't have access to the local bytes
                              # (the file lives on the printer).
    printer_id: str | None
    issued_at: float
    ttl_seconds: int

    def expired(self, now: float | None = None) -> bool:
        if now is None:
            now = time.time()
        return (now - self.issued_at) > self.ttl_seconds


class PreviewGate:
    """Thread-safe registry of outstanding preview confirmation tokens.

    Validation strategy: a token carries BOTH a content hash (computed
    at issue time from the local bytes) and a filename key (basename
    of the file as the agent/user named it).  ``validate`` accepts
    either a local path (hash-compared) or a bare filename (key-
    compared) — the path is the stronger check, but start_print can
    only see the printer-side filename, so the key exists as the only
    feasible match for that call site.
    """

    def __init__(self) -> None:
        self._tokens: dict[str, PreviewToken] = {}
        self._lock = threading.Lock()

    def issue(
        self,
        file_path_or_hash: str,
        printer_id: str | None = None,
        ttl_seconds: int = _DEFAULT_TTL_SEC,
    ) -> PreviewToken:
        """Issue a new confirmation token for a file about to be printed."""
        if _looks_like_path(file_path_or_hash):
            file_hash = hash_file(file_path_or_hash)
            filename_key = os.path.basename(file_path_or_hash)
        else:
            # Caller passed a bare filename or a pre-computed hash.
            # If it looks like a slicer output filename (common shape
            # ``something.3mf|.gcode|.gco|.stl``), treat the whole
            # thing as the filename_key and leave the hash empty —
            # the token will only be validatable by key match.
            if "." in file_path_or_hash and len(file_path_or_hash) < 256:
                filename_key = file_path_or_hash
                file_hash = ""
            else:
                filename_key = ""
                file_hash = file_path_or_hash
        token_str = _TOKEN_PREFIX + secrets.token_hex(16)
        t = PreviewToken(
            token=token_str,
            file_hash=file_hash,
            filename_key=filename_key,
            printer_id=printer_id,
            issued_at=time.time(),
            ttl_seconds=ttl_seconds,
        )
        with self._lock:
            self._tokens[token_str] = t
            # Garbage-collect expired tokens while we're here.
            now = time.time()
            self._tokens = {
                k: v for k, v in self._tokens.items() if not v.expired(now)
            }
        return t

    def validate(
        self,
        token_str: str,
        file_path_or_name: str,
        printer_id: str | None = None,
        consume: bool = True,
    ) -> tuple[bool, str | None]:
        """Validate a confirmation token against a file + printer.

        Accepts either:
          * A full local path — matched by content hash (strongest).
          * A bare filename (no separators) — matched by filename_key.

        start_print calls this with the bare printer-side file_name,
        which has no separators, so the key path is the canonical
        match for the real-world hot path.  Tests + CLI tools that
        have a local path get the stronger hash check for free.

        Returns ``(ok, reason_if_not_ok)``.  On success with
        ``consume=True`` the token is removed (single-use).
        """
        if not token_str or not token_str.startswith(_TOKEN_PREFIX):
            return False, "invalid_token_format"
        with self._lock:
            t = self._tokens.get(token_str)
        if t is None:
            return False, "token_not_found_or_already_used"
        if t.expired():
            return False, "token_expired"

        if _looks_like_path(file_path_or_name):
            # Caller has a local path — prefer content-hash match.
            if not t.file_hash:
                # Token was issued with a bare filename so we have no
                # hash to compare against; fall through to key match.
                if os.path.basename(file_path_or_name) != t.filename_key:
                    return False, "token_filename_mismatch"
            else:
                actual_hash = hash_file(file_path_or_name)
                if t.file_hash != actual_hash:
                    return False, "token_file_hash_mismatch"
        else:
            # Caller has a bare filename (e.g. the printer-side
            # file_name on start_print).  Match by filename_key; no
            # bogus hash_file() call on a non-path.
            if t.filename_key:
                if file_path_or_name != t.filename_key:
                    return False, "token_filename_mismatch"
            elif t.file_hash:
                # Token was issued with a pre-computed hash (no path,
                # no filename) — fall through to hash string equality.
                if t.file_hash != file_path_or_name:
                    return False, "token_file_hash_mismatch"
            else:
                # Neither side has a handle to match on.
                return False, "token_has_no_binding"

        # Match by printer_id if token was scoped to one
        if t.printer_id and printer_id and t.printer_id != printer_id:
            return False, "token_printer_mismatch"
        if consume:
            with self._lock:
                self._tokens.pop(token_str, None)
        return True, None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def hash_file(path: str) -> str:
    """SHA-256 hash of a file's contents (first 16 hex chars — short but
    collision-resistant enough for this use case)."""
    p = Path(path)
    if not p.is_file():
        return f"NO_FILE:{path}"
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:32]


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_gate: PreviewGate | None = None
_gate_lock = threading.Lock()


def get_preview_gate() -> PreviewGate:
    global _gate  # noqa: PLW0603
    if _gate is None:
        with _gate_lock:
            if _gate is None:
                _gate = PreviewGate()
    return _gate
