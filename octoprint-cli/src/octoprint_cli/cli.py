"""OctoPrint CLI - Agent-friendly command-line interface for OctoPrint.

Usage:
    octoprint-cli status [--json]
    octoprint-cli upload <file_path> [--json]
    octoprint-cli print <file_path> [--confirm] [--async] [--skip-if-printing] [--json]
    octoprint-cli cancel [--confirm] [--json]
    octoprint-cli pause [--json]
    octoprint-cli resume [--json]
    octoprint-cli files [--json]
    octoprint-cli preflight [<file_path>] [--json]
    octoprint-cli init
"""

from __future__ import annotations

import json
import sys
from typing import Any

import click

from octoprint_cli.client import OctoPrintClient
from octoprint_cli.config import init_config, load_config, validate_config
from octoprint_cli.exit_codes import (
    FILE_ERROR,
    OTHER_ERROR,
    PRINTER_OFFLINE,
    SUCCESS,
    exit_code_for,
)
from octoprint_cli.output import (
    format_file_list,
    format_job_action,
    format_printer_status,
    format_response,
    format_upload_result,
)
from octoprint_cli.safety import (
    check_can_cancel,
    preflight_check,
    validate_file,
)

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _emit(output: str, exit_code: int = SUCCESS) -> None:
    """Print output and exit with the given code."""
    click.echo(output)
    sys.exit(exit_code)


def _emit_error(
    code: str,
    message: str,
    json_mode: bool,
    exit_code: int | None = None,
) -> None:
    """Emit a structured error and exit."""
    if exit_code is None:
        exit_code = exit_code_for(code)
    output = format_response(
        "error",
        error={"code": code, "message": message},
        json_mode=json_mode,
    )
    _emit(output, exit_code)


def _make_client(host: str, api_key: str, json_mode: bool) -> OctoPrintClient:
    """Build a configured client, validating config first."""
    config = load_config(host=host, api_key=api_key)
    valid, err = validate_config(config)
    if not valid:
        _emit_error("VALIDATION_ERROR", f"Configuration error: {err}", json_mode)
    return OctoPrintClient(
        host=str(config["host"]),
        api_key=str(config["api_key"]),
        timeout=int(config["timeout"]),  # type: ignore[arg-type]
        retries=int(config["retries"]),  # type: ignore[arg-type]
    )


def _handle_api_error(
    result: dict[str, Any],
    json_mode: bool,
) -> None:
    """If the API result is an error, emit and exit. Otherwise return."""
    if not result["success"]:
        error = result["error"]
        _emit_error(error["code"], error["message"], json_mode)


# ------------------------------------------------------------------
# CLI group
# ------------------------------------------------------------------


@click.group()
@click.option("--host", envvar="OCTOPRINT_HOST", default=None, help="OctoPrint server URL.")
@click.option("--api-key", envvar="OCTOPRINT_API_KEY", default=None, help="OctoPrint API key.")
@click.version_option(package_name="octoprint-cli")
@click.pass_context
def cli(ctx: click.Context, host: str | None, api_key: str | None) -> None:
    """Agent-friendly CLI for OctoPrint 3D printer management."""
    ctx.ensure_object(dict)
    ctx.obj["host"] = host
    ctx.obj["api_key"] = api_key


# ------------------------------------------------------------------
# status
# ------------------------------------------------------------------


