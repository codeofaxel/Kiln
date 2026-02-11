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


# ---------------------------------------------------------------------------
# Fulfillment order formatting
# ---------------------------------------------------------------------------


def format_quote(
    quote: Dict[str, Any],
    *,
    json_mode: bool = False,
) -> str:
    """Format a fulfillment quote.

    Expects dict from ``Quote.to_dict()``.
    """
    if json_mode:
        return json.dumps(
            {"status": "success", "data": {"quote": quote}},
            indent=2,
            sort_keys=False,
        )

    provider = quote.get("provider", "")
    material = quote.get("material", "")
    qty = quote.get("quantity", 1)
    total = quote.get("total_price", 0)
    currency = quote.get("currency", "USD")
    lead = quote.get("lead_time_days")

    if RICH_AVAILABLE:
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Key", style="bold cyan", no_wrap=True)
        table.add_column("Value")

        table.add_row("Provider", provider)
        table.add_row("Material", material)
        table.add_row("Quantity", str(qty))
        table.add_row("Subtotal", f"{currency} {total:.2f}")

        fee_info = quote.get("kiln_fee")
        if fee_info:
            fee_amt = fee_info.get("fee_amount", 0)
            if fee_info.get("waived"):
                table.add_row("Kiln fee", f"[dim]{currency} 0.00 (waived — {fee_info.get('waiver_reason', 'free tier')})[/dim]")
            else:
                table.add_row("Kiln fee", f"{currency} {fee_amt:.2f}")
            total_with_fee = quote.get("total_with_fee", total)
            table.add_row("Total", f"[bold green]{currency} {total_with_fee:.2f}[/bold green]")
        else:
            table.add_row("Total", f"[bold green]{currency} {total:.2f}[/bold green]")

        if lead:
            table.add_row("Lead time", f"{lead} days")

        shipping = quote.get("shipping_options", [])
        if shipping:
            ship_lines = []
            for s in shipping:
                days = f" ({s.get('estimated_days')}d)" if s.get("estimated_days") else ""
                ship_lines.append(f"  {s.get('name', '')}: {currency} {s.get('price', 0):.2f}{days}")
            table.add_row("Shipping", "\n".join(ship_lines))

        return _render(Panel(table, title=f"Quote {quote.get('quote_id', '')}", border_style="green"))

    lines = [
        f"Quote:    {quote.get('quote_id', '')}",
        f"Provider: {provider}",
        f"Material: {material}",
        f"Quantity: {qty}",
        f"Subtotal: {currency} {total:.2f}",
    ]
    fee_info = quote.get("kiln_fee")
    if fee_info:
        fee_amt = fee_info.get("fee_amount", 0)
        if fee_info.get("waived"):
            lines.append(f"Kiln fee: {currency} 0.00 (waived)")
        else:
            lines.append(f"Kiln fee: {currency} {fee_amt:.2f}")
        lines.append(f"Total:    {currency} {quote.get('total_with_fee', total):.2f}")
    else:
        lines.append(f"Total:    {currency} {total:.2f}")
    if lead:
        lines.append(f"Lead:     {lead} days")
    return "\n".join(lines)


def format_order(
    order: Dict[str, Any],
    *,
    json_mode: bool = False,
) -> str:
    """Format an order result.

    Expects dict from ``OrderResult.to_dict()``.
    """
    if json_mode:
        return json.dumps(
            {"status": "success", "data": {"order": order}},
            indent=2,
            sort_keys=False,
        )

    status = order.get("status", "unknown")
    color = {
        "submitted": "yellow", "processing": "yellow", "printing": "blue",
        "shipping": "cyan", "delivered": "green", "cancelled": "red", "failed": "red",
    }.get(status, "white")

    if RICH_AVAILABLE:
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Key", style="bold cyan", no_wrap=True)
        table.add_column("Value")

        table.add_row("Order", order.get("order_id", ""))
        table.add_row("Status", f"[{color}]{status}[/{color}]")
        table.add_row("Provider", order.get("provider", ""))

        if order.get("total_price") is not None:
            cur = order.get("currency", "USD")
            table.add_row("Subtotal", f"{cur} {order['total_price']:.2f}")
            fee_info = order.get("kiln_fee")
            if fee_info:
                fee_amt = fee_info.get("fee_amount", 0)
                if fee_info.get("waived"):
                    table.add_row("Kiln fee", f"[dim]{cur} 0.00 (waived)[/dim]")
                else:
                    table.add_row("Kiln fee", f"{cur} {fee_amt:.2f}")
                table.add_row("Total", f"[bold]{cur} {order.get('total_with_fee', order['total_price']):.2f}[/bold]")
            else:
                table.add_row("Total", f"[bold]{cur} {order['total_price']:.2f}[/bold]")
        if order.get("tracking_url"):
            table.add_row("Tracking", order["tracking_url"])
        if order.get("estimated_delivery"):
            table.add_row("Delivery", order["estimated_delivery"])

        return _render(Panel(table, title="Order", border_style="blue"))

    cur = order.get("currency", "USD")
    lines = [
        f"Order:    {order.get('order_id', '')}",
        f"Status:   {status}",
        f"Provider: {order.get('provider', '')}",
    ]
    if order.get("total_price") is not None:
        lines.append(f"Subtotal: {cur} {order['total_price']:.2f}")
        fee_info = order.get("kiln_fee")
        if fee_info:
            fee_amt = fee_info.get("fee_amount", 0)
            if fee_info.get("waived"):
                lines.append(f"Kiln fee: {cur} 0.00 (waived)")
            else:
                lines.append(f"Kiln fee: {cur} {fee_amt:.2f}")
            lines.append(f"Total:    {cur} {order.get('total_with_fee', order['total_price']):.2f}")
        else:
            lines.append(f"Total:    {cur} {order['total_price']:.2f}")
    if order.get("tracking_url"):
        lines.append(f"Tracking: {order['tracking_url']}")
    return "\n".join(lines)


