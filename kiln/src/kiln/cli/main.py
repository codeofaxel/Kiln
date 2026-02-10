"""Kiln CLI — agent-friendly command-line interface for 3D printers.

Provides a unified ``kiln`` command with subcommands for printer discovery,
configuration, control, and monitoring.  Every subcommand supports a
``--json`` flag for machine-parseable output suitable for agent consumption.

The ``kiln serve`` subcommand starts the MCP server (original ``kiln``
behaviour).
"""

from __future__ import annotations

import sys
from typing import Any, Dict, Optional

import click

from kiln.cli.config import (
    list_printers as _list_printers,
    load_printer_config,
    remove_printer,
    save_printer,
    set_active_printer,
    validate_printer_config,
)
from kiln.cli.output import (
    format_action,
    format_discovered,
    format_error,
    format_files,
    format_printers,
    format_response,
    format_status,
)


# ---------------------------------------------------------------------------
# Adapter factory
# ---------------------------------------------------------------------------


def _make_adapter(cfg: Dict[str, Any]):
    """Create a PrinterAdapter from a config dict."""
    from kiln.printers import (
        BambuAdapter,
        MoonrakerAdapter,
        OctoPrintAdapter,
    )

    ptype = cfg.get("type", "octoprint")
    host = cfg.get("host", "")

    if ptype == "octoprint":
        return OctoPrintAdapter(host=host, api_key=cfg.get("api_key", ""))
    elif ptype == "moonraker":
        return MoonrakerAdapter(host=host, api_key=cfg.get("api_key") or None)
    elif ptype == "bambu":
        return BambuAdapter(
            host=host,
            access_code=cfg.get("access_code", ""),
            serial=cfg.get("serial", ""),
        )
    else:
        raise click.ClickException(f"Unknown printer type: {ptype!r}")


def _get_adapter_from_ctx(ctx: click.Context):
    """Resolve printer config and return an adapter instance."""
    printer_name = ctx.obj.get("printer")
    try:
        cfg = load_printer_config(printer_name)
    except ValueError as exc:
        raise click.ClickException(str(exc))

    ok, err = validate_printer_config(cfg)
    if not ok:
        raise click.ClickException(f"Invalid printer config: {err}")

    return _make_adapter(cfg)


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


@click.group()
@click.option(
    "--printer", "-p",
    default=None,
    envvar="KILN_PRINTER",
    help="Printer name to use (overrides active printer).",
)
@click.version_option(package_name="kiln")
@click.pass_context
def cli(ctx: click.Context, printer: Optional[str]) -> None:
    """Kiln — agent-friendly 3D printer control."""
    ctx.ensure_object(dict)
    ctx.obj["printer"] = printer


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--timeout", "-t", default=5.0, help="Scan duration in seconds.")
@click.option(
    "--subnet", "-s", default=None,
    help="Subnet to scan (e.g. '192.168.1'). Auto-detected if omitted.",
)
@click.option(
    "--method", "-m", "methods", multiple=True,
    type=click.Choice(["mdns", "http_probe"]),
    help="Discovery method(s) to use (repeatable). Default: mdns + http_probe.",
)
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def discover(timeout: float, subnet: Optional[str], methods: tuple, json_mode: bool) -> None:
    """Scan the local network for 3D printers.

    Uses mDNS and HTTP probing by default. Results are deduplicated
    by host+port.  Use --method to restrict to a single strategy.
    """
    from kiln.cli.discovery import discover_printers

    method_list = list(methods) if methods else None  # None = use defaults

    try:
        found = discover_printers(
            timeout=timeout,
            subnet=subnet,
            methods=method_list,
        )
    except Exception as exc:
        click.echo(format_error(str(exc), code="DISCOVERY_ERROR", json_mode=json_mode))
        sys.exit(1)

    click.echo(format_discovered([p.to_dict() for p in found], json_mode=json_mode))

    if not json_mode and not found:
        click.echo(
            "\nTip: Bambu printers don't advertise via mDNS. "
            "Use 'kiln auth' with the IP address."
        )


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--name", "-n", required=True, help="Name for this printer (e.g. 'voron').")
@click.option("--host", "-h", required=True, help="Printer URL or IP (e.g. http://octopi.local).")
@click.option(
    "--type", "printer_type",
    required=True,
    type=click.Choice(["octoprint", "moonraker", "bambu"]),
    help="Printer backend type.",
)
@click.option("--api-key", default=None, help="API key (OctoPrint/Moonraker).")
@click.option("--access-code", default=None, help="LAN access code (Bambu).")
@click.option("--serial", default=None, help="Printer serial number (Bambu).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def auth(
    name: str,
    host: str,
    printer_type: str,
    api_key: Optional[str],
    access_code: Optional[str],
    serial: Optional[str],
    json_mode: bool,
) -> None:
    """Save printer credentials to the config file."""
    try:
        path = save_printer(
            name,
            printer_type,
            host,
            api_key=api_key,
            access_code=access_code,
            serial=serial,
        )
        data = {
            "name": name,
            "type": printer_type,
            "host": host,
            "config_path": str(path),
        }
        click.echo(format_response("success", data=data, json_mode=json_mode))
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def status(ctx: click.Context, json_mode: bool) -> None:
    """Get printer state, temperatures, and job progress."""
    try:
        adapter = _get_adapter_from_ctx(ctx)
        state = adapter.get_state()
        job = adapter.get_job()
        click.echo(format_status(state.to_dict(), job.to_dict(), json_mode=json_mode))
    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def files(ctx: click.Context, json_mode: bool) -> None:
    """List G-code files on the printer."""
    try:
        adapter = _get_adapter_from_ctx(ctx)
        file_list = adapter.list_files()
        click.echo(format_files([f.to_dict() for f in file_list], json_mode=json_mode))
    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("file_path", type=click.Path(exists=True))
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def upload(ctx: click.Context, file_path: str, json_mode: bool) -> None:
    """Upload a G-code file to the printer."""
    try:
        adapter = _get_adapter_from_ctx(ctx)
        result = adapter.upload_file(file_path)
        click.echo(format_action("upload", result.to_dict(), json_mode=json_mode))
    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# print
