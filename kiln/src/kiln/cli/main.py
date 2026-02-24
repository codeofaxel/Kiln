"""Kiln CLI — agent-friendly command-line interface for 3D printers.

Provides a unified ``kiln`` command with subcommands for printer discovery,
configuration, control, and monitoring.  Every subcommand supports a
``--json`` flag for machine-parseable output suitable for agent consumption.

The ``kiln serve`` subcommand starts the MCP server (original ``kiln``
behaviour).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from typing import Any

import click

from kiln.printers.base import PrinterError

# Exception types for typed catch handlers (prefer specific over blanket Exception)
try:
    from kiln.fulfillment.base import FulfillmentError
except ImportError:
    FulfillmentError = Exception  # type: ignore[misc,assignment]

try:
    from kiln.gateway.threedos import ThreeDOSError
except ImportError:
    ThreeDOSError = Exception  # type: ignore[misc,assignment]

try:
    from kiln.generation.base import GenerationError
except ImportError:
    GenerationError = Exception  # type: ignore[misc,assignment]

from kiln.cli.config import (
    list_printers as _list_printers,
)
from kiln.cli.config import (
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
    format_fleet_status,
    format_history,
    format_job_detail,
    format_materials,
    format_order,
    format_printers,
    format_queue_summary,
    format_quote,
    format_response,
    format_status,
)

logger = logging.getLogger(__name__)

_MATERIAL_CHOICES: tuple[str, ...] = ("PLA", "PETG", "ABS", "TPU", "ASA", "Nylon", "PC")
_MATERIAL_TEMPS: dict[str, tuple[int, int, int, int]] = {
    "PLA": (200, 210, 60, 60),
    "PETG": (240, 245, 80, 80),
    "ABS": (250, 255, 100, 100),
    "TPU": (225, 230, 50, 50),
    "ASA": (250, 255, 100, 100),
    "NYLON": (260, 265, 70, 70),
    "PC": (280, 285, 110, 110),
}
_SUPPORT_MODE_CHOICES: tuple[str, ...] = ("off", "auto", "minimal", "aggressive")


def _normalise_material_type(raw: str | None) -> str | None:
    """Normalize material names to canonical keys used by temp defaults."""
    if not raw:
        return None
    value = raw.strip().upper()
    aliases = {
        "PA": "NYLON",
        "PA6": "NYLON",
        "PA12": "NYLON",
    }
    value = aliases.get(value, value)
    if value in _MATERIAL_TEMPS:
        return value
    return None


def _material_profile_overrides(material: str) -> dict[str, str]:
    """Build slicer profile overrides for a material."""
    nozzle, first_nozzle, bed, first_bed = _MATERIAL_TEMPS[material]
    return {
        "temperature": str(nozzle),
        "first_layer_temperature": str(first_nozzle),
        "bed_temperature": str(bed),
        "first_layer_bed_temperature": str(first_bed),
    }


def _material_extra_args(material: str) -> list[str]:
    """Build CLI temperature args for slicers when no bundled profile is used."""
    nozzle, first_nozzle, bed, first_bed = _MATERIAL_TEMPS[material]
    return [
        "--temperature",
        str(nozzle),
        "--first-layer-temperature",
        str(first_nozzle),
        "--bed-temperature",
        str(bed),
        "--first-layer-bed-temperature",
        str(first_bed),
    ]


def _infer_default_material(ctx: click.Context) -> str:
    """Infer material from tracked state/env, falling back to PLA."""
    try:
        from kiln.materials import MaterialTracker
        from kiln.persistence import get_db

        printer_name = (ctx.obj or {}).get("printer") or "default"
        tracker = MaterialTracker(db=get_db())
        loaded = tracker.get_material(printer_name, tool_index=0)
        loaded_type = _normalise_material_type(getattr(loaded, "material_type", None))
        if loaded_type:
            return loaded_type
    except Exception as exc:
        logger.debug("Material tracker lookup failed: %s", exc)

    for env_name in ("KILN_MATERIAL", "KILN_DEFAULT_MATERIAL", "KILN_FILAMENT"):
        env_val = _normalise_material_type(os.environ.get(env_name))
        if env_val:
            return env_val

    return "PLA"


def _resolve_material_for_slice(ctx: click.Context, material: str | None) -> tuple[str, bool]:
    """Resolve the effective material and whether it was explicitly provided."""
    explicit = _normalise_material_type(material)
    if explicit:
        return explicit, True
    return _infer_default_material(ctx), False


def _support_profile_overrides(style: str) -> dict[str, str]:
    """Support overrides optimized for minimal waste on common PLA prints."""
    if style == "minimal":
        return {
            "support_material": "1",
            "support_material_buildplate_only": "1",
            "support_material_threshold": "55",
        }
    if style == "aggressive":
        return {
            "support_material": "1",
            "support_material_buildplate_only": "0",
        }
    return {}


def _support_extra_args(style: str) -> list[str]:
    """CLI support args used when slicing without a bundled profile."""
    if style == "minimal":
        return ["--support-material", "--support-material-buildplate-only"]
    if style == "aggressive":
        return ["--support-material"]
    return []


def _auto_support_style(input_file: str) -> tuple[str | None, str | None]:
    """Infer whether the model needs supports based on printability analysis."""
    ext = os.path.splitext(input_file)[1].lower()
    if ext not in {".stl", ".obj"}:
        return None, None

    try:
        from kiln.printability import analyze_printability

        report = analyze_printability(input_file)
        reasons: list[str] = []
        if report.overhangs.needs_supports and report.overhangs.overhang_percentage >= 1.0:
            reasons.append(f"overhangs={report.overhangs.overhang_percentage:.1f}%")
        if report.bridging.needs_supports_for_bridges:
            reasons.append(f"bridges={report.bridging.max_bridge_length_mm:.1f}mm")
        if reasons:
            return "minimal", ", ".join(reasons)
    except Exception as exc:
        logger.debug("Auto-support analysis failed for %s: %s", input_file, exc)

    return None, None


def _resolve_support_style(support_mode: str, input_file: str) -> tuple[str | None, str | None]:
    """Resolve support style from CLI mode and model analysis."""
    mode = (support_mode or "off").strip().lower()
    if mode == "off":
        return None, None
    if mode in {"minimal", "aggressive"}:
        return mode, "explicit"
    if mode == "auto":
        return _auto_support_style(input_file)
    return None, None


def _resolve_slice_plan(
    ctx: click.Context,
    *,
    input_file: str,
    profile: str | None,
    printer_id: str | None,
    material: str | None,
    support_mode: str,
) -> dict[str, Any]:
    """Compute profile path and slicer args for a slice operation."""
    from kiln.slicer_profiles import resolve_slicer_profile

    effective_printer_id = _map_printer_hint_to_profile_id(printer_id) or _autodetect_printer_profile_id(ctx)
    effective_profile = profile
    extra_args: list[str] = []

    material_key, material_is_explicit = _resolve_material_for_slice(ctx, material)
    support_style, support_reason = _resolve_support_style(support_mode, input_file)

    use_material_defaults = material_is_explicit or profile is None

    if effective_profile is None and effective_printer_id:
        try:
            overrides: dict[str, str] = {}
            if use_material_defaults:
                overrides.update(_material_profile_overrides(material_key))
            if support_style:
                overrides.update(_support_profile_overrides(support_style))
            effective_profile = resolve_slicer_profile(
                effective_printer_id,
                overrides=overrides or None,
            )
        except Exception as exc:
            logger.debug("Profile resolution failed for %s: %s", effective_printer_id, exc)

    # Fallback to direct CLI overrides when no bundled profile is active.
    if use_material_defaults and effective_profile is None:
        extra_args.extend(_material_extra_args(material_key))
    if support_style and effective_profile is None:
        extra_args.extend(_support_extra_args(support_style))

    return {
        "material": material_key,
        "material_explicit": material_is_explicit,
        "printer_id": effective_printer_id,
        "profile_path": effective_profile,
        "extra_args": extra_args,
        "support_style": support_style,
        "support_reason": support_reason,
    }


def _notify_preview_if_available(preview_path: str) -> bool:
    """Best-effort preview notification via optional env-configured hooks."""
    cmd_template = os.environ.get("KILN_PREVIEW_NOTIFY_CMD", "").strip()
    if cmd_template:
        import subprocess

        try:
            cmd = cmd_template.replace("{path}", preview_path)
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=20)
            if result.returncode == 0:
                return True
            logger.warning("Preview notify command failed (exit %d): %s", result.returncode, (result.stderr or "").strip())
        except Exception as exc:
            logger.warning("Preview notify command failed: %s", exc)

    webhook_url = os.environ.get("KILN_PREVIEW_NOTIFY_URL", "").strip()
    if webhook_url:
        import urllib.request

        payload = json.dumps({"preview_path": preview_path}).encode("utf-8")
        request = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return 200 <= getattr(response, "status", 0) < 300
        except Exception as exc:
            logger.warning("Preview notify webhook failed: %s", exc)

    return False


# ---------------------------------------------------------------------------
# Adapter factory
# ---------------------------------------------------------------------------


def _make_adapter(cfg: dict[str, Any]):
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
            raise click.ClickException("Bambu support requires paho-mqtt. Install it with: pip install paho-mqtt")
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
        raise click.ClickException(str(exc)) from exc

    ok, err = validate_printer_config(cfg)
    if not ok:
        ptype = cfg.get("type", "unknown")
        pname = printer_name or "(active)"
        hint = ""
        if "api_key" in (err or ""):
            hint = f"\n  Quick fix: kiln auth --name {pname} --host {cfg.get('host', 'HOST')} --type {ptype} --api-key YOUR_KEY"
        elif "access_code" in (err or "") or "serial" in (err or ""):
            hint = (
                f"\n  Quick fix: kiln auth --name {pname} --host {cfg.get('host', 'HOST')} --type bambu"
                " --access-code CODE --serial SERIAL"
            )
        elif "host" in (err or ""):
            hint = "\n  Quick fix: kiln setup"
        raise click.ClickException(f"Invalid printer config for {pname!r}: {err}{hint}")

    return _make_adapter(cfg)


def _map_printer_hint_to_profile_id(raw: str | None) -> str | None:
    """Map free-form printer model hints to bundled slicer profile IDs."""
    if not raw:
        return None
    hint = raw.strip().lower().replace("-", "_").replace(" ", "_")
    if not hint:
        return None
    hint_compact = hint.replace("_", "")

    if (
        hint in {"prusa_mini", "prusamini"}
        or hint_compact.startswith("prusamini")
        or ("prusa" in hint and "mini" in hint)
    ):
        return "prusa_mini"
    if "mk4" in hint:
        return "prusa_mk4"
    if "mk3" in hint:
        return "prusa_mk3s"
    if "prusa_xl" in hint or hint.endswith("_xl") or hint == "xl" or "prusa" in hint and "xl" in hint:
        return "prusa_xl"
    if "ender3" in hint_compact:
        return "ender3"
    if hint in {"klipper", "moonraker"}:
        return "klipper_generic"

    return None


def _extract_model_hints(payload: dict[str, Any]) -> list[str]:
    """Extract candidate model strings from backend payloads."""
    hints: list[str] = []
    keys = ("hostname", "printer_name", "name", "model", "type")
    seen: set[str] = set()

    def _add(value: Any) -> None:
        if not isinstance(value, str):
            return
        cleaned = value.strip()
        if not cleaned:
            return
        key = cleaned.lower()
        if key in seen:
            return
        seen.add(key)
        hints.append(cleaned)

    for key in keys:
        _add(payload.get(key))

    for parent in ("printer", "device", "system"):
        obj = payload.get(parent)
        if not isinstance(obj, dict):
            continue
        for key in keys:
            _add(obj.get(key))

    return hints


def _autodetect_printer_profile_id(ctx: click.Context) -> str | None:
    """Best-effort profile auto-detection from env, config, and backend APIs."""
    env_model = os.environ.get("KILN_PRINTER_MODEL")
    mapped = _map_printer_hint_to_profile_id(env_model)
    if mapped:
        return mapped

    try:
        cfg = load_printer_config(ctx.obj.get("printer"))
    except Exception:
        return None

    for key in ("printer_id", "printer_model", "model", "profile"):
        mapped = _map_printer_hint_to_profile_id(str(cfg.get(key, "") or ""))
        if mapped:
            return mapped

    ptype = str(cfg.get("type", "")).strip().lower()
    if ptype != "prusaconnect":
        return None

    # Prusa Link usually exposes printer identity under /api/v1/info,
    # but older/newer builds may only provide hints via /api/version.
    try:
        adapter = _make_adapter(cfg)
        for endpoint in ("/api/v1/info", "/api/version"):
            try:
                info = adapter._get_json(endpoint)  # type: ignore[attr-defined]
            except Exception as exc:
                logger.debug("Prusa autodetect endpoint %s failed: %s", endpoint, exc)
                continue
            for hint in _extract_model_hints(info):
                mapped = _map_printer_hint_to_profile_id(hint)
                if mapped:
                    return mapped
    except Exception as exc:
        logger.debug("Prusa profile autodetection failed: %s", exc)

    return None


def _run_prusa_diagnostics(cfg: dict[str, Any]) -> dict[str, Any]:
    """Run non-destructive diagnostics for a Prusa Link printer config."""
    checks: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "host": cfg.get("host", ""),
        "type": cfg.get("type", ""),
        "checks": checks,
        "model_hint": None,
        "profile_id": None,
        "storage_roots": {},
        "file_count": None,
        "ok": False,
    }

    if str(cfg.get("type", "")).lower() != "prusaconnect":
        checks.append(
            {
                "name": "backend",
                "ok": False,
                "detail": "Active printer is not type 'prusaconnect'.",
            }
        )
        return summary

    try:
        adapter = _make_adapter(cfg)
    except Exception as exc:
        checks.append({"name": "adapter", "ok": False, "detail": str(exc)})
        return summary

    # Basic status endpoint
    try:
        status = adapter._get_json("/api/v1/status")  # type: ignore[attr-defined]
        printer_state = (status.get("printer") or {}).get("state")
        checks.append(
            {
                "name": "api_status",
                "ok": True,
                "detail": f"/api/v1/status reachable (state={printer_state or 'unknown'})",
            }
        )
        status_ok = True
    except Exception as exc:
        checks.append({"name": "api_status", "ok": False, "detail": str(exc)})
        status_ok = False

    # Model hint from info/version endpoints
    model_hint: str | None = None
    info_ok = False
    info_endpoint: str | None = None
    last_info_error: Exception | None = None
    for endpoint in ("/api/v1/info", "/api/version"):
        try:
            payload = adapter._get_json(endpoint)  # type: ignore[attr-defined]
        except Exception as exc:
            last_info_error = exc
            continue

        info_ok = True
        info_endpoint = endpoint
        hints = _extract_model_hints(payload)
        if hints:
            model_hint = hints[0]
            for hint in hints:
                mapped = _map_printer_hint_to_profile_id(hint)
                if mapped:
                    summary["model_hint"] = hint
                    summary["profile_id"] = mapped
                    break
            else:
                summary["model_hint"] = model_hint
        if summary.get("profile_id"):
            break

    if info_ok:
        checks.append(
            {
                "name": "api_info",
                "ok": True,
                "detail": (
                    f"{info_endpoint} reachable (model='{summary.get('model_hint')}', "
                    f"profile='{summary.get('profile_id')}')"
                    if summary.get("model_hint")
                    else f"{info_endpoint} reachable (model unknown)"
                ),
            }
        )
    else:
        checks.append(
            {
                "name": "api_info",
                "ok": False,
                "detail": str(last_info_error) if last_info_error else "No model endpoint reachable",
            }
        )

    # Storage roots
    root_ok = False
    for root in ("usb", "local"):
        try:
            payload = adapter._get_json(f"/api/v1/files/{root}")  # type: ignore[attr-defined]
            children = payload.get("children", [])
            count = len(children) if isinstance(children, list) else 0
            summary["storage_roots"][root] = {"ok": True, "entries": count}
            checks.append(
                {
                    "name": f"storage_{root}",
                    "ok": True,
                    "detail": f"/api/v1/files/{root} reachable ({count} top-level entries)",
                }
            )
            root_ok = True
        except Exception as exc:
            summary["storage_roots"][root] = {"ok": False, "error": str(exc)}
            checks.append(
                {
                    "name": f"storage_{root}",
                    "ok": False,
                    "detail": str(exc),
                    "warn": True,
                }
            )

    # Unified list-files + path-resolution check
    try:
        files = adapter.list_files()
        summary["file_count"] = len(files)
        detail = f"{len(files)} file(s) visible via Kiln adapter"
        if files:
            sample = files[0]
            detail += f"; sample: name='{sample.name}' path='{sample.path}'"
        checks.append({"name": "adapter_files", "ok": True, "detail": detail})
    except Exception as exc:
        checks.append({"name": "adapter_files", "ok": False, "detail": str(exc)})

    summary["ok"] = status_ok and root_ok
    # api_info/model detection is advisory; not required for core connectivity.
    summary["model_detected"] = info_ok and bool(summary.get("profile_id"))
    return summary


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


@click.group()
@click.option(
    "--printer",
    "-p",
    default=None,
    envvar="KILN_PRINTER",
    help="Printer name to use (overrides active printer).",
)
@click.version_option(package_name="kiln3d")
@click.pass_context
def cli(ctx: click.Context, printer: str | None) -> None:
    """Kiln — agent-friendly 3D printer control."""
    ctx.ensure_object(dict)
    ctx.obj["printer"] = printer

    # Soft nag if terms haven't been accepted yet (non-blocking).
    # Only shown in interactive terminals — never in piped/agent output.
    try:
        if sys.stderr.isatty():
            from kiln.terms import is_current

            if not is_current():
                invoked = ctx.invoked_subcommand
                if invoked not in ("setup", None):
                    click.echo(
                        click.style(
                            "  Note: Terms of use not yet accepted. Run 'kiln setup' to review and accept.",
                            fg="yellow",
                        ),
                        err=True,
                    )
    except Exception as exc:
        logger.debug("DB not initialised yet — skipping startup check: %s", exc)


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--timeout", "-t", default=5.0, help="Scan duration in seconds.")
@click.option(
    "--subnet",
    "-s",
    default=None,
    help="Subnet to scan (e.g. '192.168.1'). Auto-detected if omitted.",
)
@click.option(
    "--method",
    "-m",
    "methods",
    multiple=True,
    type=click.Choice(["mdns", "http_probe"]),
    help="Discovery method(s) to use (repeatable). Default: mdns + http_probe.",
)
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def discover(timeout: float, subnet: str | None, methods: tuple, json_mode: bool) -> None:
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
    except OSError as exc:
        click.echo(
            format_error(
                f"Network discovery failed: {exc}. Check network connectivity and try 'kiln discover --method http_probe'.",
                code="DISCOVERY_ERROR",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            format_error(
                f"Network discovery failed: {exc}. Check network connectivity and try 'kiln discover --method http_probe'.",
                code="DISCOVERY_ERROR",
                json_mode=json_mode,
            )
        )
        sys.exit(1)

    click.echo(format_discovered([p.to_dict() for p in found], json_mode=json_mode))

    if not json_mode and not found:
        click.echo(
            "\nTip: Discovery may miss printers on some networks. "
            "Use 'kiln auth' with the printer IP (works for Ethernet and Wi-Fi)."
        )


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--name", "-n", required=True, help="Name for this printer (e.g. 'voron').")
@click.option("--host", "-h", required=True, help="Printer URL or IP (e.g. http://octopi.local).")
@click.option(
    "--type",
    "printer_type",
    required=True,
    type=click.Choice(["octoprint", "moonraker", "bambu", "elegoo", "prusaconnect"]),
    help="Printer backend type.",
)
@click.option("--api-key", default=None, help="API key (OctoPrint/Moonraker/Prusa Link).")
@click.option("--access-code", default=None, help="LAN access code (Bambu).")
@click.option("--serial", default=None, help="Printer serial number (Bambu) or mainboard ID (Elegoo).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def auth(
    name: str,
    host: str,
    printer_type: str,
    api_key: str | None,
    access_code: str | None,
    serial: str | None,
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
        prusa_diagnostics: dict[str, Any] | None = None
        if printer_type == "prusaconnect":
            try:
                cfg = load_printer_config(name)
                prusa_diagnostics = _run_prusa_diagnostics(cfg)
                detected_profile = prusa_diagnostics.get("profile_id")
                if isinstance(detected_profile, str) and detected_profile:
                    # Persist detected model profile for better slicing defaults.
                    save_printer(
                        name,
                        printer_type,
                        host,
                        api_key=api_key,
                        access_code=access_code,
                        serial=serial,
                        printer_model=detected_profile,
                    )
            except Exception as exc:
                logger.debug("Prusa diagnostics after auth failed: %s", exc)

        data = {
            "name": name,
            "type": printer_type,
            "host": host,
            "config_path": str(path),
        }
        if prusa_diagnostics is not None:
            data["diagnostics"] = prusa_diagnostics

        if printer_type == "prusaconnect" and prusa_diagnostics is not None and not prusa_diagnostics.get("ok", False):
            checks = prusa_diagnostics.get("checks", [])
            failed_checks = [
                c.get("name", "unknown")
                for c in checks
                if isinstance(c, dict) and not c.get("ok", False) and not c.get("warn", False)
            ]
            failed_summary = ", ".join(failed_checks) if failed_checks else "connectivity checks"
            message = (
                "Saved printer credentials, but Prusa connectivity diagnostics failed "
                f"({failed_summary}). Run 'kiln doctor-prusa --json' for details."
            )
            if json_mode:
                click.echo(
                    format_response(
                        "error",
                        data=data,
                        error={"code": "PRUSA_DIAGNOSTICS_FAILED", "message": message},
                        json_mode=True,
                    )
                )
            else:
                click.echo(format_error(message, code="PRUSA_DIAGNOSTICS_FAILED", json_mode=False))
            sys.exit(1)

        click.echo(format_response("success", data=data, json_mode=json_mode))
        if not json_mode and prusa_diagnostics is not None:
            profile_id = prusa_diagnostics.get("profile_id")
            file_count = prusa_diagnostics.get("file_count")
            checks = prusa_diagnostics.get("checks", [])
            root_ok = any(
                c.get("name", "").startswith("storage_") and c.get("ok") for c in checks if isinstance(c, dict)
            )
            if profile_id:
                click.echo(f"Detected printer profile: {profile_id}")
            elif prusa_diagnostics.get("model_hint"):
                click.echo(f"Detected model hint: {prusa_diagnostics.get('model_hint')}")
            if file_count is not None:
                click.echo(f"Files visible through Kiln: {file_count}")
            if not root_ok:
                click.echo("Storage roots not reachable yet. Verify API key and run: kiln doctor-prusa")
            else:
                click.echo("Prusa connectivity check passed. Run: kiln doctor-prusa for full diagnostics.")
    except OSError as exc:
        click.echo(
            format_error(
                f"Failed to save printer credentials: {exc}. Check file permissions on ~/.kiln/",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to save printer credentials: {exc}",
                json_mode=json_mode,
            )
        )
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

        # Enrich JSON output with printer context so agents get everything in one call
        extra: dict = {}
        if json_mode:
            try:
                cfg = load_printer_config(ctx.obj.get("printer"))
                extra["printer_name"] = ctx.obj.get("printer") or "default"
                extra["printer_type"] = cfg.get("type", "unknown")
            except Exception as exc:
                logger.debug("Failed to enrich printer info: %s", exc)  # Best-effort enrichment

        click.echo(format_status(state.to_dict(), job.to_dict(), json_mode=json_mode, extra=extra))
    except click.ClickException:
        raise
    except PrinterError as exc:
        click.echo(
            format_error(
                f"Failed to get printer status: {exc}. Verify the printer is online and credentials are correct.",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to get printer status: {exc}",
                json_mode=json_mode,
            )
        )
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
    except PrinterError as exc:
        click.echo(
            format_error(
                f"Failed to list printer files: {exc}. Verify the printer is online.",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to list printer files: {exc}",
                json_mode=json_mode,
            )
        )
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
    except PrinterError as exc:
        click.echo(
            format_error(
                f"Failed to upload file '{file_path}': {exc}",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to upload file '{file_path}': {exc}",
                json_mode=json_mode,
            )
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--file", "-f", "file_path", default=None, type=click.Path(), help="Local G-code file to validate.")
@click.option(
    "--material",
    "-m",
    default=None,
    type=click.Choice(["PLA", "PETG", "ABS", "TPU", "ASA", "Nylon", "PC"]),
    help="Expected material — validates temps match.",
)
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def preflight(ctx: click.Context, file_path: str | None, material: str | None, json_mode: bool) -> None:
    """Run pre-print safety checks.

    Validates printer state, temperatures, and connectivity.
    Optionally validates a local G-code file with --file.
    Use --material to verify temperatures match the filament type.
    """
    from kiln.printers.base import PrinterStatus

    # Material temperature ranges (tool_min, tool_max, bed_min, bed_max)
    _MATERIAL_TEMPS: dict[str, tuple] = {
        "PLA": (180, 220, 40, 70),
        "PETG": (220, 260, 60, 90),
        "ABS": (230, 270, 90, 115),
        "TPU": (210, 240, 30, 60),
        "ASA": (230, 270, 90, 115),
        "Nylon": (240, 280, 60, 80),
        "PC": (260, 310, 90, 120),
    }

    try:
        adapter = _get_adapter_from_ctx(ctx)
        state = adapter.get_state()

        checks: list = []
        errors: list = []

        # Connected
        checks.append(
            {
                "name": "printer_connected",
                "passed": state.connected,
                "message": "Printer is connected" if state.connected else "Printer is offline",
            }
        )
        if not state.connected:
            errors.append("Printer is not connected / offline")

        # Idle
        is_idle = state.state == PrinterStatus.IDLE
        checks.append(
            {
                "name": "printer_idle",
                "passed": is_idle,
                "message": f"Printer state: {state.state.value}",
            }
        )
        if not is_idle:
            errors.append(f"Printer is not idle (state: {state.state.value})")

        # No error
        no_error = state.state != PrinterStatus.ERROR
        checks.append(
            {
                "name": "no_errors",
                "passed": no_error,
                "message": "No errors" if no_error else "Printer is in error state",
            }
        )
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
        checks.append(
            {
                "name": "temperatures_safe",
                "passed": temps_safe,
                "message": "Temperatures within limits" if temps_safe else "; ".join(temp_warnings),
            }
        )
        if not temps_safe:
            errors.extend(temp_warnings)

        # Material check (optional)
        if material:
            mat_range = _MATERIAL_TEMPS.get(material)
            if mat_range:
                tool_min, tool_max, bed_min, bed_max = mat_range
                mat_warnings: list = []

                if (
                    state.tool_temp_target is not None
                    and state.tool_temp_target > 0
                    and not (tool_min <= state.tool_temp_target <= tool_max)
                ):
                    mat_warnings.append(
                        f"Tool target ({state.tool_temp_target:.0f}C) outside {material} range ({tool_min}-{tool_max}C)"
                    )

                if (
                    state.bed_temp_target is not None
                    and state.bed_temp_target > 0
                    and not (bed_min <= state.bed_temp_target <= bed_max)
                ):
                    mat_warnings.append(
                        f"Bed target ({state.bed_temp_target:.0f}C) outside {material} range ({bed_min}-{bed_max}C)"
                    )

                mat_ok = len(mat_warnings) == 0
                checks.append(
                    {
                        "name": "material_match",
                        "passed": mat_ok,
                        "message": f"{material} temps OK" if mat_ok else "; ".join(mat_warnings),
                    }
                )
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
            checks.append(
                {
                    "name": "file_valid",
                    "passed": file_ok,
                    "message": "File OK" if file_ok else "; ".join(file_errors),
                }
            )
            if not file_ok:
                errors.extend(file_errors)

        ready = all(c["passed"] for c in checks)

        if json_mode:
            import json

            click.echo(
                json.dumps(
                    {
                        "status": "success",
                        "data": {
                            "ready": ready,
                            "checks": checks,
                            "errors": errors,
                        },
                    },
                    indent=2,
                )
            )
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
@click.option("--skip-preflight", is_flag=True, help="Skip automatic pre-print safety checks.")
@click.option("--dry-run", is_flag=True, help="Preview what would happen without actually printing.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def print_cmd(
    ctx: click.Context,
    files: tuple,
    show_status: bool,
    use_queue: bool,
    skip_preflight: bool,
    dry_run: bool,
    json_mode: bool,
) -> None:
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
            import json as _json
            import uuid

            from kiln.persistence import get_db

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
                        click.echo(
                            format_error(
                                f"Failed to upload {f}: {upload_result.message}",
                                code="UPLOAD_FAILED",
                                json_mode=json_mode,
                            )
                        )
                        continue
                    file_name = upload_result.file_name or os.path.basename(f)

                job_id = str(uuid.uuid4())[:8]
                db.save_job(
                    {
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
                    }
                )
                queued.append({"job_id": job_id, "file_name": file_name})

            if json_mode:
                click.echo(
                    _json.dumps(
                        {
                            "status": "success",
                            "data": {"queued": queued, "count": len(queued)},
                        },
                        indent=2,
                    )
                )
            else:
                click.echo(f"Queued {len(queued)} file(s) for sequential printing.")
                for q in queued:
                    click.echo(f"  {q['job_id']}: {q['file_name']}")
            return

        # Single file (or first of batch without --queue)
        if len(expanded) > 1 and not use_queue and not json_mode:
            click.echo(f"Printing {len(expanded)} files sequentially (use --queue for background)...")

        # Auto-preflight: check printer is ready before starting
        _preflight_state = None
        if not skip_preflight:
            try:
                state = adapter.get_state()
                _preflight_state = state
                preflight_errors = []
                preflight_warnings = []
                if state.state.value in ("error", "offline"):
                    preflight_errors.append(f"Printer is {state.state.value}")
                if state.tool_temp_actual is not None and state.tool_temp_actual > 50 and state.state.value == "idle":
                    preflight_warnings.append(f"Hotend is already warm ({state.tool_temp_actual:.0f}°C) while idle")
                if preflight_errors:
                    msg = "Pre-flight check failed: " + "; ".join(preflight_errors)
                    click.echo(format_error(msg, code="PREFLIGHT_FAILED", json_mode=json_mode))
                    if not json_mode:
                        click.echo("Use --skip-preflight to bypass.")
                    sys.exit(1)
                if not json_mode:
                    for warning in preflight_warnings:
                        click.echo(f"Pre-flight advisory: {warning}")
                    click.echo("Pre-flight ✓")
            except Exception as exc:
                logger.debug("Preflight check itself failed: %s", exc)  # Don't block printing if preflight itself fails

        # Dry-run: show what would happen without actually printing
        if dry_run:
            import json as _json

            summary = {
                "dry_run": True,
                "files": [os.path.basename(f) for f in expanded],
                "local_upload_needed": [f for f in expanded if os.path.isfile(f)],
                "preflight": "passed" if not skip_preflight else "skipped",
                "printer_status": _preflight_state.state.value if _preflight_state else "unknown",
                "action": "Would start printing" if len(expanded) == 1 else f"Would print {len(expanded)} files",
            }
            if json_mode:
                click.echo(_json.dumps(summary, indent=2))
            else:
                click.echo("Dry run — no actions taken:")
                click.echo(f"  Files: {', '.join(summary['files'])}")
                uploads = summary["local_upload_needed"]
                if uploads:
                    click.echo(f"  Would upload: {', '.join(os.path.basename(u) for u in uploads)}")
                click.echo(f"  Preflight: {summary['preflight']}")
                click.echo(f"  Action: {summary['action']}")
            return

        for i, f in enumerate(expanded):
            file_name = f
            if os.path.isfile(f):
                if not json_mode:
                    click.echo(f"Uploading {f}...")
                upload_result = adapter.upload_file(f)
                if not upload_result.success:
                    click.echo(
                        format_error(
                            upload_result.message
                            or f"Upload failed for '{f}' — check printer storage and connectivity",
                            code="UPLOAD_FAILED",
                            json_mode=json_mode,
                        )
                    )
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
        click.echo(
            format_error(
                f"Print operation failed: {exc}",
                json_mode=json_mode,
            )
        )
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
    except PrinterError as exc:
        click.echo(
            format_error(
                f"Failed to cancel print: {exc}. Is a print currently running?",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to cancel print: {exc}",
                json_mode=json_mode,
            )
        )
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
    except PrinterError as exc:
        click.echo(
            format_error(
                f"Failed to pause print: {exc}. Is a print currently running?",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to pause print: {exc}",
                json_mode=json_mode,
            )
        )
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
    except PrinterError as exc:
        click.echo(
            format_error(
                f"Failed to resume print: {exc}. Is the print currently paused?",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to resume print: {exc}",
                json_mode=json_mode,
            )
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# temp
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--tool", "tool_temp", type=float, default=None, help="Set hotend temperature (°C).")
@click.option("--bed", "bed_temp", type=float, default=None, help="Set bed temperature (°C).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def temp(ctx: click.Context, tool_temp: float | None, bed_temp: float | None, json_mode: bool) -> None:
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

        results: dict[str, Any] = {}
        if tool_temp is not None:
            if tool_temp < 0 or tool_temp > 300:
                click.echo(
                    format_error(
                        f"Hotend temperature {tool_temp}°C out of safe range (0-300°C).",
                        json_mode=json_mode,
                    )
                )
                sys.exit(1)
            adapter.set_tool_temp(tool_temp)
            results["tool_target"] = tool_temp
        if bed_temp is not None:
            if bed_temp < 0 or bed_temp > 130:
                click.echo(
                    format_error(
                        f"Bed temperature {bed_temp}°C out of safe range (0-130°C).",
                        json_mode=json_mode,
                    )
                )
                sys.exit(1)
            adapter.set_bed_temp(bed_temp)
            results["bed_target"] = bed_temp

        click.echo(format_response("success", data=results, json_mode=json_mode))
    except click.ClickException:
        raise
    except PrinterError as exc:
        click.echo(
            format_error(
                f"Failed to set temperature: {exc}. Verify the printer is online and idle.",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to set temperature: {exc}",
                json_mode=json_mode,
            )
        )
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
            click.echo(
                format_error(
                    "G-code blocked by safety validator: " + "; ".join(validation.errors),
                    code="GCODE_BLOCKED",
                    json_mode=json_mode,
                )
            )
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
    except PrinterError as exc:
        click.echo(
            format_error(
                f"Failed to send G-code: {exc}. Verify the printer is online and ready.",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to send G-code: {exc}",
                json_mode=json_mode,
            )
        )
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
        raise click.ClickException(str(exc)) from exc


@cli.command("remove")
@click.argument("name")
def remove(name: str) -> None:
    """Remove a saved printer from the config."""
    try:
        remove_printer(name)
        click.echo(f"Removed printer '{name}'.")
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


# ---------------------------------------------------------------------------
# slice
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("input_file", type=click.Path(exists=True))
@click.option("--output-dir", "-o", default=None, help="Output directory (default: system temp dir).")
@click.option("--output-name", default=None, help="Override output file name.")
@click.option("--profile", "-P", default=None, type=click.Path(), help="Slicer profile file (.ini/.json).")
@click.option(
    "--printer-id", default=None, help="Printer model ID for bundled profile auto-selection (e.g. prusa_mini)."
)
@click.option("--slicer", default=None, help="Explicit path to slicer binary.")
@click.option(
    "--material",
    "-m",
    default=None,
    type=click.Choice(_MATERIAL_CHOICES),
    help="Material type (defaults to loaded material, then PLA).",
)
@click.option(
    "--support-mode",
    default="auto",
    show_default=True,
    type=click.Choice(_SUPPORT_MODE_CHOICES),
    help="Support strategy: off, auto, minimal (buildplate-only), or aggressive.",
)
@click.option("--print-after", is_flag=True, help="Upload and start printing after slicing.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def slice(
    ctx: click.Context,
    input_file: str,
    output_dir: str | None,
    output_name: str | None,
    profile: str | None,
    printer_id: str | None,
    slicer: str | None,
    material: str | None,
    support_mode: str,
    print_after: bool,
    json_mode: bool,
) -> None:
    """Slice a 3D model (STL/3MF/STEP) to G-code.

    Uses PrusaSlicer or OrcaSlicer CLI.  The slicer binary is auto-detected
    on PATH or can be specified with --slicer.

    With --print-after, the sliced G-code is uploaded and printing starts
    immediately.
    """
    from kiln.slicer import SlicerError, SlicerNotFoundError, slice_file

    try:
        plan = _resolve_slice_plan(
            ctx,
            input_file=input_file,
            profile=profile,
            printer_id=printer_id,
            material=material,
            support_mode=support_mode,
        )

        result = slice_file(
            input_file,
            output_dir=output_dir,
            output_name=output_name,
            profile=plan["profile_path"],
            slicer_path=slicer,
            extra_args=plan["extra_args"] or None,
        )

        if not print_after:
            if json_mode:
                import json as _json

                payload = result.to_dict()
                if plan["printer_id"]:
                    payload["printer_id"] = plan["printer_id"]
                if plan["profile_path"]:
                    payload["profile_path"] = plan["profile_path"]
                payload["material"] = plan["material"]
                payload["support_mode"] = support_mode
                if plan["support_style"]:
                    payload["support_style"] = plan["support_style"]
                if plan["support_reason"]:
                    payload["support_reason"] = plan["support_reason"]
                click.echo(_json.dumps({"status": "success", "data": payload}, indent=2))
            else:
                click.echo(result.message)
                click.echo(f"Output: {result.output_path}")
                click.echo(f"Material: {plan['material']}")
                if plan["printer_id"]:
                    click.echo(f"Profile: {plan['printer_id']}")
                if plan["support_style"]:
                    note = f" ({plan['support_reason']})" if plan["support_reason"] else ""
                    click.echo(f"Supports: {plan['support_style']}{note}")
            return

        # --print-after: upload and start
        adapter = _get_adapter_from_ctx(ctx)
        if not json_mode:
            click.echo(result.message)
            click.echo(f"Uploading {result.output_path}...")

        upload_result = adapter.upload_file(result.output_path)
        if not upload_result.success:
            click.echo(
                format_error(
                    upload_result.message or "Upload failed",
                    code="UPLOAD_FAILED",
                    json_mode=json_mode,
                )
            )
            sys.exit(1)

        import os

        file_name = upload_result.file_name or os.path.basename(result.output_path)
        print_result = adapter.start_print(file_name)

        if json_mode:
            import json as _json

            click.echo(
                _json.dumps(
                    {
                        "status": "success",
                        "data": {
                            "slice": result.to_dict(),
                            "upload": upload_result.to_dict(),
                            "print": print_result.to_dict(),
                        },
                    },
                    indent=2,
                )
            )
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
    except PrinterError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


def _fetch_external_snapshot(source: str) -> bytes | None:
    """Fetch a snapshot from an external camera source.

    Supports HTTP/HTTPS URLs and shell commands prefixed with ``cmd:``.
    """
    import subprocess

    if source.startswith(("http://", "https://")):
        import urllib.request

        try:
            with urllib.request.urlopen(source, timeout=10) as resp:
                return resp.read()
        except Exception as exc:
            logger.warning("External snapshot fetch failed: %s", exc)
            return None
    elif source.startswith("cmd:"):
        cmd = source[4:].strip()
        if not cmd:
            return None
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                timeout=30,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout
            logger.warning("External snapshot command failed (exit %d)", result.returncode)
            return None
        except subprocess.TimeoutExpired:
            logger.warning("External snapshot command timed out")
            return None
        except OSError as exc:
            logger.warning("External snapshot command error: %s", exc)
            return None
    else:
        logger.warning("Unknown snapshot source format: %s", source)
        return None


@cli.command()
@click.option("--output", "-o", default=None, type=click.Path(), help="Save snapshot to file.")
@click.option(
    "--source",
    "-s",
    default=None,
    help="External camera source: URL (http://...) or shell command (cmd:ffmpeg ...).",
)
@click.option("--json", "json_mode", is_flag=True, help="Output JSON (base64 encoded).")
@click.pass_context
def snapshot(ctx: click.Context, output: str | None, source: str | None, json_mode: bool) -> None:
    """Capture a webcam snapshot from the printer.

    Saves the image to a file (--output) or prints base64-encoded data
    in JSON mode.  Supports OctoPrint and Moonraker webcams.
    """
    import base64

    try:
        if source is None:
            source = os.environ.get("KILN_CAMERA_SOURCE", "").strip() or None

        if source:
            image_data = _fetch_external_snapshot(source)
        else:
            adapter = _get_adapter_from_ctx(ctx)
            image_data = adapter.get_snapshot()

        if image_data is None:
            click.echo(
                format_error(
                    "Webcam not available or not supported by this printer.",
                    code="NO_WEBCAM",
                    json_mode=json_mode,
                )
            )
            sys.exit(1)

        if output:
            _safe = os.path.realpath(output)
            _home = os.path.expanduser("~")
            _tmpdir = os.path.realpath(tempfile.gettempdir())
            _allowed_prefixes = (_home, _tmpdir)
            if not any(_safe.startswith(p) for p in _allowed_prefixes):
                click.echo(
                    format_error(
                        "Output path must be under home directory or a temp directory.",
                        code="VALIDATION_ERROR",
                        json_mode=json_mode,
                    )
                )
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

            click.echo(
                _json.dumps(
                    {
                        "status": "success",
                        "data": {
                            "image_base64": base64.b64encode(image_data).decode("ascii"),
                            "size_bytes": len(image_data),
                        },
                    },
                    indent=2,
                )
            )
        else:
            default_path = os.path.join(os.path.expanduser("~"), ".kiln", "snapshots", "kiln_snapshot.jpg")
            os.makedirs(os.path.dirname(default_path), exist_ok=True)
            with open(default_path, "wb") as f:
                f.write(image_data)
            click.echo(f"Snapshot saved to {default_path} ({len(image_data)} bytes)")

    except click.ClickException:
        raise
    except PrinterError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# wait
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--interval", "-i", default=5.0, help="Poll interval in seconds (default 5).")
@click.option(
    "--timeout", "-t", "max_timeout", default=0, type=float, help="Maximum wait time in seconds (0 = unlimited)."
)
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
                    click.echo(
                        format_response(
                            "error",
                            error={"code": "PRINT_FAILED", "message": f"Printer entered {state.state.value} state"},
                            json_mode=True,
                        )
                    )
                else:
                    click.echo(f"Print ended with state: {state.state.value}")
                sys.exit(1)

            # Still printing/paused — show progress
            if not json_mode and job.completion is not None:
                from kiln.cli.output import progress_bar

                click.echo(f"\r  {progress_bar(job.completion)}  ", nl=False)

            # Timeout check
            if max_timeout > 0 and (_time.time() - start) >= max_timeout:
                click.echo(
                    format_error(
                        f"Timed out after {max_timeout}s",
                        code="TIMEOUT",
                        json_mode=json_mode,
                    )
                )
                sys.exit(1)

            _time.sleep(interval)

    except KeyboardInterrupt:
        if not json_mode:
            click.echo("\nInterrupted.")
        sys.exit(130)
    except click.ClickException:
        raise
    except PrinterError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--limit", "-n", default=20, help="Number of records (default 20).")
@click.option(
    "--status",
    "-s",
    "filter_status",
    default=None,
    type=click.Choice(["completed", "failed", "cancelled"]),
    help="Filter by job status.",
)
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def history(limit: int, filter_status: str | None, json_mode: bool) -> None:
    """Show print history from the local database.

    Displays past print jobs with status, duration, and timestamps.
    """
    try:
        from kiln.persistence import get_db

        db = get_db()
        jobs = db.list_jobs(status=filter_status, limit=min(limit, 100))

        click.echo(format_history(jobs, json_mode=json_mode))

    except OSError as exc:
        click.echo(
            format_error(
                f"Failed to read print history: {exc}. Check database at ~/.kiln/kiln.db",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to read print history: {exc}",
                json_mode=json_mode,
            )
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# order (fulfillment services)
# ---------------------------------------------------------------------------


@cli.group()
def order() -> None:
    """Outsource prints to external manufacturing services.

    Use subcommands to get quotes, place orders, and track shipments
    through services like Craftcloud and Sculpteo.
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
    except FulfillmentError as exc:
        click.echo(
            format_error(
                f"Failed to list fulfillment materials: {exc}",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to list fulfillment materials: {exc}",
                json_mode=json_mode,
            )
        )
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
    from kiln.billing import BillingLedger
    from kiln.fulfillment import QuoteRequest

    try:
        provider = _get_fulfillment_provider()
        quote = provider.get_quote(
            QuoteRequest(
                file_path=file_path,
                material_id=material,
                quantity=quantity,
                shipping_country=country,
            )
        )
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
    except FulfillmentError as exc:
        click.echo(
            format_error(
                f"Quote request failed: {exc}. Verify the material ID with 'kiln order materials'.",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            format_error(
                f"Quote request failed: {exc}",
                json_mode=json_mode,
            )
        )
        sys.exit(1)


@order.command("place")
@click.argument("quote_id")
@click.option("--shipping", "-s", "shipping_id", default="", help="Shipping option ID (from quote).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def order_place(quote_id: str, shipping_id: str, json_mode: bool) -> None:
    """Place a manufacturing order from a quote.

    Requires a quote ID from 'kiln order quote'.
    """
    from kiln.billing import BillingLedger
    from kiln.fulfillment import OrderRequest
    from kiln.payments.base import PaymentError
    from kiln.payments.manager import PaymentManager
    from kiln.persistence import get_db

    try:
        provider = _get_fulfillment_provider()
        result = provider.place_order(
            OrderRequest(
                quote_id=quote_id,
                shipping_option_id=shipping_id,
            )
        )
        order_data = result.to_dict()
        if result.total_price and result.total_price > 0:
            ledger = BillingLedger(db=get_db())
            fee_calc = ledger.calculate_fee(
                result.total_price,
                currency=result.currency,
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
                    result.order_id,
                    fee_calc,
                    payment_status="failed",
                )
                order_data["payment"] = {"status": "failed"}
            order_data["kiln_fee"] = fee_calc.to_dict()
            order_data["total_with_fee"] = fee_calc.total_cost
        click.echo(format_order(order_data, json_mode=json_mode))
    except click.ClickException:
        raise
    except FulfillmentError as exc:
        click.echo(
            format_error(
                f"Failed to place order: {exc}. Verify the quote ID from 'kiln order quote'.",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to place order: {exc}",
                json_mode=json_mode,
            )
        )
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
    except FulfillmentError as exc:
        click.echo(
            format_error(
                f"Failed to get order status for {order_id!r}: {exc}",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to get order status for {order_id!r}: {exc}",
                json_mode=json_mode,
            )
        )
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
    except FulfillmentError as exc:
        click.echo(
            format_error(
                f"Failed to cancel order {order_id!r}: {exc}. The order may no longer be cancellable.",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to cancel order {order_id!r}: {exc}",
                json_mode=json_mode,
            )
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Consumer workflow commands — for users without printers
# ---------------------------------------------------------------------------


@order.command("recommend")
@click.argument("use_case")
@click.option("--budget", type=click.Choice(["budget", "mid", "premium"]), default=None, help="Price tier preference.")
@click.option("--weather-resistant", is_flag=True, help="Filter to weather-resistant materials.")
@click.option("--food-safe", is_flag=True, help="Filter to food-safe materials.")
@click.option("--high-detail", is_flag=True, help="Prefer high-detail materials.")
@click.option("--high-strength", is_flag=True, help="Prefer high-strength materials.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def order_recommend(
    use_case: str,
    budget: str | None,
    weather_resistant: bool,
    food_safe: bool,
    high_detail: bool,
    high_strength: bool,
    json_mode: bool,
) -> None:
    """Recommend the best material for your use case.

    USE_CASE: decorative, functional, mechanical, prototype, miniature,
    jewelry, enclosure, wearable, outdoor, food_safe.
    """
    from kiln.consumer import recommend_material

    try:
        guide = recommend_material(
            use_case,
            budget=budget,
            need_weather_resistant=weather_resistant,
            need_food_safe=food_safe,
            need_high_detail=high_detail,
            need_high_strength=high_strength,
        )
        data = guide.to_dict()
        if json_mode:
            click.echo(json.dumps({"status": "success", "data": data}, indent=2))
        else:
            click.echo(f"\n  Material Recommendation: {use_case}\n")
            click.echo(f"  Best pick: {guide.best_pick.material_name} ({guide.best_pick.technology})")
            click.echo(f"  Reason: {guide.best_pick.reason}")
            click.echo(f"  Price tier: {guide.best_pick.price_tier}")
            click.echo(f"  Provider: {guide.best_pick.recommended_provider}")
            click.echo(f"\n  {guide.explanation}\n")
            if len(guide.recommendations) > 1:
                click.echo("  Alternatives:")
                for r in guide.recommendations[1:]:
                    click.echo(f"    - {r.material_name} ({r.technology}) — {r.price_tier}: {r.reason}")
                click.echo()
    except ValueError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@order.command("estimate")
@click.argument("technology")
@click.option("--volume", type=float, default=None, help="Part volume in cm³.")
@click.option("--x", "dim_x", type=float, default=None, help="Bounding box X dimension (mm).")
@click.option("--y", "dim_y", type=float, default=None, help="Bounding box Y dimension (mm).")
@click.option("--z", "dim_z", type=float, default=None, help="Bounding box Z dimension (mm).")
@click.option("--quantity", "-q", default=1, help="Number of copies.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def order_estimate(
    technology: str,
    volume: float | None,
    dim_x: float | None,
    dim_y: float | None,
    dim_z: float | None,
    quantity: int,
    json_mode: bool,
) -> None:
    """Get an instant price estimate before requesting a full quote.

    TECHNOLOGY: FDM, SLA, SLS, MJF, or DMLS.

    Provide either --volume or --x --y --z dimensions.
    """
    from kiln.consumer import estimate_price

    try:
        dims = None
        if dim_x and dim_y and dim_z:
            dims = {"x": dim_x, "y": dim_y, "z": dim_z}
        result = estimate_price(
            technology,
            volume_cm3=volume,
            dimensions_mm=dims,
            quantity=quantity,
        )
        data = result.to_dict()
        if json_mode:
            click.echo(json.dumps({"status": "success", "data": data}, indent=2))
        else:
            click.echo(f"\n  Price Estimate ({result.technology})")
            click.echo(
                f"  Range: ${result.estimated_price_low:.2f} — ${result.estimated_price_high:.2f} {result.currency}"
            )
            if result.volume_cm3:
                click.echo(f"  Volume: {result.volume_cm3} cm³")
            click.echo(f"  Confidence: {result.confidence}")
            click.echo(f"  {result.note}\n")
    except ValueError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@order.command("timeline")
@click.argument("technology")
@click.option("--shipping-days", type=int, default=None, help="Known shipping days from quote.")
@click.option("--quantity", "-q", default=1, help="Number of copies.")
@click.option("--country", default="US", help="Destination country code.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def order_timeline(
    technology: str,
    shipping_days: int | None,
    quantity: int,
    country: str,
    json_mode: bool,
) -> None:
    """Estimate order-to-delivery timeline with stage breakdown.

    TECHNOLOGY: FDM, SLA, SLS, MJF, or DMLS.
    """
    from kiln.consumer import estimate_timeline

    try:
        timeline = estimate_timeline(
            technology,
            shipping_days=shipping_days,
            quantity=quantity,
            country=country,
        )
        data = timeline.to_dict()
        if json_mode:
            click.echo(json.dumps({"status": "success", "data": data}, indent=2))
        else:
            click.echo(f"\n  Order Timeline ({technology.upper()})")
            click.echo(f"  Total: {timeline.total_days} days")
            click.echo(f"  Estimated delivery: {timeline.estimated_delivery_date}")
            click.echo(f"  Confidence: {timeline.confidence}\n")
            for stage in timeline.stages:
                click.echo(f"    [{stage.estimated_days}d] {stage.stage}: {stage.description}")
            click.echo()
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@order.command("validate-address")
@click.option("--street", required=True, help="Street address.")
@click.option("--city", required=True, help="City.")
@click.option("--state", default="", help="State/province.")
@click.option("--postal-code", default="", help="ZIP/postal code.")
@click.option("--country", required=True, help="Country code (e.g. US, GB, DE).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def order_validate_address(
    street: str,
    city: str,
    state: str,
    postal_code: str,
    country: str,
    json_mode: bool,
) -> None:
    """Validate a shipping address before placing an order."""
    from kiln.consumer import validate_address

    result = validate_address(
        {
            "street": street,
            "city": city,
            "state": state,
            "postal_code": postal_code,
            "country": country,
        }
    )
    data = result.to_dict()
    if json_mode:
        click.echo(json.dumps({"status": "success" if result.valid else "error", "data": data}, indent=2))
    else:
        status = "VALID" if result.valid else "INVALID"
        click.echo(f"\n  Address: {status}")
        if result.errors:
            for e in result.errors:
                click.echo(f"    Error: {e}")
        if result.warnings:
            for w in result.warnings:
                click.echo(f"    Warning: {w}")
        if result.valid:
            n = result.normalized
            click.echo(
                f"    Normalized: {n.get('street')}, {n.get('city')}, {n.get('state')} {n.get('postal_code')}, {n.get('country')}"
            )
        click.echo()
    if not result.valid:
        sys.exit(1)


@order.command("history")
@click.option("--limit", default=20, help="Max orders to show.")
@click.option("--provider", default="", help="Filter by provider name.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def order_history(limit: int, provider: str, json_mode: bool) -> None:
    """View past fulfillment orders."""
    from kiln.fulfillment.intelligence import get_order_history

    history = get_order_history()
    orders = history.list_orders(limit=limit, provider=provider or None)
    data = [o.to_dict() for o in orders]
    if json_mode:
        click.echo(json.dumps({"status": "success", "data": data, "count": len(data)}, indent=2))
    else:
        if not orders:
            click.echo("\n  No fulfillment orders found.\n")
        else:
            click.echo(f"\n  Fulfillment Order History ({len(orders)} orders)\n")
            for o in orders:
                click.echo(f"    {o.order_id}  {o.status:<12}  ${o.total_price:.2f}  {o.provider}  {o.material_id}")
            click.echo()


@order.command("insurance")
@click.argument("order_value", type=float)
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def order_insurance(order_value: float, json_mode: bool) -> None:
    """Show shipping insurance options for an order value.

    ORDER_VALUE: Total order value in USD.
    """
    from kiln.fulfillment.intelligence import get_insurance_options

    options = get_insurance_options(order_value)
    data = [o.to_dict() for o in options]
    if json_mode:
        click.echo(json.dumps({"status": "success", "data": data}, indent=2))
    else:
        click.echo(f"\n  Shipping Insurance Options (order: ${order_value:.2f})\n")
        for o in options:
            price_str = f"${o.price:.2f}" if o.price > 0 else "Free"
            click.echo(f"    [{o.tier.value}] {o.name} — {price_str}")
            click.echo(f"      {o.description}")
        click.echo()


@order.command("countries")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def order_countries(json_mode: bool) -> None:
    """List countries supported for fulfillment shipping."""
    from kiln.consumer import list_supported_countries

    countries = list_supported_countries()
    if json_mode:
        click.echo(json.dumps({"status": "success", "data": countries}, indent=2))
    else:
        click.echo(f"\n  Supported Shipping Countries ({len(countries)})\n")
        for code, name in sorted(countries.items(), key=lambda x: x[1]):
            click.echo(f"    {code}  {name}")
        click.echo()


# ---------------------------------------------------------------------------
# fleet
# ---------------------------------------------------------------------------


@cli.group()
def fleet() -> None:
    """Manage your printer fleet.

    View status of all registered printers and register new ones.
    Free tier: up to 2 printers.  Pro: unlimited + fleet orchestration.
    """


@fleet.command("status")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def fleet_status_cmd(json_mode: bool) -> None:
    """Show the status of all printers in the fleet."""
    from kiln.licensing import LicenseTier, check_tier

    ok, msg = check_tier(LicenseTier.PRO)
    if not ok:
        click.echo(format_error(msg, code="LICENSE_REQUIRED", json_mode=json_mode))
        sys.exit(1)

    try:
        from kiln.server import fleet_status as _fleet_status

        result = _fleet_status()
        if not result.get("success"):
            click.echo(
                format_error(
                    result.get("error", "Unknown error"),
                    code=result.get("code", "ERROR"),
                    json_mode=json_mode,
                )
            )
            sys.exit(1)

        click.echo(format_fleet_status(result.get("printers", []), json_mode=json_mode))
    except PrinterError as exc:
        click.echo(
            format_error(
                f"Fleet status check failed: {exc}",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            format_error(
                f"Fleet status check failed: {exc}",
                json_mode=json_mode,
            )
        )
        sys.exit(1)


@fleet.command("register")
@click.argument("name")
@click.argument("printer_type", type=click.Choice(["octoprint", "moonraker", "bambu", "elegoo", "prusaconnect"]))
@click.argument("host")
@click.option("--api-key", default=None, help="API key or LAN access code.")
@click.option("--serial", default=None, help="Printer serial (Bambu) or mainboard ID (Elegoo).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def fleet_register_cmd(
    name: str,
    printer_type: str,
    host: str,
    api_key: str | None,
    serial: str | None,
    json_mode: bool,
) -> None:
    """Register a printer in the fleet.

    NAME is a unique friendly name (e.g. 'voron-350').
    PRINTER_TYPE is the backend: octoprint, moonraker, bambu, or prusaconnect.
    HOST is the printer's URL or IP address.

    Free tier allows up to 2 printers.  Pro unlocks unlimited.
    """
    try:
        from kiln.server import register_printer as _register_printer

        result = _register_printer(
            name=name,
            printer_type=printer_type,
            host=host,
            api_key=api_key,
            serial=serial,
        )
        if not result.get("success"):
            click.echo(
                format_error(
                    result.get("error", "Unknown error"),
                    code=result.get("code", "ERROR"),
                    json_mode=json_mode,
                )
            )
            sys.exit(1)

        click.echo(format_response("success", data=result, json_mode=json_mode))
    except PrinterError as exc:
        click.echo(
            format_error(
                f"Failed to register printer '{name}': {exc}",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to register printer '{name}': {exc}",
                json_mode=json_mode,
            )
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# queue
# ---------------------------------------------------------------------------


@cli.group()
def queue() -> None:
    """Manage the print job queue.

    Submit, monitor, list, and cancel print jobs in the queue.
    Free tier: up to 10 queued jobs.  Pro: unlimited queue depth.
    """


@queue.command("submit")
@click.argument("file")
@click.option("--printer", default=None, help="Target printer name (omit for auto-dispatch).")
@click.option("--priority", default=0, type=int, help="Job priority (higher = first, default 0).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def queue_submit_cmd(file: str, printer: str | None, priority: int, json_mode: bool) -> None:
    """Submit a print job to the queue.

    FILE is the G-code file name (must already exist on the printer).
    Free tier: up to 10 queued jobs.  Pro: unlimited.
    """
    try:
        from kiln.plugins.queue_tools import submit_job as _submit_job

        result = _submit_job(
            file_name=file,
            printer_name=printer,
            priority=priority,
        )
        if not result.get("success"):
            click.echo(
                format_error(
                    result.get("error", "Unknown error"),
                    code=result.get("code", "ERROR"),
                    json_mode=json_mode,
                )
            )
            sys.exit(1)

        click.echo(format_response("success", data=result, json_mode=json_mode))
    except ValueError as exc:
        click.echo(
            format_error(
                f"Failed to submit job for '{file}': {exc}. Use 'kiln files' to list available files.",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to submit job for '{file}': {exc}",
                json_mode=json_mode,
            )
        )
        sys.exit(1)


@queue.command("status")
@click.argument("job_id")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def queue_status_cmd(job_id: str, json_mode: bool) -> None:
    """Check the status of a specific job.

    JOB_ID is the ID returned by 'kiln queue submit'.
    """
    try:
        from kiln.plugins.queue_tools import job_status as _job_status

        result = _job_status(job_id)
        if not result.get("success"):
            click.echo(
                format_error(
                    result.get("error", "Unknown error"),
                    code=result.get("code", "ERROR"),
                    json_mode=json_mode,
                )
            )
            sys.exit(1)

        click.echo(format_job_detail(result.get("job", {}), json_mode=json_mode))
    except ValueError as exc:
        click.echo(
            format_error(
                f"Failed to get job status for {job_id!r}: {exc}",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to get job status for {job_id!r}: {exc}",
                json_mode=json_mode,
            )
        )
        sys.exit(1)


@queue.command("list")
@click.option(
    "--status",
    "-s",
    "filter_status",
    default=None,
    type=click.Choice(["completed", "failed", "cancelled"]),
    help="Filter by job status.",
)
@click.option("--limit", "-n", default=20, type=int, help="Max records (default 20).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def queue_list_cmd(filter_status: str | None, limit: int, json_mode: bool) -> None:
    """List jobs in the queue with optional status filter."""
    try:
        if filter_status:
            from kiln.plugins.queue_tools import job_history as _job_history

            result = _job_history(limit=limit, status=filter_status)
        else:
            from kiln.plugins.queue_tools import queue_summary as _queue_summary

            result = _queue_summary()

        if not result.get("success"):
            click.echo(
                format_error(
                    result.get("error", "Unknown error"),
                    code=result.get("code", "ERROR"),
                    json_mode=json_mode,
                )
            )
            sys.exit(1)

        if filter_status:
            click.echo(format_history(result.get("jobs", []), json_mode=json_mode))
        else:
            click.echo(format_queue_summary(result, json_mode=json_mode))
    except ValueError as exc:
        click.echo(
            format_error(
                f"Failed to list queue: {exc}",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to list queue: {exc}",
                json_mode=json_mode,
            )
        )
        sys.exit(1)


@queue.command("cancel")
@click.argument("job_id")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def queue_cancel_cmd(job_id: str, json_mode: bool) -> None:
    """Cancel a queued or running job.

    JOB_ID is the ID returned by 'kiln queue submit'.
    """
    try:
        from kiln.plugins.queue_tools import cancel_job as _cancel_job

        result = _cancel_job(job_id)
        if not result.get("success"):
            click.echo(
                format_error(
                    result.get("error", "Unknown error"),
                    code=result.get("code", "ERROR"),
                    json_mode=json_mode,
                )
            )
            sys.exit(1)

        click.echo(format_response("success", data=result, json_mode=json_mode))
    except ValueError as exc:
        click.echo(
            format_error(
                f"Failed to cancel job {job_id!r}: {exc}. Only queued or running jobs can be cancelled.",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to cancel job {job_id!r}: {exc}",
                json_mode=json_mode,
            )
        )
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
            file_path,
            material=material,
            electricity_rate=electricity_rate,
            printer_wattage=printer_wattage,
        )

        if json_mode:
            click.echo(
                _json.dumps(
                    {
                        "status": "success",
                        "data": estimate.to_dict(),
                    },
                    indent=2,
                )
            )
        else:
            click.echo(f"File:       {estimate.file_name}")
            click.echo(f"Material:   {estimate.material}")
            click.echo(f"Filament:   {estimate.filament_length_meters:.2f} m ({estimate.filament_weight_grams:.1f} g)")
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
    except ValueError as exc:
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
    fulfillment_material: str | None,
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
            file_path,
            material=material,
            electricity_rate=electricity_rate,
            printer_wattage=printer_wattage,
        )
        result["local"] = {"available": True, "estimate": estimate.to_dict()}
    except ValueError as exc:
        result["local"] = {"available": False, "error": str(exc)}
    except Exception as exc:
        result["local"] = {"available": False, "error": str(exc)}

    # Fulfillment quote (optional)
    if fulfillment_material:
        try:
            from kiln.fulfillment import QuoteRequest as QR
            from kiln.fulfillment import get_provider

            provider = get_provider()
            quote = provider.get_quote(
                QR(
                    file_path=file_path,
                    material_id=fulfillment_material,
                    quantity=quantity,
                    shipping_country=country,
                )
            )
            result["fulfillment"] = {"available": True, "quote": quote.to_dict()}
        except FulfillmentError as exc:
            result["fulfillment"] = {"available": False, "error": str(exc)}
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
    ctx: click.Context,
    material_type: str,
    color: str | None,
    spool: str | None,
    tool: int,
    json_mode: bool,
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
            click.echo(
                _json.dumps(
                    {
                        "status": "success",
                        "data": mat.to_dict(),
                    },
                    indent=2,
                )
            )
        else:
            click.echo(f"Set {printer_name} tool {tool}: {mat.material_type}" + (f" ({color})" if color else ""))
    except OSError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
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
            click.echo(
                _json.dumps(
                    {
                        "status": "success",
                        "data": [m.to_dict() for m in materials],
                    },
                    indent=2,
                )
            )
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
    except OSError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
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
            click.echo(
                _json.dumps(
                    {
                        "status": "success",
                        "data": [s.to_dict() for s in spools],
                    },
                    indent=2,
                )
            )
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
    except OSError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
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
    material_type: str,
    color: str | None,
    brand: str | None,
    weight: float,
    cost: float | None,
    json_mode: bool,
) -> None:
    """Add a new filament spool to inventory."""
    import json as _json

    from kiln.materials import MaterialTracker
    from kiln.persistence import get_db

    try:
        tracker = MaterialTracker(db=get_db())
        spool = tracker.add_spool(
            material_type=material_type,
            color=color,
            brand=brand,
            weight_grams=weight,
            cost_usd=cost,
        )
        if json_mode:
            click.echo(
                _json.dumps(
                    {
                        "status": "success",
                        "data": spool.to_dict(),
                    },
                    indent=2,
                )
            )
        else:
            click.echo(f"Added spool {spool.id}: {spool.material_type} {spool.weight_grams:.0f}g")
    except OSError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
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
    ctx: click.Context,
    trigger: bool,
    show_status: bool,
    set_prints: int | None,
    set_hours: float | None,
    enable: bool | None,
    json_mode: bool,
) -> None:
    """Manage bed leveling triggers and status."""
    import json as _json

    from kiln.bed_leveling import BedLevelManager
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
            click.echo(
                _json.dumps(
                    {
                        "status": "success",
                        "data": status.to_dict(),
                    },
                    indent=2,
                )
            )
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
    except PrinterError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
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
            click.echo(
                format_error(
                    "Webcam streaming not available for this printer.",
                    code="NO_STREAM",
                    json_mode=json_mode,
                )
            )
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
    except PrinterError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
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

    click.echo(
        _json.dumps(
            {
                "status": "success",
                "data": {"message": "Cloud sync status — use MCP server for full status."},
            },
            indent=2,
        )
        if json_mode
        else "Cloud sync status available via MCP server (kiln serve)."
    )


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
    except OSError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
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
        click.echo(
            _json.dumps(
                {
                    "status": "success",
                    "data": [p.to_dict() for p in discovered],
                },
                indent=2,
            )
        )
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
@click.option(
    "--rail", default="stripe", type=click.Choice(["stripe", "crypto"]), help="Payment rail (default stripe)."
)
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def billing_setup(rail: str, json_mode: bool) -> None:
    """Link a payment method for orchestration software fees.

    Generates a setup URL to add a credit card (Stripe) or configure
    crypto payments (USDC on Solana/Base).
    """
    from kiln.cli.config import get_billing_config, get_or_create_user_id
    from kiln.payments.manager import PaymentManager
    from kiln.persistence import get_db

    try:
        config = get_billing_config()
        get_or_create_user_id()
        mgr = PaymentManager(db=get_db(), config=config)

        if rail == "stripe":
            from kiln.payments.stripe_provider import StripeProvider

            provider = StripeProvider()
            mgr.register_provider(provider)
        else:
            click.echo(
                format_error(
                    "Crypto setup: set KILN_CIRCLE_API_KEY and configure your "
                    "wallet via the Circle dashboard. Then run 'kiln billing status' "
                    "to verify.",
                    json_mode=json_mode,
                )
            )
            return

        url = mgr.get_setup_url(rail=rail)
        click.echo(format_billing_setup(url, rail, json_mode=json_mode))
    except click.ClickException:
        raise
    except OSError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@billing.command("status")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def billing_status(json_mode: bool) -> None:
    """Show current payment method, monthly spend, and limits."""
    from kiln.cli.config import get_billing_config, get_or_create_user_id
    from kiln.payments.manager import PaymentManager
    from kiln.persistence import get_db

    try:
        config = get_billing_config()
        user_id = get_or_create_user_id()
        mgr = PaymentManager(db=get_db(), config=config)
        data = mgr.get_billing_status(user_id)
        click.echo(format_billing_status(data, json_mode=json_mode))
    except OSError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@billing.command("history")
@click.option("--limit", "-n", default=20, help="Max records to show (default 20).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def billing_history(limit: int, json_mode: bool) -> None:
    """Show recent billing charges and payment outcomes."""
    from kiln.cli.config import get_billing_config
    from kiln.payments.manager import PaymentManager
    from kiln.persistence import get_db

    try:
        config = get_billing_config()
        mgr = PaymentManager(db=get_db(), config=config)
        charges = mgr.get_billing_history(limit=limit)
        click.echo(format_billing_history(charges, json_mode=json_mode))
    except OSError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# donate (tip the project)
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def donate(json_mode: bool) -> None:
    """Show crypto wallet addresses to tip/donate to the Kiln project.

    Kiln is free, open-source software.  If you find it useful,
    consider sending a tip to support development.
    """
    from kiln.wallets import get_donation_info

    info = get_donation_info()
    if json_mode:
        import json as _json

        click.echo(
            _json.dumps(
                {"status": "success", "data": info},
                indent=2,
                sort_keys=False,
            )
        )
        return

    sol = info["wallets"]["solana"]
    eth = info["wallets"]["ethereum"]

    try:
        from rich.console import Console
        from rich.panel import Panel

        console = Console(stderr=True)
        lines = [
            info["message"],
            "",
            f"[bold]Solana[/bold]  {sol['domain']}",
            f"         {sol['address']}",
            f"         Accepts: {', '.join(sol['accepts'])}",
            "",
            f"[bold]Ethereum[/bold] {eth['domain']}",
            f"          {eth['address']}",
            f"          Accepts: {', '.join(eth['accepts'])}",
            "",
            f"[dim]{info['note']}[/dim]",
        ]
        console.print(Panel("\n".join(lines), title="Support Kiln", border_style="green"))
    except ImportError:
        click.echo(info["message"])
        click.echo()
        click.echo(f"Solana:   {sol['domain']}  ({sol['address']})")
        click.echo(f"          Accepts: {', '.join(sol['accepts'])}")
        click.echo(f"Ethereum: {eth['domain']}  ({eth['address']})")
        click.echo(f"          Accepts: {', '.join(eth['accepts'])}")
        click.echo()
        click.echo(info["note"])


# ---------------------------------------------------------------------------
# setup (interactive onboarding wizard)
# ---------------------------------------------------------------------------


_PRINTER_TYPE_LABELS = {
    "octoprint": "OctoPrint",
    "moonraker": "Moonraker (Klipper)",
    "bambu": "Bambu Lab",
    "elegoo": "Elegoo (SDCP)",
    "prusaconnect": "Prusa Link",
}


@cli.command()
@click.option(
    "--skip-discovery",
    is_flag=True,
    help="Skip network scan and go straight to manual entry.",
)
@click.option(
    "--timeout",
    "-t",
    "discovery_timeout",
    default=5.0,
    help="Discovery scan timeout in seconds (default 5).",
)
def setup(skip_discovery: bool, discovery_timeout: float) -> None:
    """Interactive guided setup for your first printer.

    Scans the local LAN for printers, lets you pick one (or enter
    details manually), saves credentials, and verifies the connection.
    """
    from kiln.cli.config import get_config_path

    # -- Welcome banner ----------------------------------------------------
    click.echo()
    click.echo(click.style("  Kiln Setup", bold=True))
    click.echo(click.style("  ----------", bold=True))
    click.echo("  Configure a 3D printer for Kiln to control.\n")

    # -- Terms of use ------------------------------------------------------
    from kiln.terms import is_current, prompt_acceptance

    if not is_current() and not prompt_acceptance():
        click.echo("  You must accept the terms of use to use Kiln.")
        sys.exit(1)

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
        click.echo("  Scanning LAN for printers...")
        try:
            from kiln.cli.discovery import discover_printers

            discovered = discover_printers(timeout=discovery_timeout)
        except OSError as exc:
            click.echo(click.style(f"  Discovery failed: {exc}", fg="yellow"))
            click.echo("  Continuing with manual entry.\n")
        except Exception as exc:
            click.echo(click.style(f"  Discovery failed: {exc}", fg="yellow"))
            click.echo("  Continuing with manual entry.\n")

        if discovered:
            click.echo(f"\n  Found {len(discovered)} printer(s):\n")
            click.echo(f"    {'#':<4} {'Name':<25} {'Host':<22} {'Type':<14} {'Method'}")
            click.echo(f"    {'─' * 4} {'─' * 25} {'─' * 22} {'─' * 14} {'─' * 10}")
            for i, p in enumerate(discovered, 1):
                label = _PRINTER_TYPE_LABELS.get(p.printer_type, p.printer_type)
                display_name = p.name or "(unnamed)"
                click.echo(f"    {i:<4} {display_name:<25} {p.host:<22} {label:<14} {p.discovery_method}")
            click.echo()
        else:
            click.echo("  No printers found on the LAN.\n")
            click.echo(
                "  Tip: Discovery can miss printers on some setups (WSL/VLAN/etc).\n"
                "       Enter the printer IP manually — Ethernet and Wi-Fi both work.\n"
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
                type=click.Choice(["octoprint", "moonraker", "bambu", "elegoo", "prusaconnect"]),
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
                    type=click.Choice(["octoprint", "moonraker", "bambu", "elegoo", "prusaconnect"]),
                )
                suggested_name = printer_type
        except Exception as exc:
            logger.debug("Printer probe failed for %s: %s", host, exc)
            click.echo("  Probe failed. Enter type manually.")
            printer_type = click.prompt(
                "  Select printer type",
                type=click.Choice(["octoprint", "moonraker", "bambu", "elegoo", "prusaconnect"]),
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
    elif printer_type == "elegoo":
        click.echo("  Elegoo SDCP printers require no authentication.")
        serial = click.prompt(
            "  Mainboard ID (optional, auto-discovered if blank)",
            default="",
            show_default=False,
        )
        if not serial:
            serial = None

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
    except OSError as exc:
        click.echo(click.style(f"  Failed to save config: {exc}", fg="red"))
        sys.exit(1)
    except Exception as exc:
        click.echo(click.style(f"  Failed to save config: {exc}", fg="red"))
        sys.exit(1)

    # -- Test connection ---------------------------------------------------
    click.echo("  Testing connection...")
    try:
        cfg = load_printer_config(name)
        adapter = _make_adapter(cfg)
        state = adapter.get_state()
        click.echo(click.style("  Connected!", fg="green") + f" Printer state: {state.state.value}")
        if state.tool_temp_actual is not None:
            click.echo(f"  Hotend: {state.tool_temp_actual:.0f}C")
        if state.bed_temp_actual is not None:
            click.echo(f"  Bed:    {state.bed_temp_actual:.0f}C")
    except PrinterError as exc:
        click.echo(click.style(f"  Connection test failed: {exc}", fg="yellow"))
        click.echo(
            f"  The printer was saved but may need correct credentials.\n"
            f"  Update with: kiln auth --name {name} --host {host} "
            f"--type {printer_type} --api-key <key>"
        )
    except Exception as exc:
        click.echo(click.style(f"  Connection test failed: {exc}", fg="yellow"))
        click.echo(
            f"  The printer was saved but may need correct credentials.\n"
            f"  Update with: kiln auth --name {name} --host {host} "
            f"--type {printer_type} --api-key <key>"
        )

    # -- Auto-print safety preferences -------------------------------------
    click.echo()
    click.echo(click.style("  Print Safety Preferences", bold=True))
    click.echo()
    click.echo(
        "  By default, Kiln does NOT auto-start prints after downloading\n"
        "  or generating models.  You must call start_print separately.\n"
        "  This protects your printer from untested/malformed models.\n"
    )
    click.echo(
        "  You can enable auto-print for each model source independently.\n"
        "  These can be changed later via environment variables.\n"
    )

    auto_mkt = click.confirm(
        "  Enable auto-print for MARKETPLACE downloads?\n  (Community models — moderate risk)",
        default=False,
    )
    auto_gen = click.confirm(
        "  Enable auto-print for AI-GENERATED models?\n  (Experimental geometry — higher risk)",
        default=False,
    )

    auto_env_lines = []
    if auto_mkt:
        auto_env_lines.append("export KILN_AUTO_PRINT_MARKETPLACE=true")
    if auto_gen:
        auto_env_lines.append("export KILN_AUTO_PRINT_GENERATED=true")

    if auto_env_lines:
        click.echo()
        click.echo(click.style("  Auto-print enabled. ", fg="yellow") + "Add to your shell profile:")
        for line in auto_env_lines:
            click.echo(f"    {line}")
        click.echo()
        click.echo("  To disable later, unset the variable or set to 'false'.")
    else:
        click.echo()
        click.echo(
            click.style("  Auto-print disabled (recommended).", fg="green") + " Models will upload but not print\n"
            "  until you explicitly call start_print."
        )

    # -- Next steps --------------------------------------------------------
    click.echo()
    click.echo(click.style("  Setup complete!", bold=True))
    click.echo()
    click.echo("  Next steps:")
    click.echo("    kiln status          Check printer state")
    click.echo("    kiln files           List files on the printer")
    click.echo("    kiln print <file>    Start a print")
    click.echo("    kiln serve           Start the MCP server")
    click.echo()
    click.echo("  Auto-print toggles (change anytime via env vars):")
    click.echo(f"    KILN_AUTO_PRINT_MARKETPLACE={'true' if auto_mkt else 'false (default)'}")
    click.echo(f"    KILN_AUTO_PRINT_GENERATED={'true' if auto_gen else 'false (default)'}")
    click.echo()


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# quickstart
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.option(
    "--timeout",
    "-t",
    "discovery_timeout",
    default=5.0,
    help="Discovery scan timeout in seconds (default 5).",
)
@click.pass_context
def quickstart(ctx: click.Context, json_mode: bool, discovery_timeout: float) -> None:
    """One-command setup: verify -> discover -> configure -> status.

    Chains verify (check environment), discover (find printers on the
    network), setup (auto-configure the first discovered printer), and
    status (show printer state) into a single command.
    """
    import json as _json

    results: dict[str, Any] = {"verify": {}, "discover": {}, "setup": {}, "status": {}}
    failed = False

    # -- Step 1: Verify environment ----------------------------------------
    if not json_mode:
        click.echo()
        click.echo(click.style("  Step 1: Verify environment", bold=True))
    checks = _quickstart_verify()
    results["verify"] = {"checks": checks}
    _critical_checks = {"python", "kiln", "database"}
    verify_ok = all(c["ok"] for c in checks if c["name"] in _critical_checks)
    if not json_mode:
        for c in checks:
            if c.get("warn"):
                click.echo(f"    ⚠ {c['detail']}")
            elif c["ok"]:
                label = c["name"].replace("_", " ").title()
                click.echo(f"    ✓ {label}: {c['detail']}")
            else:
                label = c["name"].replace("_", " ").title()
                click.echo(f"    ✗ {label}: {c['detail']}")
        if not verify_ok:
            click.echo(click.style("\n  Environment checks failed. Fix issues above first.", fg="red"))
            failed = True

    # -- Step 2: Discover printers -----------------------------------------
    if not json_mode:
        click.echo()
        click.echo(click.style("  Step 2: Discover printers", bold=True))
    discovered = []
    try:
        from kiln.cli.discovery import discover_printers

        discovered = discover_printers(timeout=discovery_timeout)
        results["discover"] = {
            "count": len(discovered),
            "printers": [{"name": p.name, "host": p.host, "type": p.printer_type} for p in discovered],
        }
    except OSError as exc:
        results["discover"] = {"count": 0, "error": str(exc)}
        if not json_mode:
            click.echo(click.style(f"    Discovery failed: {exc}", fg="yellow"))
    except Exception as exc:
        results["discover"] = {"count": 0, "error": str(exc)}
        if not json_mode:
            click.echo(click.style(f"    Discovery failed: {exc}", fg="yellow"))

    if not json_mode:
        if discovered:
            click.echo(f"    Found {len(discovered)} printer(s):")
            for i, p in enumerate(discovered, 1):
                display_name = p.name or "(unnamed)"
                click.echo(f"      {i}. {display_name} [{p.printer_type}] at {p.host}")
        else:
            click.echo("    No printers found on network.")
            click.echo("    Tip: Run 'kiln setup' for manual configuration.")

    # -- Step 3: Auto-configure first printer (if needed) ------------------
    if not json_mode:
        click.echo()
        click.echo(click.style("  Step 3: Configure printer", bold=True))

    existing = _list_printers()
    if existing:
        active = next((p for p in existing if p.get("active")), existing[0])
        results["setup"] = {
            "action": "existing",
            "printer": active["name"],
        }
        if not json_mode:
            click.echo(f"    Already configured: {active['name']} [{active.get('type', '?')}]")
    elif discovered:
        # Auto-configure the first discovered printer
        first = discovered[0]
        printer_name = (first.name or first.printer_type).lower().replace(" ", "-").replace(".", "-")
        try:
            save_printer(
                printer_name,
                first.printer_type,
                first.host,
                set_active=True,
            )
            results["setup"] = {
                "action": "auto_configured",
                "printer": printer_name,
                "host": first.host,
                "type": first.printer_type,
            }
            if not json_mode:
                click.echo(f"    Auto-configured: {printer_name} [{first.printer_type}] at {first.host}")
                click.echo("    Note: You may need to add an API key with 'kiln auth'.")
        except OSError as exc:
            results["setup"] = {"action": "failed", "error": str(exc)}
            if not json_mode:
                click.echo(click.style(f"    Auto-configure failed: {exc}", fg="red"))
            failed = True
        except Exception as exc:
            results["setup"] = {"action": "failed", "error": str(exc)}
            if not json_mode:
                click.echo(click.style(f"    Auto-configure failed: {exc}", fg="red"))
            failed = True
    else:
        results["setup"] = {"action": "skipped", "reason": "no printers found"}
        if not json_mode:
            click.echo("    Skipped (no printers discovered).")
            click.echo("    Run 'kiln setup' to configure manually.")

    # -- Step 4: Show status -----------------------------------------------
    if not json_mode:
        click.echo()
        click.echo(click.style("  Step 4: Printer status", bold=True))

    try:
        printer_name_ctx = ctx.obj.get("printer") if ctx.obj else None
        cfg = load_printer_config(printer_name_ctx)
        adapter = _make_adapter(cfg)
        state = adapter.get_state()
        results["status"] = {
            "connected": state.connected,
            "status": state.state.value if hasattr(state, "state") else state.status.value,
        }
        if not json_mode:
            status_val = state.state.value if hasattr(state, "state") else state.status.value
            click.echo(f"    Connected: {state.connected}")
            click.echo(f"    Status: {status_val}")
            if state.tool_temp_actual is not None:
                click.echo(f"    Hotend: {state.tool_temp_actual:.0f}C")
            if state.bed_temp_actual is not None:
                click.echo(f"    Bed:    {state.bed_temp_actual:.0f}C")
    except ValueError as exc:
        results["status"] = {"error": str(exc), "connected": False}
        if not json_mode:
            click.echo(f"    No printer configured: {exc}")
    except PrinterError as exc:
        results["status"] = {"error": str(exc), "connected": False}
        if not json_mode:
            click.echo(click.style(f"    Status check failed: {exc}", fg="yellow"))
    except Exception as exc:
        results["status"] = {"error": str(exc), "connected": False}
        if not json_mode:
            click.echo(click.style(f"    Status check failed: {exc}", fg="yellow"))

    # -- Summary -----------------------------------------------------------
    if json_mode:
        status = "error" if failed else "success"
        click.echo(_json.dumps({"status": status, "data": results}, indent=2))
    else:
        click.echo()
        if failed:
            click.echo(click.style("  Quickstart completed with issues. See above.", fg="yellow"))
        else:
            click.echo(click.style("  Quickstart complete!", bold=True, fg="green"))
        click.echo()

    if failed and not json_mode:
        sys.exit(1)


def _quickstart_verify() -> list[dict[str, Any]]:
    """Run lightweight environment checks for quickstart.

    Returns a list of check dicts with 'name', 'ok', 'detail' keys.
    """
    import platform
    import sqlite3

    checks: list[dict[str, Any]] = []

    # Python version
    vi = sys.version_info
    ok = vi >= (3, 10)
    checks.append({"name": "python", "ok": ok, "detail": f"{vi.major}.{vi.minor}.{vi.micro}"})

    # Kiln importable
    try:
        import kiln as _kiln

        ver = getattr(_kiln, "__version__", "unknown")
        checks.append({"name": "kiln", "ok": True, "detail": f"v{ver}"})
    except ImportError as exc:
        checks.append({"name": "kiln", "ok": False, "detail": str(exc)})
    except Exception as exc:
        checks.append({"name": "kiln", "ok": False, "detail": str(exc)})

    # Slicer available
    try:
        from kiln.slicer import find_slicer

        info = find_slicer()
        label = info.name
        if info.version:
            label += f" {info.version}"
        checks.append({"name": "slicer", "ok": True, "detail": label})
    except Exception as exc:
        logger.debug("Slicer discovery failed: %s", exc)
        checks.append(
            {
                "name": "slicer",
                "ok": False,
                "detail": "not found (install prusa-slicer or set KILN_SLICER_PATH)",
            }
        )

    # Database writable
    db_dir = os.path.join(os.path.expanduser("~"), ".kiln")
    db_path = os.path.join(db_dir, "kiln.db")
    try:
        os.makedirs(db_dir, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS _verify_check (id INTEGER)")
        conn.execute("DROP TABLE IF EXISTS _verify_check")
        conn.close()
        checks.append({"name": "database", "ok": True, "detail": "writable"})
    except OSError as exc:
        checks.append({"name": "database", "ok": False, "detail": str(exc)})
    except Exception as exc:
        checks.append({"name": "database", "ok": False, "detail": str(exc)})

    # WSL 2 detection
    if sys.platform == "linux":
        try:
            release = platform.uname().release.lower()
            if "microsoft" in release or "wsl" in release:
                checks.append(
                    {
                        "name": "wsl",
                        "ok": True,
                        "warn": True,
                        "detail": "WSL 2 detected — mDNS discovery will not work, use explicit IPs",
                    }
                )
        except Exception as exc:
            logger.debug("WSL detection failed in doctor checks: %s", exc)

    return checks


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
@click.option("--host", default="127.0.0.1", help="Bind address.")
@click.option("--port", default=8420, type=int, help="Port number.")
@click.option(
    "--auth-token",
    default=None,
    help="Bearer token for REST auth (defaults to KILN_API_AUTH_TOKEN env var).",
)
@click.option(
    "--tier",
    default="full",
    type=click.Choice(["essential", "standard", "full"]),
    help="Which tool tier to expose (default: full).",
)
def rest(host: str, port: int, auth_token: str | None, tier: str) -> None:
    """Start the Kiln REST API server.

    Wraps all MCP tools as REST endpoints so any HTTP client can control
    printers.  Tools are available at POST /api/tools/{tool_name} and a
    discovery endpoint at GET /api/tools lists available tools with schemas.
    """
    from kiln.rest_api import RestApiConfig, run_rest_server

    resolved_auth_token = auth_token
    if resolved_auth_token is None:
        resolved_auth_token = os.environ.get("KILN_API_AUTH_TOKEN") or os.environ.get("KILN_AUTH_TOKEN") or None

    config = RestApiConfig(
        host=host,
        port=port,
        auth_token=resolved_auth_token,
        tool_tier=tier,
    )
    click.echo(f"Starting Kiln REST API on {host}:{port} (tier: {tier})")
    run_rest_server(config)


# ---------------------------------------------------------------------------
# agent
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--model",
    "-m",
    default="openai/gpt-4o",
    help="Model ID (default: openai/gpt-4o).",
)
@click.option("--tier", default=None, help="Tool tier (auto-detect if not set).")
@click.option(
    "--base-url",
    default="https://openrouter.ai/api/v1",
    help="LLM provider base URL.",
)
def agent(model: str, tier: str | None, base_url: str) -> None:
    """Interactive agent REPL -- chat with any LLM to control your printer.

    Requires KILN_OPENROUTER_KEY or OPENROUTER_API_KEY environment variable.
    """
    import os

    api_key = os.environ.get("KILN_OPENROUTER_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        click.echo("Set KILN_OPENROUTER_KEY or OPENROUTER_API_KEY environment variable.")
        sys.exit(1)

    try:
        from kiln.agent_loop import AgentConfig, run_agent_loop
    except ImportError:
        click.echo("Agent loop module not available. Ensure kiln.agent_loop is installed.")
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
                prompt,
                agent_config,
                conversation=conversation,
            )
            conversation = result.messages
            click.echo(f"\nAgent> {result.response}\n")
            click.echo(f"  ({result.tool_calls_made} tool calls, {result.turns} turns)\n")
        except RuntimeError as exc:
            click.echo(f"\nAgent error: {exc}\n")
        except Exception as exc:
            click.echo(f"\nAgent error: {exc}\n")


# ---------------------------------------------------------------------------
# Model Generation
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("prompt")
@click.option(
    "--provider",
    "-p",
    default="meshy",
    type=click.Choice(["meshy", "openscad", "gemini", "tripo3d", "stability"]),
    help="Generation provider (default: meshy).",
)
@click.option("--style", "-s", default=None, help="Style hint (e.g. realistic, sculpture).")
@click.option("--output-dir", "-o", default=None, help="Output directory for generated model.")
@click.option(
    "--wait/--no-wait", "wait_for", default=False, help="Wait for generation to complete (default: return immediately)."
)
@click.option("--timeout", "-t", default=600, type=int, help="Max wait time in seconds (default 600).")
@click.option("--preview/--no-preview", "preview_enabled", default=True, help="Render a 3-view preview after generation.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def generate(
    prompt: str,
    provider: str,
    style: str | None,
    output_dir: str | None,
    wait_for: bool,
    timeout: int,
    preview_enabled: bool,
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
    from kiln.generation.registry import GenerationRegistry

    try:
        if provider == "openscad":
            gen = OpenSCADProvider()
        elif provider == "meshy":
            gen = MeshyProvider()
        else:
            registry = GenerationRegistry()
            registry.auto_discover()
            gen = registry.get(provider)

        job = gen.generate(prompt, format="stl", style=style)

        # If not waiting or already done (OpenSCAD), return job info.
        if not wait_for or job.status == GenerationStatus.SUCCEEDED:
            if job.status == GenerationStatus.SUCCEEDED:
                # Download the result for synchronous providers.
                result = gen.download_result(
                    job.id, output_dir=output_dir or os.path.join(tempfile.gettempdir(), "kiln_generated")
                )
                val = validate_mesh(result.local_path)
                preview_data: dict[str, Any] | None = None
                preview_notified = False
                if preview_enabled:
                    try:
                        from kiln.preview import render_multi_view_preview

                        preview_data = render_multi_view_preview(result.local_path).to_dict()
                        preview_path = str(preview_data.get("path") or "")
                        if preview_path:
                            preview_notified = _notify_preview_if_available(preview_path)
                    except Exception as exc:
                        logger.debug("Preview render failed for %s: %s", result.local_path, exc)
                        if not json_mode:
                            click.echo(click.style(f"Preview unavailable: {exc}", fg="yellow"))

                if json_mode:
                    import json

                    click.echo(
                        json.dumps(
                            {
                                "status": "success",
                                "data": {
                                    "job": job.to_dict(),
                                    "result": result.to_dict(),
                                    "validation": val.to_dict(),
                                    "preview": preview_data,
                                    "preview_notified": preview_notified,
                                },
                            },
                            indent=2,
                        )
                    )
                else:
                    click.echo(f"Generated: {result.local_path}")
                    click.echo(f"  Format: {result.format}  Size: {result.file_size_bytes:,} bytes")
                    click.echo(f"  Triangles: {val.triangle_count:,}  Manifold: {val.is_manifold}")
                    if preview_data:
                        click.echo(f"  Preview: {preview_data['path']}")
                    if preview_notified:
                        click.echo("  Preview notification: sent")
                    if val.warnings:
                        for w in val.warnings:
                            click.echo(f"  Warning: {w}")
                return

            # Async job submitted, not waiting.
            if json_mode:
                import json

                click.echo(
                    json.dumps(
                        {
                            "status": "success",
                            "data": {"job": job.to_dict()},
                        },
                        indent=2,
                    )
                )
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
                click.echo(format_error(f"Timed out after {timeout}s", code="TIMEOUT", json_mode=json_mode))
                sys.exit(1)

            job = gen.get_job_status(job.id)

            if not json_mode and job.progress > 0:
                click.echo(f"\r  Progress: {job.progress}%  ", nl=False)

            if job.status == GenerationStatus.SUCCEEDED:
                result = gen.download_result(
                    job.id, output_dir=output_dir or os.path.join(tempfile.gettempdir(), "kiln_generated")
                )
                val = validate_mesh(result.local_path)
                preview_data: dict[str, Any] | None = None
                preview_notified = False
                if preview_enabled:
                    try:
                        from kiln.preview import render_multi_view_preview

                        preview_data = render_multi_view_preview(result.local_path).to_dict()
                        preview_path = str(preview_data.get("path") or "")
                        if preview_path:
                            preview_notified = _notify_preview_if_available(preview_path)
                    except Exception as exc:
                        logger.debug("Preview render failed for %s: %s", result.local_path, exc)
                        if not json_mode:
                            click.echo(click.style(f"Preview unavailable: {exc}", fg="yellow"))

                if json_mode:
                    import json

                    click.echo(
                        json.dumps(
                            {
                                "status": "success",
                                "data": {
                                    "job": job.to_dict(),
                                    "result": result.to_dict(),
                                    "validation": val.to_dict(),
                                    "preview": preview_data,
                                    "preview_notified": preview_notified,
                                    "elapsed_seconds": round(elapsed, 1),
                                },
                            },
                            indent=2,
                        )
                    )
                else:
                    click.echo(f"\nGenerated: {result.local_path}")
                    click.echo(f"  Format: {result.format}  Size: {result.file_size_bytes:,} bytes")
                    click.echo(f"  Triangles: {val.triangle_count:,}  Manifold: {val.is_manifold}")
                    if preview_data:
                        click.echo(f"  Preview: {preview_data['path']}")
                    if preview_notified:
                        click.echo("  Preview notification: sent")
                    click.echo(f"  Completed in {elapsed:.0f}s")
                return

            if job.status in (GenerationStatus.FAILED, GenerationStatus.CANCELLED):
                click.echo(
                    format_error(
                        f"Generation {job.status.value}: {job.error or 'unknown'}",
                        code="GENERATION_FAILED",
                        json_mode=json_mode,
                    )
                )
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
@click.option(
    "--provider", "-p", default="meshy", type=click.Choice(["meshy", "openscad", "gemini", "tripo3d", "stability"]), help="Generation provider."
)
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
    from kiln.generation.registry import GenerationRegistry

    try:
        if provider == "openscad":
            gen = OpenSCADProvider()
        elif provider == "meshy":
            gen = MeshyProvider()
        else:
            registry = GenerationRegistry()
            registry.auto_discover()
            gen = registry.get(provider)

        job = gen.get_job_status(job_id)

        if json_mode:
            import json

            click.echo(
                json.dumps(
                    {
                        "status": "success",
                        "data": {"job": job.to_dict()},
                    },
                    indent=2,
                )
            )
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
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@cli.command("generate-download")
@click.argument("job_id")
@click.option(
    "--provider", "-p", default="meshy", type=click.Choice(["meshy", "openscad", "gemini", "tripo3d", "stability"]), help="Generation provider."
)
@click.option(
    "--output-dir", "-o", default=os.path.join(tempfile.gettempdir(), "kiln_generated"), help="Output directory."
)
@click.option("--validate/--no-validate", default=True, help="Run mesh validation (default: on).")
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
    from kiln.generation.registry import GenerationRegistry

    try:
        if provider == "openscad":
            gen = OpenSCADProvider()
        elif provider == "meshy":
            gen = MeshyProvider()
        else:
            registry = GenerationRegistry()
            registry.auto_discover()
            gen = registry.get(provider)

        result = gen.download_result(job_id, output_dir=output_dir)

        validation = None
        if validate and result.format in ("stl", "obj"):
            validation = validate_mesh(result.local_path)

        if json_mode:
            import json

            data: dict[str, Any] = {"result": result.to_dict()}
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
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@cli.command("generate-and-print")
@click.argument("prompt")
@click.option(
    "--provider",
    "-p",
    default="meshy",
    type=click.Choice(["meshy", "openscad", "gemini", "tripo3d", "stability"]),
    help="Generation provider (default: meshy).",
)
@click.option("--style", "-s", default=None, help="Style hint.")
@click.option("--printer-id", default=None, help="Printer model ID for slicer profile.")
@click.option(
    "--material",
    "-m",
    default=None,
    type=click.Choice(_MATERIAL_CHOICES),
    help="Material type (defaults to loaded material, then PLA).",
)
@click.option(
    "--support-mode",
    default="auto",
    show_default=True,
    type=click.Choice(_SUPPORT_MODE_CHOICES),
    help="Support strategy: off, auto, minimal (buildplate-only), or aggressive.",
)
@click.option("--timeout", "-t", default=600, type=int, help="Max generation wait time in seconds (default 600).")
@click.option(
    "--auto-print/--no-auto-print", default=False, help="Automatically start printing after upload (default: preview only)."
)
@click.option("--preview/--no-preview", "preview_enabled", default=True, help="Render 3-view model preview after generation.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def generate_and_print_cmd(
    ctx: click.Context,
    prompt: str,
    provider: str,
    style: str | None,
    printer_id: str | None,
    material: str | None,
    support_mode: str,
    timeout: int,
    auto_print: bool,
    preview_enabled: bool,
    json_mode: bool,
) -> None:
    """Generate a 3D model, slice it, and upload to the printer.

    One-command pipeline from text description to print-ready.

    \b
    Examples:
        kiln generate-and-print "a phone stand" --provider gemini --material PLA
        kiln generate-and-print "a gear" --provider openscad --auto-print
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
    from kiln.generation.registry import GenerationRegistry
    from kiln.slicer import SlicerError, SlicerNotFoundError, slice_file

    try:
        # --- Step 1: Generate ---
        if provider == "openscad":
            gen = OpenSCADProvider()
        elif provider == "meshy":
            gen = MeshyProvider()
        else:
            registry = GenerationRegistry()
            registry.auto_discover()
            gen = registry.get(provider)

        if not json_mode:
            click.echo(f"Generating model with {gen.display_name}...")

        job = gen.generate(prompt, format="stl", style=style)

        # Wait for async providers
        if job.status not in (GenerationStatus.SUCCEEDED, GenerationStatus.FAILED):
            start = _time.time()
            while _time.time() - start < timeout:
                _time.sleep(10)
                job = gen.get_job_status(job.id)
                if not json_mode:
                    click.echo(f"  Status: {job.status.value}  Progress: {job.progress}%")
                if job.status in (GenerationStatus.SUCCEEDED, GenerationStatus.FAILED):
                    break
            else:
                click.echo(format_error("Generation timed out.", code="TIMEOUT", json_mode=json_mode))
                sys.exit(1)

        if job.status == GenerationStatus.FAILED:
            click.echo(format_error(f"Generation failed: {job.error}", code="GENERATION_FAILED", json_mode=json_mode))
            sys.exit(1)

        # --- Step 2: Download ---
        output_dir = os.path.join(tempfile.gettempdir(), "kiln_generated")
        result = gen.download_result(job.id, output_dir=output_dir)
        val = validate_mesh(result.local_path)
        if not json_mode:
            click.echo(f"Generated: {result.local_path} ({result.file_size_bytes:,} bytes, {val.triangle_count:,} triangles)")

        preview_data: dict[str, Any] | None = None
        preview_notified = False
        if preview_enabled:
            try:
                from kiln.preview import render_multi_view_preview

                preview_data = render_multi_view_preview(result.local_path).to_dict()
                preview_path = str(preview_data.get("path") or "")
                if preview_path:
                    preview_notified = _notify_preview_if_available(preview_path)
                if not json_mode:
                    click.echo(f"Preview: {preview_data['path']}")
                    if preview_notified:
                        click.echo("Preview notification: sent")
            except Exception as exc:
                logger.debug("Preview render failed for %s: %s", result.local_path, exc)
                if not json_mode:
                    click.echo(click.style(f"Preview unavailable: {exc}", fg="yellow"))

        # --- Step 3: Slice ---
        plan = _resolve_slice_plan(
            ctx,
            input_file=result.local_path,
            profile=None,
            printer_id=printer_id,
            material=material,
            support_mode=support_mode,
        )

        if not json_mode:
            click.echo("Slicing...")
        slice_result = slice_file(
            result.local_path,
            profile=plan["profile_path"],
            extra_args=plan["extra_args"] or None,
        )
        if not json_mode:
            click.echo(f"Sliced: {slice_result.output_path}")
            click.echo(f"Material: {plan['material']}")
            if plan["support_style"]:
                note = f" ({plan['support_reason']})" if plan["support_reason"] else ""
                click.echo(f"Supports: {plan['support_style']}{note}")

        # --- Step 4: Upload ---
        adapter = _get_adapter_from_ctx(ctx)
        if not json_mode:
            click.echo("Uploading to printer...")
        upload_result = adapter.upload_file(slice_result.output_path)
        if not upload_result.success:
            click.echo(format_error(upload_result.message or "Upload failed", code="UPLOAD_FAILED", json_mode=json_mode))
            sys.exit(1)

        # --- Step 5: Optionally start print ---
        if auto_print:
            remote = upload_result.remote_name or os.path.basename(slice_result.output_path)
            adapter.start_print(remote)
            if not json_mode:
                click.echo(f"Printing started: {remote}")

        if json_mode:
            import json as _json

            click.echo(
                _json.dumps(
                    {
                        "status": "success",
                        "data": {
                            "generation": job.to_dict(),
                            "validation": val.to_dict(),
                            "preview": preview_data,
                            "preview_notified": preview_notified,
                            "slice": {"output_path": slice_result.output_path, "message": slice_result.message},
                            "material": plan["material"],
                            "support_mode": support_mode,
                            "support_style": plan["support_style"],
                            "support_reason": plan["support_reason"],
                            "upload": upload_result.to_dict(),
                            "printing": auto_print,
                        },
                    },
                    indent=2,
                )
            )
        elif not auto_print:
            click.echo(
                f"Ready to print. Start with: kiln print {upload_result.remote_name or os.path.basename(slice_result.output_path)}"
            )

    except GenerationAuthError as exc:
        click.echo(format_error(str(exc), code="AUTH_ERROR", json_mode=json_mode))
        sys.exit(1)
    except GenerationError as exc:
        click.echo(format_error(str(exc), code=exc.code or "GENERATION_ERROR", json_mode=json_mode))
        sys.exit(1)
    except (SlicerNotFoundError, SlicerError) as exc:
        click.echo(format_error(str(exc), code="SLICER_ERROR", json_mode=json_mode))
        sys.exit(1)
    except PrinterError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
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
            click.echo(
                format_error(
                    "This printer does not support firmware updates.",
                    json_mode=json_mode,
                )
            )
            sys.exit(1)

        status = adapter.get_firmware_status()
        if status is None:
            click.echo(
                format_error(
                    "Could not retrieve firmware status. The printer may not support firmware queries, "
                    "or the connection timed out. Try 'kiln status' to verify connectivity.",
                    json_mode=json_mode,
                )
            )
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
    except PrinterError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@firmware.command("update")
@click.option("--component", "-c", default=None, help="Component to update (default: all).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def firmware_update_cmd(ctx: click.Context, component: str | None, json_mode: bool) -> None:
    """Apply available firmware updates.

    Optionally specify --component to update a single component,
    otherwise all components with available updates are upgraded.
    """
    import json as _json

    try:
        adapter = _get_adapter_from_ctx(ctx)
        if not adapter.capabilities.can_update_firmware:
            click.echo(
                format_error(
                    "This printer does not support firmware updates.",
                    json_mode=json_mode,
                )
            )
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
    except PrinterError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
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
            click.echo(
                format_error(
                    "This printer does not support firmware rollback.",
                    json_mode=json_mode,
                )
            )
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
    except PrinterError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


@cli.command("doctor-prusa")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def doctor_prusa(ctx: click.Context, json_mode: bool) -> None:
    """Run focused diagnostics for Prusa Link connectivity and storage."""
    import json as _json

    try:
        cfg = load_printer_config(ctx.obj.get("printer"))
    except ValueError as exc:
        click.echo(format_error(str(exc), code="CONFIG_ERROR", json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), code="CONFIG_ERROR", json_mode=json_mode))
        sys.exit(1)

    if str(cfg.get("type", "")).strip().lower() != "prusaconnect":
        click.echo(
            format_error(
                "Active printer is not Prusa Link. Set one with --printer or run: kiln auth --type prusaconnect ...",
                code="WRONG_PRINTER_TYPE",
                json_mode=json_mode,
            )
        )
        sys.exit(1)

    result = _run_prusa_diagnostics(cfg)
    if json_mode:
        click.echo(_json.dumps({"status": "success" if result.get("ok") else "error", "data": result}, indent=2))
    else:
        click.echo("Prusa Link diagnostics:")
        for check in result.get("checks", []):
            if not isinstance(check, dict):
                continue
            icon = "✓" if check.get("ok") else ("⚠" if check.get("warn") else "✗")
            click.echo(f"  {icon} {check.get('name')}: {check.get('detail')}")
        if result.get("profile_id"):
            click.echo(f"\nDetected profile: {result['profile_id']}")
        if result.get("file_count") is not None:
            click.echo(f"Files visible: {result['file_count']}")

    if not result.get("ok"):
        sys.exit(1)


@cli.command()
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def verify(ctx: click.Context, json_mode: bool) -> None:
    """Run pre-flight system checks to verify Kiln is ready to use."""
    import json as _json
    import platform
    import sqlite3

    checks: list[dict] = []

    # 1. Python version
    vi = sys.version_info
    ok = vi >= (3, 10)
    checks.append(
        {
            "name": "python",
            "ok": ok,
            "detail": f"{vi.major}.{vi.minor}.{vi.micro}",
        }
    )

    # 2. Kiln importable
    try:
        import kiln as _kiln

        ver = getattr(_kiln, "__version__", "unknown")
        checks.append({"name": "kiln", "ok": True, "detail": f"v{ver}"})
    except ImportError as exc:
        checks.append({"name": "kiln", "ok": False, "detail": str(exc)})
    except Exception as exc:
        checks.append({"name": "kiln", "ok": False, "detail": str(exc)})

    # 3. Slicer available
    try:
        from kiln.slicer import SlicerNotFoundError, find_slicer

        info = find_slicer()
        label = info.name
        if info.version:
            label += f" {info.version}"
        checks.append({"name": "slicer", "ok": True, "detail": label})
    except SlicerNotFoundError:
        checks.append(
            {
                "name": "slicer",
                "ok": False,
                "detail": "not found (install prusa-slicer or set KILN_SLICER_PATH)",
            }
        )
    except OSError as exc:
        checks.append({"name": "slicer", "ok": False, "detail": str(exc)})
    except Exception as exc:
        checks.append({"name": "slicer", "ok": False, "detail": str(exc)})

    # 4. Config / printers configured
    printer_cfg = None
    try:
        printer_name = ctx.obj.get("printer")
        printer_cfg = load_printer_config(printer_name)
        name_label = printer_name or printer_cfg.get("name", "default")
        checks.append(
            {
                "name": "config",
                "ok": True,
                "detail": f"printer '{name_label}' configured",
            }
        )
    except ValueError as exc:
        checks.append({"name": "config", "ok": False, "detail": str(exc)})
    except Exception as exc:
        checks.append({"name": "config", "ok": False, "detail": str(exc)})

    # 5. Printer reachable (use adapter with auth, not raw HTTP)
    if printer_cfg:
        host = printer_cfg.get("host", "")
        if host:
            try:
                adapter = _make_adapter(printer_cfg)
                state = adapter.get_state()
                if state.connected:
                    checks.append({"name": "printer_reachable", "ok": True, "detail": f"{host} ({state.state.value})"})
                else:
                    checks.append({"name": "printer_reachable", "ok": False, "detail": f"{host} (offline)"})
            except Exception as exc:
                logger.debug("Printer reachability check failed for %s: %s", host, exc)
                checks.append(
                    {
                        "name": "printer_reachable",
                        "ok": False,
                        "detail": f"cannot reach {host}: {exc}",
                    }
                )

        # Prusa-specific diagnostics for first-run clarity.
        if str(printer_cfg.get("type", "")).strip().lower() == "prusaconnect":
            try:
                prusa_diag = _run_prusa_diagnostics(printer_cfg)
                checks.append(
                    {
                        "name": "prusa_storage",
                        "ok": bool(prusa_diag.get("ok")),
                        "detail": (f"roots checked: usb/local; files={prusa_diag.get('file_count')}"),
                    }
                )
                if prusa_diag.get("profile_id"):
                    checks.append(
                        {
                            "name": "prusa_model",
                            "ok": True,
                            "detail": f"detected profile {prusa_diag.get('profile_id')}",
                        }
                    )
            except Exception as exc:
                logger.debug("Prusa verify diagnostics failed: %s", exc)
                checks.append(
                    {
                        "name": "prusa_storage",
                        "ok": False,
                        "detail": str(exc),
                    }
                )
    else:
        checks.append(
            {
                "name": "printer_reachable",
                "ok": False,
                "detail": "skipped (no printer configured)",
            }
        )

    # 6. SQLite writable
    db_dir = os.path.join(os.path.expanduser("~"), ".kiln")
    db_path = os.path.join(db_dir, "kiln.db")
    try:
        os.makedirs(db_dir, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS _verify_check (id INTEGER)")
        conn.execute("DROP TABLE IF EXISTS _verify_check")
        conn.close()
        checks.append({"name": "database", "ok": True, "detail": "writable"})
    except OSError as exc:
        checks.append({"name": "database", "ok": False, "detail": str(exc)})
    except Exception as exc:
        checks.append({"name": "database", "ok": False, "detail": str(exc)})

    # 7. WSL 2 detection
    wsl = False
    if sys.platform == "linux":
        try:
            release = platform.uname().release.lower()
            if "microsoft" in release or "wsl" in release:
                wsl = True
        except Exception as exc:
            logger.debug("WSL detection failed in diag checks: %s", exc)
    if wsl:
        checks.append(
            {
                "name": "wsl",
                "ok": True,
                "warn": True,
                "detail": "WSL 2 detected — mDNS discovery will not work, use explicit IPs",
            }
        )

    # --- Output ---
    if json_mode:
        click.echo(_json.dumps({"status": "ok", "checks": checks}, indent=2))
    else:
        for c in checks:
            if c.get("warn"):
                click.echo(f"  ⚠ {c['detail']}")
            elif c["ok"]:
                label = c["name"].replace("_", " ").title()
                click.echo(f"  ✓ {label}: {c['detail']}")
            else:
                label = c["name"].replace("_", " ").title()
                click.echo(f"  ✗ {label}: {c['detail']}")

        passed = sum(1 for c in checks if c["ok"])
        total = len(checks)
        click.echo(f"\n  {passed}/{total} checks passed.")

        if any(not c["ok"] for c in checks):
            sys.exit(1)


# ``kiln doctor`` is an alias for ``kiln verify``.
cli.add_command(verify, name="doctor")


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--key",
    "-k",
    default=None,
    help="License key to activate. If omitted, opens the upgrade page.",
)
@click.option(
    "--session",
    "-s",
    default=None,
    help="Stripe Checkout Session ID to retrieve and activate the license key.",
)
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def upgrade(ctx: click.Context, key: str | None, session: str | None, json_mode: bool) -> None:
    """Activate a Kiln Pro or Business license, or view current tier."""
    from kiln.licensing import LicenseTier, get_license_manager

    mgr = get_license_manager()

    if session:
        try:
            import stripe  # type: ignore[import-untyped]
        except ImportError:
            click.echo(
                format_error(
                    "stripe package not installed. Install with: pip install kiln3d[payments]",
                    code="MISSING_DEPENDENCY",
                    json_mode=json_mode,
                )
            )
            sys.exit(1)

        stripe.api_key = os.environ.get("KILN_STRIPE_SECRET_KEY", "")
        if not stripe.api_key:
            click.echo(
                format_error(
                    "KILN_STRIPE_SECRET_KEY not set. Cannot retrieve session.",
                    code="CONFIG_MISSING",
                    json_mode=json_mode,
                )
            )
            sys.exit(1)

        try:
            checkout_session = stripe.checkout.Session.retrieve(session)
        except stripe.error.StripeError as exc:
            click.echo(
                format_error(
                    f"Failed to retrieve Stripe session: {exc}",
                    code="STRIPE_ERROR",
                    json_mode=json_mode,
                )
            )
            sys.exit(1)

        license_key = (checkout_session.metadata or {}).get("license_key", "")
        if not license_key:
            if getattr(checkout_session, "payment_status", "") != "paid":
                click.echo(
                    format_error(
                        "Payment not completed. Complete payment first, then retry.",
                        code="PAYMENT_PENDING",
                        json_mode=json_mode,
                    )
                )
            else:
                click.echo(
                    format_error(
                        "License key not found on session. The webhook may not have "
                        "processed yet — wait a moment and retry.",
                        code="KEY_NOT_READY",
                        json_mode=json_mode,
                    )
                )
            sys.exit(1)

        # Activate the key (reuse existing activation path)
        try:
            info = mgr.activate_license(license_key)
            data = info.to_dict()
            if json_mode:
                import json as _json

                click.echo(_json.dumps({"success": True, **data}, indent=2))
            else:
                click.echo(f"  ✓ License activated: Kiln {info.tier.value.title()}")
                if info.license_key_hint:
                    click.echo(f"    Key: ...{info.license_key_hint}")
                click.echo(f"    Source: {info.source}")
        except Exception as exc:
            click.echo(format_error(str(exc), code="LICENSE_ERROR", json_mode=json_mode))
            sys.exit(1)
        return

    if key:
        # Activate the provided license key
        try:
            info = mgr.activate_license(key)
            data = info.to_dict()
            if json_mode:
                import json as _json

                click.echo(_json.dumps({"success": True, **data}, indent=2))
            else:
                click.echo(f"  ✓ License activated: Kiln {info.tier.value.title()}")
                if info.license_key_hint:
                    click.echo(f"    Key: ...{info.license_key_hint}")
                click.echo(f"    Source: {info.source}")
        except ValueError as exc:
            click.echo(format_error(str(exc), code="LICENSE_ERROR", json_mode=json_mode))
            sys.exit(1)
        except Exception as exc:
            click.echo(format_error(str(exc), code="LICENSE_ERROR", json_mode=json_mode))
            sys.exit(1)
    else:
        # Show current tier and upgrade info
        info = mgr.get_info()
        data = info.to_dict()
        if json_mode:
            import json as _json

            click.echo(_json.dumps({"success": True, **data}, indent=2))
        else:
            click.echo("\n  Kiln License")
            click.echo("  ────────────")
            click.echo(f"  Tier:   {info.tier.value.title()}")
            if info.license_key_hint:
                click.echo(f"  Key:    ...{info.license_key_hint}")
            click.echo(f"  Source: {info.source}")
            if info.tier == LicenseTier.FREE:
                click.echo("\n  Upgrade to Pro for fleet management, job queue,")
                click.echo("  analytics, and more.")
                click.echo("\n  Visit: https://kiln3d.com/pro")
                click.echo("  Or:    kiln upgrade --key <your-license-key>")
            else:
                click.echo("\n  ✓ Active and valid.")


@cli.command()
@click.option(
    "--email",
    "-e",
    default=None,
    help="Email address for registration.",
)
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def register(email: str | None, json_mode: bool) -> None:
    """Register for a free Kiln license key.

    Sends your email to api.kiln3d.com and saves the returned license key.
    Required for outsourced manufacturing via the fulfillment proxy.
    If you already have a license key, shows your current tier.
    """
    from kiln.licensing import get_license_manager

    mgr = get_license_manager()
    info = mgr.get_info()

    # If user already has a valid key, show it and exit.
    if info.is_valid and info.tier.value != "free":
        if json_mode:
            import json as _json

            click.echo(_json.dumps({"success": True, "already_registered": True, **info.to_dict()}, indent=2))
        else:
            click.echo(f"\n  Already registered: Kiln {info.tier.value.title()}")
            if info.license_key_hint:
                click.echo(f"  Key: ...{info.license_key_hint}")
        return

    # Prompt for email if not provided.
    if not email:
        email = click.prompt("  Email address", type=str)

    if not email or "@" not in email:
        click.echo(format_error("Valid email address required.", code="INVALID_EMAIL", json_mode=json_mode))
        sys.exit(1)

    # Call registration endpoint.
    proxy_url = os.environ.get("KILN_PROXY_URL", "https://api.kiln3d.com").rstrip("/")
    try:
        import requests

        resp = requests.post(
            f"{proxy_url}/api/license/register",
            json={"email": email},
            timeout=15,
        )
        if not resp.ok:
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            error_msg = body.get("error", f"HTTP {resp.status_code}")
            click.echo(format_error(f"Registration failed: {error_msg}", code="REGISTER_ERROR", json_mode=json_mode))
            sys.exit(1)

        data = resp.json()
        license_key = data.get("license_key", "")
        if not license_key:
            click.echo(format_error("Server returned no license key.", code="REGISTER_ERROR", json_mode=json_mode))
            sys.exit(1)

        # Activate locally.
        activated = mgr.activate_license(license_key)
        if json_mode:
            import json as _json

            click.echo(_json.dumps({"success": True, **activated.to_dict()}, indent=2))
        else:
            click.echo(f"\n  ✓ Registered! Kiln {activated.tier.value.title()} license saved.")
            if activated.license_key_hint:
                click.echo(f"    Key: ...{activated.license_key_hint}")
            click.echo(f"    Email: {email}")
            click.echo("\n  You can now use outsourced manufacturing via Kiln.")
            click.echo("  Upgrade anytime: kiln upgrade --key <pro-or-business-key>")

    except ImportError:
        click.echo(
            format_error(
                "requests package not installed. Install with: pip install requests",
                code="MISSING_DEPENDENCY",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(f"Registration failed: {exc}", code="REGISTER_ERROR", json_mode=json_mode))
        sys.exit(1)


@cli.command()
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def license_info(json_mode: bool) -> None:
    """Show current license tier and details."""
    from kiln.licensing import get_license_manager

    mgr = get_license_manager()
    info = mgr.get_info()
    data = info.to_dict()

    if json_mode:
        import json as _json

        click.echo(_json.dumps({"success": True, **data}, indent=2))
    else:
        click.echo("\n  Kiln License")
        click.echo("  ────────────")
        click.echo(f"  Tier:     {info.tier.value.title()}")
        click.echo(f"  Valid:    {'Yes' if info.is_valid else 'No'}")
        if info.license_key_hint:
            click.echo(f"  Key:      ...{info.license_key_hint}")
        click.echo(f"  Source:   {info.source}")


# ---------------------------------------------------------------------------
# partner/provider integration (3DOS-backed)
# ---------------------------------------------------------------------------

_NETWORK_ALIAS_DEPRECATED_IN = "v0.2.0"
_NETWORK_ALIAS_REMOVAL_TARGET = "v0.4.0"


def _network_alias_warning() -> None:
    """Emit a warning when legacy `kiln network ...` commands are used."""
    click.echo(
        (
            "Warning: `kiln network ...` is deprecated. "
            "Use `kiln partner ...` instead. "
            f"Deprecated in {_NETWORK_ALIAS_DEPRECATED_IN}; "
            f"removal target {_NETWORK_ALIAS_REMOVAL_TARGET}."
        ),
        err=True,
    )


def _get_threedos_client():
    """Create a 3DOS client from env config."""
    from kiln.gateway.threedos import ThreeDOSClient

    try:
        return ThreeDOSClient()
    except ValueError as exc:
        raise click.ClickException(
            f"3DOS not configured: {exc}. Set KILN_3DOS_API_KEY."
        ) from exc


def _provider_connect(
    *,
    name: str,
    location: str,
    price: float | None,
    json_mode: bool,
) -> None:
    client = _get_threedos_client()
    listing = client.register_printer(name=name, location=location, price_per_gram=price)
    if json_mode:
        click.echo(format_response("success", data=listing.to_dict(), json_mode=True))
    else:
        click.echo(f"Connected provider listing '{listing.name}' (id: {listing.id})")


def _provider_sync(
    *,
    printer_id: str,
    available: bool,
    json_mode: bool,
) -> None:
    client = _get_threedos_client()
    client.update_printer_status(printer_id=printer_id, available=available)
    if json_mode:
        click.echo(
            format_response(
                "success",
                data={"printer_id": printer_id, "available": available},
                json_mode=True,
            )
        )
    else:
        status = "available" if available else "unavailable"
        click.echo(f"Provider listing {printer_id} is now {status}")


def _provider_list(*, json_mode: bool) -> None:
    client = _get_threedos_client()
    printers = client.list_my_printers()
    if json_mode:
        click.echo(
            format_response(
                "success",
                data={"printers": [p.to_dict() for p in printers], "count": len(printers)},
                json_mode=True,
            )
        )
    else:
        if not printers:
            click.echo("No provider capacity listings connected yet.")
        else:
            for p in printers:
                avail = "available" if p.available else "offline"
                click.echo(f"  {p.name} ({p.id}) — {p.location} [{avail}]")


def _provider_find(*, material: str, location: str | None, json_mode: bool) -> None:
    client = _get_threedos_client()
    printers = client.find_printers(material=material, location=location)
    if json_mode:
        click.echo(
            format_response(
                "success",
                data={"printers": [p.to_dict() for p in printers], "count": len(printers)},
                json_mode=True,
            )
        )
    else:
        if not printers:
            click.echo(f"No provider capacity found for material '{material}'.")
        else:
            click.echo(f"Found {len(printers)} provider listing(s):")
            for p in printers:
                price_str = f"${p.price_per_gram}/g" if p.price_per_gram else "price TBD"
                click.echo(f"  {p.name} ({p.id}) — {p.location} [{price_str}]")


def _provider_submit(
    *,
    file_url: str,
    material: str,
    printer: str | None,
    json_mode: bool,
) -> None:
    client = _get_threedos_client()
    job = client.submit_network_job(file_url=file_url, material=material, printer_id=printer)
    if json_mode:
        click.echo(format_response("success", data=job.to_dict(), json_mode=True))
    else:
        cost = f" (est. ${job.estimated_cost:.2f})" if job.estimated_cost else ""
        click.echo(f"Provider job submitted: {job.id} — status: {job.status}{cost}")


def _provider_status(*, job_id: str, json_mode: bool) -> None:
    client = _get_threedos_client()
    job = client.get_network_job(job_id=job_id)
    if json_mode:
        click.echo(format_response("success", data=job.to_dict(), json_mode=True))
    else:
        cost = f" (est. ${job.estimated_cost:.2f})" if job.estimated_cost else ""
        printer_info = f" on {job.printer_id}" if job.printer_id else ""
        click.echo(f"Provider job {job.id}: {job.status}{printer_info}{cost}")


@cli.group()
def partner() -> None:
    """Partner-provider integrations for remote manufacturing routing."""


@partner.command("connect")
@click.option("--name", "-n", required=True, help="Printer/listing name.")
@click.option("--location", "-l", required=True, help="Geographic location.")
@click.option("--price", type=float, default=None, help="Price per gram (USD).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def partner_connect(name: str, location: str, price: float | None, json_mode: bool) -> None:
    """Connect a local printer listing to a provider account integration."""
    try:
        _provider_connect(name=name, location=location, price=price, json_mode=json_mode)
    except click.ClickException:
        raise
    except ThreeDOSError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@partner.command("sync")
@click.argument("printer_id")
@click.option("--available/--unavailable", default=True, help="Set availability.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def partner_sync(printer_id: str, available: bool, json_mode: bool) -> None:
    """Sync provider capacity status for a connected listing."""
    try:
        _provider_sync(printer_id=printer_id, available=available, json_mode=json_mode)
    except click.ClickException:
        raise
    except ThreeDOSError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@partner.command("list")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def partner_list(json_mode: bool) -> None:
    """List connected provider capacity listings."""
    try:
        _provider_list(json_mode=json_mode)
    except click.ClickException:
        raise
    except ThreeDOSError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@partner.command("find")
@click.option("--material", "-m", required=True, help="Material type (PLA, PETG, ABS).")
@click.option("--location", "-l", default=None, help="Geographic filter.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def partner_find(material: str, location: str | None, json_mode: bool) -> None:
    """Find available provider capacity by material/location."""
    try:
        _provider_find(material=material, location=location, json_mode=json_mode)
    except click.ClickException:
        raise
    except ThreeDOSError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@partner.command("submit")
@click.argument("file_url")
@click.option("--material", "-m", required=True, help="Material type.")
@click.option("--printer", "-p", default=None, help="Target printer ID (auto-assign if omitted).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def partner_submit(file_url: str, material: str, printer: str | None, json_mode: bool) -> None:
    """Submit a remote job through the connected provider integration."""
    try:
        _provider_submit(file_url=file_url, material=material, printer=printer, json_mode=json_mode)
    except click.ClickException:
        raise
    except ThreeDOSError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@partner.command("status")
@click.argument("job_id")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def partner_status(job_id: str, json_mode: bool) -> None:
    """Check status of a provider-managed remote job."""
    try:
        _provider_status(job_id=job_id, json_mode=json_mode)
    except click.ClickException:
        raise
    except ThreeDOSError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@cli.group()
def network() -> None:
    """Deprecated alias for `partner` commands.

    Deprecated in v0.2.0. Removal target: v0.4.0.
    """


@network.command("register")
@click.option("--name", "-n", required=True, help="Printer/listing name.")
@click.option("--location", "-l", required=True, help="Geographic location.")
@click.option("--price", type=float, default=None, help="Price per gram (USD).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def network_register(name: str, location: str, price: float | None, json_mode: bool) -> None:
    """Deprecated alias for `partner connect`."""
    _network_alias_warning()
    try:
        _provider_connect(name=name, location=location, price=price, json_mode=json_mode)
    except click.ClickException:
        raise
    except ThreeDOSError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@network.command("update")
@click.argument("printer_id")
@click.option("--available/--unavailable", default=True, help="Set availability.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def network_update(printer_id: str, available: bool, json_mode: bool) -> None:
    """Deprecated alias for `partner sync`."""
    _network_alias_warning()
    try:
        _provider_sync(printer_id=printer_id, available=available, json_mode=json_mode)
    except click.ClickException:
        raise
    except ThreeDOSError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@network.command("list")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def network_list(json_mode: bool) -> None:
    """Deprecated alias for `partner list`."""
    _network_alias_warning()
    try:
        _provider_list(json_mode=json_mode)
    except click.ClickException:
        raise
    except ThreeDOSError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@network.command("find")
@click.option("--material", "-m", required=True, help="Material type (PLA, PETG, ABS).")
@click.option("--location", "-l", default=None, help="Geographic filter.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def network_find(material: str, location: str | None, json_mode: bool) -> None:
    """Deprecated alias for `partner find`."""
    _network_alias_warning()
    try:
        _provider_find(material=material, location=location, json_mode=json_mode)
    except click.ClickException:
        raise
    except ThreeDOSError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@network.command("submit")
@click.argument("file_url")
@click.option("--material", "-m", required=True, help="Material type.")
@click.option("--printer", "-p", default=None, help="Target printer ID (auto-assign if omitted).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def network_submit(file_url: str, material: str, printer: str | None, json_mode: bool) -> None:
    """Deprecated alias for `partner submit`."""
    _network_alias_warning()
    try:
        _provider_submit(file_url=file_url, material=material, printer=printer, json_mode=json_mode)
    except click.ClickException:
        raise
    except ThreeDOSError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@network.command("status")
@click.argument("job_id")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def network_status(job_id: str, json_mode: bool) -> None:
    """Deprecated alias for `partner status`."""
    _network_alias_warning()
    try:
        _provider_status(job_id=job_id, json_mode=json_mode)
    except click.ClickException:
        raise
    except ThreeDOSError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# cache — local model cache
# ---------------------------------------------------------------------------


@cli.group()
def cache() -> None:
    """Manage the local 3D model cache."""


@cache.command("list")
@click.option("--limit", "-n", default=50, help="Maximum results.")
@click.option("--offset", default=0, help="Pagination offset.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def cache_list(limit: int, offset: int, json_mode: bool) -> None:
    """List all cached models."""
    from kiln.model_cache import get_model_cache

    try:
        entries = get_model_cache().list_all(limit=limit, offset=offset)
        data = [e.to_dict() for e in entries]

        if json_mode:
            click.echo(
                json.dumps(
                    {"status": "success", "data": {"entries": data, "count": len(data)}},
                    indent=2,
                )
            )
            return

        if not data:
            click.echo("No cached models.")
            return

        header = f"{'ID':<18} {'File':<30} {'Source':<14} {'Size':>10} {'Prints':>6}"
        click.echo(header)
        click.echo("-" * len(header))
        for e in data:
            size_kb = e["file_size_bytes"] / 1024
            size_str = f"{size_kb:.0f} KB" if size_kb < 1024 else f"{size_kb / 1024:.1f} MB"
            click.echo(
                f"{e['cache_id']:<18} {e['file_name']:<30} {e['source']:<14} {size_str:>10} {e['print_count']:>6}"
            )
    except OSError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@cache.command("search")
@click.argument("query")
@click.option("--source", "-s", default=None, help="Filter by source.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def cache_search(query: str, source: str | None, json_mode: bool) -> None:
    """Search cached models by name, tags, or prompt."""
    from kiln.model_cache import get_model_cache

    try:
        entries = get_model_cache().search(query=query, source=source)
        data = [e.to_dict() for e in entries]

        if json_mode:
            click.echo(
                json.dumps(
                    {"status": "success", "data": {"entries": data, "count": len(data)}},
                    indent=2,
                )
            )
            return

        if not data:
            click.echo(f"No cached models matching {query!r}.")
            return

        for e in data:
            tags_str = ", ".join(e.get("tags", []))
            click.echo(f"{e['cache_id']}  {e['file_name']}  [{e['source']}]  tags={tags_str}")
    except OSError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@cache.command("add")
@click.argument("file_path", type=click.Path(exists=True))
@click.option("--source", "-s", required=True, help="Model source (myminifactory, meshy, upload, ...).")
@click.option("--tags", "-t", default=None, help="Comma-separated tags.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def cache_add(file_path: str, source: str, tags: str | None, json_mode: bool) -> None:
    """Add a model file to the local cache."""
    from kiln.model_cache import get_model_cache

    try:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        entry = get_model_cache().add(file_path, source=source, tags=tag_list)

        if json_mode:
            click.echo(
                json.dumps(
                    {"status": "success", "data": entry.to_dict()},
                    indent=2,
                )
            )
            return

        click.echo(f"Cached: {entry.cache_id}  {entry.file_name}  ({entry.file_size_bytes} bytes)")
    except OSError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@cache.command("delete")
@click.argument("cache_id")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def cache_delete(cache_id: str, json_mode: bool) -> None:
    """Remove a model from the cache."""
    from kiln.model_cache import get_model_cache

    try:
        deleted = get_model_cache().delete(cache_id)
        if not deleted:
            msg = f"No cached model with id {cache_id!r}."
            if json_mode:
                click.echo(json.dumps({"status": "error", "error": msg}, indent=2))
            else:
                click.echo(msg)
            sys.exit(1)

        if json_mode:
            click.echo(json.dumps({"status": "success", "cache_id": cache_id}, indent=2))
        else:
            click.echo(f"Deleted cached model {cache_id}.")
    except OSError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# trust / untrust — mDNS discovery whitelist
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("host")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def trust(host: str, json_mode: bool) -> None:
    """Add a printer host to the trusted whitelist."""
    from kiln.cli.config import add_trusted_printer

    try:
        add_trusted_printer(host)
        if json_mode:
            click.echo(json.dumps({"status": "success", "host": host}, indent=2))
        else:
            click.echo(f"Trusted: {host}")
    except ValueError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@cli.command()
@click.argument("host")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def untrust(host: str, json_mode: bool) -> None:
    """Remove a printer host from the trusted whitelist."""
    from kiln.cli.config import remove_trusted_printer

    try:
        remove_trusted_printer(host)
        if json_mode:
            click.echo(json.dumps({"status": "success", "host": host}, indent=2))
        else:
            click.echo(f"Untrusted: {host}")
    except ValueError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# backup / restore
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--output", "-o", default=None, help="Output file path for backup.")
@click.option("--no-redact", is_flag=True, help="Skip credential redaction.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def backup(output: str | None, no_redact: bool, json_mode: bool) -> None:
    """Back up the Kiln database with credential redaction."""
    from kiln.backup import BackupError, backup_database
    from kiln.persistence import get_db

    try:
        db = get_db()
        result_path = backup_database(
            db.path,
            output,
            redact_credentials=not no_redact,
        )
        data = {"backup_path": result_path, "redacted": not no_redact}
        if json_mode:
            click.echo(format_response("success", data=data, json_mode=True))
        else:
            redact_note = " (credentials redacted)" if not no_redact else ""
            click.echo(f"Backup saved to {result_path}{redact_note}")
    except BackupError as exc:
        click.echo(format_error(str(exc), code="BACKUP_ERROR", json_mode=json_mode))
        sys.exit(1)
    except OSError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@cli.command()
@click.argument("backup_path", type=click.Path(exists=True))
@click.option("--force", is_flag=True, help="Overwrite existing database.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def restore(backup_path: str, force: bool, json_mode: bool) -> None:
    """Restore the Kiln database from a backup file."""
    from kiln.backup import BackupError, restore_database
    from kiln.persistence import get_db

    try:
        db = get_db()
        result_path = restore_database(backup_path, db.path, force=force)
        data = {"restored_path": result_path}
        if json_mode:
            click.echo(format_response("success", data=data, json_mode=True))
        else:
            click.echo(f"Database restored to {result_path}")
    except BackupError as exc:
        click.echo(format_error(str(exc), code="RESTORE_ERROR", json_mode=json_mode))
        sys.exit(1)
    except OSError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# setup-agent
# ---------------------------------------------------------------------------


@cli.command("setup-agent")
@click.option("--workspace", "-w", default=None, help="Path to agent workspace (auto-detects if omitted).")
@click.option("--force", is_flag=True, help="Overwrite existing skill file.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def setup_agent(ctx: click.Context, workspace: str | None, force: bool, json_mode: bool) -> None:
    """Install the Kiln skill into an AI agent workspace.

    Auto-detects agent workspaces (Claude Code, Cursor, Windsurf) or
    specify --workspace to target a specific directory.
    """
    from kiln.skill_manifest import detect_agent_workspaces, install_skill

    if workspace:
        result = install_skill(workspace, force=force)
        if json_mode:
            click.echo(json.dumps(result))
        else:
            if result["success"]:
                click.echo(f"Skill installed to {result['installed_path']}")
            else:
                click.echo(f"Error: {result['error']}", err=True)
                ctx.exit(1)
    else:
        workspaces = detect_agent_workspaces()
        if json_mode:
            click.echo(json.dumps({"workspaces": workspaces}))
        else:
            if not workspaces:
                click.echo("No agent workspaces detected. Use --workspace to specify one.")
                ctx.exit(1)
            for ws in workspaces:
                status = "installed" if ws["skill_installed"] else "not installed"
                click.echo(f"  {ws['agent_type']:15s} {ws['path']}  [{status}]")
            click.echo()
            click.echo("Run 'kiln setup-agent --workspace <path>' to install.")


# ---------------------------------------------------------------------------
# autonomy
# ---------------------------------------------------------------------------


@cli.group()
def autonomy() -> None:
    """Manage agent autonomy level and constraints.

    Controls how much freedom the AI agent has when operating the
    printer -- from confirm-everything to full trust.
    """


@autonomy.command("show")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def autonomy_show(json_mode: bool) -> None:
    """Show the current autonomy level and constraints."""
    from kiln.autonomy import load_autonomy_config

    try:
        cfg = load_autonomy_config()
        data = cfg.to_dict()
        if json_mode:
            click.echo(format_response("success", data=data, json_mode=True))
        else:
            level_names = {0: "Confirm All", 1: "Pre-screened", 2: "Full Trust"}
            name = level_names.get(data["level"], "Unknown")
            click.echo(f"Autonomy level: {data['level']} ({name})")
            constraints = data.get("constraints", {})
            if constraints:
                click.echo("Constraints:")
                for key, val in constraints.items():
                    click.echo(f"  {key}: {val}")
            else:
                click.echo("Constraints: (none)")
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@autonomy.command("set")
@click.argument("level", type=click.IntRange(0, 2))
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def autonomy_set(level: int, json_mode: bool) -> None:
    """Set the autonomy level (0, 1, or 2).

    \b
    0 = Confirm All   -- every confirm-level tool requires approval
    1 = Pre-screened   -- confirm-level tools allowed if constraints pass
    2 = Full Trust     -- all tools allowed except emergency-level
    """
    from kiln.autonomy import (
        AutonomyConfig,
        AutonomyLevel,
        load_autonomy_config,
        save_autonomy_config,
    )

    try:
        existing = load_autonomy_config()
        new_config = AutonomyConfig(level=AutonomyLevel(level), constraints=existing.constraints)
        save_autonomy_config(new_config)
        data = new_config.to_dict()
        if json_mode:
            click.echo(format_response("success", data=data, json_mode=True))
        else:
            level_names = {0: "Confirm All", 1: "Pre-screened", 2: "Full Trust"}
            name = level_names.get(level, "Unknown")
            click.echo(f"Autonomy level set to {level} ({name})")
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@autonomy.command("configure")
@click.option("--max-print-time", type=int, default=None, help="Max print time in seconds.")
@click.option(
    "--allowed-materials", type=str, default=None, help="Comma-separated list of allowed materials (e.g. PLA,PETG)."
)
@click.option("--max-tool-temp", type=float, default=None, help="Max tool/nozzle temperature.")
@click.option("--max-bed-temp", type=float, default=None, help="Max bed temperature.")
@click.option("--allowed-tools", type=str, default=None, help="Comma-separated tool whitelist.")
@click.option("--blocked-tools", type=str, default=None, help="Comma-separated tool blocklist.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def autonomy_configure(
    max_print_time: int | None,
    allowed_materials: str | None,
    max_tool_temp: float | None,
    max_bed_temp: float | None,
    allowed_tools: str | None,
    blocked_tools: str | None,
    json_mode: bool,
) -> None:
    """Set Level 1 constraints for pre-screened autonomy.

    Only values you provide are updated; omitted values keep their
    current setting.  Pass empty string to clear a list constraint.
    """
    from kiln.autonomy import load_autonomy_config, save_autonomy_config

    try:
        cfg = load_autonomy_config()
        c = cfg.constraints

        if max_print_time is not None:
            c.max_print_time_seconds = max_print_time if max_print_time > 0 else None
        if allowed_materials is not None:
            c.allowed_materials = [m.strip() for m in allowed_materials.split(",") if m.strip()] or None
        if max_tool_temp is not None:
            c.max_tool_temp = max_tool_temp if max_tool_temp > 0 else None
        if max_bed_temp is not None:
            c.max_bed_temp = max_bed_temp if max_bed_temp > 0 else None
        if allowed_tools is not None:
            c.allowed_tools = [t.strip() for t in allowed_tools.split(",") if t.strip()] or None
        if blocked_tools is not None:
            c.blocked_tools = [t.strip() for t in blocked_tools.split(",") if t.strip()] or None

        save_autonomy_config(cfg)
        data = cfg.to_dict()
        if json_mode:
            click.echo(format_response("success", data=data, json_mode=True))
        else:
            click.echo("Autonomy constraints updated:")
            constraints = data.get("constraints", {})
            if constraints:
                for key, val in constraints.items():
                    click.echo(f"  {key}: {val}")
            else:
                click.echo("  (none)")
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# watch (first-layer monitoring)
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--printer", default=None, help="Printer name.")
@click.option("--delay", default=120, type=int, help="Seconds before first check.")
@click.option("--checks", default=3, type=int, help="Number of first-layer snapshots.")
@click.option("--interval", default=60, type=int, help="Seconds between checks.")
@click.option("--json", "use_json", is_flag=True, help="JSON output.")
def watch(printer: str | None, delay: int, checks: int, interval: int, use_json: bool) -> None:
    """Monitor an active print's first layer with webcam snapshots."""
    from kiln.print_monitor import FirstLayerMonitor, MonitorPolicy

    try:
        # Load adapter — respect --printer flag or fall back to active printer
        cfg = load_printer_config(printer)
        ok, err = validate_printer_config(cfg)
        if not ok:
            click.echo(format_error(f"Invalid printer config: {err}", json_mode=use_json))
            sys.exit(1)
        adapter = _make_adapter(cfg)

        policy = MonitorPolicy(
            delay_seconds=delay,
            num_checks=checks,
            interval_seconds=interval,
            auto_pause=True,
        )
        monitor = FirstLayerMonitor(adapter, policy=policy, monitor_id="cli")

        if not use_json:
            total_wait = delay + checks * interval
            click.echo(
                f"Monitoring first layer: waiting {delay}s, "
                f"then {checks} checks every {interval}s "
                f"(~{total_wait}s total)..."
            )

        result = monitor.run()

        if use_json:
            click.echo(
                json.dumps(
                    {
                        "status": "success" if result.outcome == "success" else "error",
                        "data": result.to_dict(),
                    },
                    indent=2,
                )
            )
        else:
            click.echo(f"Outcome: {result.outcome}")
            click.echo(f"Elapsed: {result.elapsed_seconds:.1f}s")
            if result.snapshots:
                click.echo(f"Snapshots captured: {len(result.snapshots)}")
                for snap in result.snapshots:
                    idx = snap.get("check_index", "?")
                    pct = snap.get("completion_percent")
                    pct_str = f" ({pct:.0f}%)" if pct is not None else ""
                    click.echo(f"  Snapshot {idx}{pct_str}")
            elif result.snapshot_failures:
                click.echo(f"Snapshot failures: {result.snapshot_failures}")
            if result.message:
                click.echo(result.message)

    except click.ClickException:
        raise
    except PrinterError as exc:
        click.echo(format_error(str(exc), json_mode=use_json))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=use_json))
        sys.exit(1)


# ---------------------------------------------------------------------------
# Enterprise commands
# ---------------------------------------------------------------------------


@cli.command("audit-export")
@click.option("--format", "fmt", type=click.Choice(["json", "csv"]), default="json", help="Output format.")
@click.option("--start", "start_time", default=None, type=float, help="Start timestamp (Unix).")
@click.option("--end", "end_time", default=None, type=float, help="End timestamp (Unix).")
@click.option("--tool", "tool_name", default=None, help="Filter by tool name.")
@click.option("--action", default=None, help="Filter by action (executed, blocked, etc.).")
@click.option("--session", "session_id", default=None, help="Filter by session ID.")
@click.option("--json", "json_mode", is_flag=True, help="Wrap output in JSON envelope.")
def audit_export(
    fmt: str,
    start_time: float | None,
    end_time: float | None,
    tool_name: str | None,
    action: str | None,
    session_id: str | None,
    json_mode: bool,
) -> None:
    """Export the safety audit trail as JSON or CSV (Enterprise)."""
    try:
        from kiln.persistence import get_db

        db = get_db()
        exported = db.export_audit_trail(
            start_time=start_time,
            end_time=end_time,
            format=fmt,
            tool_name=tool_name,
            action=action,
            session_id=session_id,
        )
        if json_mode:
            click.echo(format_response("success", data={"format": fmt, "export": exported}, json_mode=True))
        else:
            click.echo(exported)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@cli.group("team")
def team_group() -> None:
    """Manage team members and seats (Business/Enterprise)."""
    pass


@team_group.command("add")
@click.argument("email")
@click.option("--role", type=click.Choice(["admin", "engineer", "operator"]), default="engineer", help="Member role.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def team_add(email: str, role: str, json_mode: bool) -> None:
    """Add a team member."""
    try:
        from kiln.licensing import get_tier
        from kiln.teams import TeamManager

        mgr = TeamManager()
        tier = get_tier().value
        member = mgr.add_member(email, role=role, tier=tier)
        if json_mode:
            click.echo(format_response("success", data=member.to_dict(), json_mode=True))
        else:
            click.echo(f"Added {email} as {role}.")
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@team_group.command("remove")
@click.argument("email")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def team_remove(email: str, json_mode: bool) -> None:
    """Remove a team member."""
    try:
        from kiln.teams import TeamManager

        mgr = TeamManager()
        removed = mgr.remove_member(email)
        if not removed:
            click.echo(format_error(f"No active member: {email}", json_mode=json_mode))
            sys.exit(1)
        if json_mode:
            click.echo(format_response("success", data={"removed": email}, json_mode=True))
        else:
            click.echo(f"Removed {email}.")
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@team_group.command("list")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def team_list(json_mode: bool) -> None:
    """List team members and seat usage."""
    try:
        from kiln.licensing import get_tier
        from kiln.teams import TeamManager

        mgr = TeamManager()
        tier = get_tier().value
        members = mgr.list_members()
        seats = mgr.seat_status(tier=tier)

        if json_mode:
            click.echo(format_response(
                "success",
                data={"members": [m.to_dict() for m in members], "seats": seats},
                json_mode=True,
            ))
        else:
            click.echo(f"Team ({seats['used']} seats used):")
            for m in members:
                click.echo(f"  {m.email} [{m.role}]")
            limit = seats.get("limit")
            if limit:
                click.echo(f"Seat limit: {seats['used']}/{limit}")
            else:
                click.echo("Seats: unlimited (Enterprise)")
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@cli.command()
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def uptime(json_mode: bool) -> None:
    """Show uptime statistics and SLA status (Enterprise)."""
    try:
        from kiln.uptime import get_uptime_tracker

        tracker = get_uptime_tracker()
        report = tracker.uptime_report()

        if json_mode:
            click.echo(format_response("success", data=report, json_mode=True))
        else:
            for window in ("1h", "24h", "7d", "30d"):
                val = report.get(f"uptime_{window}")
                label = f"Uptime ({window}):"
                click.echo(f"  {label:<18} {val:.2f}%" if val is not None else f"  {label:<18} N/A")
            sla = report.get("sla_met")
            click.echo(f"  SLA (99.9%):       {'MET' if sla else 'NOT MET'}")
            click.echo(f"  Total checks:      {report.get('total_checks', 0)}")
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