@cli.command()
@click.option("--json", "json_mode", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def status(ctx: click.Context, json_mode: bool) -> None:
    """Get printer and current job status."""
    client = _make_client(ctx.obj["host"], ctx.obj["api_key"], json_mode)

    printer_result = client.get_printer_state()
    job_result = client.get_job()

    # If printer is unreachable, still try to give useful info
    if not printer_result["success"]:
        error = printer_result["error"]
        # If it's a 409 (not connected), report that clearly
        if error.get("http_status") == 409 or error["code"] == "CONFLICT":
            if json_mode:
                output = json.dumps(
                    {
                        "status": "error",
                        "data": {"state": "Disconnected", "temperature": None, "job": None},
                        "error": {"code": "PRINTER_DISCONNECTED", "message": "Printer is not connected to OctoPrint."},
                    },
                    indent=2,
                )
                _emit(output, PRINTER_OFFLINE)
            else:
                _emit_error(
                    "PRINTER_DISCONNECTED", "Printer is not connected to OctoPrint.", json_mode, PRINTER_OFFLINE
                )
        _handle_api_error(printer_result, json_mode)

    printer_data = printer_result.get("data")
    job_data = job_result.get("data") if job_result["success"] else None

    output = format_printer_status(printer_data, job_data, json_mode=json_mode)
    _emit(output, SUCCESS)


# ------------------------------------------------------------------
# files
# ------------------------------------------------------------------


@cli.command()
@click.option("--location", default="local", help="Storage location (local/sdcard).")
@click.option("--json", "json_mode", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def files(ctx: click.Context, location: str, json_mode: bool) -> None:
    """List available files on the printer."""
    client = _make_client(ctx.obj["host"], ctx.obj["api_key"], json_mode)
    result = client.list_files(location=location, recursive=True)
    _handle_api_error(result, json_mode)

    output = format_file_list(result["data"], json_mode=json_mode)
    _emit(output, SUCCESS)


# ------------------------------------------------------------------
# upload
# ------------------------------------------------------------------


@cli.command()
@click.argument("file_path", type=click.Path(exists=True))
@click.option("--location", default="local", help="Upload destination (local/sdcard).")
@click.option("--select", is_flag=True, default=False, help="Select file after upload.")
@click.option("--json", "json_mode", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def upload(
    ctx: click.Context,
    file_path: str,
    location: str,
    select: bool,
    json_mode: bool,
) -> None:
    """Upload a G-code file to OctoPrint."""
    # Validate file locally first
    validation = validate_file(file_path)
    if not validation["valid"]:
        errors_str = "; ".join(validation["errors"])
        _emit_error("FILE_ERROR", f"File validation failed: {errors_str}", json_mode, FILE_ERROR)

    # Warn about large files
    if validation["warnings"] and not json_mode:
        for w in validation["warnings"]:
            click.echo(f"Warning: {w}", err=True)

    client = _make_client(ctx.obj["host"], ctx.obj["api_key"], json_mode)
    result = client.upload_file(file_path, location=location, select=select)
    _handle_api_error(result, json_mode)

    if json_mode:
        # Enrich with local validation info
        data = result["data"] or {}
        data["validation"] = validation["info"]
        output = format_response("success", data=data, json_mode=True)
    else:
        output = format_upload_result(result["data"], json_mode=False)

    _emit(output, SUCCESS)


# ------------------------------------------------------------------
# print
# ------------------------------------------------------------------


@cli.command(name="print")
@click.argument("file_path", type=click.Path(exists=True))
@click.option("--confirm", is_flag=True, default=False, help="Required flag to confirm print start.")
@click.option("--skip-if-printing", is_flag=True, default=False, help="Exit 0 if already printing.")
@click.option("--no-preflight", is_flag=True, default=False, help="Skip pre-flight safety checks.")
@click.option("--json", "json_mode", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def print_cmd(
    ctx: click.Context,
    file_path: str,
    confirm: bool,
    skip_if_printing: bool,
    no_preflight: bool,
    json_mode: bool,
) -> None:
    """Upload a file and start printing.

    Requires --confirm flag for safety. Performs pre-flight checks by default.
    """
    if not confirm:
        _emit_error(
            "CONFIRMATION_REQUIRED",
            "The --confirm flag is required to start a print. This prevents accidental prints in autonomous workflows.",
            json_mode,
            OTHER_ERROR,
        )

    client = _make_client(ctx.obj["host"], ctx.obj["api_key"], json_mode)

    # Check if already printing (for --skip-if-printing)
    if skip_if_printing:
        job_result = client.get_job()
        if job_result["success"]:
            state = (job_result["data"] or {}).get("state", "")
            if state.lower() in ("printing", "pausing", "paused"):
                if json_mode:
                    output = format_response(
                        "success",
                        data={
                            "action": "skipped",
                            "message": f"Printer is already {state}. Skipped.",
                            "current_state": state,
                        },
                        json_mode=True,
                    )
                else:
                    output = f"Printer is already {state}. Skipped (--skip-if-printing)."
                _emit(output, SUCCESS)

    # Pre-flight checks
    if not no_preflight:
        preflight = preflight_check(client, file_path=file_path)
        if not preflight["ready"]:
            if json_mode:
                output = format_response(
                    "error",
                    data=preflight,
                    error={
                        "code": "PREFLIGHT_FAILED",
                        "message": preflight["summary"],
                    },
                    json_mode=True,
                )
                _emit(output, OTHER_ERROR)
            else:
                _emit_error("PREFLIGHT_FAILED", preflight["summary"], json_mode)

    # Upload with select + print
    upload_result = client.upload_file(
        file_path,
        location="local",
        select=True,
        print_after=True,
    )
    _handle_api_error(upload_result, json_mode)

    output = format_job_action("start", upload_result.get("data"), json_mode=json_mode)
    _emit(output, SUCCESS)


# ------------------------------------------------------------------
# cancel
# ------------------------------------------------------------------


@cli.command()
@click.option("--confirm", is_flag=True, default=False, help="Required flag to confirm cancellation.")
@click.option("--json", "json_mode", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def cancel(ctx: click.Context, confirm: bool, json_mode: bool) -> None:
    """Cancel the current print job.

    Requires --confirm flag for safety.
    """
    if not confirm:
        _emit_error(
            "CONFIRMATION_REQUIRED",
            "The --confirm flag is required to cancel a print.",
            json_mode,
            OTHER_ERROR,
        )

    client = _make_client(ctx.obj["host"], ctx.obj["api_key"], json_mode)

    # Check if there's actually something to cancel
    cancel_check = check_can_cancel(client)
    if not cancel_check["can_cancel"]:
        _emit_error(
            "NO_ACTIVE_JOB",
            cancel_check["message"],
            json_mode,
            OTHER_ERROR,
        )

    result = client.cancel_job()
    _handle_api_error(result, json_mode)

    output = format_job_action("cancel", result.get("data"), json_mode=json_mode)
    _emit(output, SUCCESS)


# ------------------------------------------------------------------
# pause
# ------------------------------------------------------------------


@cli.command()
@click.option("--json", "json_mode", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def pause(ctx: click.Context, json_mode: bool) -> None:
    """Pause the current print job."""
    client = _make_client(ctx.obj["host"], ctx.obj["api_key"], json_mode)
    result = client.pause_job(action="pause")
    _handle_api_error(result, json_mode)

    output = format_job_action("pause", result.get("data"), json_mode=json_mode)
    _emit(output, SUCCESS)


# ------------------------------------------------------------------
# resume
# ------------------------------------------------------------------


@cli.command()
@click.option("--json", "json_mode", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def resume(ctx: click.Context, json_mode: bool) -> None:
    """Resume a paused print job."""
    client = _make_client(ctx.obj["host"], ctx.obj["api_key"], json_mode)
    result = client.pause_job(action="resume")
    _handle_api_error(result, json_mode)

    output = format_job_action("resume", result.get("data"), json_mode=json_mode)
    _emit(output, SUCCESS)


# ------------------------------------------------------------------
# preflight
# ------------------------------------------------------------------


@cli.command()
@click.argument("file_path", required=False, type=click.Path(), default=None)
@click.option("--json", "json_mode", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def preflight(ctx: click.Context, file_path: str | None, json_mode: bool) -> None:
    """Run pre-flight safety checks.

    Optionally pass a local G-code file path to include file validation.
    """
    client = _make_client(ctx.obj["host"], ctx.obj["api_key"], json_mode)
    result = preflight_check(client, file_path=file_path)

    if json_mode:
        status_str = "success" if result["ready"] else "error"
        error_data = None
        if not result["ready"]:
            error_data = {"code": "PREFLIGHT_FAILED", "message": result["summary"]}
        output = format_response(status_str, data=result, error=error_data, json_mode=True)
        exit_code = SUCCESS if result["ready"] else OTHER_ERROR
    else:
        if result["ready"]:
            output = f"Pre-flight checks PASSED: {result['summary']}"
        else:
            output = f"Pre-flight checks FAILED: {result['summary']}"
            # Print individual check details
            for check in result.get("printer", {}).get("checks", []):
                symbol = "+" if check["passed"] else "X"
                output += f"\n  [{symbol}] {check['name']}: {check['message']}"
            for warning in result.get("temperatures", {}).get("warnings", []):
                output += f"\n  [!] {warning}"
            if "file" in result:
                for err in result["file"].get("errors", []):
                    output += f"\n  [X] {err}"
                for w in result["file"].get("warnings", []):
                    output += f"\n  [!] {w}"
        exit_code = SUCCESS if result["ready"] else OTHER_ERROR

    _emit(output, exit_code)


# ------------------------------------------------------------------
# init
# ------------------------------------------------------------------


@cli.command()
@click.option("--host", prompt="OctoPrint host URL", help="OctoPrint server URL.")
@click.option("--api-key", prompt="OctoPrint API key", help="OctoPrint API key.")
def init(host: str, api_key: str) -> None:
    """Initialize configuration file (~/.octoprint-cli/config.yaml)."""
    path = init_config(host, api_key)
    click.echo(f"Configuration saved to {path}")


# ------------------------------------------------------------------
# connect / disconnect
# ------------------------------------------------------------------


@cli.command()
@click.option("--json", "json_mode", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def connect(ctx: click.Context, json_mode: bool) -> None:
    """Connect the printer to OctoPrint."""
    client = _make_client(ctx.obj["host"], ctx.obj["api_key"], json_mode)
    result = client._request("POST", "/api/connection", json={"command": "connect"})
    _handle_api_error(result, json_mode)

    if json_mode:
        output = format_response(
            "success", data={"action": "connect", "message": "Connection command sent."}, json_mode=True
        )
    else:
        output = "Connection command sent to printer."
    _emit(output, SUCCESS)


@cli.command()
@click.option("--json", "json_mode", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def disconnect(ctx: click.Context, json_mode: bool) -> None:
    """Disconnect the printer from OctoPrint."""
    client = _make_client(ctx.obj["host"], ctx.obj["api_key"], json_mode)
    result = client._request("POST", "/api/connection", json={"command": "disconnect"})
    _handle_api_error(result, json_mode)

    if json_mode:
        output = format_response(
            "success", data={"action": "disconnect", "message": "Disconnect command sent."}, json_mode=True
        )
    else:
        output = "Disconnect command sent to printer."
    _emit(output, SUCCESS)


# ------------------------------------------------------------------
# gcode
# ------------------------------------------------------------------


@cli.command()
@click.argument("commands", nargs=-1, required=True)
@click.option("--json", "json_mode", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def gcode(ctx: click.Context, commands: tuple, json_mode: bool) -> None:
    """Send G-code commands to the printer.

    Pass one or more G-code commands as arguments.
    Example: octoprint-cli gcode G28 "M104 S200"
    """
    client = _make_client(ctx.obj["host"], ctx.obj["api_key"], json_mode)
    result = client.send_gcode(list(commands))
    _handle_api_error(result, json_mode)

    if json_mode:
        output = format_response(
            "success",
            data={
                "action": "gcode",
                "commands": list(commands),
                "message": f"Sent {len(commands)} command(s).",
            },
            json_mode=True,
        )
    else:
        output = f"Sent {len(commands)} G-code command(s): {', '.join(commands)}"
    _emit(output, SUCCESS)


# ------------------------------------------------------------------
# temp
# ------------------------------------------------------------------


@cli.command()
@click.option("--tool", "tool_temp", type=float, default=None, help="Set tool/hotend target temp (C).")
@click.option("--bed", "bed_temp", type=float, default=None, help="Set bed target temp (C).")
@click.option("--off", is_flag=True, default=False, help="Turn off all heaters.")
@click.option("--json", "json_mode", is_flag=True, default=False, help="Output as JSON.")
@click.pass_context
def temp(
    ctx: click.Context,
    tool_temp: float | None,
    bed_temp: float | None,
    off: bool,
    json_mode: bool,
) -> None:
    """Get or set temperatures.

    With no options, shows current temperatures. Use --tool and --bed to set targets.
    """
    client = _make_client(ctx.obj["host"], ctx.obj["api_key"], json_mode)

    if off:
        tool_temp = 0
        bed_temp = 0

    # If setting temps
    actions_taken = []
    if tool_temp is not None:
        result = client.set_tool_temp({"tool0": int(tool_temp)})
        _handle_api_error(result, json_mode)
        actions_taken.append(f"tool0={int(tool_temp)}C")

    if bed_temp is not None:
        result = client.set_bed_temp(int(bed_temp))
        _handle_api_error(result, json_mode)
        actions_taken.append(f"bed={int(bed_temp)}C")

    if actions_taken:
        msg = "Temperature targets set: " + ", ".join(actions_taken)
        if json_mode:
            output = format_response(
                "success",
                data={
                    "action": "set_temperature",
                    "targets": {"tool0": tool_temp, "bed": bed_temp},
                    "message": msg,
                },
                json_mode=True,
            )
        else:
            output = msg
        _emit(output, SUCCESS)

    # Just display current temps
    printer_result = client.get_printer_state()
    _handle_api_error(printer_result, json_mode)

    temp_data = (printer_result["data"] or {}).get("temperature", {})
    if json_mode:
        output = format_response("success", data={"temperature": temp_data}, json_mode=True)
    else:
        from octoprint_cli.output import format_temp

        tool0 = temp_data.get("tool0", {})
        bed = temp_data.get("bed", {})
        output = (
            f"Hotend: {format_temp(tool0.get('actual'), tool0.get('target'))}\n"
            f"Bed:    {format_temp(bed.get('actual'), bed.get('target'))}"
        )
    _emit(output, SUCCESS)


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

if __name__ == "__main__":
    cli()
