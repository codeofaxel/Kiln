"""Log rotation and sensitive data scrubbing for Kiln.

Provides a logging filter that redacts API keys, tokens, and passwords
from log output, and a helper to configure rotating file handlers with
the scrub filter installed.

Only stdlib modules are used.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from kiln import redaction

_DEFAULT_LOG_DIR = os.path.join(str(Path.home()), ".kiln", "logs")


class ScrubFilter(logging.Filter):
    """Logging filter that redacts sensitive data from log messages.

    Matches common patterns for API keys, tokens, passwords, and
    Authorization headers and replaces their values with
    ``***REDACTED***``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.msg and isinstance(record.msg, str):
            record.msg = _scrub(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: _scrub(v) if isinstance(v, str) else v for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(_scrub(a) if isinstance(a, str) else a for a in record.args)
        return True


def _scrub(text: str) -> str:
    """Redact secrets from *text* via the shared pattern set.

    Deliberately secrets-only: the log file stays on the user's own
    machine, so local IPs and file paths remain readable for local
    debugging.  Text that LEAVES the machine goes through
    :func:`kiln.redaction.redact_for_report` instead (see
    :func:`read_log_tail`).
    """
    return redaction.redact_secrets(text, marker="***REDACTED***")


def read_log_tail(max_bytes: int = 16 * 1024, log_dir: str | None = None) -> str | None:
    """Return the redacted tail of the rotating log, for bug reports.

    Reads the last *max_bytes* of ``kiln.log`` and runs the FULL boundary
    redaction (secrets + private IPs + home-directory usernames) so the
    result is safe to attach to a report that leaves the machine.
    Returns ``None`` when there is no log or it can't be read — a report
    must never fail because its attachment did.
    """
    log_dir = log_dir or os.environ.get("KILN_LOG_DIR", _DEFAULT_LOG_DIR)
    log_path = os.path.join(log_dir, "kiln.log")
    try:
        size = os.path.getsize(log_path)
        with open(log_path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            data = fh.read(max_bytes)
    except OSError:
        return None
    if not data:
        return None
    text = data.decode("utf-8", "replace")
    # A mid-file start leaves a partial first line; drop it.
    if size > max_bytes and "\n" in text:
        text = text.split("\n", 1)[1]
    return redaction.redact_for_report(text)


def configure_logging(
    log_dir: str | None = None,
    *,
    max_bytes: int = 10_000_000,
    backup_count: int = 5,
    level: str | None = None,
) -> None:
    """Configure logging with rotation and sensitive data scrubbing.

    :param log_dir: Directory for log files.  Reads ``KILN_LOG_DIR`` env
        var, then falls back to ``~/.kiln/logs/``.
    :param max_bytes: Maximum log file size before rotation (default 10 MB).
    :param backup_count: Number of rotated log files to keep (default 5).
    :param level: Log level string.  Reads ``KILN_LOG_LEVEL`` env var,
        then falls back to ``"INFO"``.
    """
    log_dir = log_dir or os.environ.get("KILN_LOG_DIR", _DEFAULT_LOG_DIR)
    level = level or os.environ.get("KILN_LOG_LEVEL", "INFO")

    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "kiln.log")

    log_level = getattr(logging, level.upper(), logging.INFO)

    scrub_filter = ScrubFilter()

    root = logging.getLogger()
    root.setLevel(log_level)

    # Add rotating file handler if not already present.
    has_rotating = any(isinstance(h, RotatingFileHandler) for h in root.handlers)
    if not has_rotating:
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(name)s %(levelname)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        file_handler.addFilter(scrub_filter)
        root.addHandler(file_handler)

    # Install scrub filter on all existing handlers.
    for handler in root.handlers:
        if scrub_filter not in handler.filters:
            handler.addFilter(scrub_filter)
