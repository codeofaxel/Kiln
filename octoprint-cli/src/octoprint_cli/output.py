"""Output formatting for octoprint-cli.

Provides both JSON (machine-parseable) and human-readable (Rich) output
for all CLI responses.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from io import StringIO
from typing import Any

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def format_time(seconds: int | float | None) -> str:
    """Convert seconds to a human-readable 'Xh Ym Zs' string."""
    if seconds is None or seconds < 0:
        return "N/A"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def format_bytes(size_bytes: int | float | None) -> str:
    """Convert a byte count to a human-readable string (e.g. '1.2 MB')."""
    if size_bytes is None or size_bytes < 0:
        return "N/A"
    if size_bytes == 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    exponent = min(int(math.log(size_bytes, 1024)), len(units) - 1)
    value = size_bytes / (1024**exponent)
    if exponent == 0:
        return f"{int(value)} B"
    return f"{value:.1f} {units[exponent]}"


def format_temp(
    actual: float | None,
    target: float | None,
) -> str:
    """Format a temperature reading like '214.8\u00b0C / 220.0\u00b0C'."""
    actual_str = f"{actual:.1f}\u00b0C" if actual is not None else "N/A"
    target_str = f"{target:.1f}\u00b0C" if target is not None else "N/A"
    return f"{actual_str} / {target_str}"


def progress_bar(completion: float | None, width: int = 20) -> str:
    """Return an ASCII progress bar like '[########............] 42.3%'.

    *completion* is expected as a percentage (0-100).
    """
    if completion is None:
        completion = 0.0
    completion = max(0.0, min(100.0, completion))
    filled = int(round(width * completion / 100))
    empty = width - filled
    bar = "\u2588" * filled + "\u2591" * empty
    return f"[{bar}] {completion:.1f}%"


# ---------------------------------------------------------------------------
# Internal Rich rendering helpers
# ---------------------------------------------------------------------------


def _render_to_string(renderable: Any) -> str:
    """Render a Rich object to a plain string (with ANSI codes)."""
    if not RICH_AVAILABLE:
        return str(renderable)
    buf = StringIO()
    console = Console(file=buf, force_terminal=True, width=100)
    console.print(renderable)
    return buf.getvalue().rstrip("\n")


# ---------------------------------------------------------------------------
# format_response
# ---------------------------------------------------------------------------


def format_response(
    status: str,
    data: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    json_mode: bool = False,
) -> str:
    """Build a generic API response envelope.

    Parameters
    ----------
    status:
        ``"success"`` or ``"error"``.
    data:
        Arbitrary payload dict (used when *status* is ``"success"``).
    error:
        Error detail dict with keys ``code`` and ``message``.
    json_mode:
        When *True* return a JSON string; otherwise a Rich-formatted string.
    """
    if json_mode:
        envelope: dict[str, Any] = {
            "status": status,
            "data": data,
            "error": error,
        }
        return json.dumps(envelope, indent=2, sort_keys=False)

    # --- human-readable --------------------------------------------------
    if status == "error" and error:
        code = error.get("code", "UNKNOWN")
        message = error.get("message", "An unknown error occurred.")
        if RICH_AVAILABLE:
            text = Text()
            text.append("Error", style="bold red")
            text.append(f" [{code}]: ", style="red")
            text.append(message)
            panel = Panel(text, title="Error", border_style="red")
            return _render_to_string(panel)
        return f"Error [{code}]: {message}"

    if data:
        if RICH_AVAILABLE:
            lines = []
            for key, value in data.items():
                lines.append(f"[bold]{key}:[/bold] {value}")
            content = "\n".join(lines)
            panel = Panel(content, title="Response", border_style="green")
            return _render_to_string(panel)
        lines = [f"{key}: {value}" for key, value in data.items()]
        return "\n".join(lines)

    return f"Status: {status}"


# ---------------------------------------------------------------------------
# format_printer_status
# ---------------------------------------------------------------------------


def _extract_temp(
    temp_data: dict[str, Any] | None,
    key: str,
) -> tuple[float | None, float | None]:
    """Return (actual, target) from OctoPrint temperature payload."""
    if not temp_data or key not in temp_data:
        return None, None
    entry = temp_data[key]
    return entry.get("actual"), entry.get("target")


def format_printer_status(
    printer_data: dict[str, Any] | None,
    job_data: dict[str, Any] | None,
    json_mode: bool = False,
) -> str:
    """Format combined printer + job status.

    Parameters
    ----------
    printer_data:
        Response from ``/api/printer`` (may contain *state*, *temperature*).
    job_data:
        Response from ``/api/job`` (may contain *job*, *progress*).
    json_mode:
        Return JSON when *True*.
    """
    printer_data = printer_data or {}
    job_data = job_data or {}

    # -- extract fields ---------------------------------------------------
    state_data = printer_data.get("state", {})
    if isinstance(state_data, dict):
        state_text = state_data.get("text", "Unknown")
    else:
        state_text = str(state_data)

    temp_data = printer_data.get("temperature", {})
    tool0_actual, tool0_target = _extract_temp(temp_data, "tool0")
    bed_actual, bed_target = _extract_temp(temp_data, "bed")

    progress_info = job_data.get("progress", {}) or {}
    completion = progress_info.get("completion")
    print_time_left = progress_info.get("printTimeLeft")

    job_info = job_data.get("job", {}) or {}
    file_info = job_info.get("file", {}) or {}
    file_name = file_info.get("name")

    if json_mode:
        result: dict[str, Any] = {
            "state": state_text,
            "temperature": {
                "tool0": {"actual": tool0_actual, "target": tool0_target},
                "bed": {"actual": bed_actual, "target": bed_target},
            },
            "job": {
                "file": file_name,
                "completion": completion,
                "print_time_left": print_time_left,
            },
        }
        return json.dumps(
            {"status": "success", "data": result},
            indent=2,
            sort_keys=False,
        )

    # -- human-readable ---------------------------------------------------
    if RICH_AVAILABLE:
        # Printer info table
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Key", style="bold cyan", no_wrap=True)
        table.add_column("Value")

        table.add_row("State", state_text)
        table.add_row("Hotend (tool0)", format_temp(tool0_actual, tool0_target))
        table.add_row("Bed", format_temp(bed_actual, bed_target))

        if file_name:
            table.add_row("File", file_name)
        if completion is not None:
            table.add_row("Progress", progress_bar(completion))
        if print_time_left is not None:
            table.add_row("Time remaining", format_time(print_time_left))

        panel = Panel(table, title="Printer Status", border_style="blue")
        return _render_to_string(panel)

    # Plain-text fallback
    lines = [
        f"State: {state_text}",
        f"Hotend (tool0): {format_temp(tool0_actual, tool0_target)}",
        f"Bed: {format_temp(bed_actual, bed_target)}",
    ]
    if file_name:
        lines.append(f"File: {file_name}")
    if completion is not None:
        lines.append(f"Progress: {progress_bar(completion)}")
    if print_time_left is not None:
        lines.append(f"Time remaining: {format_time(print_time_left)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# format_file_list
# ---------------------------------------------------------------------------


def _flatten_files(
    entries: list[dict[str, Any]],
    prefix: str = "",
) -> list[dict[str, Any]]:
    """Recursively flatten OctoPrint's nested file/folder structure."""
    flat: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("type") == "folder":
            children = entry.get("children", [])
            folder_name = entry.get("name", "")
            path = f"{prefix}{folder_name}/" if prefix else f"{folder_name}/"
            flat.extend(_flatten_files(children, prefix=path))
        else:
            item = dict(entry)
            if prefix:
                item["display_name"] = prefix + item.get("name", "")
            else:
                item["display_name"] = item.get("name", "")
            flat.append(item)
    return flat