def format_materials(
    materials: List[Dict[str, Any]],
    *,
    json_mode: bool = False,
) -> str:
    """Format a list of fulfillment materials.

    Expects dicts from ``Material.to_dict()``.
    """
    if json_mode:
        return json.dumps(
            {"status": "success", "data": {"materials": materials, "count": len(materials)}},
            indent=2,
            sort_keys=False,
        )

    if not materials:
        msg = "No materials available."
        if RICH_AVAILABLE:
            return _render(Panel(msg, border_style="yellow"))
        return msg

    if RICH_AVAILABLE:
        table = Table(title="Available Materials", border_style="blue")
        table.add_column("ID", style="bold")
        table.add_column("Name")
        table.add_column("Technology")
        table.add_column("Color")
        table.add_column("Price/cm\u00b3", justify="right")

        for m in materials:
            price = m.get("price_per_cm3")
            price_str = f"{m.get('currency', 'USD')} {price:.3f}" if price else ""
            table.add_row(
                m.get("id", ""),
                m.get("name", ""),
                m.get("technology", ""),
                m.get("color", ""),
                price_str,
            )
        return _render(table)

    lines = [f"{'ID':<20} {'Name':<25} {'Tech':<8} {'Color':<10} {'Price'}"]
    lines.append("-" * 75)
    for m in materials:
        price = m.get("price_per_cm3")
        price_str = f"{m.get('currency', 'USD')} {price:.3f}" if price else ""
        lines.append(
            f"{m.get('id', ''):<20} {m.get('name', ''):<25} "
            f"{m.get('technology', ''):<8} {m.get('color', ''):<10} {price_str}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Billing formatters
# ---------------------------------------------------------------------------


def format_billing_status(
    data: Dict[str, Any],
    *,
    json_mode: bool = False,
) -> str:
    """Format billing status (payment method, spend, limits).

    Expects the dict from ``PaymentManager.get_billing_status()``.
    """
    if json_mode:
        return json.dumps(
            {"status": "success", "data": data},
            indent=2,
            sort_keys=False,
        )

    revenue = data.get("month_revenue", {})
    policy = data.get("fee_policy", {})
    limits = data.get("spend_limits", {})
    methods = data.get("payment_methods", [])
    default = data.get("default_payment_method")

    if RICH_AVAILABLE:
        parts: list = []

        # Payment method
        if default:
            parts.append(
                f"[bold]Payment method:[/bold] {default.get('label', default.get('rail', 'unknown'))}"
            )
        elif methods:
            parts.append(f"[bold]Payment methods:[/bold] {len(methods)} linked")
        else:
            parts.append("[yellow]No payment method linked.[/yellow] Run [bold]kiln billing setup[/bold] to add one.")

        # Monthly spend
        total = revenue.get("total_fees", 0.0)
        jobs = revenue.get("job_count", 0)
        waived = revenue.get("waived_count", 0)
        cap = limits.get("monthly_cap_usd", 2000.0)
        parts.append(
            f"[bold]Monthly spend:[/bold] ${total:.2f} / ${cap:.2f} cap  "
            f"({jobs} orders, {waived} waived)"
        )

        # Fee policy
        parts.append(
            f"[bold]Fee:[/bold] {policy.get('network_fee_percent', 5)}% "
            f"(min ${policy.get('min_fee_usd', 0.25):.2f}, "
            f"max ${policy.get('max_fee_usd', 50):.2f})"
        )

        free_left = max(0, policy.get("free_tier_jobs", 5) - data.get("network_jobs_this_month", 0))
        parts.append(f"[bold]Free tier:[/bold] {free_left} free orders remaining this month")

        # Available rails
        rails = data.get("available_rails", [])
        if rails:
            parts.append(f"[bold]Available rails:[/bold] {', '.join(rails)}")

        content = "\n".join(parts)
        return _render(Panel(content, title="Billing Status", border_style="blue"))

    # Plain text fallback
    lines = ["Billing Status", "=" * 40]
    if default:
        lines.append(f"Payment method: {default.get('label', default.get('rail', 'unknown'))}")
    else:
        lines.append("No payment method linked. Run 'kiln billing setup' to add one.")
    total = revenue.get("total_fees", 0.0)
    cap = limits.get("monthly_cap_usd", 2000.0)
    lines.append(f"Monthly spend: ${total:.2f} / ${cap:.2f}")
    lines.append(f"Fee: {policy.get('network_fee_percent', 5)}%")
    return "\n".join(lines)


def format_billing_history(
    charges: List[Dict[str, Any]],
    *,
    json_mode: bool = False,
) -> str:
    """Format billing charge history.

    Expects a list of charge dicts from ``BillingLedger.list_charges()``.
    """
    if json_mode:
        return json.dumps(
            {"status": "success", "data": {"charges": charges, "count": len(charges)}},
            indent=2,
            sort_keys=False,
        )

    if not charges:
        msg = "No billing charges found."
        if RICH_AVAILABLE:
            return _render(Panel(msg, border_style="yellow"))
        return msg

    if RICH_AVAILABLE:
        table = Table(title="Billing History", border_style="blue")
        table.add_column("Date", style="dim")
        table.add_column("Job ID")
        table.add_column("Order Cost", justify="right")
        table.add_column("Fee", justify="right")
        table.add_column("Waived")
        table.add_column("Payment", style="dim")
        table.add_column("Status")

        for c in charges:
            ts = c.get("created_at", 0)
            date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""
            waived_str = "Yes" if c.get("waived") else ""
            status = c.get("payment_status", "")
            status_style = "green" if status == "completed" else "yellow" if status == "pending" else "red"
            table.add_row(
                date_str,
                str(c.get("job_id", ""))[:16],
                f"${c.get('job_cost', 0):.2f}",
                f"${c.get('fee_amount', 0):.2f}",
                waived_str,
                str(c.get("payment_rail", ""))[:10],
                f"[{status_style}]{status}[/{status_style}]",
            )
        return _render(table)

    # Plain text
    lines = [f"{'Date':<17} {'Job ID':<16} {'Cost':>8} {'Fee':>8} {'Status':<10}"]
    lines.append("-" * 65)
    for c in charges:
        ts = c.get("created_at", 0)
        date_str = datetime.fromtimestamp(ts).strftime("%m/%d %H:%M") if ts else ""
        lines.append(
            f"{date_str:<17} {str(c.get('job_id', ''))[:16]:<16} "
            f"${c.get('job_cost', 0):>7.2f} ${c.get('fee_amount', 0):>7.2f} "
            f"{c.get('payment_status', ''):<10}"
        )
    return "\n".join(lines)


def format_billing_setup(
    url: str,
    rail: str,
    *,
    json_mode: bool = False,
) -> str:
    """Format the billing setup URL and instructions.

    Args:
        url: Setup URL from the payment provider.
        rail: Rail name (e.g. ``"stripe"``).
    """
    if json_mode:
        return json.dumps(
            {"status": "success", "data": {"setup_url": url, "rail": rail}},
            indent=2,
            sort_keys=False,
        )

    if RICH_AVAILABLE:
        if rail == "stripe":
            content = (
                f"Open the link below to add a credit card:\n\n"
                f"  [bold blue]{url}[/bold blue]\n\n"
                f"After setup, Kiln will charge the platform fee automatically\n"
                f"on each outsourced manufacturing order."
            )
        else:
            content = (
                f"Setup URL for [bold]{rail}[/bold]:\n\n"
                f"  [bold blue]{url}[/bold blue]"
            )
        return _render(Panel(content, title="Billing Setup", border_style="green"))

    lines = [f"Billing Setup ({rail})", "=" * 40, "", url, ""]
    if rail == "stripe":
        lines.append("Open the link to add a credit card.")
    return "\n".join(lines)
