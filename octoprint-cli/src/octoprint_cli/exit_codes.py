"""Exit codes for agent-friendly error handling.

These codes allow autonomous agents to programmatically determine
the category of failure without parsing error messages.
"""

from __future__ import annotations

# Success
SUCCESS = 0

# Printer is offline or unreachable
PRINTER_OFFLINE = 1

# File-related error (not found, invalid format, too large)
FILE_ERROR = 2

# Printer is busy (already printing, paused, etc.)
PRINTER_BUSY = 3

# Any other error (auth, server error, unknown)
OTHER_ERROR = 4


ERROR_CODE_MAP: dict[str, int] = {
    "CONNECTION_ERROR": PRINTER_OFFLINE,
    "TIMEOUT": PRINTER_OFFLINE,
    "AUTH_ERROR": OTHER_ERROR,
    "NOT_FOUND": FILE_ERROR,
    "CONFLICT": PRINTER_BUSY,
    "UNSUPPORTED_FILE_TYPE": FILE_ERROR,
    "SERVER_ERROR": OTHER_ERROR,
    "FILE_NOT_FOUND": FILE_ERROR,
    "FILE_TOO_LARGE": FILE_ERROR,
    "INVALID_FILE_TYPE": FILE_ERROR,
    "PRINTER_NOT_READY": PRINTER_OFFLINE,
    "PRINTER_BUSY": PRINTER_BUSY,
    "VALIDATION_ERROR": OTHER_ERROR,
}


def exit_code_for(error_code: str) -> int:
    """Map an error code string to a CLI exit code."""
    return ERROR_CODE_MAP.get(error_code, OTHER_ERROR)