def format_file_list(
    files_data: dict[str, Any] | None,
    json_mode: bool = False,
) -> str:
    """Format an OctoPrint file listing.

    Parameters
    ----------
    files_data:
        The response from ``/api/files`` (dict with key *files*).
    json_mode:
        Return JSON when *True*.
    """
    files_data = files_data or {}
    raw_files = files_data.get("files", [])
    flat = _flatten_files(raw_files)

    if json_mode:
        items = []
        for f in flat:
            items.append(
                {
                    "name": f.get("display_name", f.get("name")),
                    "size": f.get("size"),
                    "date": f.get("date"),
                    "type": f.get("type", "file"),
                }
            )
        return json.dumps(
            {"status": "success", "data": {"files": items}},
            indent=2,
            sort_keys=False,
        )

    # -- human-readable ---------------------------------------------------
    if not flat:
        msg = "No files found."
        if RICH_AVAILABLE:
            return _render_to_string(Panel(msg, title="Files", border_style="yellow"))
        return msg

    if RICH_AVAILABLE:
        table = Table(title="Files", border_style="blue")
        table.add_column("Name", style="bold")
        table.add_column("Size", justify="right")
        table.add_column("Date")
        table.add_column("Type")

        for f in flat:
            name = f.get("display_name", f.get("name", ""))
            size = format_bytes(f.get("size"))
            raw_date = f.get("date")
            if raw_date is not None:
                try:
                    date_str = datetime.fromtimestamp(raw_date).strftime("%Y-%m-%d %H:%M")
                except (OSError, ValueError, TypeError):
                    date_str = str(raw_date)
            else:
                date_str = "N/A"
            file_type = f.get("type", "file")
            table.add_row(name, size, date_str, file_type)

        return _render_to_string(table)

    # Plain-text fallback
    lines = [f"{'Name':<40} {'Size':>10} {'Date':<18} {'Type'}"]
    lines.append("-" * 78)
    for f in flat:
        name = f.get("display_name", f.get("name", ""))
        size = format_bytes(f.get("size"))
        raw_date = f.get("date")
        if raw_date is not None:
            try:
                date_str = datetime.fromtimestamp(raw_date).strftime("%Y-%m-%d %H:%M")
            except (OSError, ValueError, TypeError):
                date_str = str(raw_date)
        else:
            date_str = "N/A"
        file_type = f.get("type", "file")
        lines.append(f"{name:<40} {size:>10} {date_str:<18} {file_type}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# format_upload_result
# ---------------------------------------------------------------------------


def format_upload_result(
    upload_data: dict[str, Any] | None,
    json_mode: bool = False,
) -> str:
    """Format the result of a file upload.

    Parameters
    ----------
    upload_data:
        OctoPrint upload response payload.
    json_mode:
        Return JSON when *True*.
    """
    upload_data = upload_data or {}

    if json_mode:
        return json.dumps(
            {"status": "success", "data": upload_data},
            indent=2,
            sort_keys=False,
        )

    file_info = upload_data.get("files", {}).get("local", upload_data)
    file_name = file_info.get("name", "unknown")
    done = upload_data.get("done", True)

    if RICH_AVAILABLE:
        text = Text()
        text.append("Upload complete: ", style="bold green")
        text.append(file_name, style="bold")
        if not done:
            text.append(" (processing)", style="yellow")
        panel = Panel(text, title="Upload", border_style="green")
        return _render_to_string(panel)

    status_str = "" if done else " (processing)"
    return f"Upload complete: {file_name}{status_str}"


# ---------------------------------------------------------------------------
# format_job_action
# ---------------------------------------------------------------------------


def format_job_action(
    action: str,
    result_data: dict[str, Any] | None,
    json_mode: bool = False,
) -> str:
    """Format the result of a print/cancel/pause/resume action.

    Parameters
    ----------
    action:
        The action name, e.g. ``"start"``, ``"cancel"``, ``"pause"``,
        ``"resume"``.
    result_data:
        Any additional data returned by OctoPrint (often empty/None on
        success).
    json_mode:
        Return JSON when *True*.
    """
    result_data = result_data or {}

    messages = {
        "start": "Print job started.",
        "cancel": "Print job cancelled.",
        "pause": "Print job paused.",
        "resume": "Print job resumed.",
        "restart": "Print job restarted.",
    }
    message = messages.get(action, f"Action '{action}' completed.")

    if json_mode:
        return json.dumps(
            {
                "status": "success",
                "data": {
                    "action": action,
                    "message": message,
                    **result_data,
                },
            },
            indent=2,
            sort_keys=False,
        )

    # -- human-readable ---------------------------------------------------
    style_map = {
        "start": ("green", "bold green"),
        "cancel": ("red", "bold red"),
        "pause": ("yellow", "bold yellow"),
        "resume": ("green", "bold green"),
        "restart": ("green", "bold green"),
    }
    border, text_style = style_map.get(action, ("blue", "bold blue"))

    if RICH_AVAILABLE:
        text = Text(message, style=text_style)
        panel = Panel(text, title=action.capitalize(), border_style=border)
        return _render_to_string(panel)

    return message
