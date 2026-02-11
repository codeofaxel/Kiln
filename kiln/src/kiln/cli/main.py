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
    format_billing_history,
    format_billing_setup,
    format_billing_status,
    format_discovered,
    format_error,
    format_files,
    format_history,
    format_materials,
    format_order,
    format_printers,
    format_quote,
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
        PrusaConnectAdapter,
    )

    ptype = cfg.get("type", "octoprint")
    host = cfg.get("host", "")

    if ptype == "octoprint":
        return OctoPrintAdapter(host=host, api_key=cfg.get("api_key", ""))
    elif ptype == "moonraker":
        return MoonrakerAdapter(host=host, api_key=cfg.get("api_key") or None)
    elif ptype == "bambu":
        if BambuAdapter is None:
            raise click.ClickException(
                "Bambu support requires paho-mqtt. "
                "Install it with: pip install 'kiln[bambu]'"
            )
        return BambuAdapter(
            host=host,
            access_code=cfg.get("access_code", ""),
            serial=cfg.get("serial", ""),
        )
    elif ptype == "prusaconnect":
        return PrusaConnectAdapter(
            host=host,
            api_key=cfg.get("api_key") or None,
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
# preflight
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--file", "-f", "file_path", default=None, type=click.Path(), help="Local G-code file to validate.")
@click.option("--material", "-m", default=None,
              type=click.Choice(["PLA", "PETG", "ABS", "TPU", "ASA", "Nylon", "PC"]),
              help="Expected material — validates temps match.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def preflight(ctx: click.Context, file_path: Optional[str], material: Optional[str], json_mode: bool) -> None:
    """Run pre-print safety checks.

    Validates printer state, temperatures, and connectivity.
    Optionally validates a local G-code file with --file.
    Use --material to verify temperatures match the filament type.
    """
    from kiln.printers.base import PrinterStatus

    # Material temperature ranges (tool_min, tool_max, bed_min, bed_max)
    _MATERIAL_TEMPS: Dict[str, tuple] = {
        "PLA":   (180, 220, 40, 70),
        "PETG":  (220, 260, 60, 90),
        "ABS":   (230, 270, 90, 115),
        "TPU":   (210, 240, 30, 60),
        "ASA":   (230, 270, 90, 115),
        "Nylon": (240, 280, 60, 80),
        "PC":    (260, 310, 90, 120),
    }

    try:
        adapter = _get_adapter_from_ctx(ctx)
        state = adapter.get_state()

        checks: list = []
        errors: list = []

        # Connected
        checks.append({
            "name": "printer_connected",
            "passed": state.connected,
            "message": "Printer is connected" if state.connected else "Printer is offline",
        })
        if not state.connected:
            errors.append("Printer is not connected / offline")

        # Idle
        is_idle = state.state == PrinterStatus.IDLE
        checks.append({
            "name": "printer_idle",
            "passed": is_idle,
            "message": f"Printer state: {state.state.value}",
        })
        if not is_idle:
            errors.append(f"Printer is not idle (state: {state.state.value})")

        # No error
        no_error = state.state != PrinterStatus.ERROR
        checks.append({
            "name": "no_errors",
            "passed": no_error,
            "message": "No errors" if no_error else "Printer is in error state",
        })
        if not no_error:
            errors.append("Printer is in an error state")

        # Temperature safety
        MAX_TOOL, MAX_BED = 260.0, 110.0
        temp_warnings: list = []
        if state.tool_temp_actual is not None and state.tool_temp_actual > MAX_TOOL:
            temp_warnings.append(f"Tool temp ({state.tool_temp_actual:.1f}C) exceeds {MAX_TOOL:.0f}C")
        if state.bed_temp_actual is not None and state.bed_temp_actual > MAX_BED:
            temp_warnings.append(f"Bed temp ({state.bed_temp_actual:.1f}C) exceeds {MAX_BED:.0f}C")
        temps_safe = len(temp_warnings) == 0
        checks.append({
            "name": "temperatures_safe",
            "passed": temps_safe,
            "message": "Temperatures within limits" if temps_safe else "; ".join(temp_warnings),
        })
        if not temps_safe:
            errors.extend(temp_warnings)

        # Material check (optional)
        if material:
            mat_range = _MATERIAL_TEMPS.get(material)
            if mat_range:
                tool_min, tool_max, bed_min, bed_max = mat_range
                mat_warnings: list = []

                if state.tool_temp_target is not None:
                    if state.tool_temp_target > 0 and not (tool_min <= state.tool_temp_target <= tool_max):
                        mat_warnings.append(
                            f"Tool target ({state.tool_temp_target:.0f}C) outside "
                            f"{material} range ({tool_min}-{tool_max}C)"
                        )

                if state.bed_temp_target is not None:
                    if state.bed_temp_target > 0 and not (bed_min <= state.bed_temp_target <= bed_max):
                        mat_warnings.append(
                            f"Bed target ({state.bed_temp_target:.0f}C) outside "
                            f"{material} range ({bed_min}-{bed_max}C)"
                        )

                mat_ok = len(mat_warnings) == 0
                checks.append({
                    "name": "material_match",
                    "passed": mat_ok,
                    "message": f"{material} temps OK" if mat_ok else "; ".join(mat_warnings),
                })
                if not mat_ok:
                    errors.extend(mat_warnings)

        # File validation (optional)
        if file_path is not None:
            import os
            file_errors: list = []
            if not os.path.isfile(file_path):
                file_errors.append(f"File not found: {file_path}")
            elif not file_path.lower().endswith((".gcode", ".gco", ".g")):
                file_errors.append(f"Unsupported extension: {os.path.splitext(file_path)[1]}")
            file_ok = len(file_errors) == 0
            checks.append({
                "name": "file_valid",
                "passed": file_ok,
                "message": "File OK" if file_ok else "; ".join(file_errors),
            })
            if not file_ok:
                errors.extend(file_errors)

        ready = all(c["passed"] for c in checks)

        if json_mode:
            import json
            click.echo(json.dumps({
                "status": "success",
                "data": {
                    "ready": ready,
                    "checks": checks,
                    "errors": errors,
                },
            }, indent=2))
        else:
            for c in checks:
                symbol = "PASS" if c["passed"] else "FAIL"
                click.echo(f"  [{symbol}] {c['name']}: {c['message']}")
            click.echo()
            if ready:
                click.echo("Ready to print.")
            else:
                click.echo(f"Not ready: {'; '.join(errors)}")

        if not ready:
            sys.exit(1)

    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# print
# ---------------------------------------------------------------------------


@cli.command("print")
@click.argument("files", nargs=-1)
@click.option("--status", "show_status", is_flag=True, help="Show print status instead of starting a print.")
@click.option("--queue", "use_queue", is_flag=True, help="Submit files to the job queue for sequential printing.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def print_cmd(ctx: click.Context, files: tuple, show_status: bool, use_queue: bool, json_mode: bool) -> None:
    """Start a print or check print status.

    Pass a file name/path to start printing.  Use --status to check progress.
    Pass multiple files (or a glob like *.gcode) to batch print.

    If the argument is a local file that exists on disk, it will be
    auto-uploaded to the printer first, then printing starts immediately.
    If it's a file name already on the printer, it starts directly.

    With --queue, multiple files are submitted to the job scheduler and
    printed sequentially as each one finishes.
    """
    import glob as _glob
    import os

    try:
        adapter = _get_adapter_from_ctx(ctx)

        if show_status or not files:
            state = adapter.get_state()
            job = adapter.get_job()
            click.echo(format_status(state.to_dict(), job.to_dict(), json_mode=json_mode))
            return

        # Expand globs in file list
        expanded: list = []
        for f in files:
            if "*" in f or "?" in f:
                matched = sorted(_glob.glob(f))
                expanded.extend(matched)
            else:
                expanded.append(f)

        if not expanded:
            click.echo(format_error("No files matched.", code="NO_FILES", json_mode=json_mode))
            sys.exit(1)

        # Batch mode: queue multiple files
        if len(expanded) > 1 and use_queue:
            from kiln.persistence import get_db
            import json as _json
            import uuid

            db = get_db()
            import time as _time

            queued = []
            for f in expanded:
                file_name = f
                if os.path.isfile(f):
                    if not json_mode:
                        click.echo(f"Uploading {f}...")
                    upload_result = adapter.upload_file(f)
                    if not upload_result.success:
                        click.echo(format_error(
                            f"Failed to upload {f}: {upload_result.message}",
                            code="UPLOAD_FAILED",
                            json_mode=json_mode,
                        ))
                        continue
                    file_name = upload_result.file_name or os.path.basename(f)

                job_id = str(uuid.uuid4())[:8]
                db.save_job({
                    "id": job_id,
                    "file_name": file_name,
                    "printer_name": None,
                    "status": "queued",
                    "priority": 0,
                    "submitted_by": "cli",
                    "submitted_at": _time.time(),
                    "started_at": None,
                    "completed_at": None,
                    "error_message": None,
                })
                queued.append({"job_id": job_id, "file_name": file_name})

            if json_mode:
                click.echo(_json.dumps({
                    "status": "success",
                    "data": {"queued": queued, "count": len(queued)},
                }, indent=2))
            else:
                click.echo(f"Queued {len(queued)} file(s) for sequential printing.")
                for q in queued:
                    click.echo(f"  {q['job_id']}: {q['file_name']}")
            return

        # Single file (or first of batch without --queue)
        if len(expanded) > 1 and not use_queue:
            if not json_mode:
                click.echo(f"Printing {len(expanded)} files sequentially (use --queue for background)...")

        for i, f in enumerate(expanded):
            file_name = f
            if os.path.isfile(f):
                if not json_mode:
                    click.echo(f"Uploading {f}...")
                upload_result = adapter.upload_file(f)
                if not upload_result.success:
                    click.echo(format_error(
                        upload_result.message or "Upload failed",
                        code="UPLOAD_FAILED",
                        json_mode=json_mode,
                    ))
                    sys.exit(1)
                file_name = upload_result.file_name or os.path.basename(f)

            result = adapter.start_print(file_name)
            click.echo(format_action("start", result.to_dict(), json_mode=json_mode))

            # For batch without queue: only start the first file
            if len(expanded) > 1 and i == 0:
                remaining = [os.path.basename(x) for x in expanded[1:]]
                if not json_mode:
                    click.echo(f"\nRemaining files ({len(remaining)}) need --queue to print automatically:")
                    for r in remaining:
                        click.echo(f"  - {r}")
                break

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
            if tool_temp < 0 or tool_temp > 300:
                click.echo(format_error(
                    f"Hotend temperature {tool_temp}°C out of safe range (0-300°C).",
                    json_mode=json_mode,
                ))
                sys.exit(1)
            adapter.set_tool_temp(tool_temp)
            results["tool_target"] = tool_temp
        if bed_temp is not None:
            if bed_temp < 0 or bed_temp > 130:
                click.echo(format_error(
                    f"Bed temperature {bed_temp}°C out of safe range (0-130°C).",
                    json_mode=json_mode,
                ))
                sys.exit(1)
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
# slice
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("input_file", type=click.Path(exists=True))
@click.option("--output-dir", "-o", default=None, help="Output directory (default: /tmp/kiln_sliced).")
@click.option("--output-name", default=None, help="Override output file name.")
@click.option("--profile", "-P", default=None, type=click.Path(), help="Slicer profile file (.ini/.json).")
@click.option("--slicer", default=None, help="Explicit path to slicer binary.")
@click.option("--print-after", is_flag=True, help="Upload and start printing after slicing.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def slice(
    ctx: click.Context,
    input_file: str,
    output_dir: Optional[str],
    output_name: Optional[str],
    profile: Optional[str],
    slicer: Optional[str],
    print_after: bool,
    json_mode: bool,
) -> None:
    """Slice a 3D model (STL/3MF/STEP) to G-code.

    Uses PrusaSlicer or OrcaSlicer CLI.  The slicer binary is auto-detected
    on PATH or can be specified with --slicer.

    With --print-after, the sliced G-code is uploaded and printing starts
    immediately.
    """
    from kiln.slicer import SlicerError, SlicerNotFoundError, find_slicer, slice_file

    try:
        result = slice_file(
            input_file,
            output_dir=output_dir,
            output_name=output_name,
            profile=profile,
            slicer_path=slicer,
        )

        if not print_after:
            if json_mode:
                import json as _json
                click.echo(_json.dumps({"status": "success", "data": result.to_dict()}, indent=2))
            else:
                click.echo(result.message)
                click.echo(f"Output: {result.output_path}")
            return

        # --print-after: upload and start
        adapter = _get_adapter_from_ctx(ctx)
        if not json_mode:
            click.echo(result.message)
            click.echo(f"Uploading {result.output_path}...")

        upload_result = adapter.upload_file(result.output_path)
        if not upload_result.success:
            click.echo(format_error(
                upload_result.message or "Upload failed",
                code="UPLOAD_FAILED",
                json_mode=json_mode,
            ))
            sys.exit(1)

        import os
        file_name = upload_result.file_name or os.path.basename(result.output_path)
        print_result = adapter.start_print(file_name)

        if json_mode:
            import json as _json
            click.echo(_json.dumps({
                "status": "success",
                "data": {
                    "slice": result.to_dict(),
                    "upload": upload_result.to_dict(),
                    "print": print_result.to_dict(),
                },
            }, indent=2))
        else:
            click.echo(format_action("start", print_result.to_dict(), json_mode=False))

    except SlicerNotFoundError as exc:
        click.echo(format_error(str(exc), code="SLICER_NOT_FOUND", json_mode=json_mode))
        sys.exit(1)
    except SlicerError as exc:
        click.echo(format_error(str(exc), code="SLICER_ERROR", json_mode=json_mode))
        sys.exit(1)
    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--output", "-o", default=None, type=click.Path(), help="Save snapshot to file.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON (base64 encoded).")
@click.pass_context
def snapshot(ctx: click.Context, output: Optional[str], json_mode: bool) -> None:
    """Capture a webcam snapshot from the printer.

    Saves the image to a file (--output) or prints base64-encoded data
    in JSON mode.  Supports OctoPrint and Moonraker webcams.
    """
    import base64

    try:
        adapter = _get_adapter_from_ctx(ctx)
        image_data = adapter.get_snapshot()

        if image_data is None:
            click.echo(format_error(
                "Webcam not available or not supported by this printer.",
                code="NO_WEBCAM",
                json_mode=json_mode,
            ))
            sys.exit(1)

        if output:
            import os
            _safe = os.path.realpath(output)
            _home = os.path.expanduser("~")
            import tempfile
            _tmpdir = os.path.realpath(tempfile.gettempdir())
            _allowed_prefixes = (_home, "/tmp", "/private/tmp", _tmpdir)
            if not any(_safe.startswith(p) for p in _allowed_prefixes):
                click.echo(format_error(
                    "Output path must be under home directory or a temp directory.",
                    code="VALIDATION_ERROR",
                    json_mode=json_mode,
                ))
                sys.exit(1)
            os.makedirs(os.path.dirname(_safe) or ".", exist_ok=True)
            with open(_safe, "wb") as f:
                f.write(image_data)
            data = {
                "file": output,
                "size_bytes": len(image_data),
            }
            click.echo(format_response("success", data=data, json_mode=json_mode))
        elif json_mode:
            import json as _json
            click.echo(_json.dumps({
                "status": "success",
                "data": {
                    "image_base64": base64.b64encode(image_data).decode("ascii"),
                    "size_bytes": len(image_data),
                },
            }, indent=2))
        else:
            default_path = "/tmp/kiln_snapshot.jpg"
            with open(default_path, "wb") as f:
                f.write(image_data)
            click.echo(f"Snapshot saved to {default_path} ({len(image_data)} bytes)")

    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# wait
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--interval", "-i", default=5.0, help="Poll interval in seconds (default 5).")
@click.option("--timeout", "-t", "max_timeout", default=0, type=float,
              help="Maximum wait time in seconds (0 = unlimited).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON on completion.")
@click.pass_context
def wait(ctx: click.Context, interval: float, max_timeout: float, json_mode: bool) -> None:
    """Block until the current print finishes.

    Polls printer status at the given interval.  Exits with code 0 on
    successful completion, 1 on failure/cancellation/error.
    """
    import time as _time

    from kiln.printers.base import PrinterStatus

    try:
        adapter = _get_adapter_from_ctx(ctx)
        start = _time.time()

        while True:
            state = adapter.get_state()
            job = adapter.get_job()

            # Terminal states
            if state.state == PrinterStatus.IDLE:
                # If we never saw a print, it's already idle
                data = {
                    "final_state": state.state.value,
                    "file_name": job.file_name,
                    "elapsed_seconds": round(_time.time() - start, 1),
                }
                click.echo(format_response("success", data=data, json_mode=json_mode))
                return

            if state.state in (PrinterStatus.ERROR, PrinterStatus.OFFLINE):
                data = {
                    "final_state": state.state.value,
                    "file_name": job.file_name,
                    "elapsed_seconds": round(_time.time() - start, 1),
                }
                if json_mode:
                    click.echo(format_response("error",
                                               error={"code": "PRINT_FAILED",
                                                      "message": f"Printer entered {state.state.value} state"},
                                               json_mode=True))
                else:
                    click.echo(f"Print ended with state: {state.state.value}")
                sys.exit(1)

            # Still printing/paused — show progress
            if not json_mode and job.completion is not None:
                from kiln.cli.output import progress_bar
                click.echo(f"\r  {progress_bar(job.completion)}  ", nl=False)

            # Timeout check
            if max_timeout > 0 and (_time.time() - start) >= max_timeout:
                click.echo(format_error(
                    f"Timed out after {max_timeout}s",
                    code="TIMEOUT",
                    json_mode=json_mode,
                ))
                sys.exit(1)

            _time.sleep(interval)

    except KeyboardInterrupt:
        if not json_mode:
            click.echo("\nInterrupted.")
        sys.exit(130)
    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--limit", "-n", default=20, help="Number of records (default 20).")
@click.option("--status", "-s", "filter_status", default=None,
              type=click.Choice(["completed", "failed", "cancelled"]),
              help="Filter by job status.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def history(limit: int, filter_status: Optional[str], json_mode: bool) -> None:
    """Show print history from the local database.

    Displays past print jobs with status, duration, and timestamps.
    """
    try:
        from kiln.persistence import get_db

        db = get_db()
        jobs = db.list_jobs(status=filter_status, limit=min(limit, 100))

        click.echo(format_history(jobs, json_mode=json_mode))

    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# order (fulfillment services)
# ---------------------------------------------------------------------------


@cli.group()
def order() -> None:
    """Outsource prints to external manufacturing services.

    Use subcommands to get quotes, place orders, and track shipments
    through services like Craftcloud, Shapeways, and Sculpteo.
    """


def _get_fulfillment_provider():
    """Create a fulfillment provider from env config.

    Uses the provider registry to select the right provider based on
    ``KILN_FULFILLMENT_PROVIDER`` or auto-detect from API key env vars.
    """
    from kiln.fulfillment import get_provider

    try:
        return get_provider()
    except (KeyError, RuntimeError, ValueError) as exc:
        raise click.ClickException(
            f"Fulfillment provider not configured: {exc}. "
            "Set KILN_FULFILLMENT_PROVIDER and the matching API key env var."
        ) from exc


@order.command("materials")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def order_materials(json_mode: bool) -> None:
    """List available materials from fulfillment services."""
    try:
        provider = _get_fulfillment_provider()
        materials = provider.list_materials()
        click.echo(format_materials([m.to_dict() for m in materials], json_mode=json_mode))
    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@order.command("quote")
@click.argument("file_path", type=click.Path(exists=True))
@click.option("--material", "-m", required=True, help="Material ID (from 'kiln order materials').")
@click.option("--quantity", "-q", default=1, help="Number of copies (default 1).")
@click.option("--country", default="US", help="Shipping country code (default US).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def order_quote(file_path: str, material: str, quantity: int, country: str, json_mode: bool) -> None:
    """Get a manufacturing quote for a 3D model.

    Upload a model file (STL, 3MF, OBJ) and receive pricing, lead time,
    and shipping options from Craftcloud's network of 150+ print services.
    """
    from kiln.fulfillment import QuoteRequest
    from kiln.billing import BillingLedger

    try:
        provider = _get_fulfillment_provider()
        quote = provider.get_quote(QuoteRequest(
            file_path=file_path,
            material_id=material,
            quantity=quantity,
            shipping_country=country,
        ))
        quote_data = quote.to_dict()
        ledger = BillingLedger()
        fee_calc = ledger.calculate_fee(quote.total_price, currency=quote.currency)
        quote_data["kiln_fee"] = fee_calc.to_dict()
        quote_data["total_with_fee"] = fee_calc.total_cost
        click.echo(format_quote(quote_data, json_mode=json_mode))
    except click.ClickException:
        raise
    except FileNotFoundError as exc:
        click.echo(format_error(str(exc), code="FILE_NOT_FOUND", json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@order.command("place")
@click.argument("quote_id")
@click.option("--shipping", "-s", "shipping_id", default="", help="Shipping option ID (from quote).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def order_place(quote_id: str, shipping_id: str, json_mode: bool) -> None:
    """Place a manufacturing order from a quote.

    Requires a quote ID from 'kiln order quote'.
    """
    from kiln.fulfillment import OrderRequest
    from kiln.billing import BillingLedger
    from kiln.persistence import get_db
    from kiln.payments.manager import PaymentManager
    from kiln.payments.base import PaymentError

    try:
        provider = _get_fulfillment_provider()
        result = provider.place_order(OrderRequest(
            quote_id=quote_id,
            shipping_option_id=shipping_id,
        ))
        order_data = result.to_dict()
        if result.total_price and result.total_price > 0:
            ledger = BillingLedger(db=get_db())
            fee_calc = ledger.calculate_fee(
                result.total_price, currency=result.currency,
            )
            try:
                mgr = PaymentManager()
                if mgr.available_rails:
                    pay_result = mgr.charge_fee(result.order_id, fee_calc)
                    order_data["payment"] = pay_result.to_dict()
                else:
                    ledger.record_charge(result.order_id, fee_calc)
                    order_data["payment"] = {"status": "no_payment_method"}
            except PaymentError:
                ledger.record_charge(
                    result.order_id, fee_calc, payment_status="failed",
                )
                order_data["payment"] = {"status": "failed"}
            order_data["kiln_fee"] = fee_calc.to_dict()
            order_data["total_with_fee"] = fee_calc.total_cost
        click.echo(format_order(order_data, json_mode=json_mode))
    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@order.command("status")
@click.argument("order_id")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def order_status(order_id: str, json_mode: bool) -> None:
    """Check the status of a fulfillment order."""
    try:
        provider = _get_fulfillment_provider()
        result = provider.get_order_status(order_id)
        click.echo(format_order(result.to_dict(), json_mode=json_mode))
    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@order.command("cancel")
@click.argument("order_id")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def order_cancel(order_id: str, json_mode: bool) -> None:
    """Cancel a fulfillment order (if still cancellable)."""
    try:
        provider = _get_fulfillment_provider()
        result = provider.cancel_order(order_id)
        click.echo(format_order(result.to_dict(), json_mode=json_mode))
    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# serve (MCP server)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# cost estimation
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("file_path", type=click.Path(exists=True))
@click.option("--material", "-m", default="PLA", help="Filament material (default PLA).")
@click.option("--electricity-rate", default=0.12, type=float, help="USD per kWh.")
@click.option("--printer-wattage", default=200.0, type=float, help="Printer watts.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def cost(
    file_path: str,
    material: str,
    electricity_rate: float,
    printer_wattage: float,
    json_mode: bool,
) -> None:
    """Estimate print cost from a G-code file."""
    import json as _json
    from kiln.cost_estimator import CostEstimator

    try:
        estimator = CostEstimator()
        estimate = estimator.estimate_from_file(
            file_path, material=material,
            electricity_rate=electricity_rate,
            printer_wattage=printer_wattage,
        )

        if json_mode:
            click.echo(_json.dumps({
                "status": "success", "data": estimate.to_dict(),
            }, indent=2))
        else:
            click.echo(f"File:       {estimate.file_name}")
            click.echo(f"Material:   {estimate.material}")
            click.echo(f"Filament:   {estimate.filament_length_meters:.2f} m "
                       f"({estimate.filament_weight_grams:.1f} g)")
            click.echo(f"Filament $: ${estimate.filament_cost_usd:.2f}")
            if estimate.estimated_time_seconds:
                hours = estimate.estimated_time_seconds / 3600
                click.echo(f"Est. time:  {hours:.1f} hours")
                click.echo(f"Elec. $:    ${estimate.electricity_cost_usd:.2f}")
            click.echo(f"Total $:    ${estimate.total_cost_usd:.2f}")
            for w in estimate.warnings:
                click.echo(f"Warning:    {w}")
    except FileNotFoundError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@cli.command("compare-cost")
@click.argument("file_path", type=click.Path(exists=True))
@click.option("--material", "-m", default="PLA", help="Filament material for local estimate.")
@click.option("--fulfillment-material", default=None, help="Material ID for fulfillment quote.")
@click.option("--quantity", "-q", default=1, type=int, help="Quantity for fulfillment.")
@click.option("--electricity-rate", default=0.12, type=float, help="USD per kWh.")
@click.option("--printer-wattage", default=200.0, type=float, help="Printer watts.")
@click.option("--country", default="US", help="Shipping country code.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def compare_cost(
    file_path: str,
    material: str,
    fulfillment_material: Optional[str],
    quantity: int,
    electricity_rate: float,
    printer_wattage: float,
    country: str,
    json_mode: bool,
) -> None:
    """Compare local printing cost vs. outsourced manufacturing."""
    import json as _json
    from kiln.cost_estimator import CostEstimator

    result: dict = {}

    # Local estimate
    try:
        estimator = CostEstimator()
        estimate = estimator.estimate_from_file(
            file_path, material=material,
            electricity_rate=electricity_rate,
            printer_wattage=printer_wattage,
        )
        result["local"] = {"available": True, "estimate": estimate.to_dict()}
    except Exception as exc:
        result["local"] = {"available": False, "error": str(exc)}

    # Fulfillment quote (optional)
    if fulfillment_material:
        try:
            from kiln.fulfillment import get_provider, QuoteRequest as QR
            provider = get_provider()
            quote = provider.get_quote(QR(
                file_path=file_path,
                material_id=fulfillment_material,
                quantity=quantity,
                shipping_country=country,
            ))
            result["fulfillment"] = {"available": True, "quote": quote.to_dict()}
        except Exception as exc:
            result["fulfillment"] = {"available": False, "error": str(exc)}
    else:
        result["fulfillment"] = {"available": False, "error": "No --fulfillment-material specified"}

    if json_mode:
        click.echo(_json.dumps({"status": "success", "data": result}, indent=2))
    else:
        click.echo("=== Local Printing ===")
        if result["local"]["available"]:
            est = result["local"]["estimate"]
            click.echo(f"  Material:   {est['material']}")
            click.echo(f"  Filament:   {est['filament_weight_grams']:.1f} g")
            click.echo(f"  Total:      ${est['total_cost_usd']:.2f}")
            if est.get("estimated_time_seconds"):
                click.echo(f"  Time:       {est['estimated_time_seconds'] / 3600:.1f} hours")
        else:
            click.echo(f"  Error: {result['local'].get('error', 'unavailable')}")

        click.echo()
        click.echo("=== Outsourced Manufacturing ===")
        if result["fulfillment"]["available"]:
            q = result["fulfillment"]["quote"]
            click.echo(f"  Material:   {q['material']}")
            click.echo(f"  Unit price: ${q['unit_price']:.2f}")
            click.echo(f"  Total:      ${q['total_price']:.2f}")
            if q.get("lead_time_days"):
                click.echo(f"  Lead time:  {q['lead_time_days']} days")
            for so in q.get("shipping_options", []):
                click.echo(f"  Shipping:   {so['name']} — ${so['price']:.2f} ({so.get('estimated_days', '?')} days)")
        else:
            click.echo(f"  {result['fulfillment'].get('error', 'unavailable')}")


# ---------------------------------------------------------------------------
# material tracking
# ---------------------------------------------------------------------------


@cli.group()
def material() -> None:
    """Manage loaded filament materials and spool inventory."""


@material.command("set")
@click.option("--type", "-t", "material_type", required=True, help="Material type (PLA, PETG, etc.).")
@click.option("--color", "-c", default=None, help="Filament color.")
@click.option("--spool", default=None, help="Spool ID to link.")
@click.option("--tool", default=0, type=int, help="Tool/extruder index.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def material_set(
    ctx: click.Context, material_type: str, color: Optional[str],
    spool: Optional[str], tool: int, json_mode: bool,
) -> None:
    """Record which material is loaded in the printer."""
    import json as _json
    from kiln.materials import MaterialTracker
    from kiln.persistence import get_db

    try:
        printer_name = ctx.obj.get("printer") or "default"
        tracker = MaterialTracker(db=get_db())
        mat = tracker.set_material(
            printer_name=printer_name,
            material_type=material_type,
            color=color,
            spool_id=spool,
            tool_index=tool,
        )
        if json_mode:
            click.echo(_json.dumps({
                "status": "success", "data": mat.to_dict(),
            }, indent=2))
        else:
            click.echo(f"Set {printer_name} tool {tool}: {mat.material_type}"
                       + (f" ({color})" if color else ""))
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@material.command("show")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def material_show(ctx: click.Context, json_mode: bool) -> None:
    """Show loaded materials for the active printer."""
    import json as _json
    from kiln.materials import MaterialTracker
    from kiln.persistence import get_db

    try:
        printer_name = ctx.obj.get("printer") or "default"
        tracker = MaterialTracker(db=get_db())
        materials = tracker.get_all_materials(printer_name)
        if json_mode:
            click.echo(_json.dumps({
                "status": "success",
                "data": [m.to_dict() for m in materials],
            }, indent=2))
        else:
            if not materials:
                click.echo("No materials loaded.")
            for m in materials:
                line = f"Tool {m.tool_index}: {m.material_type}"
                if m.color:
                    line += f" ({m.color})"
                if m.remaining_grams is not None:
                    line += f" — {m.remaining_grams:.0f}g remaining"
                click.echo(line)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@material.command("spools")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def material_spools(json_mode: bool) -> None:
    """List all tracked filament spools."""
    import json as _json
    from kiln.materials import MaterialTracker
    from kiln.persistence import get_db

    try:
        tracker = MaterialTracker(db=get_db())
        spools = tracker.list_spools()
        if json_mode:
            click.echo(_json.dumps({
                "status": "success",
                "data": [s.to_dict() for s in spools],
            }, indent=2))
        else:
            if not spools:
                click.echo("No spools tracked.")
            for s in spools:
                line = f"{s.id[:8]}  {s.material_type}"
                if s.color:
                    line += f" ({s.color})"
                if s.brand:
                    line += f" — {s.brand}"
                line += f" — {s.remaining_grams:.0f}/{s.weight_grams:.0f}g"
                click.echo(line)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@material.command("add-spool")
@click.option("--type", "-t", "material_type", required=True, help="Material type.")
@click.option("--color", "-c", default=None, help="Color.")
@click.option("--brand", "-b", default=None, help="Brand.")
@click.option("--weight", default=1000.0, type=float, help="Total weight in grams.")
@click.option("--cost", default=None, type=float, help="Cost in USD.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def material_add_spool(
    material_type: str, color: Optional[str], brand: Optional[str],
    weight: float, cost: Optional[float], json_mode: bool,
) -> None:
    """Add a new filament spool to inventory."""
    import json as _json
    from kiln.materials import MaterialTracker
    from kiln.persistence import get_db

    try:
        tracker = MaterialTracker(db=get_db())
        spool = tracker.add_spool(
            material_type=material_type, color=color, brand=brand,
            weight_grams=weight, cost_usd=cost,
        )
        if json_mode:
            click.echo(_json.dumps({
                "status": "success", "data": spool.to_dict(),
            }, indent=2))
        else:
            click.echo(f"Added spool {spool.id}: {spool.material_type} {spool.weight_grams:.0f}g")
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# bed leveling
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--trigger", is_flag=True, help="Trigger bed leveling now.")
@click.option("--status", "show_status", is_flag=True, default=True, help="Show leveling status.")
@click.option("--set-prints", default=None, type=int, help="Set max prints between levels.")
@click.option("--set-hours", default=None, type=float, help="Set max hours between levels.")
@click.option("--enable/--disable", default=None, help="Enable/disable auto-leveling.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def level(
    ctx: click.Context, trigger: bool, show_status: bool,
    set_prints: Optional[int], set_hours: Optional[float],
    enable: Optional[bool], json_mode: bool,
) -> None:
    """Manage bed leveling triggers and status."""
    import json as _json
    from kiln.bed_leveling import BedLevelManager, LevelingPolicy
    from kiln.persistence import get_db

    try:
        printer_name = ctx.obj.get("printer") or "default"
        mgr = BedLevelManager(db=get_db())

        # Update policy if options given
        if set_prints is not None or set_hours is not None or enable is not None:
            policy = mgr.get_policy(printer_name)
            if set_prints is not None:
                policy.max_prints_between_levels = set_prints
            if set_hours is not None:
                policy.max_hours_between_levels = set_hours
            if enable is not None:
                policy.enabled = enable
            mgr.set_policy(printer_name, policy)
            click.echo(f"Updated leveling policy for {printer_name}")

        if trigger:
            adapter = _get_adapter_from_ctx(ctx)
            result = mgr.trigger_level(printer_name, adapter)
            if json_mode:
                click.echo(_json.dumps({"status": "success", "data": result}, indent=2))
            else:
                click.echo(result["message"])
            return

        status = mgr.check_needed(printer_name)
        if json_mode:
            click.echo(_json.dumps({
                "status": "success", "data": status.to_dict(),
            }, indent=2))
        else:
            click.echo(f"Printer:        {status.printer_name}")
            click.echo(f"Needs leveling: {'Yes' if status.needs_leveling else 'No'}")
            if status.trigger_reason:
                click.echo(f"Reason:         {status.trigger_reason}")
            click.echo(f"Prints since:   {status.prints_since_level}")
            if status.last_leveled_at:
                import time
                age = (time.time() - status.last_leveled_at) / 3600
                click.echo(f"Last leveled:   {age:.1f} hours ago")
    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# webcam streaming
# ---------------------------------------------------------------------------


@cli.command("stream")
@click.option("--port", default=8081, type=int, help="Local port for stream server.")
@click.option("--stop", "do_stop", is_flag=True, help="Stop active stream.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def stream(ctx: click.Context, port: int, do_stop: bool, json_mode: bool) -> None:
    """Start or stop the MJPEG webcam streaming proxy."""
    import json as _json
    from kiln.streaming import MJPEGProxy

    proxy = MJPEGProxy()

    try:
        if do_stop:
            info = proxy.stop()
            if json_mode:
                click.echo(_json.dumps({"status": "success", "data": info.to_dict()}, indent=2))
            else:
                click.echo("Stream stopped.")
            return

        adapter = _get_adapter_from_ctx(ctx)
        stream_url = adapter.get_stream_url()
        if stream_url is None:
            click.echo(format_error(
                "Webcam streaming not available for this printer.",
                code="NO_STREAM",
                json_mode=json_mode,
            ))
            sys.exit(1)

        printer_name = ctx.obj.get("printer") or "default"
        info = proxy.start(source_url=stream_url, port=port, printer_name=printer_name)
        if json_mode:
            click.echo(_json.dumps({"status": "success", "data": info.to_dict()}, indent=2))
        else:
            click.echo(f"Stream started at {info.local_url}")
            click.echo("Press Ctrl+C to stop.")
            import time
            try:
                while proxy.active:
                    time.sleep(1)
            except KeyboardInterrupt:
                proxy.stop()
                click.echo("\nStream stopped.")
    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# cloud sync
# ---------------------------------------------------------------------------


@cli.group()
def sync() -> None:
    """Cloud sync management."""


@sync.command("status")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def sync_status(json_mode: bool) -> None:
    """Show cloud sync status."""
    import json as _json
    click.echo(_json.dumps({
        "status": "success",
        "data": {"message": "Cloud sync status — use MCP server for full status."},
    }, indent=2) if json_mode else "Cloud sync status available via MCP server (kiln serve).")


@sync.command("now")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def sync_now(json_mode: bool) -> None:
    """Trigger an immediate sync cycle."""
    click.echo("Sync trigger available via MCP server (kiln serve).")


@sync.command("configure")
@click.option("--url", required=True, help="Cloud sync endpoint URL.")
@click.option("--api-key", required=True, help="API key.")
@click.option("--interval", default=60.0, type=float, help="Sync interval in seconds.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def sync_configure(url: str, api_key: str, interval: float, json_mode: bool) -> None:
    """Save cloud sync configuration."""
    import json as _json
    from kiln.cloud_sync import SyncConfig
    from kiln.persistence import get_db

    try:
        config = SyncConfig(cloud_url=url, api_key=api_key, sync_interval_seconds=interval)
        from dataclasses import asdict
        get_db().set_setting("cloud_sync_config", _json.dumps(asdict(config)))
        if json_mode:
            click.echo(_json.dumps({"status": "success", "data": config.to_dict()}, indent=2))
        else:
            click.echo(f"Cloud sync configured: {url} (interval {interval}s)")
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# plugins
# ---------------------------------------------------------------------------


@cli.group()
def plugins() -> None:
    """Plugin management."""


@plugins.command("list")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def plugins_list(json_mode: bool) -> None:
    """List all discovered plugins."""
    import json as _json
    from kiln.plugins import PluginManager

    mgr = PluginManager()
    discovered = mgr.discover()
    if json_mode:
        click.echo(_json.dumps({
            "status": "success",
            "data": [p.to_dict() for p in discovered],
        }, indent=2))
    else:
        if not discovered:
            click.echo("No plugins found.")
        for p in discovered:
            status = "active" if p.active else "inactive"
            if p.error:
                status = f"error: {p.error}"
            click.echo(f"{p.name} v{p.version} [{status}]")
            if p.description:
                click.echo(f"  {p.description}")


@plugins.command("info")
@click.argument("name")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def plugins_info(name: str, json_mode: bool) -> None:
    """Show details for a specific plugin."""
    import json as _json
    from kiln.plugins import PluginManager

    mgr = PluginManager()
    mgr.discover()
    info = mgr.get_plugin_info(name)
    if info is None:
        click.echo(format_error(f"Plugin {name!r} not found.", json_mode=json_mode))
        sys.exit(1)
    if json_mode:
        click.echo(_json.dumps({"status": "success", "data": info.to_dict()}, indent=2))
    else:
        click.echo(f"Name:    {info.name}")
        click.echo(f"Version: {info.version}")
        click.echo(f"Active:  {info.active}")
        if info.description:
            click.echo(f"Desc:    {info.description}")
        if info.hooks:
            click.echo(f"Hooks:   {', '.join(info.hooks)}")
        if info.error:
            click.echo(f"Error:   {info.error}")


# ---------------------------------------------------------------------------
# billing
# ---------------------------------------------------------------------------


@cli.group()
def billing() -> None:
    """Manage payment methods and view billing history.

    Use subcommands to link a payment method (credit card or crypto),
    view monthly spend, and see charge history.
    """


@billing.command("setup")
@click.option("--rail", default="stripe", type=click.Choice(["stripe", "crypto"]),
              help="Payment rail (default stripe).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def billing_setup(rail: str, json_mode: bool) -> None:
    """Link a payment method for platform fees.

    Generates a setup URL to add a credit card (Stripe) or configure
    crypto payments (USDC on Solana/Base).
    """
    from kiln.cli.config import get_billing_config, get_or_create_user_id
    from kiln.persistence import get_db
    from kiln.payments.manager import PaymentManager

    try:
        config = get_billing_config()
        user_id = get_or_create_user_id()
        mgr = PaymentManager(db=get_db(), config=config)

        if rail == "stripe":
            from kiln.payments.stripe_provider import StripeProvider
            provider = StripeProvider()
            mgr.register_provider(provider)
        else:
            click.echo(format_error(
                "Crypto setup: set KILN_CIRCLE_API_KEY and configure your "
                "wallet via the Circle dashboard. Then run 'kiln billing status' "
                "to verify.",
                json_mode=json_mode,
            ))
            return

        url = mgr.get_setup_url(rail=rail)
        click.echo(format_billing_setup(url, rail, json_mode=json_mode))
    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@billing.command("status")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def billing_status(json_mode: bool) -> None:
    """Show current payment method, monthly spend, and limits."""
    from kiln.cli.config import get_billing_config, get_or_create_user_id
    from kiln.persistence import get_db
    from kiln.payments.manager import PaymentManager

    try:
        config = get_billing_config()
        user_id = get_or_create_user_id()
        mgr = PaymentManager(db=get_db(), config=config)
        data = mgr.get_billing_status(user_id)
        click.echo(format_billing_status(data, json_mode=json_mode))
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@billing.command("history")
@click.option("--limit", "-n", default=20, help="Max records to show (default 20).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def billing_history(limit: int, json_mode: bool) -> None:
    """Show recent billing charges and payment outcomes."""
    from kiln.cli.config import get_billing_config
    from kiln.persistence import get_db
    from kiln.payments.manager import PaymentManager

    try:
        config = get_billing_config()
        mgr = PaymentManager(db=get_db(), config=config)
        charges = mgr.get_billing_history(limit=limit)
        click.echo(format_billing_history(charges, json_mode=json_mode))
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# setup (interactive onboarding wizard)
# ---------------------------------------------------------------------------


_PRINTER_TYPE_LABELS = {
    "octoprint": "OctoPrint",
    "moonraker": "Moonraker (Klipper)",
    "bambu": "Bambu Lab",
    "prusaconnect": "Prusa Connect",
}


@cli.command()
@click.option(
    "--skip-discovery", is_flag=True,
    help="Skip network scan and go straight to manual entry.",
)
@click.option(
    "--timeout", "-t", "discovery_timeout", default=5.0,
    help="Discovery scan timeout in seconds (default 5).",
)
def setup(skip_discovery: bool, discovery_timeout: float) -> None:
    """Interactive guided setup for your first printer.

    Scans the local network for printers, lets you pick one (or enter
    details manually), saves credentials, and verifies the connection.
    """
    from kiln.cli.config import get_config_path

    # -- Welcome banner ----------------------------------------------------
    click.echo()
    click.echo(click.style("  Kiln Setup", bold=True))
    click.echo(click.style("  ----------", bold=True))
    click.echo("  Configure a 3D printer for Kiln to control.\n")

    # -- Check existing config ---------------------------------------------
    config_path = get_config_path()
    existing = _list_printers()
    if existing:
        click.echo(f"  Found {len(existing)} printer(s) already configured:")
        for p in existing:
            marker = " (active)" if p.get("active") else ""
            click.echo(f"    - {p['name']} [{p['type']}] {p['host']}{marker}")
        click.echo()
        action = click.prompt(
            "  Add another printer or start fresh?",
            type=click.Choice(["add", "fresh", "quit"]),
            default="add",
        )
        if action == "quit":
            click.echo("  Setup cancelled.")
            return
        if action == "fresh":
            if not click.confirm("  This will remove all saved printers. Continue?"):
                click.echo("  Setup cancelled.")
                return
            # Wipe printers section
            from kiln.cli.config import _read_config_file, _write_config_file
            raw = _read_config_file(config_path)
            raw["printers"] = {}
            raw.pop("active_printer", None)
            _write_config_file(config_path, raw)
            click.echo("  Cleared existing printer config.\n")

    # -- Discovery ---------------------------------------------------------
    discovered = []
    if not skip_discovery:
        click.echo("  Scanning network for printers...")
        try:
            from kiln.cli.discovery import discover_printers
            discovered = discover_printers(timeout=discovery_timeout)
        except Exception as exc:
            click.echo(click.style(f"  Discovery failed: {exc}", fg="yellow"))
            click.echo("  Continuing with manual entry.\n")

        if discovered:
            click.echo(f"\n  Found {len(discovered)} printer(s):\n")
            click.echo(f"    {'#':<4} {'Name':<25} {'Host':<22} {'Type':<14} {'Method'}")
            click.echo(f"    {'─'*4} {'─'*25} {'─'*22} {'─'*14} {'─'*10}")
            for i, p in enumerate(discovered, 1):
                label = _PRINTER_TYPE_LABELS.get(p.printer_type, p.printer_type)
                display_name = p.name or "(unnamed)"
                click.echo(
                    f"    {i:<4} {display_name:<25} {p.host:<22} {label:<14} {p.discovery_method}"
                )
            click.echo()
        else:
            click.echo("  No printers found on the network.\n")
            click.echo(
                "  Tip: Bambu printers don't advertise via mDNS.\n"
                "       Enter the IP address manually.\n"
            )

    # -- Selection ---------------------------------------------------------
    selected = None
    if discovered:
        choice = click.prompt(
            "  Enter printer number, or 'm' for manual entry",
            default="1",
        )
        if choice.lower() == "m":
            pass  # fall through to manual
        else:
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(discovered):
                    selected = discovered[idx]
                else:
                    click.echo(click.style("  Invalid number. Switching to manual entry.", fg="yellow"))
            except ValueError:
                click.echo(click.style("  Invalid input. Switching to manual entry.", fg="yellow"))

    # -- Manual entry (or refine selected) ---------------------------------
    if selected is not None:
        host = selected.host
        printer_type = selected.printer_type
        if printer_type == "unknown":
            printer_type = click.prompt(
                "  Printer type could not be auto-detected. Select type",
                type=click.Choice(["octoprint", "moonraker", "bambu", "prusaconnect"]),
            )
        suggested_name = (selected.name or printer_type).lower().replace(" ", "-").replace(".", "-")
    else:
        # Full manual entry
        host = click.prompt("  Printer host (IP or hostname)")
        click.echo("  Probing host...")
        try:
            from kiln.cli.discovery import probe_host
            probed = probe_host(host)
            if probed:
                p = probed[0]
                printer_type = p.printer_type
                click.echo(
                    f"  Detected: {_PRINTER_TYPE_LABELS.get(printer_type, printer_type)}"
                    + (f" ({p.name})" if p.name else "")
                )
                suggested_name = (p.name or printer_type).lower().replace(" ", "-").replace(".", "-")
            else:
                click.echo("  Could not auto-detect printer type.")
                printer_type = click.prompt(
                    "  Select printer type",
                    type=click.Choice(["octoprint", "moonraker", "bambu", "prusaconnect"]),
                )
                suggested_name = printer_type
        except Exception:
            click.echo("  Probe failed. Enter type manually.")
            printer_type = click.prompt(
                "  Select printer type",
                type=click.Choice(["octoprint", "moonraker", "bambu", "prusaconnect"]),
            )
            suggested_name = printer_type

    # -- Friendly name -----------------------------------------------------
    name = click.prompt("  Friendly name for this printer", default=suggested_name)
    # Sanitize: lowercase, no spaces
    name = name.strip().lower().replace(" ", "-")

    # -- Credentials -------------------------------------------------------
    api_key = None
    access_code = None
    serial = None

    if printer_type in ("octoprint", "moonraker", "prusaconnect"):
        api_key = click.prompt(
            f"  API key for {_PRINTER_TYPE_LABELS.get(printer_type, printer_type)}",
            default="",
            show_default=False,
        )
        if not api_key:
            api_key = None
    elif printer_type == "bambu":
        access_code = click.prompt("  LAN access code (from printer screen)")
        serial = click.prompt("  Printer serial number")

    # -- Save --------------------------------------------------------------
    click.echo()
    try:
        path = save_printer(
            name,
            printer_type,
            host,
            api_key=api_key,
            access_code=access_code,
            serial=serial,
            set_active=True,
        )
        click.echo(f"  Saved printer '{name}' to {path}")
    except Exception as exc:
        click.echo(click.style(f"  Failed to save config: {exc}", fg="red"))
        sys.exit(1)

    # -- Test connection ---------------------------------------------------
    click.echo("  Testing connection...")
    try:
        cfg = load_printer_config(name)
        adapter = _make_adapter(cfg)
        state = adapter.get_state()
        click.echo(
            click.style("  Connected!", fg="green")
            + f" Printer state: {state.state.value}"
        )
        if state.tool_temp_actual is not None:
            click.echo(f"  Hotend: {state.tool_temp_actual:.0f}C")
        if state.bed_temp_actual is not None:
            click.echo(f"  Bed:    {state.bed_temp_actual:.0f}C")
    except Exception as exc:
        click.echo(click.style(f"  Connection test failed: {exc}", fg="yellow"))
        click.echo(
            "  The printer was saved but may need correct credentials.\n"
            "  Update with: kiln auth --name {name} --host {host} "
            f"--type {printer_type} --api-key <key>"
        )

    # -- Next steps --------------------------------------------------------
    click.echo()
    click.echo(click.style("  Setup complete!", bold=True))
    click.echo()
    click.echo("  Next steps:")
    click.echo(f"    kiln status          Check printer state")
    click.echo(f"    kiln files           List files on the printer")
    click.echo(f"    kiln print <file>    Start a print")
    click.echo(f"    kiln serve           Start the MCP server")
    click.echo()


# ---------------------------------------------------------------------------
# serve
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
# rest
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--host", default="0.0.0.0", help="Bind address.")
@click.option("--port", default=8420, type=int, help="Port number.")
@click.option(
    "--auth-token", default=None,
    help="Bearer token for authentication (optional).",
)
@click.option(
    "--tier", default="full",
    type=click.Choice(["essential", "standard", "full"]),
    help="Which tool tier to expose (default: full).",
)
def rest(host: str, port: int, auth_token: Optional[str], tier: str) -> None:
    """Start the Kiln REST API server.

    Wraps all MCP tools as REST endpoints so any HTTP client can control
    printers.  Tools are available at POST /api/tools/{tool_name} and a
    discovery endpoint at GET /api/tools lists available tools with schemas.
    """
    from kiln.rest_api import run_rest_server, RestApiConfig

    config = RestApiConfig(
        host=host, port=port, auth_token=auth_token, tool_tier=tier,
    )
    click.echo(f"Starting Kiln REST API on {host}:{port} (tier: {tier})")
    run_rest_server(config)


# ---------------------------------------------------------------------------
# agent
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--model", "-m", default="openai/gpt-4o",
    help="Model ID (default: openai/gpt-4o).",
)
@click.option("--tier", default=None, help="Tool tier (auto-detect if not set).")
@click.option(
    "--base-url", default="https://openrouter.ai/api/v1",
    help="LLM provider base URL.",
)
def agent(model: str, tier: Optional[str], base_url: str) -> None:
    """Interactive agent REPL -- chat with any LLM to control your printer.

    Requires KILN_OPENROUTER_KEY or OPENROUTER_API_KEY environment variable.
    """
    import os

    api_key = os.environ.get("KILN_OPENROUTER_KEY") or os.environ.get(
        "OPENROUTER_API_KEY"
    )
    if not api_key:
        click.echo(
            "Set KILN_OPENROUTER_KEY or OPENROUTER_API_KEY environment variable."
        )
        sys.exit(1)

    try:
        from kiln.agent_loop import run_agent_loop, AgentConfig
    except ImportError:
        click.echo(
            "Agent loop module not available. Ensure kiln.agent_loop is installed."
        )
        sys.exit(1)

    agent_config = AgentConfig(
        api_key=api_key,
        model=model,
        tool_tier=tier or "full",
        base_url=base_url,
    )

    click.echo(f"Kiln Agent -- model: {model}, tier: {agent_config.tool_tier}")
    click.echo("Type 'quit' to exit.\n")

    conversation = None
    while True:
        try:
            prompt = click.prompt("You", prompt_suffix="> ")
        except (EOFError, KeyboardInterrupt):
            click.echo()
            break
        if prompt.lower() in ("quit", "exit", "q"):
            break
        try:
            result = run_agent_loop(
                prompt, agent_config, conversation=conversation,
            )
            conversation = result.messages
            click.echo(f"\nAgent> {result.response}\n")
            click.echo(
                f"  ({result.tool_calls_made} tool calls, {result.turns} turns)\n"
            )
        except Exception as exc:
            click.echo(f"\nAgent error: {exc}\n")


# ---------------------------------------------------------------------------
# Model Generation
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("prompt")
@click.option("--provider", "-p", default="meshy",
              type=click.Choice(["meshy", "openscad"]),
              help="Generation provider (default: meshy).")
@click.option("--style", "-s", default=None, help="Style hint (e.g. realistic, sculpture).")
@click.option("--output-dir", "-o", default=None, help="Output directory for generated model.")
@click.option("--wait/--no-wait", "wait_for", default=False,
              help="Wait for generation to complete (default: return immediately).")
@click.option("--timeout", "-t", default=600, type=int, help="Max wait time in seconds (default 600).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def generate(
    prompt: str,
    provider: str,
    style: Optional[str],
    output_dir: Optional[str],
    wait_for: bool,
    timeout: int,
    json_mode: bool,
) -> None:
    """Generate a 3D model from a text description.

    PROMPT is the text description (for Meshy) or OpenSCAD code (for openscad).

    \b
    Examples:
        kiln generate "a phone stand with cable slot" --provider meshy
        kiln generate "cube([20,20,10]);" --provider openscad
        kiln generate "a gear with 24 teeth" --wait --json
    """
    import time as _time
    from kiln.generation import (
        GenerationAuthError,
        GenerationError,
        GenerationStatus,
        MeshyProvider,
        OpenSCADProvider,
        validate_mesh,
    )

    try:
        if provider == "meshy":
            gen = MeshyProvider()
        elif provider == "openscad":
            gen = OpenSCADProvider()
        else:
            click.echo(format_error(f"Unknown provider: {provider!r}", json_mode=json_mode))
            sys.exit(1)

        job = gen.generate(prompt, format="stl", style=style)

        # If not waiting or already done (OpenSCAD), return job info.
        if not wait_for or job.status == GenerationStatus.SUCCEEDED:
            if job.status == GenerationStatus.SUCCEEDED:
                # Download the result for synchronous providers.
                result = gen.download_result(job.id, output_dir=output_dir or "/tmp/kiln_generated")
                val = validate_mesh(result.local_path)

                if json_mode:
                    import json
                    click.echo(json.dumps({
                        "status": "success",
                        "data": {
                            "job": job.to_dict(),
                            "result": result.to_dict(),
                            "validation": val.to_dict(),
                        },
                    }, indent=2))
                else:
                    click.echo(f"Generated: {result.local_path}")
                    click.echo(f"  Format: {result.format}  Size: {result.file_size_bytes:,} bytes")
                    click.echo(f"  Triangles: {val.triangle_count:,}  Manifold: {val.is_manifold}")
                    if val.warnings:
                        for w in val.warnings:
                            click.echo(f"  Warning: {w}")
                return

            # Async job submitted, not waiting.
            if json_mode:
                import json
                click.echo(json.dumps({
                    "status": "success",
                    "data": {"job": job.to_dict()},
                }, indent=2))
            else:
                click.echo(f"Job submitted: {job.id}")
                click.echo(f"  Provider: {gen.display_name}  Status: {job.status.value}")
                click.echo(f"  Track with: kiln generate-status {job.id}")
            return

        # Wait for async completion.
        if not json_mode:
            click.echo(f"Job {job.id} submitted to {gen.display_name}. Waiting...")

        start = _time.time()
        while True:
            elapsed = _time.time() - start
            if elapsed >= timeout:
                click.echo(format_error(
                    f"Timed out after {timeout}s", code="TIMEOUT", json_mode=json_mode
                ))
                sys.exit(1)

            job = gen.get_job_status(job.id)

            if not json_mode and job.progress > 0:
                click.echo(f"\r  Progress: {job.progress}%  ", nl=False)

            if job.status == GenerationStatus.SUCCEEDED:
                result = gen.download_result(job.id, output_dir=output_dir or "/tmp/kiln_generated")
                val = validate_mesh(result.local_path)

                if json_mode:
                    import json
                    click.echo(json.dumps({
                        "status": "success",
                        "data": {
                            "job": job.to_dict(),
                            "result": result.to_dict(),
                            "validation": val.to_dict(),
                            "elapsed_seconds": round(elapsed, 1),
                        },
                    }, indent=2))
                else:
                    click.echo(f"\nGenerated: {result.local_path}")
                    click.echo(f"  Format: {result.format}  Size: {result.file_size_bytes:,} bytes")
                    click.echo(f"  Triangles: {val.triangle_count:,}  Manifold: {val.is_manifold}")
                    click.echo(f"  Completed in {elapsed:.0f}s")
                return

            if job.status in (GenerationStatus.FAILED, GenerationStatus.CANCELLED):
                click.echo(format_error(
                    f"Generation {job.status.value}: {job.error or 'unknown'}",
                    code="GENERATION_FAILED",
                    json_mode=json_mode,
                ))
                sys.exit(1)

            _time.sleep(10)

    except GenerationAuthError as exc:
        click.echo(format_error(str(exc), code="AUTH_ERROR", json_mode=json_mode))
        sys.exit(1)
    except GenerationError as exc:
        click.echo(format_error(str(exc), code=exc.code or "GENERATION_ERROR", json_mode=json_mode))
        sys.exit(1)
    except KeyboardInterrupt:
        if not json_mode:
            click.echo("\nInterrupted.")
        sys.exit(130)
    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@cli.command("generate-status")
@click.argument("job_id")
@click.option("--provider", "-p", default="meshy",
              type=click.Choice(["meshy", "openscad"]),
              help="Generation provider.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def generate_status(job_id: str, provider: str, json_mode: bool) -> None:
    """Check the status of a generation job.

    JOB_ID is the ID returned by 'kiln generate'.
    """
    from kiln.generation import (
        GenerationAuthError,
        GenerationError,
        MeshyProvider,
        OpenSCADProvider,
    )

    try:
        if provider == "meshy":
            gen = MeshyProvider()
        else:
            gen = OpenSCADProvider()

        job = gen.get_job_status(job_id)

        if json_mode:
            import json
            click.echo(json.dumps({
                "status": "success",
                "data": {"job": job.to_dict()},
            }, indent=2))
        else:
            click.echo(f"Job: {job.id}")
            click.echo(f"  Provider: {job.provider}  Status: {job.status.value}")
            click.echo(f"  Progress: {job.progress}%")
            if job.error:
                click.echo(f"  Error: {job.error}")

    except GenerationAuthError as exc:
        click.echo(format_error(str(exc), code="AUTH_ERROR", json_mode=json_mode))
        sys.exit(1)
    except GenerationError as exc:
        click.echo(format_error(str(exc), code=exc.code or "GENERATION_ERROR", json_mode=json_mode))
        sys.exit(1)
    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@cli.command("generate-download")
@click.argument("job_id")
@click.option("--provider", "-p", default="meshy",
              type=click.Choice(["meshy", "openscad"]),
              help="Generation provider.")
@click.option("--output-dir", "-o", default="/tmp/kiln_generated",
              help="Output directory.")
@click.option("--validate/--no-validate", default=True,
              help="Run mesh validation (default: on).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def generate_download(
    job_id: str,
    provider: str,
    output_dir: str,
    validate: bool,
    json_mode: bool,
) -> None:
    """Download a completed generated model.

    JOB_ID is the ID returned by 'kiln generate'.
    """
    from kiln.generation import (
        GenerationAuthError,
        GenerationError,
        MeshyProvider,
        OpenSCADProvider,
        validate_mesh,
    )

    try:
        if provider == "meshy":
            gen = MeshyProvider()
        else:
            gen = OpenSCADProvider()

        result = gen.download_result(job_id, output_dir=output_dir)

        validation = None
        if validate and result.format in ("stl", "obj"):
            validation = validate_mesh(result.local_path)

        if json_mode:
            import json
            data: Dict[str, Any] = {"result": result.to_dict()}
            if validation:
                data["validation"] = validation.to_dict()
            click.echo(json.dumps({"status": "success", "data": data}, indent=2))
        else:
            click.echo(f"Downloaded: {result.local_path}")
            click.echo(f"  Format: {result.format}  Size: {result.file_size_bytes:,} bytes")
            if validation:
                click.echo(f"  Triangles: {validation.triangle_count:,}  Manifold: {validation.is_manifold}")
                if not validation.valid:
                    for e in validation.errors:
                        click.echo(f"  Error: {e}")
                for w in validation.warnings:
                    click.echo(f"  Warning: {w}")

    except GenerationAuthError as exc:
        click.echo(format_error(str(exc), code="AUTH_ERROR", json_mode=json_mode))
        sys.exit(1)
    except GenerationError as exc:
        click.echo(format_error(str(exc), code=exc.code or "GENERATION_ERROR", json_mode=json_mode))
        sys.exit(1)
    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# Firmware
# ---------------------------------------------------------------------------


@cli.group()
def firmware() -> None:
    """Check and apply firmware updates.

    Query available updates, apply upgrades, or roll back to a previous
    version.  Supported for OctoPrint and Moonraker printers.
    """


@firmware.command("status")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def firmware_status_cmd(ctx: click.Context, json_mode: bool) -> None:
    """Show firmware component versions and available updates."""
    import json as _json

    try:
        adapter = _get_adapter_from_ctx(ctx)
        if not adapter.capabilities.can_update_firmware:
            click.echo(format_error(
                "This printer does not support firmware updates.",
                json_mode=json_mode,
            ))
            sys.exit(1)

        status = adapter.get_firmware_status()
        if status is None:
            click.echo(format_error(
                "Could not retrieve firmware status.",
                json_mode=json_mode,
            ))
            sys.exit(1)

        data = {
            "busy": status.busy,
            "updates_available": status.updates_available,
            "components": [
                {
                    "name": c.name,
                    "current_version": c.current_version,
                    "remote_version": c.remote_version,
                    "update_available": c.update_available,
                    "component_type": c.component_type,
                }
                for c in status.components
            ],
        }

        if json_mode:
            click.echo(_json.dumps({"status": "success", "data": data}, indent=2))
        else:
            click.echo(f"Updates available: {status.updates_available}")
            if status.busy:
                click.echo("  (update in progress)")
            for c in status.components:
                marker = " *" if c.update_available else ""
                ver = c.current_version
                if c.remote_version and c.update_available:
                    ver += f" -> {c.remote_version}"
                click.echo(f"  {c.name}: {ver}{marker}")

    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@firmware.command("update")
@click.option("--component", "-c", default=None, help="Component to update (default: all).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def firmware_update_cmd(ctx: click.Context, component: Optional[str], json_mode: bool) -> None:
    """Apply available firmware updates.

    Optionally specify --component to update a single component,
    otherwise all components with available updates are upgraded.
    """
    import json as _json

    try:
        adapter = _get_adapter_from_ctx(ctx)
        if not adapter.capabilities.can_update_firmware:
            click.echo(format_error(
                "This printer does not support firmware updates.",
                json_mode=json_mode,
            ))
            sys.exit(1)

        result = adapter.update_firmware(component=component)

        data = {
            "success": result.success,
            "message": result.message,
            "component": result.component,
        }

        if json_mode:
            click.echo(_json.dumps({"status": "success" if result.success else "error", "data": data}, indent=2))
        else:
            click.echo(result.message)

        if not result.success:
            sys.exit(1)

    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@firmware.command("rollback")
@click.argument("component")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def firmware_rollback_cmd(ctx: click.Context, component: str, json_mode: bool) -> None:
    """Roll back a firmware component to its previous version.

    COMPONENT is the name of the component to roll back (e.g. klipper).
    Only supported on Moonraker printers.
    """
    import json as _json

    try:
        adapter = _get_adapter_from_ctx(ctx)
        if not adapter.capabilities.can_update_firmware:
            click.echo(format_error(
                "This printer does not support firmware rollback.",
                json_mode=json_mode,
            ))
            sys.exit(1)

        result = adapter.rollback_firmware(component)

        data = {
            "success": result.success,
            "message": result.message,
            "component": result.component,
        }

        if json_mode:
            click.echo(_json.dumps({"status": "success" if result.success else "error", "data": data}, indent=2))
        else:
            click.echo(result.message)

        if not result.success:
            sys.exit(1)

    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point."""
    cli()


if __name__ == "__main__":
    main()
