"""Output formatting for the Kiln CLI.

Provides both JSON (machine-parseable) and human-readable (Rich) output.
All public functions accept a ``json_mode`` flag:
    - ``True``  → compact JSON string ready for agent consumption
    - ``False`` → Rich-formatted (or plain-text fallback) for humans
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from io import StringIO
from typing import Any, Dict, List, Optional, Union

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def format_time(seconds: Optional[Union[int, float]]) -> str:
    """Convert seconds to ``Xh Ym Zs``."""
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


def format_bytes(size_bytes: Optional[Union[int, float]]) -> str:
    """Convert bytes to ``1.2 MB``."""
    if size_bytes is None or size_bytes < 0:
        return "N/A"
    if size_bytes == 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    exponent = min(int(math.log(size_bytes, 1024)), len(units) - 1)
    value = size_bytes / (1024 ** exponent)
    if exponent == 0:
        return f"{int(value)} B"
    return f"{value:.1f} {units[exponent]}"


def format_temp(
    actual: Optional[float],
    target: Optional[float],
) -> str:
    """Format temperatures like ``214.8°C / 220.0°C``."""
    actual_str = f"{actual:.1f}\u00b0C" if actual is not None else "N/A"
    target_str = f"{target:.1f}\u00b0C" if target is not None else "off"
    return f"{actual_str} \u2192 {target_str}"


def progress_bar(completion: Optional[float], width: int = 20) -> str:
    """ASCII progress bar: ``[████████░░░░] 42.3%``."""
    if completion is None:
        completion = 0.0
    completion = max(0.0, min(100.0, completion))
    filled = int(round(width * completion / 100))
    empty = width - filled
    bar = "\u2588" * filled + "\u2591" * empty
    return f"[{bar}] {completion:.1f}%"


def _render(renderable: Any) -> str:
    """Render a Rich object to string, or fall back to ``str()``."""
    if not RICH_AVAILABLE:
        return str(renderable)
    buf = StringIO()
    console = Console(file=buf, force_terminal=True, width=100)
    console.print(renderable)
    return buf.getvalue().rstrip("\n")


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def format_response(
    status: str,
    data: Optional[Dict[str, Any]] = None,
    error: Optional[Dict[str, Any]] = None,
    *,
    json_mode: bool = False,
) -> str:
    """Build a standard ``{status, data, error}`` response."""
    if json_mode:
        envelope: Dict[str, Any] = {"status": status}
        if data is not None:
            envelope["data"] = data
        if error is not None:
            envelope["error"] = error
        return json.dumps(envelope, indent=2, sort_keys=False)

    if status == "error" and error:
        code = error.get("code", "UNKNOWN")
        msg = error.get("message", "An unknown error occurred.")
        if RICH_AVAILABLE:
            t = Text()
            t.append("Error", style="bold red")
            t.append(f" [{code}]: ", style="red")
            t.append(msg)
            return _render(Panel(t, title="Error", border_style="red"))
        return f"Error [{code}]: {msg}"

    if data:
        if RICH_AVAILABLE:
            lines = [f"[bold]{k}:[/bold] {v}" for k, v in data.items()]
            return _render(Panel("\n".join(lines), border_style="green"))
        return "\n".join(f"{k}: {v}" for k, v in data.items())

    return f"Status: {status}"


def format_error(
    message: str,
    code: str = "ERROR",
    *,
    json_mode: bool = False,
) -> str:
    """Shortcut for a standard error response."""
    return format_response(
        "error",
        error={"code": code, "message": message},
        json_mode=json_mode,
    )


# ---------------------------------------------------------------------------
# Printer status
# ---------------------------------------------------------------------------


def format_status(
    state: Dict[str, Any],
    job: Dict[str, Any],
    *,
    json_mode: bool = False,
) -> str:
    """Format printer state + job progress.

    Expects dicts from ``PrinterState.to_dict()`` and ``JobProgress.to_dict()``.
    """
    if json_mode:
        return json.dumps(
            {"status": "success", "data": {"printer": state, "job": job}},
            indent=2,
            sort_keys=False,
        )

    state_text = state.get("state", "unknown")
    connected = state.get("connected", False)
    tool_actual = state.get("tool_temp_actual")
    tool_target = state.get("tool_temp_target")
    bed_actual = state.get("bed_temp_actual")
    bed_target = state.get("bed_temp_target")

    file_name = job.get("file_name")
    completion = job.get("completion")
    time_left = job.get("print_time_left_seconds")

    if RICH_AVAILABLE:
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Key", style="bold cyan", no_wrap=True)
        table.add_column("Value")

        color = {"idle": "green", "printing": "yellow", "paused": "yellow",
                 "error": "red", "offline": "red"}.get(state_text, "white")
        table.add_row("State", f"[{color}]{state_text}[/{color}]")
        table.add_row("Connected", "yes" if connected else "[red]no[/red]")
        table.add_row("Hotend", format_temp(tool_actual, tool_target))
        table.add_row("Bed", format_temp(bed_actual, bed_target))

        if file_name:
            table.add_row("File", file_name)
        if completion is not None:
            table.add_row("Progress", progress_bar(completion))
        if time_left is not None:
            table.add_row("Time left", format_time(time_left))

        return _render(Panel(table, title="Printer Status", border_style="blue"))

    lines = [
        f"State:     {state_text}",
        f"Connected: {'yes' if connected else 'no'}",
        f"Hotend:    {format_temp(tool_actual, tool_target)}",
        f"Bed:       {format_temp(bed_actual, bed_target)}",
    ]
    if file_name:
        lines.append(f"File:      {file_name}")
    if completion is not None:
        lines.append(f"Progress:  {progress_bar(completion)}")
    if time_left is not None:
        lines.append(f"Time left: {format_time(time_left)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# File listing
# ---------------------------------------------------------------------------


def format_files(
    files: List[Dict[str, Any]],
    *,
    json_mode: bool = False,
) -> str:
    """Format a list of printer files.

    Expects dicts from ``PrinterFile.to_dict()``.
    """
    if json_mode:
        return json.dumps(
            {"status": "success", "data": {"files": files, "count": len(files)}},
            indent=2,
            sort_keys=False,
        )

    if not files:
        msg = "No files on printer."
        if RICH_AVAILABLE:
            return _render(Panel(msg, title="Files", border_style="yellow"))
        return msg

    if RICH_AVAILABLE:
        table = Table(title="Files", border_style="blue")
        table.add_column("Name", style="bold")
        table.add_column("Size", justify="right")
        table.add_column("Date")

        for f in files:
            name = f.get("name", "")
            size = format_bytes(f.get("size_bytes"))
            raw_date = f.get("date")
            if raw_date is not None:
                try:
                    date_str = datetime.fromtimestamp(raw_date).strftime("%Y-%m-%d %H:%M")
                except (OSError, ValueError, TypeError):
                    date_str = str(raw_date)
            else:
                date_str = ""
            table.add_row(name, size, date_str)
        return _render(table)

    lines = [f"{'Name':<40} {'Size':>10} {'Date'}"]
    lines.append("-" * 65)
    for f in files:
        name = f.get("name", "")
        size = format_bytes(f.get("size_bytes"))
        raw_date = f.get("date")
        if raw_date is not None:
            try:
                date_str = datetime.fromtimestamp(raw_date).strftime("%Y-%m-%d %H:%M")
            except (OSError, ValueError, TypeError):
                date_str = str(raw_date)
        else:
            date_str = ""
        lines.append(f"{name:<40} {size:>10} {date_str}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Action results (print, cancel, pause, resume, upload)
# ---------------------------------------------------------------------------


def format_action(
    action: str,
    result: Dict[str, Any],
    *,
    json_mode: bool = False,
) -> str:
    """Format the result of a print-control or upload action.

    *result* should come from ``PrintResult.to_dict()`` or
    ``UploadResult.to_dict()``.
    """
    if json_mode:
        return json.dumps(
            {"status": "success", "data": {"action": action, **result}},
            indent=2,
            sort_keys=False,
        )

    message = result.get("message", f"{action.capitalize()} completed.")
    success = result.get("success", True)

    style_map = {
        "start": ("green", "bold green"),
        "cancel": ("red", "bold red"),
        "pause": ("yellow", "bold yellow"),
        "resume": ("green", "bold green"),
        "upload": ("green", "bold green"),
    }
    border, text_style = style_map.get(action, ("blue", "bold blue"))

    if not success:
        border, text_style = "red", "bold red"

    if RICH_AVAILABLE:
        return _render(Panel(Text(message, style=text_style),
                             title=action.capitalize(), border_style=border))
    return message


# ---------------------------------------------------------------------------
# Printer list
# ---------------------------------------------------------------------------


def format_printers(
    printers: List[Dict[str, Any]],
    *,
    json_mode: bool = False,
) -> str:
    """Format the list of configured printers."""
    if json_mode:
        return json.dumps(
            {"status": "success", "data": {"printers": printers, "count": len(printers)}},
            indent=2,
            sort_keys=False,
        )

    if not printers:
        msg = "No printers configured.  Run 'kiln auth' to add one."
        if RICH_AVAILABLE:
            return _render(Panel(msg, border_style="yellow"))
        return msg

    if RICH_AVAILABLE:
        table = Table(title="Configured Printers", border_style="blue")
        table.add_column("Name", style="bold")
        table.add_column("Type")
        table.add_column("Host")
        table.add_column("Active")
        for p in printers:
            active = "\u2713" if p.get("active") else ""
            table.add_row(p["name"], p.get("type", ""), p.get("host", ""), active)
        return _render(table)

    lines = [f"{'Name':<20} {'Type':<12} {'Host':<35} {'Active'}"]
    lines.append("-" * 75)
    for p in printers:
        active = "*" if p.get("active") else ""
        lines.append(f"{p['name']:<20} {p.get('type', ''):<12} {p.get('host', ''):<35} {active}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Discovery results
# ---------------------------------------------------------------------------


def format_history(
    jobs: List[Dict[str, Any]],
    *,
    json_mode: bool = False,
) -> str:
    """Format job history records.

    Expects dicts from ``KilnDB.list_jobs()`` or ``PrintQueue.list_jobs()``.
    """
    if json_mode:
        return json.dumps(
            {"status": "success", "data": {"jobs": jobs, "count": len(jobs)}},
            indent=2,
            sort_keys=False,
        )

    if not jobs:
        msg = "No print history found."
        if RICH_AVAILABLE:
            return _render(Panel(msg, title="History", border_style="yellow"))
        return msg

    if RICH_AVAILABLE:
        table = Table(title="Print History", border_style="blue")
        table.add_column("File", style="bold")
        table.add_column("Status")
        table.add_column("Printer")
        table.add_column("Duration", justify="right")
        table.add_column("Date")

        for j in jobs:
            status = j.get("status", "unknown")
            color = {"completed": "green", "failed": "red", "cancelled": "yellow"}.get(
                status, "white"
            )

            # Duration from timestamps
            started = j.get("started_at")
            completed = j.get("completed_at")
            if started and completed:
                duration = format_time(completed - started)
            else:
                duration = "N/A"

            # Date
            submitted = j.get("submitted_at")
            if submitted is not None:
                try:
                    date_str = datetime.fromtimestamp(submitted).strftime("%Y-%m-%d %H:%M")
                except (OSError, ValueError, TypeError):
                    date_str = ""
            else:
                date_str = ""

            table.add_row(
                j.get("file_name", ""),
                f"[{color}]{status}[/{color}]",
                j.get("printer_name", "") or "",
                duration,
                date_str,
            )
        return _render(table)

    # Plain-text fallback
    lines = [f"{'File':<30} {'Status':<12} {'Printer':<15} {'Duration':>10} {'Date'}"]
    lines.append("-" * 80)
    for j in jobs:
        started = j.get("started_at")
        completed = j.get("completed_at")
        duration = format_time(completed - started) if started and completed else "N/A"

        submitted = j.get("submitted_at")
        if submitted is not None:
            try:
                date_str = datetime.fromtimestamp(submitted).strftime("%Y-%m-%d %H:%M")
            except (OSError, ValueError, TypeError):
                date_str = ""
        else:
            date_str = ""

        lines.append(
            f"{j.get('file_name', ''):<30} {j.get('status', ''):<12} "
            f"{(j.get('printer_name') or ''):<15} {duration:>10} {date_str}"
        )
    return "\n".join(lines)


def format_discovered(
    printers: List[Dict[str, Any]],
    *,
    json_mode: bool = False,
) -> str:
    """Format discovered printers.

    Handles both the core ``kiln.discovery.DiscoveredPrinter`` dict layout
    (keys: host, port, printer_type, name, version, api_available,
    discovery_method) and the legacy CLI-only layout (name, type, host).
    """
    if json_mode:
        return json.dumps(
            {"status": "success", "data": {"printers": printers, "count": len(printers)}},
            indent=2,
            sort_keys=False,
        )

    if not printers:
        msg = "No printers found on the network."
        if RICH_AVAILABLE:
            return _render(Panel(msg, border_style="yellow"))
        return msg

    def _type(p: Dict[str, Any]) -> str:
        return p.get("printer_type") or p.get("type") or ""

    def _host_display(p: Dict[str, Any]) -> str:
        host = p.get("host", "")
        port = p.get("port")
        if port and port not in (80, 443):
            return f"{host}:{port}"
        return host

    def _api_badge(p: Dict[str, Any]) -> str:
        avail = p.get("api_available")
        if avail is None:
            return ""
        return "yes" if avail else "no"

    if RICH_AVAILABLE:
        table = Table(title="Discovered Printers", border_style="green")
        table.add_column("Name", style="bold")
        table.add_column("Type")
        table.add_column("Host")
        table.add_column("Version")
        table.add_column("API", justify="center")
        table.add_column("Method")

        for p in printers:
            api_text = _api_badge(p)
            if RICH_AVAILABLE and api_text == "yes":
                api_text = "[green]yes[/green]"
            elif RICH_AVAILABLE and api_text == "no":
                api_text = "[red]no[/red]"

            table.add_row(
                p.get("name", ""),
                _type(p),
                _host_display(p),
                p.get("version", ""),
                api_text,
                p.get("discovery_method", ""),
            )
        return _render(table)

    # Plain-text fallback
    header = (
        f"{'Name':<25} {'Type':<12} {'Host':<25} {'Version':<12} "
        f"{'API':<5} {'Method'}"
    )
    lines = [header, "-" * len(header)]
    for p in printers:
        lines.append(
            f"{p.get('name', ''):<25} {_type(p):<12} "
            f"{_host_display(p):<25} {p.get('version', ''):<12} "
            f"{_api_badge(p):<5} {p.get('discovery_method', '')}"
        )
    return "\n".join(lines)
