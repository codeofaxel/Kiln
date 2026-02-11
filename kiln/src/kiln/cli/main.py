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
    format_history,
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
            os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
            with open(output, "wb") as f:
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