# ---------------------------------------------------------------------------


@cli.command("print")
@click.argument("file", required=False)
@click.option("--status", "show_status", is_flag=True, help="Show print status instead of starting a print.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def print_cmd(ctx: click.Context, file: Optional[str], show_status: bool, json_mode: bool) -> None:
    """Start a print or check print status.

    Pass a file name/path to start printing.  Use --status to check progress.
    """
    try:
        adapter = _get_adapter_from_ctx(ctx)

        if show_status or file is None:
            state = adapter.get_state()
            job = adapter.get_job()
            click.echo(format_status(state.to_dict(), job.to_dict(), json_mode=json_mode))
            return

        result = adapter.start_print(file)
        click.echo(format_action("start", result.to_dict(), json_mode=json_mode))
    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# cancel / pause / resume
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def cancel(ctx: click.Context, json_mode: bool) -> None:
    """Cancel the current print job."""
    try:
        adapter = _get_adapter_from_ctx(ctx)
        result = adapter.cancel_print()
        click.echo(format_action("cancel", result.to_dict(), json_mode=json_mode))
    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@cli.command()
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def pause(ctx: click.Context, json_mode: bool) -> None:
    """Pause the current print job."""
    try:
        adapter = _get_adapter_from_ctx(ctx)
        result = adapter.pause_print()
        click.echo(format_action("pause", result.to_dict(), json_mode=json_mode))
    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@cli.command()
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def resume(ctx: click.Context, json_mode: bool) -> None:
    """Resume a paused print job."""
    try:
        adapter = _get_adapter_from_ctx(ctx)
        result = adapter.resume_print()
        click.echo(format_action("resume", result.to_dict(), json_mode=json_mode))
    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# temp
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--tool", "tool_temp", type=float, default=None, help="Set hotend temperature (°C).")
@click.option("--bed", "bed_temp", type=float, default=None, help="Set bed temperature (°C).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def temp(ctx: click.Context, tool_temp: Optional[float], bed_temp: Optional[float], json_mode: bool) -> None:
    """Get or set printer temperatures.

    With no flags, shows current temperatures.  Pass --tool and/or --bed to
    set target temperatures.
    """
    try:
        adapter = _get_adapter_from_ctx(ctx)

        if tool_temp is None and bed_temp is None:
            state = adapter.get_state()
            data = {
                "tool_actual": state.tool_temp_actual,
                "tool_target": state.tool_temp_target,
                "bed_actual": state.bed_temp_actual,
                "bed_target": state.bed_temp_target,
            }
            click.echo(format_response("success", data=data, json_mode=json_mode))
            return

        results: Dict[str, Any] = {}
        if tool_temp is not None:
            adapter.set_tool_temp(tool_temp)
            results["tool_target"] = tool_temp
        if bed_temp is not None:
            adapter.set_bed_temp(bed_temp)
            results["bed_target"] = bed_temp

        click.echo(format_response("success", data=results, json_mode=json_mode))
    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# gcode
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("commands", nargs=-1, required=True)
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def gcode(ctx: click.Context, commands: tuple, json_mode: bool) -> None:
    """Send raw G-code commands to the printer.

    Commands are validated before sending.  Pass multiple commands as
    separate arguments or as a single newline-separated string.
    """
    from kiln.gcode import validate_gcode

    try:
        adapter = _get_adapter_from_ctx(ctx)

        cmd_list = list(commands)
        validation = validate_gcode(cmd_list)

        if not validation.valid:
            data = {
                "blocked": validation.blocked_commands,
                "errors": validation.errors,
            }
            click.echo(format_error(
                "G-code blocked by safety validator: " + "; ".join(validation.errors),
                code="GCODE_BLOCKED",
                json_mode=json_mode,
            ))
            sys.exit(1)

        adapter.send_gcode(validation.commands)

        data = {
            "commands_sent": validation.commands,
            "count": len(validation.commands),
        }
        if validation.warnings:
            data["warnings"] = validation.warnings

        click.echo(format_response("success", data=data, json_mode=json_mode))
    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# printers / use
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def printers(json_mode: bool) -> None:
    """List configured printers."""
    result = _list_printers()
    click.echo(format_printers(result, json_mode=json_mode))


@cli.command()
@click.argument("name")
def use(name: str) -> None:
    """Set the active printer."""
    try:
        set_active_printer(name)
        click.echo(f"Active printer set to '{name}'.")
    except ValueError as exc:
        raise click.ClickException(str(exc))


@cli.command("remove")
@click.argument("name")
def remove(name: str) -> None:
    """Remove a saved printer from the config."""
    try:
        remove_printer(name)
        click.echo(f"Removed printer '{name}'.")
    except ValueError as exc:
        raise click.ClickException(str(exc))


# ---------------------------------------------------------------------------
# serve (MCP server)
# ---------------------------------------------------------------------------


@cli.command()
def serve() -> None:
    """Start the Kiln MCP server.

    Launches the MCP server with the job scheduler, webhook delivery,
    and persistence subsystems.  Configure your printer via environment
    variables (KILN_PRINTER_HOST, KILN_PRINTER_API_KEY, KILN_PRINTER_TYPE)
    or register printers dynamically via the register_printer tool.
    """
    from kiln.server import main as _server_main
    _server_main()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point."""
    cli()


if __name__ == "__main__":
    main()
