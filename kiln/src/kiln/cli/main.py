"""Kiln CLI — agent-friendly command-line interface for 3D printers.

Provides a unified ``kiln`` command with subcommands for printer discovery,
configuration, control, and monitoring.  Every subcommand supports a
``--json`` flag for machine-parseable output suitable for agent consumption.

The ``kiln serve`` subcommand starts the MCP server (original ``kiln``
behaviour).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import deque
from datetime import datetime as _dt
from datetime import timezone as _tz
from pathlib import Path
from typing import Any

import click

from kiln.auto_record_hook import note_cancel_requested
from kiln.print_start_verdict import resolve_print_start
from kiln.printer_backends import (
    DEFAULT_SERIAL_BAUDRATE,
    NETWORK_PRINTER_TYPES,
    PRINTER_TYPE_LABELS,
    PRINTER_TYPES,
    format_printer_types,
)
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

from kiln.cli.auth_commands import register_auth_cli
from kiln.cli.bridge_commands import register_bridge_cli
from kiln.cli.config import (
    _normalize_printer_type,
    load_printer_config,
    remove_printer,
    save_printer,
    set_active_printer,
    validate_printer_config,
)
from kiln.cli.config import (
    list_printers as _list_printers,
)
from kiln.cli.install_mcp import register_install_mcp_cli
from kiln.cli.install_openscad import register_install_openscad_cli
from kiln.cli.install_step_backend import register_install_step_backend_cli
from kiln.cli.output import (
    format_action,
    format_discovered,
    format_error,
    format_files,
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
from kiln.cli.spend_caps_commands import register_spend_caps_cli
from kiln.materials import MATERIAL_TEMPS, normalise_material_type
from kiln.routing_candidates import (
    adapter_supports_extension,
    collect_routing_candidates,
)

logger = logging.getLogger(__name__)

_MATERIAL_CHOICES: tuple[str, ...] = ("PLA", "PETG", "ABS", "TPU", "ASA", "Nylon", "PC")
# Material temps and name normalisation moved to kiln.materials, and
# candidate building to kiln.routing_candidates, so the route_print_job
# tool can reach the same code without importing the CLI.  Aliased back
# under their original private names: this module's call sites and the
# tests that patch them are unchanged.
_MATERIAL_TEMPS = MATERIAL_TEMPS
_normalise_material_type = normalise_material_type
_adapter_supports_extension = adapter_supports_extension
_collect_routing_candidates = collect_routing_candidates
_SUPPORT_MODE_CHOICES: tuple[str, ...] = ("off", "auto", "minimal", "aggressive")
_INGEST_EXTENSIONS: tuple[str, ...] = (".gcode", ".gco", ".g", ".3mf")


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


def _resolve_generation_provider(provider: str) -> GenerationProvider:  # noqa: F821
    """Resolve a generation provider by name.

    Handles openscad and meshy as direct imports, and routes all other
    providers through the registry (auto-discovers from env vars).
    Gives an actionable error when the API key is missing.
    """
    from kiln.generation import MeshyProvider, OpenSCADProvider
    from kiln.generation.registry import GenerationRegistry

    if provider == "openscad":
        return OpenSCADProvider()
    if provider == "meshy":
        return MeshyProvider()
    if provider == "gemini":
        from kiln.generation.gemini import GeminiDeepThinkProvider

        return GeminiDeepThinkProvider()

    registry = GenerationRegistry()
    registry.auto_discover()

    try:
        return registry.get(provider)
    except Exception as exc:
        # Give a helpful hint about which env var is needed
        hint_map = {
            "tripo3d": "KILN_TRIPO3D_API_KEY",
            "stability": "KILN_STABILITY_API_KEY",
        }
        env_var = hint_map.get(provider)
        if env_var:
            import os

            if not os.environ.get(env_var, "").strip():
                from kiln.generation.base import GenerationError

                raise GenerationError(
                    f"Provider {provider!r} requires {env_var} to be set.",
                    code="AUTH_ERROR",
                ) from exc
        raise


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
            import shlex

            safe_path = shlex.quote(preview_path)
            cmd = cmd_template.replace("{path}", safe_path)
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


def _resolve_emergency_printer_name(ctx: click.Context, printer_name: str | None = None) -> str:
    """Resolve printer identifier used for emergency latch checks."""
    if printer_name and printer_name.strip():
        return printer_name.strip()
    selected = (ctx.obj or {}).get("printer")
    if isinstance(selected, str) and selected.strip():
        return selected.strip()
    return "default"


def _emergency_latch_status(printer_name: str) -> dict[str, Any] | None:
    """Best-effort emergency latch status lookup for CLI safety gates."""
    try:
        from kiln.emergency import get_emergency_coordinator

        return get_emergency_coordinator().get_latch_status(printer_name)
    except Exception as exc:
        logger.debug("Emergency status lookup failed for %s: %s", printer_name, exc)
        return None


def _emergency_block_message(printer_name: str, status: dict[str, Any]) -> str:
    blockers = status.get("critical_interlocks_pending") or []
    message = f"Emergency latch is active for printer '{printer_name}'."
    if blockers:
        message += " Critical interlocks pending: " + ", ".join(str(x) for x in blockers) + "."
    message += " Resolve hazards, acknowledge, then clear with `kiln emergency-clear`."
    return message


# ---------------------------------------------------------------------------
# Adapter factory
# ---------------------------------------------------------------------------


class _PrinterTypeChoice(click.Choice):
    """A ``--type`` that accepts renamed spellings and shows the current ones.

    ``click.Choice`` rejects a value before any of our code sees it, so a
    plain Choice would break every script still passing ``--type serial``
    even though config.yaml and the env var both accept it.  Normalizing
    first means the alias table is honoured at the parse layer too, while
    ``--help`` lists only the canonical names.
    """

    def convert(self, value, param, ctx):  # noqa: ANN001, ANN201
        return super().convert(
            _normalize_printer_type(str(value).strip().lower()), param, ctx
        )


def _make_adapter(cfg: dict[str, Any]):
    """Create a PrinterAdapter from a config dict."""
    from kiln.printers import (
        BambuAdapter,
        CrealityAdapter,
        DuetAdapter,
        ElegooAdapter,
        MoonrakerAdapter,
        OctoPrintAdapter,
        PrusaLinkAdapter,
    )

    # Callers hand us anything from a hand-written config.yaml, so honour
    # the legacy aliases here too rather than trusting every caller to.
    ptype = _normalize_printer_type(cfg.get("type", "octoprint"))
    host = cfg.get("host", "")

    if ptype == "octoprint":
        return OctoPrintAdapter(host=host, api_key=cfg.get("api_key", ""))
    elif ptype == "moonraker":
        return MoonrakerAdapter(host=host, api_key=cfg.get("api_key") or None)
    elif ptype == "duet":
        # The machine password (M551) travels in the generic api_key slot;
        # omit it entirely so the adapter applies the firmware default.
        _password = cfg.get("api_key") or None
        return DuetAdapter(host=host, **({"password": _password} if _password else {}))
    elif ptype == "creality":
        return CrealityAdapter(
            host=host,
            api_key=cfg.get("api_key") or None,
            model=cfg.get("printer_model") or None,
        )
    elif ptype == "bambu":
        if BambuAdapter is None:
            raise click.ClickException("Bambu support requires paho-mqtt. Install it with: pip install paho-mqtt")
        return BambuAdapter(
            host=host,
            access_code=cfg.get("access_code", ""),
            serial=cfg.get("serial", ""),
            printer_model=cfg.get("printer_model") or None,
        )
    elif ptype == "elegoo":
        if ElegooAdapter is None:
            raise click.ClickException(
                "Elegoo support requires websocket-client. "
                "Install it with: uv pip install 'kiln3d[elegoo]' or pip install websocket-client"
            )
        return ElegooAdapter(host=host, mainboard_id=cfg.get("serial") or "")
    elif ptype == "prusalink":
        return PrusaLinkAdapter(
            host=host,
            api_key=cfg.get("api_key") or None,
        )
    elif ptype == "usb":
        # `kiln auth --type serial` and register_printer() both store the
        # port path in `host`; `port` is the key config.yaml uses when it
        # was hand-written.  Accept either — without this branch every
        # `kiln` command refused a USB printer the server happily drove.
        from kiln.printers import SerialPrinterAdapter

        port = cfg.get("port") or host
        if not port:
            raise click.ClickException(
                "Serial printers need a port path (e.g. /dev/ttyUSB0, COM3)."
            )
        return SerialPrinterAdapter(
            port=port,
            baudrate=int(cfg.get("baudrate") or DEFAULT_SERIAL_BAUDRATE),
        )
    else:
        raise click.ClickException(
            f"Unknown printer type: {ptype!r}. "
            f"Supported: {format_printer_types()}."
        )


def _ams_flags_unsupported_message(adapter: Any) -> str:
    """Why ``--use-ams`` / ``--ams-mapping`` cannot be honoured at *adapter*.

    Names what the printer DOES have — a Klipper MMU Kiln can see but not
    drive, or nothing — so the refusal is a fact about the machine, not a
    bare "unsupported".
    """
    from kiln.multi_material import multi_material_status

    mm = multi_material_status(adapter)
    base = "--use-ams and --ams-mapping are Bambu AMS instructions"
    if mm.detected:
        return (
            f"{base}, and this printer's {mm.label} is not driven by Kiln. "
            f"{mm.describe()} Drop the flags: the unit's own tool map routes "
            f"each tool change (Happy Hare: MMU_TTG_MAP)."
        )
    if mm.kind == "unknown":
        return f"{base}. {mm.describe()} Drop the flags, or check the printer connection."
    return (
        f"{base}, and this printer ({getattr(adapter, 'name', type(adapter).__name__)}) "
        "reports no multi-material unit. Drop the flags to print from its single feed."
    )


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
        pname = printer_name or "(default)"
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



def _list_configured_printer_names() -> list[str]:
    """Return configured printer names in stable order."""
    names: list[str] = []
    for entry in _list_printers():
        name = str(entry.get("name", "")).strip()
        if name:
            names.append(name)
    return names


def _load_fleet_adapters(printer_filter: str | None = None) -> tuple[dict[str, Any], list[str]]:
    """Load adapters for configured printers, optionally narrowed to one printer."""
    errors: list[str] = []
    adapters: dict[str, Any] = {}

    if printer_filter:
        targets = [printer_filter.strip()]
    else:
        targets = _list_configured_printer_names()

    if not targets:
        return {}, ["No configured printers found. Run 'kiln setup' or 'kiln auth' first."]

    for name in targets:
        try:
            cfg = load_printer_config(name)
            ok, err = validate_printer_config(cfg)
            if not ok:
                errors.append(f"{name}: invalid config ({err})")
                continue
            adapters[name] = _make_adapter(cfg)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    return adapters, errors



def _route_printer_for_job(
    *,
    material: str,
    candidates: list[dict[str, Any]],
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """Return (printer_name, routing_data, error_message)."""
    if not candidates:
        return None, None, "No eligible printers available for this file/material."

    from kiln.job_router import RoutingCriteria, get_job_router

    try:
        router = get_job_router()
        result = router.route_job(
            RoutingCriteria(material=material),
            available_printers=candidates,
        )
        chosen = result.recommended_printer.printer_id
        return chosen, result.to_dict(), None
    except Exception as exc:
        # Deterministic fallback: prefer lowest queue depth, then idle.
        ranked = sorted(
            candidates,
            key=lambda c: (
                int(c.get("queue_depth", 0)),
                0 if str(c.get("status", "unknown")) == "idle" else 1,
                str(c.get("printer_id", "")),
            ),
        )
        if not ranked:
            return None, None, f"Routing failed: {exc}"
        return str(ranked[0]["printer_id"]), None, None


def _scan_ingest_directory(watch_dir: Path, seen: dict[str, float]) -> list[Path]:
    """Return newly created/updated printable files since the last scan."""
    discovered: list[Path] = []
    for entry in sorted(watch_dir.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_file():
            continue
        if entry.suffix.lower() not in _INGEST_EXTENSIONS:
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        key = str(entry.resolve())
        if seen.get(key) == mtime:
            continue
        seen[key] = mtime
        discovered.append(entry)
    return discovered


def _normalise_ingest_seen(raw_seen: Any) -> dict[str, float]:
    """Normalize persisted seen-map payload into {path: mtime}."""
    if not isinstance(raw_seen, dict):
        return {}
    seen: dict[str, float] = {}
    for key, value in raw_seen.items():
        if not isinstance(key, str):
            continue
        try:
            seen[key] = float(value)
        except (TypeError, ValueError):
            continue
    return seen


def _load_ingest_seen_state(state_path: Path) -> dict[str, float]:
    """Load persisted ingest seen-state from disk."""
    payload = _read_json_file(state_path, default={})
    return _normalise_ingest_seen(payload.get("seen"))


def _save_ingest_seen_state(state_path: Path, watch_dir: Path, seen: dict[str, float]) -> None:
    """Persist ingest seen-state to disk."""
    payload = {
        "watch_dir": str(watch_dir),
        "updated_at": time.time(),
        "seen": seen,
    }
    _write_json_file(state_path, payload)


def _filter_stable_ingest_files(
    detected: list[Path],
    *,
    seen: dict[str, float],
    min_stable_seconds: float,
) -> list[Path]:
    """Keep only files old enough to avoid ingesting partially-written files."""
    if min_stable_seconds <= 0:
        return detected

    stable: list[Path] = []
    now = time.time()
    for path in detected:
        try:
            stat = path.stat()
            age = now - stat.st_mtime
        except OSError:
            continue
        if age >= min_stable_seconds:
            stable.append(path)
            continue
        # Re-arm the file so the next scan can pick it up once stable.
        with contextlib.suppress(OSError):
            seen.pop(str(path.resolve()), None)
    return stable


def _default_ingest_service_dir() -> Path:
    """Return the base directory for ingest service metadata files."""
    override = os.environ.get("KILN_INGEST_SERVICE_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".kiln" / "ingest_service"


def _default_ingest_service_config_path() -> Path:
    return _default_ingest_service_dir() / "service.json"


def _default_ingest_pid_path() -> Path:
    return _default_ingest_service_dir() / "service.pid"


def _default_ingest_log_path() -> Path:
    return _default_ingest_service_dir() / "service.log"


def _default_ingest_state_path() -> Path:
    return _default_ingest_service_dir() / "watch_state.json"


def _read_json_file(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load JSON file content with safe defaults."""
    if not path.exists():
        return dict(default or {})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return dict(default or {})


def _write_json_file(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically to reduce state-file corruption risk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _is_pid_running(pid: int) -> bool:
    """Return True when a process exists for PID."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _read_pid_file(pid_path: Path) -> int | None:
    """Read PID from file, returning None when missing/invalid."""
    if not pid_path.exists():
        return None
    try:
        value = pid_path.read_text(encoding="utf-8").strip()
        if not value:
            return None
        return int(value)
    except Exception:
        return None


def _resolve_kiln_command() -> list[str]:
    """Resolve executable command prefix for launching Kiln subprocesses."""
    kiln_bin = shutil.which("kiln")
    if kiln_bin:
        return [kiln_bin]
    return [sys.executable, "-m", "kiln"]


def _build_ingest_watch_command(config: dict[str, Any]) -> list[str]:
    """Build command line for background ingest watch process."""
    cmd = _resolve_kiln_command() + [
        "ingest",
        "watch",
        "--dir",
        str(config["watch_dir"]),
        "--interval",
        str(config.get("interval", 2.0)),
        "--state-file",
        str(config["state_file"]),
        "--min-stable-seconds",
        str(config.get("min_stable_seconds", 2.0)),
    ]
    if bool(config.get("auto_queue", False)):
        cmd.append("--auto-queue")
    printer = str(config.get("printer", "") or "").strip()
    if printer:
        cmd.extend(["--printer", printer])
    material = str(config.get("material", "PLA") or "PLA").strip().upper()
    if material:
        cmd.extend(["--material", material])
    return cmd


def _tail_text(path: Path, max_lines: int = 30) -> str:
    """Return tail lines from a text file for diagnostics."""
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    return "\n".join(lines[-max_lines:])


def _resolve_service_config_path(config_path: str | None) -> Path:
    """Resolve ingest service config file path."""
    if config_path:
        return Path(config_path).expanduser().resolve()
    return _default_ingest_service_config_path()


def _resolve_service_sidecar_paths(config_path: Path, config: dict[str, Any]) -> tuple[Path, Path, Path]:
    """Resolve pid/log/state paths from config with safe defaults."""
    config_dir = config_path.parent
    pid_path = Path(str(config.get("pid_file") or (config_dir / "service.pid"))).expanduser().resolve()
    log_path = Path(str(config.get("log_file") or (config_dir / "service.log"))).expanduser().resolve()
    state_path = Path(str(config.get("state_file") or (config_dir / "watch_state.json"))).expanduser().resolve()
    return pid_path, log_path, state_path


def _coerce_bool(value: Any) -> bool:
    """Interpret bool-like values from config payloads."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _map_printer_hint_to_profile_id(raw: str | None) -> str | None:
    """Map free-form printer model hints to bundled slicer profile IDs.

    Delegates to :mod:`kiln.printer_profile_ids`, the table the MCP
    server reads.  The CLI's own copy of this table never learned the
    Bambu models the server's copy gained in 2026-03, so ``kiln slice``
    resolved no profile for a Bambu and ``kiln print`` then wrapped
    generic-default gcode into a 3MF that assumes otherwise.
    """
    from kiln.printer_profile_ids import map_printer_hint_to_profile_id

    return map_printer_hint_to_profile_id(raw)


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
    aimed = ctx.obj.get("printer") if ctx.obj else None
    env_model = os.environ.get("KILN_PRINTER_MODEL")
    # ``KILN_PRINTER_MODEL`` states the DEFAULT machine's model, so when
    # ``--printer`` names another one, that machine's own config entry has
    # the better claim.  Env-first stays for an unaimed call, where the
    # variable is about the printer being asked after.
    if not aimed:
        mapped = _map_printer_hint_to_profile_id(env_model)
        if mapped:
            return mapped

    try:
        cfg = load_printer_config(aimed)
    except Exception:
        # An aimed call that can't read its target's config has no model —
        # falling back to the env var here would be the same borrow.
        return None

    for key in ("printer_id", "printer_model", "model", "profile"):
        mapped = _map_printer_hint_to_profile_id(str(cfg.get(key, "") or ""))
        if mapped:
            return mapped

    ptype = str(cfg.get("type", "")).strip().lower()
    if ptype != "prusalink":
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

    if str(cfg.get("type", "")).lower() != "prusalink":
        checks.append(
            {
                "name": "backend",
                "ok": False,
                "detail": "Active printer is not type 'prusalink'.",
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


def _run_creality_diagnostics(cfg: dict[str, Any]) -> dict[str, Any]:
    """Run non-destructive diagnostics for a Creality Moonraker config."""
    checks: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "host": cfg.get("host", ""),
        "type": cfg.get("type", ""),
        "checks": checks,
        "ok": False,
        "resolved_url": None,
        "browser_test_url": None,
        "klippy_state": None,
        "likely_cause": None,
        "user_message": None,
        "firmware_lockdown_possible": False,
        "connection_checklist": [],
        "cfs_status": None,
        "next_steps": [],
    }

    if str(cfg.get("type", "")).strip().lower() != "creality":
        checks.append(
            {
                "name": "backend",
                "ok": False,
                "detail": "Active printer is not type 'creality'.",
            }
        )
        return summary

    from kiln.printers.creality import diagnose_creality_moonraker

    diag = diagnose_creality_moonraker(
        str(cfg.get("host", "") or ""),
        api_key=cfg.get("api_key") or None,
        model=str(cfg.get("printer_model") or ""),
    )
    diag_dict = diag.to_dict()
    summary.update(
        {
            "ok": diag.ok,
            "resolved_url": diag.resolved_url,
            "browser_test_url": diag.browser_test_url,
            "klippy_state": diag.klippy_state,
            "likely_cause": diag.likely_cause,
            "user_message": diag.user_message,
            "firmware_lockdown_possible": diag.firmware_lockdown_possible,
            "connection_checklist": diag.connection_checklist,
            "next_steps": diag.next_steps,
        }
    )
    for check in diag_dict.get("checks", []):
        checks.append(
            {
                "name": "moonraker_probe",
                "ok": bool(check.get("ok")),
                "detail": f"{check.get('url')}: {check.get('detail')}",
                "warn": bool(check.get("auth_required")),
            }
        )

    if diag.ok:
        try:
            adapter = _make_adapter({**cfg, "host": diag.resolved_url or cfg.get("host", "")})
            if hasattr(adapter, "get_cfs_status"):
                cfs = adapter.get_cfs_status()
                summary["cfs_status"] = cfs
                checks.append(
                    {
                        "name": "cfs_discovery",
                        "ok": True,
                        "warn": bool(cfs.get("hardware_unverified", True)),
                        "detail": (
                            f"detected={bool(cfs.get('detected'))}; "
                            f"slots={cfs.get('slot_count') or 'unknown'}; "
                            "active slot control hardware-unverified"
                        ),
                    }
                )
        except Exception as exc:
            logger.debug("Creality CFS discovery failed: %s", exc)
            checks.append(
                {
                    "name": "cfs_discovery",
                    "ok": True,
                    "warn": True,
                    "detail": f"CFS discovery skipped: {exc}",
                }
            )

    return summary


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


class _DidYouMeanGroup(click.Group):
    """Click group subclass that suggests close matches for mistyped commands.

    When a user (or agent) types ``kiln printer status`` instead of
    ``kiln status``, Click normally prints a bare "No such command" error.
    This subclass uses :func:`difflib.get_close_matches` to suggest the
    closest valid command, reducing friction for agents and humans alike.

    It also resolves ``{tool_count}`` in the banner docstring against
    the live MCP registry the first time ``--help`` is rendered.  The
    lookup is lazy so importing the CLI never triggers loading the
    full MCP server — the registry is only queried if someone actually
    asks for help.
    """

    def format_help_text(
        self, ctx: click.Context, formatter: click.HelpFormatter
    ) -> None:
        """Render help text after substituting ``{tool_count}`` with the live count."""
        if self.help and "{tool_count}" in self.help:
            try:
                from kiln.skill_manifest import get_tool_count

                count = get_tool_count()
            except Exception:  # noqa: BLE001 — help must never crash
                count = 0
            # Fall back to a human-readable word if we couldn't reach
            # the registry (e.g. a minimal install with no server deps).
            replacement = str(count) if count else "many"
            original_help = self.help
            self.help = original_help.replace("{tool_count}", replacement)
            try:
                super().format_help_text(ctx, formatter)
            finally:
                self.help = original_help
            return
        super().format_help_text(ctx, formatter)

    def resolve_command(self, ctx: click.Context, args: list[str]) -> tuple:
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError as exc:
            import difflib

            cmd_name = args[0] if args else ""
            available = self.list_commands(ctx)
            matches = difflib.get_close_matches(cmd_name, available, n=3, cutoff=0.5)

            if matches:
                suggestions = ", ".join(f"'{m}'" for m in matches)
                hint = f"\n\nDid you mean: {suggestions}?"
                # Also check if any subword matches (e.g. "printer status" → "status")
                for word in cmd_name.split():
                    word_matches = difflib.get_close_matches(
                        word, available, n=1, cutoff=0.6
                    )
                    if word_matches and word_matches[0] not in matches:
                        hint += f"\n  (Hint: try 'kiln {word_matches[0]}')"
                raise click.UsageError(str(exc) + hint) from None
            raise


@click.group(cls=_DidYouMeanGroup)
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
    """Kiln — agent-friendly 3D printer control.

    \b
    🤖 AI agent? Use Kiln as an MCP server instead of CLI:
       kiln serve
       MCP provides {tool_count} tools with richer descriptions,
       structured JSON responses, and tool chaining.
       See: https://kiln3d.com/docs
    """
    # Load environment variables so API keys (KILN_GEMINI_API_KEY, etc.) are
    # available to all subcommands, not just the server.
    with contextlib.suppress(ImportError):
        from dotenv import load_dotenv

        load_dotenv()  # .env in cwd
        load_dotenv(Path.home() / ".kiln" / ".env")  # ~/.kiln/.env

    ctx.ensure_object(dict)
    ctx.obj["printer"] = printer

    # Terms-of-use gate (one-time, account-aware).  Once accepted, is_current()
    # short-circuits on the local record so this never prompts again.  Onboarding,
    # identity, config, and maintenance commands run BEFORE acceptance (see
    # _TERMS_GATE_EXEMPT); everything that does substantive work gates.  Two
    # failure modes kept distinct: a terms-check INFRA error fails OPEN (never
    # block a user over a DB/network hiccup), but a user who is SHOWN the terms
    # and declines/aborts must BLOCK — so _enforce_terms_gate's own SystemExit /
    # click.Abort / KeyboardInterrupt are NOT swallowed here.
    invoked = ctx.invoked_subcommand
    # Help is --help only (Kiln does not alias -h to help; -h is --host on the
    # auth command).  Matching '-h' here would let `kiln <cmd> -h <host>` bypass.
    help_requested = "--help" in sys.argv[1:]
    if invoked not in _TERMS_GATE_EXEMPT and not help_requested:
        try:
            from kiln.terms import is_current

            accepted = is_current()
        except Exception as exc:
            logger.debug("terms gate skipped (terms check failed): %s", exc)
            accepted = True  # fail OPEN only on a terms-check infra error
        if not accepted:
            # decline -> SystemExit(1); Ctrl-C / EOF -> click.Abort; both propagate.
            _enforce_terms_gate()

    # Kick a non-blocking PyPI update check, then soft-nag if a newer Kiln
    # is out.  Same discipline as the terms nag: stderr, interactive
    # terminals only (never in piped/agent output), and never fatal.  The
    # banner reads a cached result — the kick warms it for next time.
    try:
        from kiln.version_check import kick_background_check, update_banner_line

        kick_background_check()
        if sys.stderr.isatty() and ctx.invoked_subcommand not in ("self-update", None):
            line = update_banner_line()
            if line:
                click.echo(click.style(f"  {line}", fg="yellow"), err=True)
    except Exception as exc:
        logger.debug("Update check skipped: %s", exc)

    # Count this CLI invocation as a CLI session, and give the day's
    # counters a ride to the dashboard.  A CLI-only install never runs
    # the MCP server, so without this beat its telemetry NEVER uploads —
    # "does the CLI have users at all" stays unanswerable, which is the
    # question the surface split exists to answer.  The daemon
    # subcommands are excluded: `serve` is the MCP door (kiln.server.main
    # declares "mcp" and records its own session) and `rest` (kiln-pro)
    # is a server whose callers are not this terminal.  Both the session
    # record and the heartbeat are best-effort, daily-deduped, and
    # CI/container/test-suppressed by their own guards.
    if invoked not in ("serve", "rest"):
        try:
            from kiln.daily_stats import record_surface_session
            from kiln.heartbeat import send_heartbeat_async

            record_surface_session()
            send_heartbeat_async()
        except Exception as exc:
            logger.debug("Surface session record skipped: %s", exc)


# Commands that must run BEFORE terms acceptance — onboarding, identity, config,
# maintenance, the accept action itself, and the long-running server daemons
# (`serve` = MCP, `rest` = REST API), which gate per tool / per request rather
# than at boot.  Starting a server is an operator/infrastructure action, not an
# end user accepting terms — the end user accepts on their OWN surface (the web
# checkout, the MCP first-run gate), so gating the daemon's boot just bricks the
# server with no human at the keyboard to consent.  Everything else (the
# substantive print/design/control commands) gates.
# Gating the identity commands would brick onboarding and is CIRCULAR for OAuth
# users: `kiln signin` is what establishes the bearer is_current() needs to
# import a web-side acceptance.  Pinned by tests in test_cli_terms_gate.py so
# this set cannot silently drift.
_TERMS_GATE_EXEMPT = frozenset({
    None,
    "setup",
    "accept-terms",
    "serve",        # MCP server daemon — gates per tool, not at boot
    "rest",         # REST API server daemon — gates per request/tier, not at boot
    "auth",         # save printer credentials (config, setup-family)
    "self-update",  # maintenance — don't gate updating Kiln
    # setup / diagnostics — config-wiring + read-only checks that do NO
    # substantive print/design work.  Gating these only bricks a fresh install
    # before the user can wire Kiln up; consent still happens at first real tool
    # use (the MCP first-run gate) or first substantive CLI command.
    "install-mcp",
    "uninstall-mcp",
    "install-openscad",  # installs the design engine — a dependency, not design
    "verify",       # `kiln doctor` is an alias of `verify`
    "doctor",
    # identity / account (kiln.cli.auth_commands.register_auth_cli):
    "signin",
    "signout",
    "whoami",
    "pair",
    "link",
    "login",
    "logout",
    "invite",
})


def _terms_gate_interactive() -> bool:
    """True when we can prompt the user — a real terminal on both ends."""
    try:
        return sys.stdin.isatty() and sys.stderr.isatty()
    except Exception:
        return False


def _enforce_terms_gate() -> None:
    """Block until the user has accepted the current Terms of Use.

    One-time: after acceptance ``is_current()`` is True and the caller never
    reaches here again.  Three paths:

      * interactive terminal — show the summary and confirm (decline -> exit 1);
      * non-interactive + ``KILN_ACCEPT_TERMS=1`` — record acceptance by
        configuration (the operator has accepted) and proceed;
      * non-interactive without the flag — exit 1 with the fix, so CI / scripts
        fail loudly with the escape hatch instead of silently running unaccepted.
    """
    from kiln.terms import prompt_acceptance, record_acceptance

    if (os.environ.get("KILN_ACCEPT_TERMS") or "").strip().lower() in ("1", "true", "yes"):
        record_acceptance(method="env")
        return

    if _terms_gate_interactive():
        if prompt_acceptance(method="cli"):
            return
        click.echo("  You must accept the terms of use to use Kiln.", err=True)
        raise SystemExit(1)

    click.echo(
        click.style(
            "  Terms of use not yet accepted. Run 'kiln accept-terms' once "
            "(or set KILN_ACCEPT_TERMS=1 for unattended use).\n"
            "  Full terms: https://kiln3d.com/terms",
            fg="yellow",
        ),
        err=True,
    )
    raise SystemExit(1)


@cli.command("accept-terms")
@click.option("--yes", "-y", is_flag=True, help="Accept non-interactively (scripts / CI).")
def accept_terms(yes: bool) -> None:
    """Review and accept Kiln's Terms of Use (one-time).

    Account-aware: when you're signed in or licensed, acceptance is mirrored to
    your Kiln account so it is honored on your other devices.  For unattended
    use pass ``--yes`` or set ``KILN_ACCEPT_TERMS=1``.
    Full terms: https://kiln3d.com/terms
    """
    from kiln.terms import is_current, prompt_acceptance, record_acceptance, review

    if is_current():
        # Already accepted — SHOW the terms + when they were accepted, so the
        # command is a genuine review rather than a one-liner you can't inspect.
        review()
        return

    if yes or not _terms_gate_interactive():
        record_acceptance(method="cli_noninteractive")
        click.echo(
            click.style(
                "  Terms of use accepted. Full terms: https://kiln3d.com/terms",
                fg="green",
            )
        )
        return

    if not prompt_acceptance(method="cli"):
        click.echo("  Terms not accepted.", err=True)
        raise SystemExit(1)


def _ensure_utf8_streams() -> None:
    """Normalise stdout/stderr to UTF-8 when they report another encoding.

    The CLI prints non-ASCII status glyphs and box-drawing characters in
    both its help text and command output.  Windows consoles default to a
    legacy code page (cp1252 on US installs) whose codec cannot encode
    them, so ``click.echo`` raises ``UnicodeEncodeError`` and the command
    aborts before printing anything useful.

    Streams that already report UTF-8 — every normal macOS and Linux
    terminal — are left untouched, so this is a no-op outside the
    legacy-code-page case it exists to fix.
    """
    for stream in (sys.stdout, sys.stderr):
        encoding = getattr(stream, "encoding", None)
        if encoding and encoding.lower().replace("-", "") == "utf8":
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(OSError, ValueError):
            reconfigure(encoding="utf-8")


# ---------------------------------------------------------------------------
# self-update  (distinct from `upgrade`, which manages the Pro subscription)
# ---------------------------------------------------------------------------


@cli.command(name="self-update")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--dry-run", is_flag=True, help="Print the upgrade command without running it.")
def self_update(yes: bool, dry_run: bool) -> None:
    """Update the Kiln software to the latest published version.

    Runs ``pip install --upgrade kiln3d`` in the current interpreter.
    Kiln never updates itself automatically — this is the explicit path
    for when you want it done for you.  (To change your subscription
    tier, use ``kiln upgrade`` instead.)
    """
    import subprocess

    from kiln import __version__ as current
    from kiln.version_check import PACKAGE_NAME, latest_version

    latest = latest_version()
    if latest:
        click.echo(f"Installed: {current}    Latest on PyPI: {latest}")
        if latest == current:
            click.echo("You're already on the latest version.")
    else:
        click.echo(f"Installed: {current}    (couldn't reach PyPI to check the latest)")

    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE_NAME]
    printable = " ".join(cmd)
    if dry_run:
        click.echo(f"Would run: {printable}")
        return
    if not yes and not click.confirm(f"Run '{printable}'?", default=True):
        click.echo("Cancelled.")
        return
    raise SystemExit(subprocess.call(cmd))


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
@click.option(
    "--host",
    "-h",
    required=True,
    help="Printer URL or IP (e.g. http://octopi.local), or the serial port "
    "path for a USB printer (e.g. /dev/ttyUSB0, COM3).",
)
@click.option(
    "--type",
    "printer_type",
    required=True,
    type=_PrinterTypeChoice(list(PRINTER_TYPES)),
    help="Printer backend type.",
)
@click.option(
    "--api-key",
    default=None,
    help="API key (OctoPrint/Moonraker/Creality/Prusa Link), or machine password (Duet).",
)
@click.option("--access-code", default=None, help="LAN access code (Bambu).")
@click.option("--serial", default=None, help="Printer serial number (Bambu) or mainboard ID (Elegoo).")
@click.option(
    "--baudrate",
    type=int,
    default=None,
    help=f"Baud rate for a USB printer (default {DEFAULT_SERIAL_BAUDRATE}; "
    "many Marlin boards are flashed for 250000).",
)
@click.option("--printer-model", default=None, help="Printer model profile (e.g. k1_max, sparkx_i7, ender3_v4).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def auth(
    name: str,
    host: str,
    printer_type: str,
    api_key: str | None,
    access_code: str | None,
    serial: str | None,
    baudrate: int | None,
    printer_model: str | None,
    json_mode: bool,
) -> None:
    """Save printer credentials to the config file."""
    # Outside the try: the handler below reports every exception as a failed
    # save, and nothing has been written yet.
    if baudrate is not None and printer_type != "usb":
        raise click.UsageError(
            "--baudrate applies to --type usb only; "
            f"{printer_type} printers are reached over the network."
        )
    try:
        path = save_printer(
            name,
            printer_type,
            host,
            api_key=api_key,
            access_code=access_code,
            serial=serial,
            baudrate=baudrate,
            printer_model=printer_model,
        )
        prusa_diagnostics: dict[str, Any] | None = None
        creality_diagnostics: dict[str, Any] | None = None
        saved_host = host
        if printer_type == "prusalink":
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
        elif printer_type == "creality":
            try:
                cfg = load_printer_config(name)
                creality_diagnostics = _run_creality_diagnostics(cfg)
                resolved_url = creality_diagnostics.get("resolved_url")
                if isinstance(resolved_url, str) and resolved_url and resolved_url != host:
                    saved_host = resolved_url
                    save_printer(
                        name,
                        printer_type,
                        saved_host,
                        api_key=api_key,
                        access_code=access_code,
                        serial=serial,
                        printer_model=printer_model,
                    )
            except Exception as exc:
                logger.debug("Creality diagnostics after auth failed: %s", exc)

        data = {
            "name": name,
            "type": printer_type,
            "host": saved_host,
            "config_path": str(path),
        }
        if printer_model:
            data["printer_model"] = printer_model
        if prusa_diagnostics is not None:
            data["diagnostics"] = prusa_diagnostics
        if creality_diagnostics is not None:
            data["diagnostics"] = creality_diagnostics

        if printer_type == "prusalink" and prusa_diagnostics is not None and not prusa_diagnostics.get("ok", False):
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

        if printer_type == "creality" and creality_diagnostics is not None and not creality_diagnostics.get("ok", False):
            checks = creality_diagnostics.get("checks", [])
            failed_checks = [
                c.get("name", "unknown")
                for c in checks
                if isinstance(c, dict) and not c.get("ok", False) and not c.get("warn", False)
            ]
            failed_summary = ", ".join(failed_checks) if failed_checks else "Moonraker probe"
            message = (
                "Saved printer credentials, but Creality Moonraker diagnostics failed "
                f"({failed_summary}). Run 'kiln doctor-creality --json' for details."
            )
            if json_mode:
                click.echo(
                    format_response(
                        "error",
                        data=data,
                        error={"code": "CREALITY_MOONRAKER_NOT_REACHABLE", "message": message},
                        json_mode=True,
                    )
                )
            else:
                click.echo(format_error(message, code="CREALITY_MOONRAKER_NOT_REACHABLE", json_mode=False))
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
        if not json_mode and creality_diagnostics is not None:
            resolved_url = creality_diagnostics.get("resolved_url")
            browser_url = creality_diagnostics.get("browser_test_url")
            if resolved_url:
                click.echo(f"Creality Moonraker resolved: {resolved_url}")
            if browser_url:
                click.echo(f"Browser test: {browser_url}")
            cfs = creality_diagnostics.get("cfs_status")
            if isinstance(cfs, dict):
                detected = "detected" if cfs.get("detected") else "not detected"
                click.echo(
                    f"CFS discovery: {detected}; active slot control is hardware-unverified in Kiln."
                )
            click.echo("Creality connectivity check passed. Run: kiln doctor-creality for full diagnostics.")
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

        # Migration nag: warn if the active printer has no printer_model.
        # Incident #0 (2026-04-15) exposed that the field silently
        # de-activates the safety stack for every user who set up their
        # config before 2026-04-16.  Emit exactly once per `kiln status`
        # invocation, and only in human mode so agents don't get noisy
        # json output.
        if not json_mode:
            try:
                from kiln.cli.config import _read_config_file, get_config_path
                from kiln.cli.printer_model_prompt import (
                    check_existing_config_for_missing_model,
                    suggest_bambu_model,
                )
                raw_cfg = _read_config_file(get_config_path())
                missing = check_existing_config_for_missing_model(raw_cfg)
                if missing:
                    active_name = ctx.obj.get("printer") if ctx.obj else None
                    if active_name is None:
                        active_name = raw_cfg.get("active_printer") or "default"
                    if active_name in missing:
                        entry = (raw_cfg.get("printers") or {}).get(active_name, {})
                        suggestion = None
                        if entry.get("type") == "bambu" and entry.get("serial"):
                            suggestion = suggest_bambu_model(entry["serial"])
                        click.echo()
                        click.echo(click.style(
                            "⚠ SAFETY GAP: printer_model is NOT set for "
                            f"'{active_name}'",
                            fg="yellow", bold=True,
                        ))
                        click.echo(
                            "  Until it's set, Kiln can't check that prints fit "
                            "the bed or stay within safe temperatures — those "
                            "checks are skipped."
                        )
                        if suggestion:
                            click.echo(click.style(
                                f"  Suggested: add `printer_model: {suggestion}` "
                                f"to ~/.kiln/config.yaml",
                                fg="cyan",
                            ))
                        else:
                            click.echo(
                                "  Fix: add `printer_model: <value>` to the "
                                "printer entry in ~/.kiln/config.yaml"
                            )
                        click.echo(
                            "  Or run `kiln setup` for the interactive flow."
                        )
            except Exception:
                pass  # migration nag is best-effort; never break status
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
# report (standalone monitor_print equivalent)
# ---------------------------------------------------------------------------


# Material cost per kg — matches server.py _MATERIAL_COST_PER_KG
_CLI_COST_PER_KG: dict[str, float] = {
    "pla": 20.0, "pla+": 22.0, "petg": 22.0, "abs": 18.0,
    "tpu": 30.0, "asa": 25.0, "nylon": 35.0, "pc": 40.0,
}
_CLI_AVG_G_PER_HOUR: float = 7.5


def _estimate_print_cost_cli(
    elapsed_s: int | float | None,
    remaining_s: int | float | None,
    material: str | None = None,
) -> dict[str, Any] | None:
    """Estimate filament cost from total print time (CLI version).

    Mirrors ``_estimate_print_cost`` in ``server.py`` so that
    ``kiln report`` matches the ``monitor_print`` MCP tool output.
    """
    elapsed = elapsed_s if elapsed_s is not None and elapsed_s >= 0 else 0
    remaining = remaining_s if remaining_s is not None and remaining_s >= 0 else 0
    total_s = elapsed + remaining
    if total_s <= 0:
        return None
    mat_key = (material or "pla").lower().strip()
    cost_per_kg = _CLI_COST_PER_KG.get(mat_key, _CLI_COST_PER_KG["pla"])
    total_hours = total_s / 3600.0
    estimated_weight_g = total_hours * _CLI_AVG_G_PER_HOUR
    estimated_cost = (estimated_weight_g / 1000.0) * cost_per_kg
    return {
        "material": mat_key.upper(),
        "estimated_weight_g": round(estimated_weight_g, 1),
        "estimated_cost_usd": round(estimated_cost, 2),
        "cost_per_kg_usd": cost_per_kg,
    }


def _format_duration_cli(seconds: int | float | None) -> str:
    """Format seconds as a human-readable duration string."""
    if seconds is None or seconds < 0:
        return "N/A"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"~{hours}h {minutes}min"
    if minutes > 0:
        return f"~{minutes} min"
    return f"~{secs}s"


def _generate_print_comment_cli(
    state_str: str,
    *,
    completion: float | None,
    tool_actual: float | None,
    tool_target: float | None,
    bed_actual: float | None,
    bed_target: float | None,
    print_error: int | None,
) -> str:
    """Generate a health observation comment about the print."""
    if print_error and print_error > 0:
        return f"Error detected (code {print_error}). Check printer."
    if state_str == "paused":
        return "Print is paused."
    if state_str not in ("printing", "preparing"):
        return f"Printer state: {state_str}."

    comments: list[str] = []
    if tool_actual is not None and tool_target is not None and tool_target > 0 and abs(tool_actual - tool_target) > 10:
        comments.append(
            "Nozzle still heating up."
            if tool_actual < tool_target
            else "Nozzle temperature deviation detected."
        )
    if bed_actual is not None and bed_target is not None and bed_target > 0 and abs(bed_actual - bed_target) > 10:
        comments.append(
            "Bed still heating up."
            if bed_actual < bed_target
            else "Bed temperature deviation detected."
        )

    if completion is not None and completion >= 90:
        comments.append("Almost done!")
    elif completion is not None and completion < 5 and state_str == "printing":
        comments.append("Print just started.")

    if not comments:
        comments.append("Print progressing normally.")
    return " ".join(comments)


@cli.command()
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.option("--no-snapshot", is_flag=True, help="Skip camera snapshot.")
@click.option(
    "--repeat",
    type=int,
    default=0,
    help="Repeat every N seconds (0 = once).",
)
@click.pass_context
def report(
    ctx: click.Context,
    json_mode: bool,
    no_snapshot: bool,
    repeat: int,
) -> None:
    """Print status report matching the monitor_print MCP tool format.

    Produces the same standardized output as the ``monitor_print()``
    MCP tool, but without requiring the full MCP server.
    """
    try:
        adapter = _get_adapter_from_ctx(ctx)
    except (click.ClickException, PrinterError, Exception) as exc:
        click.echo(format_error(f"Failed to connect: {exc}", json_mode=json_mode))
        sys.exit(1)

    while True:
        try:
            state = adapter.get_state()
            job = adapter.get_job()
            sd = state.to_dict()
            jd = job.to_dict()

            state_str = sd.get("state", "unknown")
            completion = jd.get("completion")
            file_name = jd.get("file_name") or "N/A"
            current_layer = jd.get("current_layer")
            total_layers = jd.get("total_layers")
            elapsed_s = jd.get("print_time_seconds")
            remaining_s = jd.get("print_time_left_seconds")
            tool_actual = sd.get("tool_temp_actual")
            tool_target = sd.get("tool_temp_target")
            bed_actual = sd.get("bed_temp_actual")
            bed_target = sd.get("bed_temp_target")
            chamber_actual = sd.get("chamber_temp_actual")
            speed_profile = sd.get("speed_profile")
            speed_magnitude = sd.get("speed_magnitude")
            print_error = sd.get("print_error", 0)

            # Snapshot
            snapshot_line = "Skipped"
            if not no_snapshot:
                try:
                    image_data = adapter.get_snapshot()
                    if image_data is not None:
                        import uuid as _uuid

                        snap_path = os.path.join(
                            tempfile.gettempdir(),
                            f"kiln_monitor_{_uuid.uuid4().hex[:12]}.jpg",
                        )
                        with open(snap_path, "wb") as f:
                            f.write(image_data)
                        snapshot_line = snap_path
                    else:
                        snapshot_line = "No camera available"
                except Exception:
                    snapshot_line = "Snapshot capture failed"

            comment = _generate_print_comment_cli(
                state_str,
                completion=completion,
                tool_actual=tool_actual,
                tool_target=tool_target,
                bed_actual=bed_actual,
                bed_target=bed_target,
                print_error=print_error,
            )

            if json_mode:
                data = {
                    "state": state_str,
                    "completion": completion,
                    "file_name": file_name,
                    "current_layer": current_layer,
                    "total_layers": total_layers,
                    "elapsed_seconds": elapsed_s,
                    "remaining_seconds": remaining_s,
                    "nozzle_actual": tool_actual,
                    "nozzle_target": tool_target,
                    "bed_actual": bed_actual,
                    "bed_target": bed_target,
                    "chamber_actual": chamber_actual,
                    "speed_profile": speed_profile,
                    "speed_magnitude": speed_magnitude,
                    "print_error": print_error,
                    "snapshot_path": snapshot_line if snapshot_line not in ("Skipped", "No camera available", "Snapshot capture failed") else None,
                    "comment": comment,
                }
                click.echo(json.dumps({"status": "success", "data": data}))
            else:
                progress_str = f"{completion:.0f}" if completion is not None else "N/A"
                layer_str = (
                    f"{current_layer} / {total_layers}"
                    if current_layer is not None and total_layers is not None
                    else "N/A"
                )
                nozzle_str = (
                    f"{tool_actual:.0f}\u00b0C \u2192 {tool_target:.0f}\u00b0C target"
                    if tool_actual is not None and tool_target is not None
                    else "N/A"
                )
                bed_str = (
                    f"{bed_actual:.0f}\u00b0C \u2192 {bed_target:.0f}\u00b0C target"
                    if bed_actual is not None and bed_target is not None
                    else "N/A"
                )
                speed_str = (
                    f"{speed_profile} ({speed_magnitude}%)"
                    if speed_profile is not None and speed_magnitude is not None
                    else "N/A"
                )
                error_str = f"Code {print_error}" if print_error and print_error > 0 else "None"

                lines = [
                    f"Print Status \u2014 {progress_str}% complete",
                    f"- File: {file_name}",
                    f"- Layer: {layer_str}",
                    f"- Time elapsed: {_format_duration_cli(elapsed_s)} | Remaining: {_format_duration_cli(remaining_s)}",
                    f"- Nozzle: {nozzle_str}",
                    f"- Bed: {bed_str}",
                ]
                if chamber_actual is not None:
                    lines.append(f"- Chamber: {chamber_actual:.0f}\u00b0C")
                lines.extend([
                    f"- Speed: {speed_str}",
                    f"- Errors: {error_str}",
                ])

                # Cost estimate (matches monitor_print MCP tool output)
                cost_info = _estimate_print_cost_cli(elapsed_s, remaining_s)
                if cost_info is not None:
                    lines.append(
                        f"- Estimated filament cost: ~${cost_info['estimated_cost_usd']:.2f} "
                        f"({cost_info['material']} @ ${cost_info['cost_per_kg_usd']:.0f}/kg, "
                        f"~{cost_info['estimated_weight_g']:.0f}g)"
                    )

                lines.extend([
                    f"Camera: {snapshot_line}",
                    f"Comments: {comment}",
                ])
                click.echo("\n".join(lines))

        except PrinterError as exc:
            click.echo(format_error(f"Monitor failed: {exc}", json_mode=json_mode))
            if repeat <= 0:
                sys.exit(1)
        except Exception as exc:
            click.echo(format_error(f"Monitor failed: {exc}", json_mode=json_mode))
            if repeat <= 0:
                sys.exit(1)

        if repeat <= 0:
            break
        try:
            time.sleep(repeat)
        except KeyboardInterrupt:
            break


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
@click.option("--plate", "plate_number", type=click.IntRange(min=1), default=1, help="Plate number for multi-plate 3MF files (Bambu). Default 1.")
@click.option("--use-ams/--no-ams", default=None, help="Enable AMS filament feeding (Bambu). Default: auto-detect. Use --no-ams to force external spool.")
@click.option(
    "--ams-mapping",
    type=str,
    default=None,
    help="AMS slot mapping per extruder, comma-separated (e.g. '0,1'). Implies --use-ams.",
)
@click.option("--no-nozzle-check", is_flag=True, help="Disable nozzle clumping/blob detection (Bambu). Use when prints trigger false HMS 0300-8014 errors.")
@click.option("--object", "object_name", type=str, default=None, help="Extract and print a single object from a multi-object .gcode.3mf (Bambu). Partial name match supported (e.g. 'cap').")
@click.option("--list-objects", is_flag=True, help="List named objects on the plate of a .gcode.3mf file, then exit.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def print_cmd(
    ctx: click.Context,
    files: tuple,
    show_status: bool,
    use_queue: bool,
    skip_preflight: bool,
    dry_run: bool,
    plate_number: int,
    use_ams: bool | None,
    ams_mapping: str | None,
    no_nozzle_check: bool,
    object_name: str | None,
    list_objects: bool,
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

    Use --object to extract and print a single object from a multi-object
    Bambu .gcode.3mf file (e.g. --object "cap" to print just the lid).
    """
    import glob as _glob
    import os

    # --list-objects: inspect plate objects without needing a printer connection
    if list_objects:
        if not files or len(files) != 1:
            click.echo(
                format_error(
                    "--list-objects requires exactly one .gcode.3mf file.",
                    code="INVALID_ARGS",
                    json_mode=json_mode,
                )
            )
            sys.exit(1)
        source_file = files[0]
        if not os.path.isfile(source_file):
            click.echo(format_error(f"File not found: {source_file}", code="FILE_NOT_FOUND", json_mode=json_mode))
            sys.exit(1)

        from kiln.generation.validation import list_plate_objects as _list_plate_objects

        try:
            plate_info = _list_plate_objects(source_file, plate_number=plate_number)
        except (ValueError, FileNotFoundError) as exc:
            click.echo(format_error(str(exc), code="LIST_OBJECTS_FAILED", json_mode=json_mode))
            sys.exit(1)

        if json_mode:
            import json as _json

            click.echo(_json.dumps(plate_info, indent=2))
        else:
            objects = plate_info.get("objects", [])
            click.echo(f"Plate {plate_number} — {len(objects)} object(s):")
            for obj in objects:
                area = obj.get("area_mm2", 0)
                click.echo(f"  {obj['name']}  (label_id={obj['label_id']}, {area:.0f} mm²)")
            bed = plate_info.get("bed_type", "unknown")
            colors = plate_info.get("filament_colors", [])
            click.echo(f"  Bed: {bed}  Filaments: {', '.join(colors) if colors else 'N/A'}")
        return

    try:
        adapter = _get_adapter_from_ctx(ctx)

        # --object: extract a single object from a multi-object .gcode.3mf
        if object_name is not None:
            if show_status:
                click.echo(format_error("--object cannot be used with --status.", code="INVALID_ARGS", json_mode=json_mode))
                sys.exit(1)
            if use_queue:
                click.echo(format_error("--object cannot be used with --queue.", code="INVALID_ARGS", json_mode=json_mode))
                sys.exit(1)
            if not files or len(files) != 1:
                click.echo(
                    format_error(
                        "--object requires exactly one .gcode.3mf file.",
                        code="INVALID_ARGS",
                        json_mode=json_mode,
                    )
                )
                sys.exit(1)
            source_file = files[0]
            if not os.path.isfile(source_file):
                click.echo(
                    format_error(
                        f"File not found: {source_file}",
                        code="FILE_NOT_FOUND",
                        json_mode=json_mode,
                    )
                )
                sys.exit(1)

            from kiln.generation.validation import extract_plate_object_gcode

            if not json_mode:
                click.echo(f"Extracting object {object_name!r} from {os.path.basename(source_file)}...")
            try:
                extract_result = extract_plate_object_gcode(
                    source_file,
                    object_name,
                    plate_number=plate_number,
                )
            except ValueError as exc:
                click.echo(format_error(str(exc), code="EXTRACT_FAILED", json_mode=json_mode))
                sys.exit(1)

            extracted_path = extract_result["output_path"]
            if not json_mode:
                click.echo(
                    f"Extracted {extract_result['matched_object']['name']} → "
                    f"{os.path.basename(extracted_path)} "
                    f"({extract_result['kept_lines']} lines)"
                )

            # Replace files tuple with the extracted gcode so the normal
            # upload+print flow handles it.
            files = (extracted_path,)

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

        # Hard gate direct print starts while emergency latch is active.
        if not use_queue:
            safety_printer = _resolve_emergency_printer_name(ctx)
            estop_status = _emergency_latch_status(safety_printer)
            if estop_status and bool(estop_status.get("latched")):
                click.echo(
                    format_error(
                        _emergency_block_message(safety_printer, estop_status),
                        code="E_STOP_LATCHED",
                        json_mode=json_mode,
                    )
                )
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

        from kiln.printers.bambu import BambuAdapter as _BambuAdapter

        if not isinstance(adapter, _BambuAdapter) and (
            use_ams is not None or ams_mapping is not None
        ):
            # These flags are Bambu AMS instructions.  They used to be
            # dropped inside the loop below with exit 0, so `kiln print
            # --ams-mapping 0,1` at a Klipper MMU "succeeded" and printed
            # from whatever the MMU's own tool map said.  A flag Kiln cannot
            # honour is an error, said before anything is uploaded, and the
            # message names what the printer does have.
            click.echo(
                format_error(
                    _ams_flags_unsupported_message(adapter),
                    code="AMS_UNSUPPORTED_ON_PRINTER",
                    json_mode=json_mode,
                )
            )
            sys.exit(1)

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

            # Build kwargs for Bambu-specific print parameters.
            # Only pass these to adapters that accept **kwargs (Bambu,
            # OctoPrint, Moonraker).  Avoids TypeError on adapters
            # with a strict start_print(file_name) signature.
            print_kwargs: dict[str, Any] = {}
            from kiln.printers.bambu import BambuAdapter

            is_bambu = isinstance(adapter, BambuAdapter)
            if is_bambu:
                if plate_number != 1:
                    print_kwargs["plate_number"] = plate_number
                # Parse --ams-mapping (e.g. "0,1") into list[int].
                if ams_mapping is not None:
                    try:
                        parsed_ams_mapping = [int(x.strip()) for x in ams_mapping.split(",") if x.strip()]
                    except ValueError:
                        click.echo(
                            format_error(
                                f"Invalid --ams-mapping value: {ams_mapping!r}. "
                                "Expected comma-separated integers (e.g. '0,1').",
                                code="INVALID_AMS_MAPPING",
                                json_mode=json_mode,
                            )
                        )
                        sys.exit(1)
                    print_kwargs["ams_mapping"] = parsed_ams_mapping
                    print_kwargs["use_ams"] = True
                elif use_ams is not None:
                    print_kwargs["use_ams"] = use_ams
                if no_nozzle_check:
                    print_kwargs["nozzle_clog_detect"] = False
                # Pass local file path so the adapter can inspect 3MF
                # metadata for auto-detection of filament count.
                if os.path.isfile(f):
                    print_kwargs["local_file_path"] = os.path.abspath(f)

            result = adapter.start_print(file_name, **print_kwargs)
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
        # This process sends the stop and exits; the ending is seen by
        # whatever is watching the printer, usually the running server. The
        # intent has to be durable to survive that gap, and note_cancel_
        # requested is what makes it so -- so this reads like the others.
        note_cancel_requested(adapter)
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
@click.option(
    "--option",
    "-o",
    "options",
    multiple=True,
    type=click.Choice(["bed_leveling", "vibration", "flow", "all"]),
    help="Calibration routine(s) to run. Repeat for multiple. Default: bed_leveling.",
)
@click.pass_context
def calibrate(ctx: click.Context, json_mode: bool, options: tuple[str, ...]) -> None:
    """Run printer calibration (bed leveling, Z offset, vibration)."""
    try:
        adapter = _get_adapter_from_ctx(ctx)
        opt_list: list[str] | None = list(options) if options else None
        result = adapter.run_calibration(options=opt_list)
        click.echo(format_action("calibrate", result.to_dict(), json_mode=json_mode))
    except click.ClickException:
        raise
    except PrinterError as exc:
        click.echo(
            format_error(
                f"Failed to run calibration: {exc}. Is the printer idle?",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to run calibration: {exc}",
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
@click.option(
    "--force",
    is_flag=True,
    help=(
        "Send the resume even if Kiln thinks the printer isn't paused. "
        "Use when the printer's screen disagrees with what Kiln reports."
    ),
)
@click.pass_context
def resume(ctx: click.Context, json_mode: bool, force: bool) -> None:
    """Resume a paused print job."""
    try:
        safety_printer = _resolve_emergency_printer_name(ctx)
        estop_status = _emergency_latch_status(safety_printer)
        if estop_status and bool(estop_status.get("latched")):
            click.echo(
                format_error(
                    _emergency_block_message(safety_printer, estop_status),
                    code="E_STOP_LATCHED",
                    json_mode=json_mode,
                )
            )
            sys.exit(1)
        adapter = _get_adapter_from_ctx(ctx)
        result = adapter.resume_print(force=force)
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
# emergency-stop / emergency-status / emergency-clear
# ---------------------------------------------------------------------------


@cli.command("emergency-stop")
@click.option("--printer", "printer_name", default=None, help="Target printer name (default: active printer).")
@click.option("--all", "all_printers", is_flag=True, help="Stop all known printers.")
@click.option(
    "--reason",
    default="user_request",
    help="Reason code (e.g. user_request, thermal_runaway, software_fault).",
)
@click.option("--source", default="cli", help="Source label for audit context.")
@click.option("--note", default=None, help="Optional operator note.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def emergency_stop_cmd(
    ctx: click.Context,
    printer_name: str | None,
    all_printers: bool,
    reason: str,
    source: str,
    note: str | None,
    json_mode: bool,
) -> None:
    """Trigger emergency stop for one printer or all printers."""
    if all_printers and printer_name:
        click.echo(
            format_error(
                "Use either --printer or --all, not both.",
                code="INVALID_ARGS",
                json_mode=json_mode,
            )
        )
        sys.exit(1)

    try:
        from kiln.emergency import EmergencyReason, get_emergency_coordinator

        try:
            reason_enum = EmergencyReason(str(reason or "user_request").strip().lower())
        except ValueError:
            valid = ", ".join(r.value for r in EmergencyReason)
            click.echo(
                format_error(
                    f"Invalid reason {reason!r}. Valid reasons: {valid}.",
                    code="INVALID_ARGS",
                    json_mode=json_mode,
                )
            )
            sys.exit(1)

        coord = get_emergency_coordinator()
        if all_printers:
            records = coord.emergency_stop_all(reason=reason_enum, source=source, note=note)
            payload = {
                "count": len(records),
                "results": [r.to_dict() for r in records],
            }
            click.echo(format_response("success", data=payload, json_mode=json_mode))
            return

        target = _resolve_emergency_printer_name(ctx, printer_name)
        record = coord.emergency_stop(target, reason=reason_enum, source=source, note=note)
        click.echo(
            format_response(
                "success",
                data={"printer": target, "emergency_stop": record.to_dict()},
                json_mode=json_mode,
            )
        )
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to execute emergency stop: {exc}",
                code="EMERGENCY_STOP_FAILED",
                json_mode=json_mode,
            )
        )
        sys.exit(1)


@cli.command("emergency-status")
@click.option("--printer", "printer_name", default=None, help="Target printer name (default: active printer).")
@click.option("--all", "all_printers", is_flag=True, help="Show status for all known printers.")
@click.option("--include-unlatched", is_flag=True, help="Include printers that are not currently latched.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def emergency_status_cmd(
    ctx: click.Context,
    printer_name: str | None,
    all_printers: bool,
    include_unlatched: bool,
    json_mode: bool,
) -> None:
    """Show emergency latch status for one printer or the fleet."""
    if all_printers and printer_name:
        click.echo(
            format_error(
                "Use either --printer or --all, not both.",
                code="INVALID_ARGS",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    try:
        from kiln.emergency import get_emergency_coordinator

        coord = get_emergency_coordinator()
        if all_printers:
            rows = coord.list_latch_statuses(include_unlatched=include_unlatched)
            click.echo(
                format_response(
                    "success",
                    data={"count": len(rows), "emergency_status": rows},
                    json_mode=json_mode,
                )
            )
            return

        target = _resolve_emergency_printer_name(ctx, printer_name)
        status = coord.get_latch_status(target)
        click.echo(format_response("success", data={"printer": target, "emergency_status": status}, json_mode=json_mode))
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to read emergency status: {exc}",
                code="EMERGENCY_STATUS_FAILED",
                json_mode=json_mode,
            )
        )
        sys.exit(1)


@cli.command("emergency-clear")
@click.option("--printer", "printer_name", default=None, help="Target printer name (default: active printer).")
@click.option("--ack-note", required=True, help="Acknowledgement note required to clear latch.")
@click.option("--ack-by", default=None, help="Operator identifier for audit.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def emergency_clear_cmd(
    ctx: click.Context,
    printer_name: str | None,
    ack_note: str,
    ack_by: str | None,
    json_mode: bool,
) -> None:
    """Acknowledge and clear an emergency latch."""
    target = _resolve_emergency_printer_name(ctx, printer_name)
    actor = (ack_by or "").strip() or os.environ.get("USER", "operator")
    try:
        from kiln.emergency import get_emergency_coordinator

        coord = get_emergency_coordinator()
        result = coord.clear_stop_with_ack(target, acknowledged_by=actor, ack_note=ack_note)
        if not result.get("success"):
            click.echo(
                format_error(
                    str(result.get("message") or "Failed to clear emergency latch."),
                    code="E_STOP_CLEAR_BLOCKED",
                    json_mode=json_mode,
                )
            )
            sys.exit(1)

        click.echo(
            format_response(
                "success",
                data={"printer": target, "cleared": True, "emergency_status": result.get("status")},
                json_mode=json_mode,
            )
        )
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to clear emergency latch: {exc}",
                code="EMERGENCY_CLEAR_FAILED",
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
@click.option("--printer", "printer_name", default=None, help="Target printer name (default: active printer).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def temp(
    ctx: click.Context,
    tool_temp: float | None,
    bed_temp: float | None,
    printer_name: str | None,
    json_mode: bool,
) -> None:
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

        # The ceilings come from the machine's own safety profile — the same
        # resolver set_temperature uses over MCP — not from a pair of numbers
        # typed here.  Hardcoding 300/130 was wrong in BOTH directions: it let
        # an Ender 3 (250°C profile) be driven to 300, which is the exact
        # unidentified-hotend hazard server.py records fixing on 2026-07-20 on
        # its own path, and it refused a legitimate 350°C on the machines rated
        # for it.  A false "safe" is the error direction that damages hardware,
        # so the copy had to go rather than be corrected to a better constant.
        from kiln.server import _get_temp_limits

        max_tool, max_bed = _get_temp_limits(printer_name)

        results: dict[str, Any] = {}
        if tool_temp is not None:
            if tool_temp < 0 or tool_temp > max_tool:
                click.echo(
                    format_error(
                        f"Hotend temperature {tool_temp}°C out of safe range "
                        f"(0-{max_tool:g}°C for this printer).",
                        json_mode=json_mode,
                    )
                )
                sys.exit(1)
            adapter.set_tool_temp(tool_temp)
            results["tool_target"] = tool_temp
        if bed_temp is not None:
            if bed_temp < 0 or bed_temp > max_bed:
                click.echo(
                    format_error(
                        f"Bed temperature {bed_temp}°C out of safe range "
                        f"(0-{max_bed:g}°C for this printer).",
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
# fan / light / emergency-trip
#
# The three hardware controls that existed only over MCP.  They matter from a
# terminal for the same reason pause and cancel do: when `kiln serve` is wedged
# — common enough that serve_siblings.py exists to clean up after it — the
# operator still has a printer in front of them.  Each delegates to the server
# tool rather than the adapter, so the terms gate, the printer resolution and
# the refusal shapes stay identical across both doors.
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--node", default="part", help="Fan node (part, aux, chamber).")
@click.option("--percent", type=int, default=100, help="Fan speed, 0-100.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def fan(node: str, percent: int, json_mode: bool) -> None:
    """Set a printer fan's speed."""
    if percent < 0 or percent > 100:
        click.echo(
            format_error(
                f"Fan speed {percent}% out of range (0-100).",
                code="INVALID_ARGS",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    try:
        from kiln.server import set_fan as _set_fan

        result = _set_fan(node=node, percent=percent)
        if not result.get("success", False):
            click.echo(format_error(result.get("error", "Failed to set fan."), json_mode=json_mode))
            sys.exit(1)
        click.echo(format_response("success", data=result, json_mode=json_mode))
    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(f"Failed to set fan: {exc}", json_mode=json_mode))
        sys.exit(1)


@cli.command()
@click.option("--node", default="chamber_light", help="Light node (chamber_light, work_light).")
@click.option("--mode", default="on", help="on, off, or flashing where the machine supports it.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def light(node: str, mode: str, json_mode: bool) -> None:
    """Turn a printer light on or off."""
    try:
        from kiln.server import set_printer_light as _set_light

        result = _set_light(node=node, mode=mode)
        if not result.get("success", False):
            click.echo(format_error(result.get("error", "Failed to set light."), json_mode=json_mode))
            sys.exit(1)
        click.echo(format_response("success", data=result, json_mode=json_mode))
    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(f"Failed to set printer light: {exc}", json_mode=json_mode))
        sys.exit(1)


@cli.command("emergency-trip")
@click.argument("printer_name")
@click.option("--input", "input_name", default="external_button", help="Which input tripped.")
@click.option("--token", default=None, help="Shared secret, when the input is configured to require one.")
@click.option("--note", default=None, help="Optional operator note for the audit trail.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def emergency_trip_cmd(
    printer_name: str,
    input_name: str,
    token: str | None,
    note: str | None,
    json_mode: bool,
) -> None:
    """Report a hardware emergency input (e.g. a physical button) as tripped."""
    try:
        from kiln.server import emergency_trip_input as _trip

        result = _trip(
            printer_name=printer_name,
            input_name=input_name,
            token=token,
            note=note,
        )
        if not result.get("success", False):
            click.echo(
                format_error(
                    result.get("error", "Failed to record emergency trip."),
                    code=result.get("code"),
                    json_mode=json_mode,
                )
            )
            sys.exit(1)
        click.echo(format_response("success", data=result, json_mode=json_mode))
    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(format_error(f"Failed to record emergency trip: {exc}", json_mode=json_mode))
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
@click.option("--copies", "-c", default=1, type=click.IntRange(1, 20), help="Number of copies to arrange on the plate (1-20, default 1).")
@click.option("--spacing", default=10.0, type=float, help="Gap between copies in mm (default 10).")
@click.option("--use-ams/--no-ams", default=None, help="Enable AMS filament feeding (Bambu). Default: auto-detect.")
@click.option(
    "--ams-mapping",
    type=str,
    default=None,
    help=(
        "AMS slot mapping per copy, comma-separated (e.g. '0,1,2'). "
        "When --copies matches the number of AMS slots, each copy prints "
        "in a different color. Implies --use-ams."
    ),
)
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
    copies: int,
    spacing: float,
    use_ams: bool | None,
    ams_mapping: str | None,
    json_mode: bool,
) -> None:
    """Slice a 3D model (STL/3MF/STEP) to G-code.

    Uses PrusaSlicer or OrcaSlicer CLI.  The slicer binary is auto-detected
    on PATH or can be specified with --slicer.

    \b
    With --copies N, arranges N copies on one build plate using
    PrusaSlicer's --duplicate flag (or STL mesh duplication as fallback).

    \b
    With --copies N --ams-mapping A,B,C (N values), each copy prints in a
    different AMS color.  Kiln builds a multi-body 3MF, slices with multi-
    material settings, and wraps tool changes for Bambu AMS compatibility.

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

        extra_args = plan["extra_args"] or []

        # Parse --ams-mapping if provided
        parsed_ams_mapping: list[int] | None = None
        if ams_mapping is not None:
            try:
                parsed_ams_mapping = [int(x.strip()) for x in ams_mapping.split(",") if x.strip()]
            except ValueError:
                click.echo(
                    format_error(
                        f"Invalid --ams-mapping value: {ams_mapping!r}. "
                        "Expected comma-separated integers (e.g. '0,1,2').",
                        code="INVALID_AMS_MAPPING",
                        json_mode=json_mode,
                    )
                )
                sys.exit(1)

        # Detect multicolor mode: copies > 1 AND ams_mapping has same count
        multicolor_mode = (
            copies > 1
            and parsed_ams_mapping is not None
            and len(parsed_ams_mapping) == copies
        )

        # Multi-copy: use PrusaSlicer --duplicate or STL mesh duplication
        actual_input = input_file
        copy_strategy = None

        if multicolor_mode:
            # Multi-color copies: slice each copy individually and merge with T commands
            from kiln.slicer import slice_multicolor_copies

            if not json_mode:
                click.echo(f"Slicing multi-color plate: {copies} copies, each a different AMS color...")

            result = slice_multicolor_copies(
                input_file,
                copies,
                spacing_mm=spacing,
                slicer_path=slicer,
                profile=plan["profile_path"],
                extra_args=extra_args or None,
                output_dir=output_dir,
            )
            copy_strategy = "multicolor_merge"

        elif copies > 1:
            from kiln.slicer import find_slicer, supports_duplicate_flag

            slicer_info = find_slicer(slicer)

            if supports_duplicate_flag(slicer_info):
                extra_args.extend(["--duplicate", str(copies), "--duplicate-distance", str(spacing)])
                copy_strategy = "prusaslicer_duplicate"
            else:
                # Fallback: STL mesh duplication for OrcaSlicer etc.
                from kiln.auto_orient import duplicate_stl_on_plate

                actual_input = duplicate_stl_on_plate(
                    input_file,
                    copies,
                    spacing_mm=spacing,
                )
                copy_strategy = "stl_mesh_duplication"

        if not multicolor_mode:
            result = slice_file(
                actual_input,
                output_dir=output_dir,
                output_name=output_name,
                profile=plan["profile_path"],
                slicer_path=slicer,
                extra_args=extra_args or None,
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
                if copies > 1:
                    payload["copies"] = copies
                    payload["spacing_mm"] = spacing
                    payload["copy_strategy"] = copy_strategy
                click.echo(_json.dumps({"status": "success", "data": payload}, indent=2))
            else:
                click.echo(result.message)
                click.echo(f"Output: {result.output_path}")
                click.echo(f"Material: {plan['material']}")
                if copies > 1:
                    click.echo(f"Copies: {copies} (strategy: {copy_strategy}, spacing: {spacing}mm)")
                if plan["printer_id"]:
                    click.echo(f"Profile: {plan['printer_id']}")
                if plan["support_style"]:
                    note = f" ({plan['support_reason']})" if plan["support_reason"] else ""
                    click.echo(f"Supports: {plan['support_style']}{note}")
            return

        # --print-after: wrap for Bambu if needed, upload, and start
        adapter = _get_adapter_from_ctx(ctx)
        from kiln.printers.bambu import BambuAdapter as _BambuAdapter

        if not isinstance(adapter, _BambuAdapter) and (
            use_ams is not None or parsed_ams_mapping is not None
        ):
            # The merged multi-colour gcode carries bare T0..Tn and the
            # slot map is a Bambu instruction; at any other printer both
            # were sent and silently dropped, and every copy printed in
            # one colour.  Refuse before upload, keep the sliced file.
            click.echo(
                format_error(
                    _ams_flags_unsupported_message(adapter)
                    + f" The sliced file is at {result.output_path}.",
                    code="AMS_UNSUPPORTED_ON_PRINTER",
                    json_mode=json_mode,
                )
            )
            sys.exit(1)
        if not json_mode:
            click.echo(result.message)

        # Bambu printers need gcode wrapped in a 3MF with proprietary
        # start/end sequences.  For multi-color, this also wraps T commands
        # in M620/M621 AMS load blocks.
        upload_path = result.output_path
        from kiln.printers.bambu import BambuAdapter

        if isinstance(adapter, BambuAdapter) and upload_path.endswith(".gcode"):
            try:
                wrap_kwargs: dict[str, Any] = {}
                if multicolor_mode and parsed_ams_mapping:
                    wrap_kwargs["num_filaments"] = copies
                    # We don't know actual AMS colors, so use distinct defaults
                    default_colors = [
                        "#FF0000", "#00FF00", "#0000FF", "#FFFF00",
                        "#FF00FF", "#00FFFF", "#FF8800", "#8800FF",
                        "#FFFFFF", "#808080", "#000000", "#FF4444",
                        "#44FF44", "#4444FF", "#FFAA00", "#AA00FF",
                    ]
                    wrap_kwargs["filament_colors"] = default_colors[:copies]
                if not json_mode:
                    if multicolor_mode:
                        click.echo("Wrapping gcode as multi-color Bambu 3MF...")
                    else:
                        click.echo("Wrapping gcode as Bambu 3MF...")
                upload_path = adapter.wrap_gcode_as_3mf(upload_path, **wrap_kwargs)
                if not json_mode:
                    click.echo(f"Bambu 3MF: {upload_path}")
            except Exception as exc:
                logger.warning("Bambu 3MF wrapping failed: %s", exc)
                if not json_mode:
                    click.echo(f"Warning: Bambu 3MF wrapping failed ({exc}), uploading raw gcode")

        if not json_mode:
            click.echo(f"Uploading {upload_path}...")

        upload_result = adapter.upload_file(upload_path)
        if not upload_result.success:
            click.echo(
                format_error(
                    upload_result.message or "Upload failed",
                    code="UPLOAD_FAILED",
                    json_mode=json_mode,
                )
            )
            sys.exit(1)

        file_name = upload_result.file_name or os.path.basename(upload_path)

        # Build print kwargs (AMS mapping, etc.)
        print_kwargs: dict[str, Any] = {}
        if isinstance(adapter, BambuAdapter):
            if parsed_ams_mapping:
                print_kwargs["ams_mapping"] = parsed_ams_mapping
                print_kwargs["use_ams"] = True
            elif use_ams is not None:
                print_kwargs["use_ams"] = use_ams

        print_result = adapter.start_print(file_name, **print_kwargs)

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
    in JSON mode.  Supports OctoPrint, Moonraker, and Creality webcams when exposed by the backend.
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
# monitor
# ---------------------------------------------------------------------------


_SEVERITY_COLOR = {
    "clear": "green",
    "amber": "yellow",
    "red": "red",
    "critical": "red",
    "warning": "yellow",
    "ok": "green",
}


def _render_smart_monitoring_panel(health_monitor, printer_name: str) -> None:
    """Render the Smart Monitoring lines under the status row.

    Mirrors the 5 Tier-1 fields from MCP ``monitor_print``: monitoring
    summary, predictive risk, predictive red issue, detective failure
    match, auto-pause status, auto-recover stage, and reroute hint.
    Each line only appears when its underlying state is present.

    Best-effort: any helper failure debug-logs and skips the affected
    line; the panel itself only renders when monitoring is active.
    """
    signals = health_monitor.get_latest_signals(printer_name)
    if not signals.get("monitoring_active"):
        return

    # kiln-pro side — auto-recover stage + reroute hint, when installed.
    auto_recover_block = None
    reroute_block = None
    try:
        from kiln_pro.recovery.auto_recover_engine import (
            AutoRecoverStatus as _AR_Status,
        )
        from kiln_pro.recovery.auto_recover_engine import (
            list_sessions as _ar_list_sessions,
        )
        ar_sessions = _ar_list_sessions(printer_name=printer_name)
        if ar_sessions:
            terminal_states = {
                _AR_Status.DONE_SUCCESS,
                _AR_Status.DONE_FAILURE,
                _AR_Status.NO_FAILURE,
                _AR_Status.CANCELLED,
                _AR_Status.ERRORED,
            }
            active = [s for s in ar_sessions if s.status not in terminal_states]
            if active:
                latest_active = max(active, key=lambda s: s.started_at)
                auto_recover_block = {
                    "stage": latest_active.status.value,
                    "auto_recover_id": latest_active.auto_recover_id,
                }
            with_reroute = [s for s in ar_sessions if s.reroute_recommendation]
            if with_reroute:
                latest_rr = max(with_reroute, key=lambda s: s.started_at)
                r = latest_rr.reroute_recommendation or {}
                reroute_block = {
                    "target_printer_id": r.get("target_printer_id"),
                    "should_reroute": bool(r.get("should_reroute")),
                    "reason": r.get("reason"),
                    "blocked_by_rule": r.get("blocked_by_rule"),
                }
    except ImportError:
        pass  # kiln-pro not installed — clean skip on free tier
    except Exception as _ar_exc:
        logger.debug("auto_recover surfacing skipped in CLI panel: %s", _ar_exc)

    # Render — Rich when available, plain text fallback otherwise.
    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.text import Text
        rich_available = True
    except ImportError:
        rich_available = False

    sid = signals.get("session_id") or ""
    sid_short = sid[:8] if sid else "?"
    body_lines: list[tuple[str, str]] = []
    body_lines.append((
        "default",
        f"Monitoring: {signals.get('report_count', 0)} reports, "
        f"{signals.get('issue_count', 0)} issues, session {sid_short}",
    ))

    risk = signals.get("risk")
    if risk:
        kinds = risk.get("kinds") or []
        kinds_str = ", ".join(kinds) if kinds else "(no signals)"
        severity = (risk.get("severity") or "clear").lower()
        body_lines.append((
            _SEVERITY_COLOR.get(severity, "default"),
            f"Risk: {risk.get('score', 0.0):.2f} {severity} ({kinds_str})",
        ))

    pred = signals.get("predictive")
    if pred:
        severity = (pred.get("severity") or "red").lower()
        body_lines.append((
            _SEVERITY_COLOR.get(severity, "red"),
            f"Predictive: {severity} {pred.get('kind', 'signal')} — "
            f"{pred.get('detail', '') or '(no detail)'}",
        ))

    det = signals.get("detective")
    if det:
        det_age = ""
        reported_at = det.get("reported_at")
        if reported_at:
            try:
                age_s = max(0.0, time.time() - float(reported_at))
                det_age = (
                    f", {age_s / 60.0:.0f}m ago"
                    if age_s >= 60
                    else f", {age_s:.0f}s ago"
                )
            except (TypeError, ValueError):
                pass
        severity = (det.get("severity") or "warning").lower()
        body_lines.append((
            _SEVERITY_COLOR.get(severity, "yellow"),
            f"Detective: {severity} {det.get('failure_type', 'failure')} "
            f"({(det.get('failure_id') or 'n/a')[:8]}{det_age})",
        ))

    if auto_recover_block:
        ar_id_short = (auto_recover_block["auto_recover_id"] or "?")[:8]
        body_lines.append((
            "cyan",
            f"Auto-recover: {auto_recover_block['stage']} (id {ar_id_short})",
        ))

    ap = signals.get("auto_pause")
    if ap:
        ap_age_s = float(ap.get("age_seconds") or 0.0)
        ap_age = (
            f"{ap_age_s / 60.0:.0f}m ago"
            if ap_age_s >= 60
            else f"{ap_age_s:.0f}s ago"
        )
        ap_status = "paused"
        if ap.get("skipped"):
            ap_status = f"skipped ({ap['skipped']})"
        elif ap.get("error"):
            ap_status = f"error ({ap['error']})"
        body_lines.append((
            "yellow",
            f"Auto-pause: {ap_age} "
            f"({ap.get('issue_type', 'issue')} -> {ap_status})",
        ))

    if reroute_block:
        if reroute_block.get("should_reroute"):
            target = reroute_block.get("target_printer_id") or "?"
            body_lines.append((
                "cyan",
                f"Reroute: {target} ready (was {printer_name})",
            ))
        else:
            blocked = reroute_block.get("blocked_by_rule") or "blocked"
            reason = (reroute_block.get("reason") or "")[:60]
            body_lines.append((
                "default",
                f"Reroute: blocked ({blocked} — {reason})",
            ))

    if rich_available:
        text = Text()
        for idx, (style, line) in enumerate(body_lines):
            text.append(line, style=style if style != "default" else None)
            if idx < len(body_lines) - 1:
                text.append("\n")
        Console().print(
            Panel(text, title="Smart Monitoring", border_style="blue", padding=(0, 1)),
        )
    else:
        click.echo("    [Smart Monitoring]")
        for _, line in body_lines:
            click.echo(f"    {line}")


@cli.command()
@click.option("--interval", default=10.0, type=float, help="State poll interval in seconds (default: 10).")
@click.option("--auto-pause/--no-auto-pause", default=True, help="Auto-pause on critical alerts (default: enabled).")
@click.option("--auto-cancel", is_flag=True, default=False, help="Auto-cancel on emergency alerts (default: disabled).")
@click.option("--timeout", default=0.0, type=float, help="Max monitoring time in seconds (0 = unlimited).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON Lines to stdout.")
@click.pass_context
def monitor(ctx: click.Context, interval: float,
            auto_pause: bool, auto_cancel: bool, timeout: float, json_mode: bool) -> None:
    """Monitor a print job for safety anomalies.

    Runs for the duration of the print, polling printer state at --interval.
    Detects temperature drift, stalls, errors, and connection loss using
    the same predictive + detective stack the MCP tools use.  Auto-pauses
    on critical alerts by default.

    Surfaces the same Tier-1 smart-monitoring fields the MCP
    ``monitor_print`` tool reports — predictive risk, detective failure
    matches, auto-pause status, auto-recover stage, and reroute hints —
    in both the Rich terminal panel and the JSON Lines stream.

    \b
    Exit codes:
      0  — print completed successfully
      1  — print failed, cancelled, or monitoring error
      130 — interrupted by Ctrl+C

    \b
    Examples:
      kiln monitor                        # Rich terminal output, auto-pause on
      kiln monitor --json                 # JSON Lines for agent consumption
      kiln monitor --interval 5           # Poll every 5 seconds
      kiln monitor --auto-cancel          # Also auto-cancel on emergency

    \b
    Note: --snapshot-interval and --snapshot-dir were removed in this
    release; the periodic snapshot-to-disk loop is not currently wired
    (use --json output for snapshot paths instead).  If you need
    snapshot-to-disk back, file an issue and the loop can be ported as
    a real feature rather than a soft no-op flag.
    """
    import time as _time

    from kiln.print_health_monitor import (
        HealthSeverity,
        MonitorPolicy,
        PrintHealthMonitor,
    )

    try:
        # Resolve adapter early for friendly error messages, but the
        # health monitor itself looks the printer up via the registry
        # singleton when each tick runs.
        _get_adapter_from_ctx(ctx)
        printer_name = ctx.obj.get("printer") or "default"

        # Build the policy directly from the CLI flags.  Wall-clock
        # timeout becomes the session timeout; check_count is set high
        # so a long print isn't capped by the default 5-tick limit; the
        # user-facing --timeout flag is the authoritative wall-clock cap.
        policy = MonitorPolicy(
            check_delay_seconds=0,       # CLI mode: start checking immediately
            check_count=10**9,           # effectively unlimited; --timeout caps
            check_interval_seconds=int(max(1.0, interval)),
            auto_pause_on_failure=auto_pause,
            auto_cancel_on_emergency=auto_cancel,
            session_timeout_seconds=float(timeout) if timeout > 0 else 0.0,
        )

        if not json_mode:
            click.echo(f"  Monitoring printer '{printer_name}' (poll every {interval}s, "
                       f"Ctrl+C to stop)")
            flags = []
            if auto_pause:
                flags.append("auto-pause")
            if auto_cancel:
                flags.append("auto-cancel")
            if flags:
                click.echo(f"  Safety: {', '.join(flags)}")
            click.echo()

        health_monitor = PrintHealthMonitor()
        session_id = health_monitor.start_monitoring(
            printer_name,
            interval_seconds=float(interval),
            policy=policy,
            output_stream=sys.stdout if json_mode else None,
            enable_report_queue=not json_mode,
        )

        # In JSON mode the monitor itself writes the NDJSON stream;
        # the CLI just blocks until the session ends (timeout, stop,
        # or terminal status).  In Rich mode, consume the iterator
        # and render each report.
        exit_code = 0
        try:
            if json_mode:
                # Wait for the loop to finish or timeout.  Polling on
                # session.status keeps this dependency-free.
                start = _time.time()
                while True:
                    session = health_monitor.get_session(session_id)
                    if session.status.value not in ("monitoring",):
                        break
                    if timeout > 0 and (_time.time() - start) >= timeout + 5.0:
                        break  # safety net beyond the policy's own timeout
                    _time.sleep(0.5)
            else:
                from kiln.cli.output import progress_bar

                for report in health_monitor.iter_reports(session_id, timeout=30.0):
                    metrics = {m.metric_name: m for m in report.metrics}

                    parts: list[str] = []
                    hotend = metrics.get("hotend_temperature")
                    if hotend is not None:
                        parts.append(
                            f"Hotend: {hotend.current_value:.1f}°C → {hotend.expected_value:.0f}°C"
                        )
                    bed = metrics.get("bed_temperature")
                    if bed is not None:
                        parts.append(
                            f"Bed: {bed.current_value:.1f}°C → {bed.expected_value:.0f}°C"
                        )

                    progress = metrics.get("print_progress")
                    if progress is not None:
                        parts.append(progress_bar(progress.current_value))

                    parts.append(f"phase={report.phase.value}")
                    parts.append(f"status={report.overall_status.value}")

                    click.echo("  " + "  ".join(parts))

                    # Surface any critical metric details right after
                    # the status line so the operator sees what tripped.
                    for m in report.metrics:
                        if m.severity == HealthSeverity.CRITICAL and m.detail:
                            click.echo(f"    [CRITICAL] {m.detail}")
                        elif m.severity == HealthSeverity.WARNING and m.detail:
                            click.echo(f"    [WARNING]  {m.detail}")

                    # Smart-monitoring panel — surfaces the same Tier-1
                    # fields the MCP monitor_print one-shot reports.
                    # Skipped when monitoring is inactive so a healthy
                    # tick keeps the existing compact output.  Best-
                    # effort: any rendering failure debug-logs and the
                    # tick continues without the panel.
                    try:
                        _render_smart_monitoring_panel(
                            health_monitor, printer_name,
                        )
                    except Exception as _smart_exc:
                        logger.debug(
                            "Smart-monitoring panel render skipped: %s",
                            _smart_exc,
                        )

                    # End-of-print detection: print_progress >= 99% and
                    # status is no longer CRITICAL ⇒ treat as completed.
                    if progress is not None and progress.current_value >= 99.0:
                        break

            # Determine final exit code from session state.
            session = health_monitor.get_session(session_id)
            if session.status.value in ("failed", "stalled", "aborted") or any(
                issue.get("auto_cancel_triggered")
                for issue in session.issues
            ):
                exit_code = 1
        finally:
            # Always tear down the background monitor so the daemon
            # thread stops.  KeyError if it already exited cleanly.
            with contextlib.suppress(KeyError):
                health_monitor.stop_monitoring(printer_name)

        sys.exit(exit_code)

    except KeyboardInterrupt:
        if not json_mode:
            click.echo("\nMonitoring interrupted.")
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
    through configured fulfillment providers.
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
    try:
        from kiln.billing import BillingLedger
    except ImportError:
        BillingLedger = None
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
        if BillingLedger is not None:
            ledger = BillingLedger()
            fee_calc = ledger.calculate_fee(quote.total_price, currency=quote.currency)
            quote_data["kiln_fee"] = fee_calc.to_dict()
            quote_data["total_with_fee"] = float(fee_calc.total_cost)
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


def _build_fulfillment_shipping_address(
    *,
    first_name: str,
    last_name: str,
    email: str,
    phone: str,
    street: str,
    street2: str,
    city: str,
    state: str,
    postal_code: str,
    country: str,
    company: str,
    vat_id: str,
) -> dict[str, str]:
    """Build a provider shipping address from CLI options."""
    provided = {
        "first_name": first_name.strip(),
        "last_name": last_name.strip(),
        "email": email.strip(),
        "phone": phone.strip(),
        "street": street.strip(),
        "street2": street2.strip(),
        "city": city.strip(),
        "state": state.strip(),
        "postal_code": postal_code.strip(),
        "company": company.strip(),
        "vat_id": vat_id.strip(),
    }
    if not any(provided.values()):
        return {}

    required = ("first_name", "last_name", "email", "phone", "street", "city", "postal_code")
    missing = [name.replace("_", "-") for name in required if not provided[name]]
    normalized_country = (country or "US").strip().upper()
    if normalized_country == "US" and not provided["state"]:
        missing.append("state")
    if missing:
        raise click.ClickException(
            "Shipping address is incomplete. Missing: "
            + ", ".join(missing)
            + ". Provide the full shipping contact/address or omit all shipping fields."
        )
    if "@" not in provided["email"]:
        raise click.ClickException("Shipping email must be a valid email address.")

    address = {
        "first_name": provided["first_name"],
        "last_name": provided["last_name"],
        "email": provided["email"],
        "phone": provided["phone"],
        "street": provided["street"],
        "city": provided["city"],
        "postal_code": provided["postal_code"],
        "country": normalized_country,
    }
    for key in ("street2", "state", "company", "vat_id"):
        if provided[key]:
            address[key] = provided[key]
    return address


def _shipping_address_summary(address: dict[str, str]) -> str:
    from kiln.fulfillment_profiles import summarize_shipping_address

    return summarize_shipping_address(address, redact_sensitive=False)


def _save_shipping_profile_from_cli(
    shipping_address: dict[str, str],
    profile_name: str,
) -> None:
    """Save a shipping profile from an explicit CLI decision."""
    from kiln.fulfillment_profiles import save_shipping_profile

    try:
        save_shipping_profile(profile_name, shipping_address)
    except ValueError as exc:
        if "already exists" not in str(exc):
            raise click.ClickException(str(exc)) from exc
        if not click.confirm(
            f"Shipping profile '{profile_name}' already exists. Replace it?",
            default=False,
        ):
            click.echo("Shipping profile was not saved.")
            return
        save_shipping_profile(profile_name, shipping_address, overwrite=True)
    click.echo("Shipping profile saved locally.")


def _resolve_shipping_profile_save_step(
    shipping_address: dict[str, str],
    *,
    used_shipping_profile: bool,
    save_as: str,
    do_not_save: bool,
    json_mode: bool,
) -> None:
    """Ask or enforce the save-profile decision for one-shot addresses."""
    if used_shipping_profile:
        return
    profile_name = save_as.strip()
    if profile_name and do_not_save:
        raise click.ClickException(
            "Choose either --save-shipping-profile-as or --do-not-save-shipping-profile, not both."
        )
    if profile_name:
        _save_shipping_profile_from_cli(shipping_address, profile_name)
        return
    if do_not_save:
        return
    if json_mode:
        raise click.ClickException(
            "Before placing a fulfillment order, ask the user whether Kiln should "
            "save this shipping contact/address as a local profile. Then pass "
            "--save-shipping-profile-as NAME if they say yes, or "
            "--do-not-save-shipping-profile if they say no."
        )
    if not click.confirm(
        "Save this shipping contact/address as a local profile for future orders?",
        default=False,
    ):
        return
    profile_name = click.prompt("Shipping profile name", default="home").strip()
    _save_shipping_profile_from_cli(shipping_address, profile_name)


@order.command("place")
@click.argument("quote_id")
@click.option("--shipping", "-s", "shipping_id", default="", help="Shipping option ID (from quote).")
@click.option("--shipping-profile", default="", help="Saved shipping profile name.")
@click.option("--first-name", default="", help="Shipping contact first name.")
@click.option("--last-name", default="", help="Shipping contact last name.")
@click.option("--email", default="", help="Shipping contact email.")
@click.option("--phone", default="", help="Shipping contact phone number.")
@click.option("--street", default="", help="Shipping street address.")
@click.option("--street2", default="", help="Shipping street address line 2.")
@click.option("--city", default="", help="Shipping city.")
@click.option("--state", default="", help="Shipping state/province.")
@click.option("--postal-code", default="", help="Shipping postal/ZIP code.")
@click.option("--country", default="US", help="Shipping country code.")
@click.option("--company", default="", help="Shipping company name.")
@click.option("--vat-id", default="", help="Business tax/VAT ID for billing address.")
@click.option("--preview-file", type=click.Path(exists=True), default=None, help="Model file whose rendered preview was reviewed.")
@click.option("--confirm-preview", is_flag=True, help="Confirm the rendered model preview was reviewed and approved.")
@click.option("--confirm-shipping", is_flag=True, help="Confirm the contact/shipping details were reviewed and approved.")
@click.option("--save-shipping-profile-as", default="", help="Save explicit shipping fields as a local profile after user approval.")
@click.option("--do-not-save-shipping-profile", is_flag=True, help="Confirm the user chose not to save explicit shipping fields.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def order_place(
    quote_id: str,
    shipping_id: str,
    shipping_profile: str,
    first_name: str,
    last_name: str,
    email: str,
    phone: str,
    street: str,
    street2: str,
    city: str,
    state: str,
    postal_code: str,
    country: str,
    company: str,
    vat_id: str,
    preview_file: str | None,
    confirm_preview: bool,
    confirm_shipping: bool,
    save_shipping_profile_as: str,
    do_not_save_shipping_profile: bool,
    json_mode: bool,
) -> None:
    """Place a manufacturing order from a quote.

    Requires a quote ID from 'kiln order quote'.
    """
    try:
        from kiln.billing import BillingLedger
    except ImportError:
        BillingLedger = None
    from kiln.fulfillment import OrderRequest
    try:
        from kiln_pro.payments.base import PaymentError
    except ImportError:
        PaymentError = None
    try:
        from kiln_pro.payments.manager import PaymentManager
    except ImportError:
        PaymentManager = None
    from kiln.persistence import get_db

    try:
        manual_shipping_fields = any(
            value.strip()
            for value in (
                first_name,
                last_name,
                email,
                phone,
                street,
                street2,
                city,
                state,
                postal_code,
                company,
                vat_id,
            )
        )
        if shipping_profile and manual_shipping_fields:
            raise click.ClickException(
                "Provide either --shipping-profile or explicit shipping fields, not both."
            )
        if shipping_profile:
            from kiln.fulfillment_profiles import get_shipping_profile

            shipping_address = get_shipping_profile(shipping_profile).shipping_address
        else:
            shipping_address = _build_fulfillment_shipping_address(
                first_name=first_name,
                last_name=last_name,
                email=email,
                phone=phone,
                street=street,
                street2=street2,
                city=city,
                state=state,
                postal_code=postal_code,
                country=country,
                company=company,
                vat_id=vat_id,
            )
        if not shipping_address:
            raise click.ClickException(
                "Shipping contact/address is required before placing a fulfillment order. "
                "Pass explicit shipping fields or --shipping-profile."
            )
        if not preview_file:
            raise click.ClickException(
                "Fulfillment order requires a reviewed model preview. "
                "Run `kiln preview MODEL_FILE`, show the rendered preview to the user, "
                "then rerun order place with --preview-file MODEL_FILE and --confirm-preview."
            )
        if not confirm_preview:
            raise click.ClickException(
                "Preview approval is required before placing a fulfillment order. "
                f"Review the rendered preview for {preview_file!r}, then rerun with --confirm-preview."
            )
        if not confirm_shipping:
            raise click.ClickException(
                "Shipping/contact approval is required before placing a fulfillment order. "
                f"Review: {_shipping_address_summary(shipping_address)}. "
                "Then rerun with --confirm-shipping."
            )
        _resolve_shipping_profile_save_step(
            shipping_address,
            used_shipping_profile=bool(shipping_profile),
            save_as=save_shipping_profile_as,
            do_not_save=do_not_save_shipping_profile,
            json_mode=json_mode,
        )
        provider = _get_fulfillment_provider()
        result = provider.place_order(
            OrderRequest(
                quote_id=quote_id,
                shipping_option_id=shipping_id,
                shipping_address=shipping_address,
                preview_confirmed=True,
                shipping_confirmed=True,
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
            order_data["total_with_fee"] = float(fee_calc.total_cost)
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
@click.option(
    "--idempotency-key",
    default=None,
    help=(
        "One-time key so a retried submit returns the original job "
        "instead of queueing a duplicate (same key + same details = "
        "same job)."
    ),
)
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def queue_submit_cmd(
    file: str,
    printer: str | None,
    priority: int,
    idempotency_key: str | None,
    json_mode: bool,
) -> None:
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
            idempotency_key=idempotency_key,
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
    """Cancel a job that is still waiting in the queue.

    JOB_ID is the ID returned by 'kiln queue submit'.  To stop a print the
    machine has already started, use 'kiln cancel' instead.
    """
    try:
        from kiln.plugins.queue_tools import cancel_queued_job as _cancel_queued_job

        result = _cancel_queued_job(job_id)
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


@queue.command("clear")
@click.option("--printer", "-p", default=None, help="Only clear jobs queued for this printer.")
@click.option("--dry-run", is_flag=True, help="Preview which jobs would be cancelled without cancelling any.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def queue_clear_cmd(printer: str | None, dry_run: bool, json_mode: bool) -> None:
    """Cancel all queued jobs at once (bulk sibling of 'kiln queue cancel').

    Clears every job in the QUEUED state immediately. Pass --printer to scope
    the sweep to one printer, or --dry-run to preview what would be cancelled
    without changing anything. A running print is never cancelled.
    """
    try:
        from kiln.plugins.queue_tools import cancel_queued_jobs as _cancel_queued_jobs

        result = _cancel_queued_jobs(printer_name=printer, dry_run=dry_run)
        if not result.get("success"):
            click.echo(
                format_error(
                    result.get("error", "Unknown error"),
                    code=result.get("code", "ERROR"),
                    json_mode=json_mode,
                )
            )
            sys.exit(1)

        if json_mode:
            click.echo(format_response("success", data=result, json_mode=True))
            return

        click.echo(result["message"])
        cancelled = result.get("count", 0)
        skipped = result.get("skipped", [])
        verb = "would be cancelled" if result.get("dry_run") else "cancelled"
        summary = f"{cancelled} queued job(s) {verb}"
        if skipped:
            summary += f", {len(skipped)} skipped"
        summary += "."
        click.echo(summary)
    except ValueError as exc:
        click.echo(
            format_error(
                f"Failed to clear the queue: {exc}",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to clear the queue: {exc}",
                json_mode=json_mode,
            )
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# ingest (watch-folder handoff)
# ---------------------------------------------------------------------------


@cli.group()
def ingest() -> None:
    """Watch a folder for printable files and optionally auto-submit them."""


@ingest.command("watch")
@click.option(
    "--dir",
    "watch_dir",
    required=True,
    type=click.Path(exists=True, file_okay=False, dir_okay=True),
    help="Directory to watch for printable files.",
)
@click.option("--interval", default=2.0, show_default=True, help="Polling interval in seconds.")
@click.option("--once", is_flag=True, help="Run one scan cycle and exit.")
@click.option(
    "--auto-queue",
    is_flag=True,
    help="Opt in to automatic queue/dispatch behavior (default is detect-only).",
)
@click.option(
    "--printer",
    default=None,
    help="Optional fixed printer name. Omit to auto-route across configured printers.",
)
@click.option(
    "--material",
    "-m",
    default="PLA",
    show_default=True,
    type=click.Choice(_MATERIAL_CHOICES, case_sensitive=False),
    help="Target material used for auto-routing.",
)
@click.option(
    "--state-file",
    default=None,
    type=click.Path(dir_okay=False, file_okay=True, path_type=str),
    help="Optional JSON state file for restart-safe ingest tracking.",
)
@click.option(
    "--min-stable-seconds",
    default=0.0,
    show_default=True,
    type=float,
    help="Require files to be stable for this many seconds before ingest.",
)
@click.option("--json", "json_mode", is_flag=True, help="Output JSON (requires --once).")
def ingest_watch_cmd(
    watch_dir: str,
    interval: float,
    once: bool,
    auto_queue: bool,
    printer: str | None,
    material: str,
    state_file: str | None,
    min_stable_seconds: float,
    json_mode: bool,
) -> None:
    """Watch a folder and detect new printable files for Kiln workflows."""
    if json_mode and not once:
        click.echo(
            format_error(
                "--json is supported only with --once for ingest watch.",
                code="INVALID_ARGS",
                json_mode=True,
            )
        )
        sys.exit(1)

    watch_path = Path(watch_dir).expanduser().resolve()
    if not watch_path.is_dir():
        click.echo(
            format_error(
                f"Watch directory is not valid: {watch_path}",
                code="INVALID_ARGS",
                json_mode=json_mode,
            )
        )
        sys.exit(1)

    material_type = _normalise_material_type(material) or "PLA"
    state_path: Path | None = None
    if state_file:
        state_path = Path(state_file).expanduser().resolve()
    seen: dict[str, float] = {}
    if state_path:
        seen = _load_ingest_seen_state(state_path)
    pending: dict[str, deque[Path]] = {}
    adapters: dict[str, Any] = {}
    adapter_errors: list[str] = []
    queued: list[dict[str, Any]] = []
    dispatched: list[dict[str, Any]] = []
    errors: list[str] = []

    if auto_queue:
        adapters, adapter_errors = _load_fleet_adapters(printer_filter=printer)
        errors.extend(adapter_errors)
        if not adapters:
            click.echo(
                format_error(
                    "No valid printers available for auto-queue mode. "
                    "Use detect-only mode or fix printer config.",
                    code="NO_PRINTERS",
                    json_mode=json_mode,
                )
            )
            sys.exit(1)
        pending = {name: deque() for name in adapters}

    def _dispatch_pending() -> None:
        if not auto_queue:
            return
        for printer_name, queue_items in pending.items():
            if not queue_items:
                continue
            estop_status = _emergency_latch_status(printer_name)
            if estop_status and bool(estop_status.get("latched")):
                errors.append(_emergency_block_message(printer_name, estop_status))
                continue
            adapter = adapters[printer_name]
            try:
                state = adapter.get_state()
                status = str(getattr(getattr(state, "state", None), "value", "unknown")).lower()
            except Exception as exc:
                errors.append(f"{printer_name}: state check failed ({exc})")
                continue

            if status != "idle":
                continue

            local_path = queue_items[0]
            if not local_path.exists():
                queue_items.popleft()
                errors.append(f"{printer_name}: file disappeared before dispatch ({local_path})")
                continue

            try:
                upload_result = adapter.upload_file(str(local_path))
                if not upload_result.success:
                    queue_items.popleft()
                    errors.append(
                        f"{printer_name}: upload failed for {local_path.name} ({upload_result.message or 'unknown'})"
                    )
                    continue

                remote_name = upload_result.file_name or local_path.name
                # An unconfirmed start is not a failed one: dropping the job
                # here would leave a running print with nothing tracking it.
                sent_at = time.monotonic()
                start_result = adapter.start_print(remote_name)
                start_verdict = resolve_print_start(
                    adapter, start_result, sent_at=sent_at,
                    file_name=remote_name,
                )
                if not start_verdict.ok:
                    queue_items.popleft()
                    errors.append(
                        f"{printer_name}: start failed for {remote_name} ({start_verdict.message or 'unknown'})"
                    )
                    continue

                queue_items.popleft()
                dispatched.append(
                    {
                        "printer": printer_name,
                        "file": str(local_path),
                        "remote_file": remote_name,
                    }
                )
                if not json_mode:
                    click.echo(f"Dispatched: {local_path.name} -> {printer_name}")
            except Exception as exc:
                queue_items.popleft()
                errors.append(f"{printer_name}: dispatch failed for {local_path.name} ({exc})")

    def _enqueue_detected(paths: list[Path]) -> None:
        for path in paths:
            if not auto_queue:
                if not json_mode:
                    click.echo(f"Detected: {path.name}")
                continue

            file_ext = path.suffix.lower()
            pending_counts = {name: len(items) for name, items in pending.items()}
            candidates = _collect_routing_candidates(
                adapters=adapters,
                material=material_type,
                pending_counts=pending_counts,
                file_extension=file_ext,
            )
            chosen, routing_data, route_error = _route_printer_for_job(
                material=material_type,
                candidates=candidates,
            )
            if route_error or not chosen:
                errors.append(f"{path.name}: {route_error or 'no routing candidate'}")
                continue

            pending[chosen].append(path)
            row = {
                "file": str(path),
                "printer": chosen,
            }
            if routing_data:
                row["score"] = routing_data.get("recommended_printer", {}).get("score")
            queued.append(row)
            if not json_mode:
                click.echo(f"Queued: {path.name} -> {chosen}")

    def _scan_cycle() -> list[Path]:
        try:
            detected = _scan_ingest_directory(watch_path, seen)
            detected = _filter_stable_ingest_files(
                detected,
                seen=seen,
                min_stable_seconds=max(0.0, float(min_stable_seconds)),
            )
        except Exception as exc:
            errors.append(f"scan: {exc}")
            return []
        _enqueue_detected(detected)
        _dispatch_pending()
        if state_path:
            try:
                _save_ingest_seen_state(state_path, watch_path, seen)
            except Exception as exc:
                errors.append(f"state: {exc}")
        return detected

    if not json_mode:
        mode = "auto-queue" if auto_queue else "detect-only"
        click.echo(f"Watching {watch_path} ({mode})")
        click.echo(f"File types: {', '.join(_INGEST_EXTENSIONS)}")
        click.echo(f"Stability window: {max(0.0, float(min_stable_seconds)):.1f}s")
        if auto_queue and printer:
            click.echo(f"Fixed printer: {printer}")
        if state_path:
            click.echo(f"State file: {state_path}")

    detected_all: list[str] = []

    try:
        if once:
            detected = _scan_cycle()
            detected_all.extend(str(p) for p in detected)
        else:
            while True:
                detected = _scan_cycle()
                detected_all.extend(str(p) for p in detected)
                time.sleep(max(0.2, float(interval)))
    except KeyboardInterrupt:
        if json_mode:
            pass
        else:
            click.echo("\nStopped ingest watcher.")

    if json_mode:
        payload = {
            "watch_dir": str(watch_path),
            "mode": "auto_queue" if auto_queue else "detect_only",
            "material": material_type,
            "state_file": str(state_path) if state_path else None,
            "min_stable_seconds": max(0.0, float(min_stable_seconds)),
            "detected": detected_all,
            "queued": queued,
            "dispatched": dispatched,
            "errors": errors,
            "pending": {name: [str(p) for p in items] for name, items in pending.items()},
        }
        click.echo(format_response("success", data=payload, json_mode=True))


@ingest.group("service")
def ingest_service_group() -> None:
    """Manage ingest watcher as an explicit background service."""


@ingest_service_group.command("install")
@click.option(
    "--dir",
    "watch_dir",
    required=True,
    type=click.Path(exists=True, file_okay=False, dir_okay=True),
    help="Directory to watch for printable files.",
)
@click.option("--interval", default=2.0, show_default=True, help="Polling interval in seconds.")
@click.option(
    "--auto-queue",
    is_flag=True,
    help="Opt in to automatic queue/dispatch behavior (default is detect-only).",
)
@click.option(
    "--printer",
    default=None,
    help="Optional fixed printer name. Omit to auto-route across configured printers.",
)
@click.option(
    "--material",
    "-m",
    default="PLA",
    show_default=True,
    type=click.Choice(_MATERIAL_CHOICES, case_sensitive=False),
    help="Target material used for auto-routing.",
)
@click.option(
    "--min-stable-seconds",
    default=2.0,
    show_default=True,
    type=float,
    help="Require files to be stable for this many seconds before ingest.",
)
@click.option(
    "--state-file",
    default=None,
    type=click.Path(dir_okay=False, file_okay=True, path_type=str),
    help="Optional JSON state file for restart-safe ingest tracking.",
)
@click.option(
    "--config-path",
    default=None,
    type=click.Path(dir_okay=False, file_okay=True, path_type=str),
    help="Optional path for ingest service configuration.",
)
@click.option("--force", is_flag=True, help="Overwrite existing service config when not running.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def ingest_service_install_cmd(
    watch_dir: str,
    interval: float,
    auto_queue: bool,
    printer: str | None,
    material: str,
    min_stable_seconds: float,
    state_file: str | None,
    config_path: str | None,
    force: bool,
    json_mode: bool,
) -> None:
    """Install or update background ingest service configuration."""
    if interval <= 0:
        click.echo(format_error("Interval must be > 0.", code="INVALID_ARGS", json_mode=json_mode))
        sys.exit(1)
    if min_stable_seconds < 0:
        click.echo(format_error("min-stable-seconds must be >= 0.", code="INVALID_ARGS", json_mode=json_mode))
        sys.exit(1)

    watch_path = Path(watch_dir).expanduser().resolve()
    if not watch_path.is_dir():
        click.echo(
            format_error(
                f"Watch directory is not valid: {watch_path}",
                code="INVALID_ARGS",
                json_mode=json_mode,
            )
        )
        sys.exit(1)

    cfg_path = _resolve_service_config_path(config_path)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    existing_cfg = _read_json_file(cfg_path, default={})
    if cfg_path.exists() and existing_cfg:
        existing_pid_path, _, _ = _resolve_service_sidecar_paths(cfg_path, existing_cfg)
        existing_pid = _read_pid_file(existing_pid_path)
        if existing_pid and _is_pid_running(existing_pid):
            click.echo(
                format_error(
                    "Ingest service is running. Stop it before reinstalling config.",
                    code="SERVICE_RUNNING",
                    json_mode=json_mode,
                )
            )
            sys.exit(1)
        if not force:
            click.echo(
                format_error(
                    f"Service config already exists at {cfg_path}. Use --force to overwrite.",
                    code="ALREADY_EXISTS",
                    json_mode=json_mode,
                )
            )
            sys.exit(1)

    cfg_dir = cfg_path.parent
    state_path = Path(state_file).expanduser().resolve() if state_file else (cfg_dir / "watch_state.json").resolve()
    pid_path = (cfg_dir / "service.pid").resolve()
    log_path = (cfg_dir / "service.log").resolve()
    material_type = _normalise_material_type(material) or "PLA"

    payload: dict[str, Any] = {
        "watch_dir": str(watch_path),
        "interval": float(interval),
        "auto_queue": bool(auto_queue),
        "printer": (printer or "").strip(),
        "material": material_type,
        "min_stable_seconds": float(min_stable_seconds),
        "state_file": str(state_path),
        "pid_file": str(pid_path),
        "log_file": str(log_path),
        "installed_at": time.time(),
    }
    try:
        _write_json_file(cfg_path, payload)
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to write service config: {exc}",
                code="SERVICE_INSTALL_FAILED",
                json_mode=json_mode,
            )
        )
        sys.exit(1)

    if json_mode:
        click.echo(
            format_response(
                "success",
                data={
                    "config_path": str(cfg_path),
                    "mode": "auto_queue" if auto_queue else "detect_only",
                    "watch_dir": str(watch_path),
                    "state_file": str(state_path),
                    "log_file": str(log_path),
                    "pid_file": str(pid_path),
                },
                json_mode=True,
            )
        )
        return

    click.echo("Installed ingest service configuration.")
    click.echo(f"  Config: {cfg_path}")
    click.echo(f"  Watch:  {watch_path}")
    click.echo(f"  Mode:   {'auto-queue' if auto_queue else 'detect-only'}")
    click.echo(f"  State:  {state_path}")
    click.echo(f"  Logs:   {log_path}")


@ingest_service_group.command("start")
@click.option(
    "--config-path",
    default=None,
    type=click.Path(dir_okay=False, file_okay=True, path_type=str),
    help="Optional path for ingest service configuration.",
)
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def ingest_service_start_cmd(config_path: str | None, json_mode: bool) -> None:
    """Start ingest watcher service in the background."""
    cfg_path = _resolve_service_config_path(config_path)
    if not cfg_path.exists():
        click.echo(
            format_error(
                f"Service config not found: {cfg_path}. Run `kiln ingest service install` first.",
                code="SERVICE_NOT_INSTALLED",
                json_mode=json_mode,
            )
        )
        sys.exit(1)

    cfg = _read_json_file(cfg_path, default={})
    watch_dir = str(cfg.get("watch_dir", "")).strip()
    if not watch_dir:
        click.echo(
            format_error(
                "Service config is missing watch_dir.",
                code="SERVICE_CONFIG_INVALID",
                json_mode=json_mode,
            )
        )
        sys.exit(1)

    watch_path = Path(watch_dir).expanduser().resolve()
    if not watch_path.is_dir():
        click.echo(
            format_error(
                f"Configured watch directory does not exist: {watch_path}",
                code="SERVICE_CONFIG_INVALID",
                json_mode=json_mode,
            )
        )
        sys.exit(1)

    try:
        interval = float(cfg.get("interval", 2.0))
    except (TypeError, ValueError):
        interval = 2.0
    if interval <= 0:
        interval = 2.0

    try:
        min_stable_seconds = float(cfg.get("min_stable_seconds", 2.0))
    except (TypeError, ValueError):
        min_stable_seconds = 2.0
    if min_stable_seconds < 0:
        min_stable_seconds = 0.0

    auto_queue = _coerce_bool(cfg.get("auto_queue", False))
    printer = str(cfg.get("printer", "")).strip()
    material = _normalise_material_type(str(cfg.get("material", "PLA"))) or "PLA"
    pid_path, log_path, state_path = _resolve_service_sidecar_paths(cfg_path, cfg)

    existing_pid = _read_pid_file(pid_path)
    if existing_pid and _is_pid_running(existing_pid):
        click.echo(
            format_error(
                f"Ingest service already running (pid={existing_pid}).",
                code="SERVICE_RUNNING",
                json_mode=json_mode,
            )
        )
        sys.exit(1)

    if existing_pid and not _is_pid_running(existing_pid):
        with contextlib.suppress(Exception):
            pid_path.unlink(missing_ok=True)

    cfg.update(
        {
            "watch_dir": str(watch_path),
            "interval": float(interval),
            "auto_queue": bool(auto_queue),
            "printer": printer,
            "material": material,
            "min_stable_seconds": float(min_stable_seconds),
            "pid_file": str(pid_path),
            "log_file": str(log_path),
            "state_file": str(state_path),
            "last_start_attempt_at": time.time(),
        }
    )
    _write_json_file(cfg_path, cfg)

    cmd = _build_ingest_watch_command(cfg)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with log_path.open("a", encoding="utf-8") as log_file:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            log_file.write(f"\n[{stamp}] starting ingest service\n")
            log_file.flush()
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
            )
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to launch ingest service: {exc}",
                code="SERVICE_START_FAILED",
                json_mode=json_mode,
            )
        )
        sys.exit(1)

    pid_path.write_text(f"{proc.pid}\n", encoding="utf-8")
    time.sleep(0.4)
    if proc.poll() is not None:
        pid_path.unlink(missing_ok=True)
        tail = _tail_text(log_path, max_lines=30)
        message = f"Ingest service exited immediately with code {proc.returncode}."
        if tail:
            message = f"{message}\nRecent logs:\n{tail}"
        click.echo(format_error(message, code="SERVICE_START_FAILED", json_mode=json_mode))
        sys.exit(1)

    cfg["last_started_at"] = time.time()
    cfg["last_started_pid"] = proc.pid
    _write_json_file(cfg_path, cfg)

    payload = {
        "running": True,
        "pid": proc.pid,
        "config_path": str(cfg_path),
        "log_file": str(log_path),
        "state_file": str(state_path),
        "mode": "auto_queue" if auto_queue else "detect_only",
        "command": cmd,
    }
    if json_mode:
        click.echo(format_response("success", data=payload, json_mode=True))
        return

    click.echo(f"Ingest service started (pid={proc.pid}).")
    click.echo(f"  Config: {cfg_path}")
    click.echo(f"  Logs:   {log_path}")
    click.echo(f"  State:  {state_path}")


@ingest_service_group.command("stop")
@click.option(
    "--config-path",
    default=None,
    type=click.Path(dir_okay=False, file_okay=True, path_type=str),
    help="Optional path for ingest service configuration.",
)
@click.option(
    "--timeout",
    default=8.0,
    show_default=True,
    type=float,
    help="Seconds to wait for graceful shutdown before force-kill.",
)
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def ingest_service_stop_cmd(config_path: str | None, timeout: float, json_mode: bool) -> None:
    """Stop the background ingest watcher service."""
    cfg_path = _resolve_service_config_path(config_path)
    cfg = _read_json_file(cfg_path, default={}) if cfg_path.exists() else {}
    pid_path, log_path, _ = _resolve_service_sidecar_paths(cfg_path, cfg)
    pid = _read_pid_file(pid_path)

    if not pid:
        payload = {
            "running": False,
            "stopped": False,
            "reason": "not_running",
            "config_path": str(cfg_path),
            "pid_file": str(pid_path),
        }
        if json_mode:
            click.echo(format_response("success", data=payload, json_mode=True))
        else:
            click.echo("Ingest service is not running.")
        return

    if not _is_pid_running(pid):
        pid_path.unlink(missing_ok=True)
        payload = {
            "running": False,
            "stopped": False,
            "reason": "stale_pid_removed",
            "pid": pid,
            "pid_file": str(pid_path),
        }
        if json_mode:
            click.echo(format_response("success", data=payload, json_mode=True))
        else:
            click.echo(f"Removed stale ingest service pid file (pid={pid}).")
        return

    forced = False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pid_path.unlink(missing_ok=True)
        if json_mode:
            click.echo(
                format_response(
                    "success",
                    data={"running": False, "stopped": True, "pid": pid, "forced": False},
                    json_mode=True,
                )
            )
        else:
            click.echo("Ingest service stopped.")
        return
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to signal ingest service pid {pid}: {exc}",
                code="SERVICE_STOP_FAILED",
                json_mode=json_mode,
            )
        )
        sys.exit(1)

    deadline = time.time() + max(0.5, timeout)
    while time.time() < deadline:
        if not _is_pid_running(pid):
            break
        time.sleep(0.2)

    if _is_pid_running(pid):
        forced = True
        kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
        try:
            os.kill(pid, kill_signal)
        except Exception as exc:
            click.echo(
                format_error(
                    f"Failed to force-stop ingest service pid {pid}: {exc}",
                    code="SERVICE_STOP_FAILED",
                    json_mode=json_mode,
                )
            )
            sys.exit(1)
        time.sleep(0.2)

    if _is_pid_running(pid):
        click.echo(
            format_error(
                f"Ingest service pid {pid} is still running after stop attempt.",
                code="SERVICE_STOP_FAILED",
                json_mode=json_mode,
            )
        )
        sys.exit(1)

    pid_path.unlink(missing_ok=True)
    payload = {
        "running": False,
        "stopped": True,
        "pid": pid,
        "forced": forced,
        "log_file": str(log_path),
    }
    if json_mode:
        click.echo(format_response("success", data=payload, json_mode=True))
        return

    click.echo(f"Ingest service stopped (pid={pid}).")
    if forced:
        click.echo("  Shutdown required force-kill after timeout.")


@ingest_service_group.command("status")
@click.option(
    "--config-path",
    default=None,
    type=click.Path(dir_okay=False, file_okay=True, path_type=str),
    help="Optional path for ingest service configuration.",
)
@click.option("--tail-lines", default=20, show_default=True, type=int, help="Number of recent log lines to include.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def ingest_service_status_cmd(config_path: str | None, tail_lines: int, json_mode: bool) -> None:
    """Show ingest service install/runtime status."""
    cfg_path = _resolve_service_config_path(config_path)
    cfg_exists = cfg_path.exists()
    cfg = _read_json_file(cfg_path, default={}) if cfg_exists else {}
    pid_path, log_path, state_path = _resolve_service_sidecar_paths(cfg_path, cfg)

    pid = _read_pid_file(pid_path)
    running = bool(pid and _is_pid_running(pid))
    stale_pid = bool(pid and not running)
    mode = "auto_queue" if _coerce_bool(cfg.get("auto_queue", False)) else "detect_only"
    seen_entries = len(_load_ingest_seen_state(state_path))
    tail = _tail_text(log_path, max_lines=max(0, tail_lines)) if tail_lines > 0 else ""

    payload = {
        "installed": cfg_exists,
        "running": running,
        "stale_pid": stale_pid,
        "pid": pid if running else None,
        "config_path": str(cfg_path),
        "watch_dir": str(cfg.get("watch_dir", "")).strip() or None,
        "mode": mode,
        "interval": cfg.get("interval", 2.0),
        "min_stable_seconds": cfg.get("min_stable_seconds", 2.0),
        "state_file": str(state_path),
        "state_seen_entries": seen_entries,
        "pid_file": str(pid_path),
        "log_file": str(log_path),
        "log_tail": tail,
    }
    if json_mode:
        click.echo(format_response("success", data=payload, json_mode=True))
        return

    if not cfg_exists:
        click.echo(f"Ingest service is not installed. Expected config: {cfg_path}")
        return

    status_label = "running" if running else "stopped"
    if stale_pid:
        status_label = "stopped (stale pid file)"
    click.echo(f"Ingest service: {status_label}")
    click.echo(f"  Config: {cfg_path}")
    click.echo(f"  Watch:  {payload['watch_dir'] or '-'}")
    click.echo(f"  Mode:   {'auto-queue' if mode == 'auto_queue' else 'detect-only'}")
    click.echo(f"  State:  {state_path} ({seen_entries} tracked entries)")
    click.echo(f"  Logs:   {log_path}")
    if running and pid:
        click.echo(f"  PID:    {pid}")
    if tail:
        click.echo("  Recent logs:")
        for line in tail.splitlines():
            click.echo(f"    {line}")


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


@cli.command("estimate-before-design")
@click.option("--width", "width_mm", type=float, default=0.0, help="Part width (X) in mm.")
@click.option("--depth", "depth_mm", type=float, default=0.0, help="Part depth (Y) in mm.")
@click.option("--height", "height_mm", type=float, default=0.0, help="Part height (Z) in mm.")
@click.option("--template", "template_id", default="", help='Design template ID (e.g. "phone_stand").')
@click.option("--template-overrides", default="", help='JSON template param overrides (e.g. \'{"phone_width": 85}\').')
@click.option("--materials", "-m", default="PLA", help='Comma-separated materials (e.g. "PLA,PLA").')
@click.option("--fractions", default="", help='Comma-separated volume fractions (e.g. "0.85,0.15").')
@click.option("--roles", default="", help='Comma-separated role labels (e.g. "body,accent").')
@click.option("--infill", type=float, default=-1.0, help="Infill percent (0-100). -1 = auto.")
@click.option("--layer-height", type=float, default=0.0, help="Layer height in mm. 0 = auto.")
@click.option("--nozzle", type=float, default=0.4, help="Nozzle diameter in mm.")
@click.option("--walls", type=int, default=3, help="Number of perimeter shells.")
@click.option("--printer", "printer_id", default="", help='Printer model ID (e.g. "bambu_a1").')
@click.option("--electricity-rate", type=float, default=0.12, help="USD per kWh.")
@click.option("--printer-wattage", type=float, default=200.0, help="Printer power in watts.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def estimate_before_design_cmd(
    ctx: click.Context,
    width_mm: float,
    depth_mm: float,
    height_mm: float,
    template_id: str,
    template_overrides: str,
    materials: str,
    fractions: str,
    roles: str,
    infill: float,
    layer_height: float,
    nozzle: float,
    walls: int,
    printer_id: str,
    electricity_rate: float,
    printer_wattage: float,
    json_mode: bool,
) -> None:
    """Estimate print time, cost, and filament usage BEFORE generating a model.

    No file needed — works from dimensions or a template ID alone.

    Examples:

        kiln estimate-before-design --width 120 --depth 120 --height 15 --printer bambu_a1

        kiln estimate-before-design --template phone_stand --printer bambu_a1

        kiln estimate-before-design --width 90 --depth 90 --height 12 -m "PLA,PLA" --fractions "0.85,0.15" --printer bambu_a1
    """
    import json as _json

    from kiln.pre_estimate import estimate_from_dimensions, estimate_from_template

    try:
        mat_list = [m.strip() for m in materials.split(",") if m.strip()]
        if not mat_list:
            mat_list = ["PLA"]

        frac_list: list[float] | None = None
        if fractions.strip():
            frac_list = [float(f.strip()) for f in fractions.split(",")]

        role_list: list[str] | None = None
        if roles.strip():
            role_list = [r.strip() for r in roles.split(",")]

        eff_infill: float | None = None if infill < 0 else infill
        eff_layer: float | None = layer_height if layer_height > 0 else None
        eff_printer: str | None = printer_id if printer_id else None

        tpl_overrides: dict | None = None
        if template_overrides.strip():
            tpl_overrides = _json.loads(template_overrides)

        if template_id.strip():
            est = estimate_from_template(
                template_id.strip(),
                param_overrides=tpl_overrides,
                materials=mat_list,
                material_fractions=frac_list,
                material_roles=role_list,
                infill_percent=eff_infill,
                layer_height_mm=eff_layer,
                nozzle_mm=nozzle,
                wall_layers=walls,
                printer_id=eff_printer,
                electricity_rate=electricity_rate,
                printer_wattage=printer_wattage,
            )
        else:
            if width_mm <= 0 or depth_mm <= 0 or height_mm <= 0:
                raise click.UsageError(
                    "Provide --width/--depth/--height or --template."
                )
            est = estimate_from_dimensions(
                width_mm,
                depth_mm,
                height_mm,
                materials=mat_list,
                material_fractions=frac_list,
                material_roles=role_list,
                infill_percent=eff_infill,
                layer_height_mm=eff_layer,
                nozzle_mm=nozzle,
                wall_layers=walls,
                printer_id=eff_printer,
                electricity_rate=electricity_rate,
                printer_wattage=printer_wattage,
            )

        if json_mode:
            click.echo(_json.dumps({"status": "success", "data": est.to_dict()}, indent=2))
        else:
            click.echo("\n  Pre-Design Estimate")
            click.echo(f"  Dimensions: {est.width_mm} × {est.depth_mm} × {est.height_mm} mm")
            click.echo(f"  Volume: ~{est.volume_mm3:.0f} mm³")
            click.echo(f"  Time: {est.estimated_time_human}")
            if est.tool_changes > 0:
                tc_time = est.tool_change_time_seconds
                tc_h, tc_rem = divmod(tc_time, 3600)
                tc_m = tc_rem // 60
                tc_str = f"{tc_h}h {tc_m}m" if tc_h else f"{tc_m}m"
                click.echo(
                    f"  Tool swaps: {est.tool_changes} ({est.tool_change_type}, +{tc_str})"
                )
            click.echo("\n  Filament:")
            for f in est.filaments:
                click.echo(
                    f"    {f.material} ({f.role}): {f.weight_grams}g, "
                    f"{f.length_meters}m — ${f.cost_usd:.2f}"
                )
            click.echo("\n  Cost:")
            click.echo(f"    Filament: ${est.filament_cost_usd:.2f}")
            click.echo(f"    Electricity: ${est.electricity_cost_usd:.2f}")
            click.echo(f"    Total: ${est.total_cost_usd:.2f}")
            click.echo(f"\n  Settings: {est.infill_percent:.0f}% infill, {est.layer_height_mm}mm layers, {est.nozzle_mm}mm nozzle")
            if est.printer_id:
                click.echo(f"  Printer: {est.printer_id}")
            click.echo(f"  Confidence: {est.confidence}")
            for note in est.confidence_notes:
                click.echo(f"    - {note}")
            for warn in est.warnings:
                click.echo(f"  ⚠ {warn}")
            click.echo()

    except click.UsageError:
        raise
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
@click.option(
    "--provider",
    default=None,
    type=click.Choice(["craftcloud", "proxy"]),
    help="Fulfillment provider (default: auto-detect, falls back to craftcloud).",
)
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def compare_cost(
    file_path: str,
    material: str,
    fulfillment_material: str | None,
    quantity: int,
    electricity_rate: float,
    printer_wattage: float,
    country: str,
    provider: str | None,
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

            # Use explicit provider, or auto-detect, or fall back to
            # craftcloud (which works without any API key for quotes).
            fulfillment_provider = None
            try:
                fulfillment_provider = get_provider(provider)
            except Exception:
                # Proxy unreachable or no env vars — fall back to Craftcloud direct
                if provider is None:
                    fulfillment_provider = get_provider("craftcloud")
                else:
                    raise

            # Resolve simple material names (PLA, Nylon, etc.) to provider IDs
            resolved_material = fulfillment_material
            if len(fulfillment_material) < 20 or "-" not in fulfillment_material:
                # Looks like a simple name, not a UUID — resolve it
                try:
                    resolved_material = _resolve_fulfillment_material(
                        fulfillment_material, provider_name=provider
                    )
                    if not json_mode:
                        click.echo(f"  Resolved material {fulfillment_material!r} → {resolved_material}")
                except click.ClickException:
                    # Fall through with the original name; provider may accept it
                    pass

            # Get quote, falling back to Craftcloud if provider fails
            def _get_quote_with_fallback() -> Any:
                try:
                    return fulfillment_provider.get_quote(
                        QR(
                            file_path=file_path,
                            material_id=resolved_material,
                            quantity=quantity,
                            shipping_country=country,
                        )
                    )
                except Exception:
                    if provider is None and fulfillment_provider.name != "craftcloud":
                        if not json_mode:
                            click.echo(f"  {fulfillment_provider.name} unavailable, falling back to craftcloud...")
                        cc = get_provider("craftcloud")
                        return cc.get_quote(
                            QR(
                                file_path=file_path,
                                material_id=resolved_material,
                                quantity=quantity,
                                shipping_country=country,
                            )
                        )
                    raise

            quote = _get_quote_with_fallback()
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
# Fulfillment material lookup
# ---------------------------------------------------------------------------

# Common material name aliases → search terms for the Craftcloud catalog
_MATERIAL_ALIASES: dict[str, list[str]] = {
    "pla": ["PLA"],
    "petg": ["PETG"],
    "abs": ["ABS"],
    "tpu": ["TPU"],
    "asa": ["ASA"],
    "nylon": ["Nylon", "PA12", "PA11"],
    "pa12": ["PA12", "Nylon"],
    "pa11": ["PA11", "Nylon"],
    "resin": ["Resin", "SLA"],
    "metal": ["Steel", "Aluminum", "Titanium"],
    "steel": ["Steel", "Stainless"],
    "aluminum": ["Aluminum"],
    "titanium": ["Titanium"],
    "copper": ["Copper"],
    "carbon": ["Carbon"],
    "wood": ["Wood"],
    "flex": ["Flex", "TPU"],
}


def _resolve_fulfillment_material(
    simple_name: str,
    provider_name: str | None = None,
) -> str:
    """Resolve a simple material name (PLA, PETG, etc.) to a provider material ID.

    Fetches the provider's material catalog, searches for matches by name,
    and returns the best matching material config ID.

    :param simple_name: Simple material name like "PLA", "Nylon", etc.
    :param provider_name: Fulfillment provider name (default: auto-detect).
    :returns: The material config ID string (e.g., a UUID for Craftcloud).
    :raises click.ClickException: If no matching material is found.
    """
    from kiln.fulfillment import get_provider

    try:
        prov = get_provider(provider_name)
    except Exception:
        if provider_name is None:
            prov = get_provider("craftcloud")
        else:
            raise

    try:
        materials = prov.list_materials()
    except Exception:
        if provider_name is None and prov.name != "craftcloud":
            prov = get_provider("craftcloud")
            materials = prov.list_materials()
        else:
            raise

    if not materials:
        raise click.ClickException("No materials available from the fulfillment provider.")

    import re as _re

    query = simple_name.strip().lower()
    search_terms = _MATERIAL_ALIASES.get(query, [simple_name])

    # Score each material by how well it matches (word-boundary matching)
    scored: list[tuple[int, Any]] = []
    for mat in materials:
        name_lower = mat.name.lower()
        tech_lower = mat.technology.lower()
        score = 0
        for term in search_terms:
            term_l = term.lower()
            pattern = r"(?<![a-z])" + _re.escape(term_l) + r"(?![a-z])"
            if _re.search(pattern, name_lower):
                score += 10
            if _re.search(pattern, tech_lower):
                score += 3
        # Bonus for "Standard" finish (most common)
        if "standard" in mat.finish.lower():
            score += 2
        # Bonus for FDM technology (matches PLA/PETG/ABS expectations)
        if query in ("pla", "petg", "abs", "tpu", "asa") and "fdm" in tech_lower:
            score += 5
        if score > 0:
            scored.append((score, mat))

    if not scored:
        # If the name looks like a UUID already, pass it through
        if len(simple_name) > 20 and "-" in simple_name:
            return simple_name
        raise click.ClickException(
            f"No matching material found for {simple_name!r}. "
            f"Run 'kiln fulfillment-materials' to see available materials."
        )

    scored.sort(key=lambda x: x[0], reverse=True)
    best = scored[0][1]
    return best.id


@cli.command("fulfillment-materials")
@click.option("--search", "-s", default=None, help="Search/filter materials by name (e.g. PLA, Nylon, Resin).")
@click.option(
    "--provider",
    default=None,
    type=click.Choice(["craftcloud", "proxy"]),
    help="Fulfillment provider (default: auto-detect).",
)
@click.option("--technology", "-t", default=None, help="Filter by technology (FDM, SLA, SLS, MJF, etc.).")
@click.option("--limit", "-n", default=25, type=int, help="Max materials to show (default 25).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def fulfillment_materials(
    search: str | None,
    provider: str | None,
    technology: str | None,
    limit: int,
    json_mode: bool,
) -> None:
    """List available materials from fulfillment providers.

    Shows material IDs, names, technologies, colors, and finishes.
    Use --search to find specific materials (e.g. PLA, Nylon, Resin).
    Use the material ID with compare-cost --fulfillment-material.

    \b
    Examples:
        kiln fulfillment-materials
        kiln fulfillment-materials --search PLA
        kiln fulfillment-materials --technology SLS --limit 50
        kiln fulfillment-materials --search Nylon --json
    """
    import json

    from kiln.fulfillment import get_provider
    from kiln.fulfillment.base import FulfillmentError

    try:
        try:
            prov = get_provider(provider)
        except Exception:
            if provider is None:
                prov = get_provider("craftcloud")
            else:
                raise

        if not json_mode:
            click.echo(f"Fetching materials from {prov.name}...")

        try:
            materials = prov.list_materials()
        except Exception:
            # If auto-detected provider fails (e.g., proxy unreachable),
            # fall back to Craftcloud which works without API key.
            if provider is None and prov.name != "craftcloud":
                if not json_mode:
                    click.echo(f"  {prov.name} unavailable, falling back to craftcloud...")
                prov = get_provider("craftcloud")
                materials = prov.list_materials()
            else:
                raise

        if not materials:
            click.echo(format_error("No materials returned by provider.", json_mode=json_mode))
            return

        # Filter by search term (word-boundary matching to avoid
        # false positives like "PLA" matching "electroplated")
        if search:
            import re as _re

            query = search.strip().lower()
            search_terms = _MATERIAL_ALIASES.get(query, [search])
            filtered = []
            for mat in materials:
                text = f"{mat.name} {mat.technology} {mat.color} {mat.finish}".lower()
                for term in search_terms:
                    # Word boundary match: PLA must not be part of a larger word
                    if _re.search(r"(?<![a-z])" + _re.escape(term.lower()) + r"(?![a-z])", text):
                        filtered.append(mat)
                        break
            materials = filtered

        # Filter by technology (check both the technology field and material name,
        # since some providers like Craftcloud use "3d_printing" as technology
        # and embed the actual tech (SLS, FDM, SLA, etc.) in the name)
        if technology:
            import re as _re2

            tech_q = technology.strip().lower()
            tech_pattern = r"(?<![a-z])" + _re2.escape(tech_q) + r"(?![a-z])"
            materials = [
                m
                for m in materials
                if m.technology.lower() == tech_q
                or _re2.search(tech_pattern, m.name.lower())
                or _re2.search(tech_pattern, m.technology.lower())
            ]

        total_count = len(materials)
        materials = materials[:limit]

        if json_mode:
            click.echo(
                json.dumps(
                    {
                        "status": "success",
                        "data": {
                            "provider": prov.name,
                            "total_count": total_count,
                            "shown": len(materials),
                            "materials": [m.to_dict() for m in materials],
                        },
                    },
                    indent=2,
                )
            )
        else:
            click.echo(f"\nFound {total_count} materials" + (f" (showing first {limit})" if total_count > limit else ""))
            click.echo(f"{'ID':<40} {'Technology':<8} {'Name':<50} {'Color':<12} {'Finish'}")
            click.echo("─" * 130)
            for mat in materials:
                name_display = mat.name[:48] + ".." if len(mat.name) > 50 else mat.name
                click.echo(
                    f"{mat.id:<40} {mat.technology:<8} {name_display:<50} {mat.color:<12} {mat.finish}"
                )
            if total_count > limit:
                click.echo(f"\n… {total_count - limit} more. Use --limit {total_count} to see all.")
            click.echo("\nUse material ID with: kiln compare-cost model.stl --fulfillment-material <ID>")

    except FulfillmentError as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


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
            # Stated at the writer: this is the operator typing a material
            # name, so readers must not report it back as a sensor reading.
            determined_by="user_reported",
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
@click.option("--live", is_flag=True, default=False, help="Query printer directly (AMS/filament sensor).")
@click.pass_context
def material_show(ctx: click.Context, json_mode: bool, live: bool) -> None:
    """Show loaded materials for the active printer.

    By default queries the local material database.  Use --live to query
    the printer directly (e.g. Bambu AMS slot data via MQTT).  When the
    local database is empty, --live is tried automatically as a fallback.
    """
    import json as _json

    from kiln.materials import MaterialTracker
    from kiln.persistence import get_db

    try:
        printer_name = ctx.obj.get("printer") or "default"

        # --live: skip local DB, go straight to printer.
        if not live:
            tracker = MaterialTracker(db=get_db())
            materials = tracker.get_all_materials(printer_name)
            if materials:
                if json_mode:
                    click.echo(
                        _json.dumps(
                            {"status": "success", "source": "database",
                             "data": [m.to_dict() for m in materials]},
                            indent=2,
                        )
                    )
                else:
                    for m in materials:
                        line = f"Tool {m.tool_index}: {m.material_type}"
                        if m.color:
                            line += f" ({m.color})"
                        if m.remaining_grams is not None:
                            line += f" — {m.remaining_grams:.0f}g remaining"
                        click.echo(line)
                    # These rows are a record of what someone typed, not a
                    # reading.  Printed under a "loaded materials" heading
                    # with no provenance, they read as a measurement — which
                    # is how a stale row became "PETG is loaded" to a user.
                    if not all(m.is_sensed for m in materials):
                        click.echo(
                            "Recorded with `kiln material set` — no sensor "
                            "confirmed this. Use --live to ask the printer."
                        )
                return
            # Local DB empty — fall through to live query.

        # Live query: ask the printer what's loaded.
        import time as _time

        try:
            adapter = _get_adapter_from_ctx(ctx)
        except (click.ClickException, Exception):
            if not live:
                # Was a silent fallback — just say nothing loaded.
                if json_mode:
                    click.echo(_json.dumps({"status": "success", "source": "database", "data": []}, indent=2))
                else:
                    click.echo("No materials loaded. Use --live to query printer directly.")
                return
            raise

        ams_data = None
        if hasattr(adapter, "get_ams_status"):
            # AMS data may arrive after the initial MQTT pushall response.
            # Bambu printers send AMS tray info in a separate MQTT message
            # that can arrive 3-8s after the initial status dump.
            # Warm up the MQTT session with get_state(), then poll for
            # the AMS payload with increasing delays.
            if not json_mode:
                click.echo("Querying printer for AMS data...")
            with contextlib.suppress(Exception):
                adapter.get_state()  # Triggers MQTT connect + pushall.
            for _attempt in range(5):
                _time.sleep(2.0)
                ams_data = adapter.get_ams_status()
                if ams_data and ams_data.get("units"):
                    break
                # Request another pushall to coax the AMS data out.
                if hasattr(adapter, "_publish_command") and hasattr(adapter, "_next_seq"):
                    with contextlib.suppress(Exception):
                        adapter._publish_command(
                            {"pushing": {"sequence_id": adapter._next_seq(), "command": "pushall"}}
                        )

        if ams_data and ams_data.get("units"):
            tray_now = str(ams_data.get("tray_now", "255"))
            slots: list[dict] = []
            for unit in ams_data["units"]:
                unit_id = int(unit.get("unit_id", 0))
                humidity = unit.get("humidity")
                # The adapter flags humidity / remaining unknown on hardware
                # that can't measure them (AMS Lite, untagged spools); drop
                # the value rather than show a placeholder as a real reading.
                humidity_known = bool(unit.get("humidity_known"))
                for tray in unit.get("trays", []):
                    slot_num = unit_id * 4 + int(tray.get("slot", 0)) + 1  # 1-indexed
                    color_hex = tray.get("tray_color", "")
                    # Convert RRGGBBAA hex to readable color name or short hex.
                    color_display = f"#{color_hex[:6]}" if len(color_hex) >= 6 else color_hex
                    tray_type = tray.get("tray_type", "")
                    remain = tray.get("remain")
                    remaining_known = tray.get("remaining_known")
                    is_active = str(tray.get("slot", -1)) == str(tray_now)
                    entry = {
                        "slot": slot_num,
                        "type": tray_type,
                        "color": color_display,
                        "color_raw": color_hex,
                        "remain_pct": (
                            remain
                            if remaining_known and isinstance(remain, (int, float))
                            else None
                        ),
                        "active": is_active,
                        "nozzle_temp_min": tray.get("nozzle_temp_min"),
                        "nozzle_temp_max": tray.get("nozzle_temp_max"),
                        "bed_temp": tray.get("bed_temp"),
                    }
                    if humidity is not None and humidity_known:
                        entry["humidity_pct"] = humidity
                    slots.append(entry)

            if json_mode:
                click.echo(_json.dumps({"status": "success", "source": "printer", "data": slots}, indent=2))
            else:
                click.echo("AMS slots (live from printer):")
                for s in slots:
                    active = " ◀ active" if s["active"] else ""
                    remain = f" — {s['remain_pct']}% left" if s.get("remain_pct") is not None else ""
                    ttype = s["type"] or "empty"
                    color = f" ({s['color']})" if s["color"] else ""
                    click.echo(f"  Slot {s['slot']}: {ttype}{color}{remain}{active}")
        else:
            # No AMS — try filament sensor.
            filament = None
            if hasattr(adapter, "get_filament_status"):
                filament = adapter.get_filament_status()
            if filament:
                if json_mode:
                    click.echo(_json.dumps({"status": "success", "source": "printer", "data": filament}, indent=2))
                else:
                    detected = filament.get("detected", "unknown")
                    click.echo(f"Filament sensor: {'detected' if detected else 'not detected'}")
            else:
                if json_mode:
                    click.echo(_json.dumps({"status": "success", "source": "printer", "data": []}, indent=2))
                else:
                    click.echo("No AMS or filament data available from printer.")
    except click.ClickException:
        raise
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
            marker = " (default)" if p.get("active") else ""
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
                label = PRINTER_TYPE_LABELS.get(p.printer_type, p.printer_type)
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
                type=_PrinterTypeChoice(list(NETWORK_PRINTER_TYPES)),
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
                    f"  Detected: {PRINTER_TYPE_LABELS.get(printer_type, printer_type)}"
                    + (f" ({p.name})" if p.name else "")
                )
                suggested_name = (p.name or printer_type).lower().replace(" ", "-").replace(".", "-")
            else:
                click.echo("  Could not auto-detect printer type.")
                printer_type = click.prompt(
                    "  Select printer type",
                    type=_PrinterTypeChoice(list(NETWORK_PRINTER_TYPES)),
                )
                suggested_name = printer_type
        except Exception as exc:
            logger.debug("Printer probe failed for %s: %s", host, exc)
            click.echo("  Probe failed. Enter type manually.")
            printer_type = click.prompt(
                "  Select printer type",
                type=_PrinterTypeChoice(list(NETWORK_PRINTER_TYPES)),
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

    if printer_type in ("octoprint", "moonraker", "creality", "prusalink", "duet"):
        # RepRapFirmware authenticates with a machine password (set by M551),
        # not an API key -- asking for the wrong thing by name sends people
        # hunting for a key their printer never issues.
        credential = "Machine password" if printer_type == "duet" else "API key"
        api_key = click.prompt(
            f"  {credential} for {PRINTER_TYPE_LABELS.get(printer_type, printer_type)}",
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

    # -- printer_model (activates safety stack) ----------------------------
    # Incident #0 (2026-04-15) exposed that setup never asked for this
    # field, so every existing user's config skips the bed-fit / bounds /
    # temperature safety gates silently.  Always prompt here.  For
    # Bambu printers with a recognisable serial, we offer a confident
    # default the user can confirm with Enter.
    from kiln.cli.printer_model_prompt import prompt_for_printer_model
    printer_model = prompt_for_printer_model(
        printer_type, serial=serial, allow_skip=False,
    )

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
            printer_model=printer_model,
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
        if printer_type == "bambu" and hasattr(adapter, "get_ams_status"):
            try:
                ams_data = adapter.get_ams_status()
                loaded = []
                for unit in ams_data.get("units", []):
                    for tray in unit.get("trays", []):
                        tray_type = str(tray.get("tray_type", "") or "").strip()
                        if tray_type:
                            loaded.append(tray)
                if loaded:
                    click.echo(
                        click.style("  AMS visible!", fg="green")
                        + f" {len(loaded)} loaded tray(s) detected."
                    )
                    tray_now = str(ams_data.get("tray_now", "255"))
                    if tray_now == "255":
                        selected = ams_data.get("tray_pre") or ams_data.get("tray_tar")
                        if selected not in (None, "", "255"):
                            click.echo(f"  Selected AMS tray: {selected}")
                        else:
                            click.echo("  Active AMS tray not reported yet; start_print auto-routing will use loaded trays.")
                else:
                    click.echo(click.style("  AMS reachable, but no loaded trays were reported.", fg="yellow"))
            except Exception as exc:
                click.echo(click.style(f"  AMS check failed: {exc}", fg="yellow"))
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
    # Check for existing printers missing printer_model — one-time
    # migration nag for users who set up before incident #0 (2026-04-15).
    from kiln.cli.printer_model_prompt import (
        check_existing_config_for_missing_model,
        suggest_bambu_model,
    )
    if existing:
        active = next((p for p in existing if p.get("active")), existing[0])
        results["setup"] = {
            "action": "existing",
            "printer": active["name"],
        }
        if not json_mode:
            click.echo(f"    Already configured: {active['name']} [{active.get('type', '?')}]")
        # Migration nag: warn if printer_model isn't set
        try:
            from kiln.cli.config import _read_config_file, get_config_path
            raw_cfg = _read_config_file(get_config_path())
            missing = check_existing_config_for_missing_model(raw_cfg)
            if missing:
                suggestion = None
                if active.get("type") == "bambu" and active.get("serial"):
                    suggestion = suggest_bambu_model(active["serial"])
                if not json_mode:
                    click.echo()
                    click.echo(click.style(
                        "    ⚠ SAFETY GAP: printer_model is NOT set for: "
                        f"{', '.join(missing)}",
                        fg="yellow", bold=True,
                    ))
                    click.echo(
                        "    Until it's set, Kiln can't check that prints fit the\n"
                        "    bed or stay within safe temperatures — those checks are\n"
                        "    skipped, so an unsafe print could reach the printer."
                    )
                    if suggestion:
                        click.echo(click.style(
                            f"    Suggested value for your Bambu: printer_model: {suggestion}",
                            fg="cyan",
                        ))
                    click.echo(
                        "    Fix: add `printer_model: <value>` under the printer "
                        "entry in\n    ~/.kiln/config.yaml.  Run `kiln setup` to "
                        "re-run the interactive\n    flow which now asks for this."
                    )
                results["setup"]["missing_printer_model"] = missing
        except Exception:
            pass
    elif discovered:
        # Auto-configure the first discovered printer
        first = discovered[0]
        printer_name = (first.name or first.printer_type).lower().replace(" ", "-").replace(".", "-")
        # Offer a suggested printer_model for Bambu discoveries so the
        # non-interactive quickstart flow doesn't ship without the field.
        auto_model = None
        if first.printer_type == "bambu":
            serial_hint = getattr(first, "serial", None) or ""
            auto_model = suggest_bambu_model(serial_hint)
        try:
            save_printer(
                printer_name,
                first.printer_type,
                first.host,
                printer_model=auto_model,
                set_active=True,
            )
            results["setup"] = {
                "action": "auto_configured",
                "printer": printer_name,
                "host": first.host,
                "type": first.printer_type,
                "printer_model": auto_model,
            }
            if not json_mode:
                click.echo(f"    Auto-configured: {printer_name} [{first.printer_type}] at {first.host}")
                if auto_model:
                    click.echo(click.style(
                        f"    ✓ printer_model suggested from serial: {auto_model}",
                        fg="green",
                    ))
                else:
                    click.echo(click.style(
                        "    ⚠ printer_model NOT set — Kiln can't check that prints\n"
                        "      fit the bed or stay within safe temperatures.  Run\n"
                        "      `kiln setup` for the interactive flow that asks for it.",
                        fg="yellow",
                    ))
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


def _database_check() -> dict[str, Any]:
    """Can Kiln open its database?  The check both doctor doors call.

    This used to connect with raw ``sqlite3``, create a scratch table and
    drop it — which answers "is this file writable", a question nobody
    was asking.  The server dies further in, inside
    ``KilnDB._ensure_schema``, and a database can be perfectly writable
    and still fail there.  When a schema-ordering bug took down every
    upgraded install on 2026-08-12, this check reported ``✓ writable``:
    a confident all-clear on the one thing that was broken, printed to
    the user most likely to be looking for an answer.

    It now opens the real :class:`~kiln.persistence.KilnDB`, exactly as
    the server does — which also means it honours ``KILN_DB_PATH``
    instead of assuming ``~/.kiln/kiln.db`` and inspecting a file the
    server never touches.
    """
    from kiln import startup_failure

    diagnosis = startup_failure.probe_database()
    if diagnosis is None:
        return {"name": "database", "ok": True, "detail": "opens cleanly"}
    steps = diagnosis.steps_elsewhere()
    detail = diagnosis.headline
    if steps:
        detail = f"{detail} {steps[0]}"
    return {"name": "database", "ok": False, "detail": detail}


def _last_startup_failure_check() -> dict[str, Any] | None:
    """Report the last time the MCP server failed to start, if it did.

    ``None`` when there is no breadcrumb, so a healthy machine gets no
    line at all rather than a reassuring one nobody reads.  A successful
    start clears the file, so anything here describes a launch that has
    not yet been fixed.
    """
    from kiln import startup_failure

    crumb = startup_failure.read()
    if not crumb:
        return None
    when = f" on {crumb['when']}" if crumb.get("when") else ""
    return {
        "name": "last_startup",
        "ok": False,
        "detail": (
            f"the MCP server failed to start{when}: {crumb['headline']} "
            f"Full explanation and fix: {crumb['path']}"
        ),
    }


def _quickstart_verify() -> list[dict[str, Any]]:
    """Run lightweight environment checks for quickstart.

    Returns a list of check dicts with 'name', 'ok', 'detail' keys.
    """
    import platform

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
                "detail": (
                    "not found (install prusa-slicer, orcaslicer or "
                    "bambustudio, or set KILN_SLICER_PATH)"
                ),
            }
        )

    # Database opens (not merely writable — see _database_check)
    checks.append(_database_check())

    # Did the MCP server last fail to start?  Only appears when it did.
    _startup = _last_startup_failure_check()
    if _startup is not None:
        checks.append(_startup)

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
    # ``kiln.server.main`` guards everything it does, but it cannot guard
    # its own import — and importing that module pulls in most of Kiln.
    # A failure here is the same silence from the user's point of view,
    # so it gets the same breadcrumb and the same recovery server.  Only
    # the import is wrapped: once ``main()`` is running it owns the
    # difference between "could not start" and "died mid-session".
    from kiln import startup_failure

    try:
        from kiln.server import main as _server_main
    except Exception as exc:  # noqa: BLE001
        diagnosis, breadcrumb = startup_failure.handle(
            exc, phase="importing the server"
        )
        startup_failure.serve_safe_mode(diagnosis, breadcrumb)
        sys.exit(1)

    _server_main()


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
    default="gemini",
    type=click.Choice(["gemini", "openscad", "meshy", "tripo3d", "stability"]),
    help="Generation provider (default: gemini). Gemini supports text and image input.",
)
@click.option("--style", "-s", default=None, help="Style hint (e.g. realistic, sculpture).")
@click.option("--image", "-i", default=None, type=click.Path(exists=True),
              help="Image file (photo/sketch/napkin drawing) for image-to-3D generation (Gemini only).")
@click.option("--output-dir", "-o", default=None, help="Output directory for generated model.")
@click.option(
    "--wait/--no-wait", "wait_for", default=False, help="Wait for generation to complete (default: return immediately)."
)
@click.option("--timeout", "-t", default=600, type=int, help="Max wait time in seconds (default 600).")
@click.option("--preview/--no-preview", "preview_enabled", default=True, help="Render a 3-view preview after generation.")
@click.option("--verify/--no-verify", "verify_enabled", default=True, help="Run visual verification after generation (Gemini only).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def generate(
    prompt: str,
    provider: str,
    style: str | None,
    image: str | None,
    output_dir: str | None,
    wait_for: bool,
    timeout: int,
    preview_enabled: bool,
    verify_enabled: bool,
    json_mode: bool,
) -> None:
    """Generate a 3D model from a text description or image.

    PROMPT is the text description (for Meshy/Gemini) or OpenSCAD code (for openscad).
    Use --image to provide a photo, sketch, or napkin drawing for image-to-3D (Gemini).

    \b
    Examples:
        kiln generate "a phone stand with cable slot" --provider gemini
        kiln generate "cube([20,20,10]);" --provider openscad
        kiln generate "a gear with 24 teeth" --provider gemini --wait --json
        kiln generate "recreate this object" --provider gemini --image sketch.png
    """
    import time as _time

    from kiln.generation import (
        GenerationAuthError,
        GenerationError,
        GenerationStatus,
        validate_mesh,
    )

    if image and provider != "gemini":
        click.echo(
            click.style(
                f"Warning: --image is only supported by the gemini provider (got --provider {provider}). "
                f"Image will be ignored.",
                fg="yellow",
            )
        )

    try:
        gen = _resolve_generation_provider(provider)

        gen_kwargs: dict[str, Any] = {"format": "stl", "style": style, "verify": verify_enabled}
        if image and provider == "gemini":
            gen_kwargs["image_path"] = os.path.abspath(image)

        job = gen.generate(prompt, **gen_kwargs)

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
    "--provider", "-p", default="gemini", type=click.Choice(["gemini", "openscad", "meshy", "tripo3d", "stability"]), help="Generation provider."
)
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def generate_status(job_id: str, provider: str, json_mode: bool) -> None:
    """Check the status of a generation job.

    JOB_ID is the ID returned by 'kiln generate'.
    """
    from kiln.generation import (
        GenerationAuthError,
        GenerationError,
    )

    try:
        gen = _resolve_generation_provider(provider)

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
    "--provider", "-p", default="gemini", type=click.Choice(["gemini", "openscad", "meshy", "tripo3d", "stability"]), help="Generation provider."
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
        validate_mesh,
    )

    try:
        gen = _resolve_generation_provider(provider)

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
    default="gemini",
    type=click.Choice(["gemini", "openscad", "meshy", "tripo3d", "stability"]),
    help="Generation provider (default: gemini).",
)
@click.option("--style", "-s", default=None, help="Style hint.")
@click.option("--image", "-i", default=None, type=click.Path(exists=True),
              help="Image file (photo/sketch/napkin drawing) for image-to-3D generation (Gemini only).")
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
    image: str | None,
    printer_id: str | None,
    material: str | None,
    support_mode: str,
    timeout: int,
    auto_print: bool,
    preview_enabled: bool,
    json_mode: bool,
) -> None:
    """Generate a 3D model from text or image, slice it, and upload to the printer.

    One-command pipeline from description to print-ready.

    \b
    Examples:
        kiln generate-and-print "a phone stand" --provider gemini --material PLA
        kiln generate-and-print "a gear" --provider openscad --auto-print
        kiln generate-and-print "recreate this" --image sketch.png --material PETG
    """
    import time as _time

    from kiln.generation import (
        GenerationAuthError,
        GenerationError,
        GenerationStatus,
        validate_mesh,
    )
    from kiln.slicer import SlicerError, SlicerNotFoundError, slice_file

    if image and provider != "gemini":
        click.echo(
            click.style(
                f"Warning: --image is only supported by the gemini provider (got --provider {provider}). "
                f"Image will be ignored.",
                fg="yellow",
            )
        )

    try:
        # --- Step 1: Generate ---
        gen = _resolve_generation_provider(provider)

        if not json_mode:
            click.echo(f"Generating model with {gen.display_name}...")

        gen_kwargs: dict[str, Any] = {"format": "stl", "style": style}
        if image and provider == "gemini":
            gen_kwargs["image_path"] = os.path.abspath(image)

        job = gen.generate(prompt, **gen_kwargs)

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
    version.  Supported for OctoPrint, Moonraker, and Creality printers when exposed by the backend.
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
    Only supported on Moonraker-backed printers.
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
# local-first
# ---------------------------------------------------------------------------


@cli.command("local-first")
@click.option(
    "--apply",
    is_flag=True,
    help="Apply local-first defaults to local Kiln settings (disables cloud sync config).",
)
@click.option(
    "--write-env",
    is_flag=True,
    help="Write ~/.kiln/local-first.env with recommended local-first environment variables.",
)
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def local_first_cmd(apply: bool, write_env: bool, json_mode: bool) -> None:
    """Generate and optionally apply local-first defaults for edge deployments."""
    exports = {
        "KILN_LLM_PRIVACY_MODE": "1",
        "KILN_CONFIRM_MODE": "true",
        "KILN_CONFIRM_UPLOAD": "true",
        "KILN_AUTO_PRINT_MARKETPLACE": "false",
        "KILN_AUTO_PRINT_GENERATED": "false",
    }
    export_lines = [f"export {k}={v}" for k, v in exports.items()]
    applied: list[str] = []
    errors: list[str] = []
    warnings: list[str] = []
    env_file_path: str | None = None

    if apply:
        try:
            from kiln.persistence import get_db

            db = get_db()
            existing_cloud_sync = db.get_setting("cloud_sync_config", "") or ""
            if existing_cloud_sync.strip():
                db.set_setting("cloud_sync_config_backup", existing_cloud_sync)
                applied.append("cloud_sync_backup_saved")
                db.set_setting("cloud_sync_config", "")
                applied.append("cloud_sync_disabled")
            else:
                applied.append("cloud_sync_already_disabled")
        except Exception as exc:
            errors.append(f"Failed to disable cloud sync config: {exc}")

    if write_env:
        try:
            env_path = Path.home() / ".kiln" / "local-first.env"
            env_path.parent.mkdir(parents=True, exist_ok=True)
            header = [
                "# Kiln local-first profile",
                "# Source this file to keep workflows local/privacy-first by default.",
            ]
            env_path.write_text("\n".join(header + export_lines) + "\n", encoding="utf-8")
            env_file_path = str(env_path)
        except Exception as exc:
            errors.append(f"Failed to write local-first env file: {exc}")

    active_cloud_vars = [
        name for name in ("KILN_OPENROUTER_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY") if os.environ.get(name)
    ]
    if active_cloud_vars:
        warnings.append(
            "Cloud model API keys detected in environment: "
            + ", ".join(active_cloud_vars)
            + ". Local-first still works, but remote model calls may occur if enabled."
        )

    payload = {
        "profile": "local-first",
        "exports": exports,
        "export_lines": export_lines,
        "applied": applied,
        "warnings": warnings,
        "env_file": env_file_path,
        "notes": [
            "This profile is privacy-first and local-first by default.",
            "No print content or model payloads are sent to Kiln's licensing service.",
            "If you use cloud LLMs, set KILN_OPENROUTER_KEY explicitly.",
        ],
    }

    if errors:
        if json_mode:
            click.echo(
                format_response(
                    "error",
                    data=payload,
                    error={"code": "LOCAL_FIRST_ERROR", "message": "; ".join(errors)},
                    json_mode=True,
                )
            )
        else:
            click.echo(format_error("; ".join(errors), code="LOCAL_FIRST_ERROR", json_mode=False))
        sys.exit(1)

    if json_mode:
        click.echo(format_response("success", data=payload, json_mode=True))
        return

    click.echo("Local-first profile prepared.")
    if apply:
        if "cloud_sync_disabled" in applied:
            click.echo("  - Cloud sync config disabled in local settings.")
        elif "cloud_sync_already_disabled" in applied:
            click.echo("  - Cloud sync config was already disabled.")
        if "cloud_sync_backup_saved" in applied:
            click.echo("  - Previous cloud sync config was backed up to key: cloud_sync_config_backup")
    if env_file_path:
        click.echo(f"  - Wrote env file: {env_file_path}")
        click.echo(f"    source {env_file_path}")
    else:
        click.echo("Recommended exports:")
        for line in export_lines:
            click.echo(f"  {line}")
    for warning in warnings:
        click.echo(f"Warning: {warning}")


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

    if str(cfg.get("type", "")).strip().lower() != "prusalink":
        click.echo(
            format_error(
                "Active printer is not Prusa Link. Set one with --printer or run: kiln auth --type prusalink ...",
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


@cli.command("doctor-creality")
@click.option("--host", default=None, help="Printer IP/hostname/URL to probe without loading saved config.")
@click.option("--api-key", default=None, help="Moonraker API key, if local auth is enabled.")
@click.option("--model", default=None, help="Printer model hint (e.g. k1_max) for capability guidance.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def doctor_creality(
    ctx: click.Context,
    host: str | None,
    api_key: str | None,
    model: str | None,
    json_mode: bool,
) -> None:
    """Run focused diagnostics for Creality Moonraker and CFS discovery."""
    import json as _json

    if host:
        cfg: dict[str, Any] = {
            "type": "creality",
            "host": host,
            "api_key": api_key,
            "printer_model": model,
        }
    else:
        try:
            cfg = load_printer_config(ctx.obj.get("printer"))
        except ValueError as exc:
            click.echo(format_error(str(exc), code="CONFIG_ERROR", json_mode=json_mode))
            sys.exit(1)
        except Exception as exc:
            click.echo(format_error(str(exc), code="CONFIG_ERROR", json_mode=json_mode))
            sys.exit(1)

        if api_key:
            cfg["api_key"] = api_key
        if model:
            cfg["printer_model"] = model

    if str(cfg.get("type", "")).strip().lower() != "creality":
        click.echo(
            format_error(
                "Active printer is not Creality. Set one with --printer, run: kiln auth --type creality ..., or pass --host.",
                code="WRONG_PRINTER_TYPE",
                json_mode=json_mode,
            )
        )
        sys.exit(1)

    result = _run_creality_diagnostics(cfg)
    if json_mode:
        click.echo(_json.dumps({"status": "success" if result.get("ok") else "error", "data": result}, indent=2))
    else:
        click.echo("Creality Moonraker diagnostics:")
        if result.get("user_message"):
            click.echo(str(result["user_message"]))
        for check in result.get("checks", []):
            if not isinstance(check, dict):
                continue
            icon = "✓" if check.get("ok") else ("⚠" if check.get("warn") else "✗")
            click.echo(f"  {icon} {check.get('name')}: {check.get('detail')}")
        if result.get("resolved_url"):
            click.echo(f"\nResolved Moonraker URL: {result['resolved_url']}")
        if result.get("browser_test_url"):
            click.echo(f"Browser test: {result['browser_test_url']}")
        if result.get("likely_cause"):
            click.echo(f"Likely cause: {result['likely_cause']}")

        model_hint = model or cfg.get("printer_model")
        mapped_model = _map_printer_hint_to_profile_id(str(model_hint or ""))
        if mapped_model in {"k1", "k1_max", "k1c", "k1_se"}:
            click.echo(
                "\nK1-series CFS-C note: stock machines are single-material. "
                "Use CFS-C only after the retrofit hardware, firmware update, "
                "and compatible hotend path are installed."
            )

        cfs = result.get("cfs_status")
        if isinstance(cfs, dict):
            click.echo("\nCFS discovery:")
            click.echo(f"  detected: {bool(cfs.get('detected'))}")
            click.echo(f"  candidate objects: {', '.join(cfs.get('candidate_objects') or []) or 'none'}")
            click.echo(f"  candidate commands: {', '.join(cfs.get('candidate_commands') or []) or 'none'}")
            click.echo(f"  slots: {cfs.get('slot_count') or 'unknown'}")
            click.echo("  active slot control: hardware-unverified/read-only")

        checklist = result.get("connection_checklist") or []
        if checklist and not result.get("ok"):
            click.echo("\nConnection checklist:")
            for item in checklist:
                click.echo(f"  - {item}")

        if result.get("firmware_lockdown_possible"):
            click.echo(
                "\nFirmware/local access note: the printer answered, but not with Moonraker. "
                "Stock firmware on that version may have local Moonraker disabled, locked down, "
                "or exposed on a different port."
            )

        next_steps = result.get("next_steps") or []
        if next_steps:
            click.echo("\nNext steps:")
            for step in next_steps:
                click.echo(f"  - {step}")

    if not result.get("ok"):
        sys.exit(1)


# ---------------------------------------------------------------------------
# Deep network diagnostics for kiln doctor --deep
# ---------------------------------------------------------------------------

# Known router brands that commonly enable device isolation by default.
_ROUTER_ISOLATION_GUIDES: dict[str, str] = {
    "spectrum": (
        "Spectrum: Open the My Spectrum app > Services > Advanced Settings > "
        "turn off 'Security Shield'. Or visit http://192.168.1.1 > "
        "Advanced > Security > disable device isolation."
    ),
    "xfinity": (
        "Xfinity: Open the Xfinity app > More > WiFi > Advanced Security > "
        "turn it OFF.  This blocks device-to-device LAN traffic."
    ),
    "eero": (
        "Eero: Open the eero app > Settings > Network Settings > Advanced > "
        "disable 'Thread' and check that 'Local Network Access' is enabled."
    ),
    "google_wifi": (
        "Google Wifi / Nest Wifi: Open the Google Home app > Wi-Fi > "
        "Settings > Advanced Networking > ensure device isolation / "
        "AP isolation is OFF."
    ),
    "att": (
        "AT&T Gateway: Visit http://192.168.1.254 > Home Network > "
        "Subnets & DHCP > disable 'Public Subnet Only' and 'IP Passthrough'."
    ),
    "verizon": (
        "Verizon Fios: Visit http://192.168.1.1 > Advanced > Network "
        "Settings > ensure 'Client Isolation' is OFF."
    ),
    "tp_link": (
        "TP-Link: Visit the router admin page > Advanced > Wireless > "
        "Wireless Settings > uncheck 'Enable AP Isolation'."
    ),
    "netgear": (
        "Netgear: Visit http://routerlogin.net > Advanced > Wireless > "
        "uncheck 'Enable Wireless Isolation'."
    ),
}


def _deep_network_diagnostics(host: str, printer_cfg: dict) -> list[dict]:
    """Run thorough network diagnostics when the printer is unreachable.

    Tests ICMP reachability, TCP port connectivity, and provides
    actionable guidance based on failure patterns.

    :param host: Printer IP address or hostname.
    :param printer_cfg: Full printer config dict.
    :returns: List of check dicts for the doctor output.
    """
    import socket
    import subprocess

    checks: list[dict] = []
    printer_type = str(printer_cfg.get("type", "")).strip().lower()

    # --- 1. ICMP Ping ---
    ping_ok = False
    try:
        result = subprocess.run(
            ["ping", "-c", "2", "-W", "2", host],
            capture_output=True,
            text=True,
            timeout=10,
        )
        ping_ok = result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        pass

    if ping_ok:
        checks.append({"name": "ping", "ok": True, "detail": f"{host} responds to ping"})
    else:
        checks.append(
            {
                "name": "ping",
                "ok": False,
                "detail": (
                    f"{host} does not respond to ping. "
                    "Check: is the printer powered on and connected to WiFi? "
                    "Verify the IP address in printer Settings > Network."
                ),
            }
        )
        # No point continuing if we can't even ping.
        return checks

    # --- 2. TCP port scan ---
    # Ports depend on printer type.
    port_map: dict[str, list[tuple[int, str]]] = {
        "bambu": [
            (8883, "MQTTS (control/status)"),
            (990, "FTPS (file upload)"),
        ],
        "octoprint": [
            (80, "HTTP"),
            (443, "HTTPS"),
        ],
        "moonraker": [
            (7125, "Moonraker API"),
            (80, "HTTP"),
        ],
        "duet": [
            (80, "HTTP"),
            (443, "HTTPS"),
        ],
        "creality": [
            (7125, "Moonraker API"),
            (80, "HTTP"),
            (4408, "Moonraker/Fluidd alternate"),
        ],
        "prusalink": [
            (80, "HTTP"),
            (443, "HTTPS"),
        ],
        "elegoo": [
            (3000, "SDCP"),
        ],
    }
    ports_to_check = port_map.get(printer_type, [(80, "HTTP"), (443, "HTTPS")])

    any_port_open = False
    all_no_route = True
    for port, label in ports_to_check:
        try:
            sock = socket.create_connection((host, port), timeout=5)
            sock.close()
            checks.append({"name": f"port_{port}", "ok": True, "detail": f"Port {port} ({label}): open"})
            any_port_open = True
            all_no_route = False
        except TimeoutError:
            checks.append(
                {"name": f"port_{port}", "ok": False, "detail": f"Port {port} ({label}): timeout (filtered)"}
            )
            all_no_route = False
        except ConnectionRefusedError:
            checks.append(
                {
                    "name": f"port_{port}",
                    "ok": False,
                    "detail": f"Port {port} ({label}): refused (service not running)",
                }
            )
            all_no_route = False
        except OSError as exc:
            err_str = str(exc)
            checks.append({"name": f"port_{port}", "ok": False, "detail": f"Port {port} ({label}): {err_str}"})

    # --- 3. Diagnosis based on failure pattern ---
    if any_port_open:
        # Some ports work — likely an auth or service issue, not network.
        return checks

    if ping_ok and not any_port_open:
        # Classic pattern: ping works, all TCP blocked.
        if all_no_route:
            diagnosis = (
                "Ping works but all TCP ports return 'No route to host'. "
                "This almost always means your router's firewall or device "
                "isolation is blocking device-to-device traffic."
            )
        else:
            diagnosis = (
                "Ping works but no service ports are reachable. "
                "This may be a router firewall, device isolation, or "
                "the printer's LAN services are not running."
            )

        checks.append({"name": "diagnosis", "ok": False, "detail": diagnosis})

        # Printer-specific advice
        if printer_type == "bambu":
            checks.append(
                {
                    "name": "bambu_lan_check",
                    "ok": False,
                    "detail": (
                        "Bambu printers require LAN Only Mode enabled on the "
                        "touchscreen (Settings > Network). After enabling, "
                        "wait 30-60 seconds for MQTT to start. If it still "
                        "fails, power cycle the printer with LAN mode OFF, "
                        "let it fully boot, then enable LAN Only Mode."
                    ),
                }
            )

        # Router-specific guides
        checks.append(
            {
                "name": "router_guide",
                "ok": False,
                "detail": (
                    "Common routers that block device-to-device traffic by default:"
                ),
            }
        )
        for brand, guide in _ROUTER_ISOLATION_GUIDES.items():
            checks.append(
                {
                    "name": f"router_{brand}",
                    "ok": True,
                    "warn": True,
                    "detail": guide,
                }
            )

        # General tips
        checks.append(
            {
                "name": "general_tips",
                "ok": True,
                "warn": True,
                "detail": (
                    "General fixes: (1) Disable AP/client isolation in router settings. "
                    "(2) Ensure printer and computer are on the same subnet/VLAN. "
                    "(3) Try connecting the printer via Ethernet if available. "
                    "(4) Temporarily disable router firewall to test."
                ),
            }
        )

    return checks


@cli.command()
@click.option(
    "--open-sessions",
    type=int,
    default=None,
    help=(
        "How many agent sessions you actually have open. Keeps that many "
        "most-recently-started servers and closes the rest."
    ),
)
@click.option("--yes", "assume_yes", is_flag=True, help="Skip the confirmation prompt.")
@click.option(
    "--force",
    is_flag=True,
    help="Proceed even while a printer has a job in flight (monitoring may stop).",
)
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def trim(open_sessions: int | None, assume_yes: bool, force: bool, json_mode: bool) -> None:
    """Close leftover Kiln servers from agent sessions you've closed.

    Every agent session spawns its own background server, and client
    apps don't reliably close them when a session ends.  This closes
    the leftovers.  It never closes this process, and it refuses while
    a printer has a job in flight — closing a server can't stop a
    print, but it can stop the monitoring of one.
    """
    import json as _json

    from kiln.serve_siblings import perform_trim, plan_trim, printing_now

    plan = plan_trim(open_sessions=open_sessions)
    if plan["scanned"] is None:
        click.echo("Cannot read the process table on this platform.", err=True)
        raise SystemExit(1)

    if not plan["candidates"]:
        if json_mode:
            click.echo(_json.dumps({"trimmed": [], "plan": plan}))
        else:
            click.echo(
                f"All {plan['scanned']} running Kiln server(s) look current — "
                f"nothing to clean up."
            )
        return

    printing = printing_now()
    if printing["active"] and not force:
        msg = (
            f"Not closing anything — a print is in progress: "
            f"{', '.join(printing['active'])}. Closing a server can't stop the "
            f"print itself, but it can stop Kiln monitoring it. Run again once "
            f"it finishes (or --force if you're sure)."
        )
        if json_mode:
            click.echo(_json.dumps({"blocked": True, "printing": printing, "plan": plan}))
        else:
            click.echo(msg, err=True)
        raise SystemExit(1)

    if not json_mode:
        click.echo(f"{len(plan['candidates'])} leftover Kiln server(s) can be closed:")
        for cand in plan["candidates"]:
            click.echo(f"  · {cand['reason']}")
        click.echo(f"Keeping {len(plan['kept'])} (including this session's own).")
        if printing["unknown"]:
            click.echo(
                f"  note: couldn't check {len(printing['unknown'])} printer(s) — "
                f"{'; '.join(printing['unknown'][:3])}"
            )
    if not assume_yes and not click.confirm("Close them?", default=True):
        click.echo("Nothing closed.")
        return

    result = perform_trim(open_sessions=open_sessions, force=force)
    if json_mode:
        click.echo(_json.dumps(result))
    else:
        click.echo(
            f"Closed {len(result['trimmed'])} leftover server(s); "
            f"{len(result['kept'])} kept."
        )
        if result["failed"]:
            click.echo(f"{len(result['failed'])} could not be closed:", err=True)
            for item in result["failed"]:
                click.echo(f"  PID {item['pid']}: {item['error']}", err=True)


@cli.command()
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.option("--deep", is_flag=True, help="Run deep network diagnostics when printer is unreachable.")
@click.pass_context
def verify(ctx: click.Context, json_mode: bool, deep: bool) -> None:
    """Run pre-flight system checks to verify Kiln is ready to use."""
    import json as _json
    import platform

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
                "detail": (
                    "not found (install prusa-slicer, orcaslicer or "
                    "bambustudio, or set KILN_SLICER_PATH)"
                ),
            }
        )
    except OSError as exc:
        checks.append({"name": "slicer", "ok": False, "detail": str(exc)})
    except Exception as exc:
        checks.append({"name": "slicer", "ok": False, "detail": str(exc)})

    # 4. Serve-process pile-up — servers accumulated from closed MCP
    # sessions.  Shared detector (kiln.serve_siblings), same numbers
    # the health_check / kiln_health / get_started tools report.
    try:
        from kiln.serve_siblings import check_serve_siblings

        _siblings = check_serve_siblings()
        if _siblings["count"] is None:
            checks.append(
                {
                    "name": "serve_processes",
                    "ok": True,
                    "detail": "process scan unavailable on this platform",
                }
            )
        else:
            checks.append(
                {
                    "name": "serve_processes",
                    "ok": _siblings["warning"] is None,
                    "detail": (
                        _siblings["warning"]
                        or f"{_siblings['count']} running (normal: one per open MCP session)"
                    ),
                }
            )
    except Exception as exc:
        checks.append({"name": "serve_processes", "ok": True, "detail": f"check skipped: {exc}"})

    # 4b. Printer connection slots — who on this machine is actually holding
    # one.  Separate from the process count above because they answer
    # different questions: a server that never touched the printer holds no
    # slot, and a Bambu locks out the next caller once its few slots are
    # taken.  Without this the symptom (a timeout) is indistinguishable from
    # a powered-off printer, and the user power-cycles a healthy machine.
    try:
        from kiln.serve_siblings import printer_slot_report

        _slots = printer_slot_report()
        if not _slots["checked"]:
            pass  # no slot-rationing printer configured, or no way to scan
        elif _slots["warning"]:
            _pids = ", ".join(
                str(p) for r in _slots["hosts"] for p in r["pids"]
            )
            checks.append(
                {
                    "name": "printer_connections",
                    "ok": False,
                    "detail": f"{_slots['warning']} (PIDs {_pids})",
                }
            )
        else:
            _detail = ", ".join(
                f"{r['total']} local connection(s) to {r['host']}"
                for r in _slots["hosts"]
            )
            checks.append(
                {"name": "printer_connections", "ok": True, "detail": _detail}
            )
    except Exception as exc:
        checks.append(
            {"name": "printer_connections", "ok": True, "detail": f"check skipped: {exc}"}
        )

    # 5. Config / printers configured
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

    # 6. Printer reachable (use adapter with auth, not raw HTTP)
    verify_adapter = None
    if printer_cfg:
        host = printer_cfg.get("host", "")
        if host:
            try:
                adapter = _make_adapter(printer_cfg)
                verify_adapter = adapter
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

        # 6b. Printer identity — does every source agree what this machine is?
        #
        # A disagreement is the one printer-model problem a user cannot see
        # for themselves, and it can be a SAFETY problem rather than a
        # cosmetic one: the config-declared model is what the temperature
        # ceilings and bed-fit checks key off, so declaring an X1C on a
        # machine that is really an A1 applies all-metal limits to a
        # PTFE-lined hotend.  Fails the run (not a warning) — the same
        # class of wrong-limits-silently-applied that got printer-model
        # inference scrapped in the first place.
        if verify_adapter is not None:
            try:
                from kiln.community_autofire import (
                    detect_identity_conflict,
                    resolve_adapter_model,
                )

                conflict = detect_identity_conflict(verify_adapter)
                if conflict is not None:
                    checks.append(
                        {
                            "name": "printer_model",
                            "ok": False,
                            "detail": conflict.describe(),
                            "claims": dict(conflict.claims),
                        }
                    )
                else:
                    model = resolve_adapter_model(verify_adapter)
                    if model:
                        checks.append(
                            {"name": "printer_model", "ok": True, "detail": model}
                        )
                    else:
                        checks.append(
                            {
                                "name": "printer_model",
                                "ok": True,
                                "warn": True,
                                "detail": (
                                    "not set and not self-reported — model-specific "
                                    "checks (temperature limits, bed fit) fall back to "
                                    "defaults. Add printer_model to ~/.kiln/config.yaml."
                                ),
                            }
                        )
            except Exception as exc:
                logger.debug("Printer identity check failed: %s", exc)
                checks.append(
                    {
                        "name": "printer_model",
                        "ok": True,
                        "detail": f"check skipped: {exc}",
                    }
                )

        # Prusa-specific diagnostics for first-run clarity.
        if str(printer_cfg.get("type", "")).strip().lower() == "prusalink":
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
        elif str(printer_cfg.get("type", "")).strip().lower() == "creality":
            try:
                creality_diag = _run_creality_diagnostics(printer_cfg)
                detail = str(creality_diag.get("resolved_url") or printer_cfg.get("host", ""))
                if creality_diag.get("klippy_state"):
                    detail += f" (klippy_state={creality_diag.get('klippy_state')})"
                checks.append(
                    {
                        "name": "creality_moonraker",
                        "ok": bool(creality_diag.get("ok")),
                        "detail": detail,
                    }
                )
                cfs = creality_diag.get("cfs_status")
                if isinstance(cfs, dict):
                    checks.append(
                        {
                            "name": "creality_cfs",
                            "ok": True,
                            "warn": bool(cfs.get("hardware_unverified", True)),
                            "detail": (
                                f"detected={bool(cfs.get('detected'))}; "
                                "active slot control hardware-unverified"
                            ),
                        }
                    )
            except Exception as exc:
                logger.debug("Creality verify diagnostics failed: %s", exc)
                checks.append(
                    {
                        "name": "creality_moonraker",
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

    # 5b. Deep network diagnostics (only when --deep)
    if deep and printer_cfg:
        host = printer_cfg.get("host", "")
        if host:
            checks.extend(_deep_network_diagnostics(host, printer_cfg))
    elif deep and not printer_cfg:
        checks.append(
            {
                "name": "deep_diag",
                "ok": False,
                "detail": "skipped (no printer configured — run kiln auth first)",
            }
        )

    # 7. OpenSCAD — REQUIRED for local OpenSCAD-native design (Kiln's default
    #    "make" path), and it must be the development snapshot: the old stable
    #    build silently breaks SVG/text booleans and lacks the Manifold backend.
    #    Report honestly and version-check it (never label it "optional").
    openscad_path = shutil.which("openscad")
    if not openscad_path and sys.platform == "darwin":
        _mac_scad = "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD"
        if os.path.isfile(_mac_scad) and os.access(_mac_scad, os.X_OK):
            openscad_path = _mac_scad
    if openscad_path:
        try:
            from kiln.emboss_generator import (
                _OPENSCAD_MIN_VERSION_YEAR,
                _detect_openscad_version,
                _openscad_version_year,
            )

            _scad_ver = _detect_openscad_version(openscad_path)
            _scad_year = _openscad_version_year(_scad_ver)
        except Exception:
            _scad_ver, _scad_year, _OPENSCAD_MIN_VERSION_YEAR = "", 0, 2024
        if _scad_year and _scad_year >= _OPENSCAD_MIN_VERSION_YEAR:
            checks.append({
                "name": "openscad",
                "ok": True,
                "detail": f"{openscad_path} ({_scad_ver}, current)",
            })
        elif _scad_year:
            checks.append({
                "name": "openscad",
                "ok": True,
                "warn": True,
                "detail": (
                    f"{openscad_path} ({_scad_ver}) is OUTDATED — Kiln needs the "
                    f"{_OPENSCAD_MIN_VERSION_YEAR}+ development snapshot (the old "
                    "build silently breaks SVG/text). Upgrade: kiln install-openscad"
                ),
            })
        else:
            checks.append({
                "name": "openscad",
                "ok": True,
                "warn": True,
                "detail": (
                    f"{openscad_path} (version unverified — make sure it's the "
                    f"{_OPENSCAD_MIN_VERSION_YEAR}+ development snapshot)"
                ),
            })
    else:
        checks.append({
            "name": "openscad",
            "ok": True,
            "warn": True,
            "detail": (
                "not found — REQUIRED for designing locally (Kiln's default make "
                "path). Install the development snapshot: kiln install-openscad"
            ),
        })

    # 6b. STEP converter — OPTIONAL, unlike OpenSCAD above.  Deliberately
    #     never warns when absent: most users never open a STEP file, and a
    #     doctor that cries about something you don't need teaches people to
    #     ignore it.  It reports either way so the capability is DISCOVERABLE
    #     — a user who has CAD files learns the one command here instead of
    #     finding out only when an import fails.
    try:
        from kiln.step_import import INSTALL_COMMAND, check_step_support

        _step_info = check_step_support()
        if _step_info["any_available"]:
            _step_backend = next(
                (n for n, b in sorted(
                    _step_info["backends"].items(),
                    key=lambda kv: kv[1].get("priority", 99),
                ) if b.get("available")),
                "unknown",
            )
            checks.append({
                "name": "step-import",
                "ok": True,
                "detail": f"STEP/STP CAD files supported (via {_step_backend})",
            })
        else:
            checks.append({
                "name": "step-import",
                "ok": True,
                "detail": (
                    "not installed — optional, only needed to open STEP/STP CAD "
                    f"files. Add it any time: {INSTALL_COMMAND}"
                ),
            })
    except Exception as _step_exc:  # noqa: BLE001 — never break doctor
        # Report the broken probe rather than omitting the line.  Silently
        # dropping a check from the one command whose job is revealing
        # problems is the worst possible failure mode for it.
        checks.append({
            "name": "step-import",
            "ok": True,
            "warn": True,
            "detail": f"could not check STEP support ({type(_step_exc).__name__})",
        })

    # 8. SQLite opens (not merely writable — see _database_check)
    checks.append(_database_check())

    # 8b. Did the MCP server last fail to start?  A startup crash happens
    # before any tool exists to report it, so the server leaves a
    # breadcrumb behind and this is where a stuck user finds it.  Absent
    # on a machine that has never had one — silence beats reassurance.
    _startup = _last_startup_failure_check()
    if _startup is not None:
        checks.append(_startup)

    # 9. WSL 2 detection
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
            label = c["name"].replace("_", " ").title()
            if c.get("warn"):
                click.echo(f"  ⚠ {label}: {c['detail']}")
            elif c["ok"]:
                click.echo(f"  ✓ {label}: {c['detail']}")
            else:
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
                click.echo("\n  Already subscribed?  Run `kiln signin` to sync this machine.")
                click.echo("  New?                 https://kiln3d.com/pricing")
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
# Pro/Enterprise CLI plugins (loaded from kiln-pro when installed)
# ---------------------------------------------------------------------------

# Pro-CLI registration moved to end-of-module (search: "register_pro_cli(cli)")
# so existing @cli.group decorators (e.g. versions, ingest) are fully
# registered before kiln-pro tries to graft subcommands onto them.  Running
# here at line 9031 used to silently skip pro-tier subcommand injection into
# the public `versions` group because the group hadn't been decorated yet.


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# step
# ---------------------------------------------------------------------------


@cli.group("step")
def step_group() -> None:
    """Import and inspect STEP/STP CAD files."""
    pass


@step_group.command("import")
@click.argument("file_path")
@click.option("--output-dir", default=None, help="Output directory (default: same as input).")
@click.option("--merge/--no-merge", "merge_bodies", default=True, help="Merge multi-body STEP into single STL.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def step_import(file_path: str, output_dir: str | None, merge_bodies: bool, json_mode: bool) -> None:
    """Convert a STEP/STP file to STL for printing."""
    try:
        from kiln.step_import import convert_step_to_stl

        result = convert_step_to_stl(file_path, output_dir=output_dir, merge_bodies=merge_bodies)
        if json_mode:
            click.echo(format_response("success", data=result.to_dict(), json_mode=True))
        else:
            click.echo(f"✓ Converted {os.path.basename(file_path)} → {len(result.output_paths)} file(s)")
            for p in result.output_paths:
                click.echo(f"  {p}")
            click.echo(f"  Bodies: {result.body_count} | Time: {result.conversion_time_s:.1f}s")
            if result.warnings:
                for w in result.warnings:
                    click.echo(f"  ⚠ {w}")
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@step_group.command("info")
@click.argument("file_path")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def step_info(file_path: str, json_mode: bool) -> None:
    """Show metadata from a STEP file without converting it."""
    try:
        from kiln.step_import import get_step_metadata

        meta = get_step_metadata(file_path)
        if json_mode:
            click.echo(format_response("success", data=meta, json_mode=True))
        else:
            click.echo(f"STEP file: {os.path.basename(file_path)}")
            for key, val in meta.items():
                click.echo(f"  {key}: {val}")
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@step_group.command("check")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def step_check(json_mode: bool) -> None:
    """Check which STEP conversion backends are available."""
    try:
        from kiln.step_import import check_step_support

        support = check_step_support()
        if json_mode:
            click.echo(format_response("success", data=support, json_mode=True))
        else:
            click.echo("STEP import backends:")
            for backend, info in support.get("backends", {}).items():
                # info is the backend's dict, which is always truthy — read
                # the field.  Testing the dict itself reported every backend
                # as available on every machine, including the ones that had
                # just been looked for and not found.
                status = "✓ available" if info.get("available") else "✗ not found"
                where = info.get("executable")
                click.echo(f"  {backend}: {status}" + (f"  ({where})" if where else ""))
            if support.get("any_available"):
                click.echo("\n✓ STEP import is ready.")
            else:
                click.echo("\n✗ No backends found. Install FreeCAD, Gmsh, or CadQuery.")
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------


@cli.group("versions")
def versions_group() -> None:
    """Manage design version history."""
    pass


@versions_group.command("save")
@click.argument("design_id")
@click.argument("file_path")
@click.option("--notes", default="", help="Notes about this version.")
@click.option("--prompt", "design_prompt", default="", help="Original design prompt.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def versions_save(design_id: str, file_path: str, notes: str, design_prompt: str, json_mode: bool) -> None:
    """Save a new version of a design from a file."""
    try:
        from kiln.design_versions import DesignVersionStore

        source = Path(file_path).read_text()
        store = DesignVersionStore()
        try:
            version = store.save_version(design_id, source, notes=notes, prompt=design_prompt)
            if json_mode:
                click.echo(format_response("success", data=version.to_dict(), json_mode=True))
            else:
                click.echo(f"✓ Saved {design_id} v{version.version_number} ({version.version_id[:8]})")
        finally:
            store.close()
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@versions_group.command("list")
@click.argument("design_id")
@click.option("--limit", default=20, help="Max versions to show.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def versions_list(design_id: str, limit: int, json_mode: bool) -> None:
    """List all versions of a design."""
    try:
        from kiln.design_versions import DesignVersionStore

        store = DesignVersionStore()
        try:
            versions = store.list_versions(design_id, limit=limit)
            if json_mode:
                click.echo(format_response("success", data=[v.to_dict() for v in versions], json_mode=True))
            else:
                if not versions:
                    click.echo(f"No versions found for '{design_id}'.")
                    return
                click.echo(f"Versions of '{design_id}' ({len(versions)} total):")
                for v in versions:
                    ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(v.created_at))
                    notes = f" — {v.notes}" if v.notes else ""
                    click.echo(f"  v{v.version_number}  {v.version_id[:8]}  {ts}{notes}")
        finally:
            store.close()
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@versions_group.command("diff")
@click.argument("version_a")
@click.argument("version_b")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def versions_diff(version_a: str, version_b: str, json_mode: bool) -> None:
    """Show diff between two design versions."""
    try:
        from kiln.design_versions import DesignVersionStore

        store = DesignVersionStore()
        try:
            diff = store.diff_versions(version_a, version_b)
            if json_mode:
                click.echo(format_response("success", data={"diff": diff}, json_mode=True))
            else:
                if diff:
                    click.echo(diff)
                else:
                    click.echo("No differences between versions.")
        finally:
            store.close()
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


@versions_group.command("rollback")
@click.argument("design_id")
@click.argument("version_id")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def versions_rollback(design_id: str, version_id: str, json_mode: bool) -> None:
    """Rollback a design to a previous version (creates a new version from it)."""
    try:
        from kiln.design_versions import DesignVersionStore

        store = DesignVersionStore()
        try:
            new_version = store.rollback(design_id, version_id)
            if json_mode:
                click.echo(format_response("success", data=new_version.to_dict(), json_mode=True))
            else:
                click.echo(f"✓ Rolled back '{design_id}' to {version_id[:8]} → new v{new_version.version_number}")
        finally:
            store.close()
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# goals — saved design sessions (kiln-pro DesignBrief artifacts)
# ---------------------------------------------------------------------------


@cli.group("goals")
def goals_group() -> None:
    """Manage saved design goals captured by `design_session`."""
    pass


@goals_group.command("list")
@click.option(
    "--filter",
    "filter_status",
    type=click.Choice([
        "all", "needs_questions", "ready_to_generate",
        "matches_what_you_asked_for",
    ]),
    default="all",
    help="Filter by goal status (default: all).",
)
@click.option("--limit", default=50, help="Max sessions to show (default 50).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def goals_list(filter_status: str, limit: int, json_mode: bool) -> None:
    """List your saved design goals.

    A saved goal is what `design_session` writes when you tell Kiln
    what you're making and answer the duty / environment / materials /
    safety questions. This is the CLI equivalent of the web /goals
    inbox: shows what you've committed and which prints have honored
    each goal.

    Requires Kiln Pro.
    """
    try:
        from kiln_pro.design_brief import list_briefs
    except ImportError:
        click.echo(
            format_error(
                "Saved goals are a Kiln Pro feature. Already subscribed? Run "
                "`kiln signin` on this machine. New? https://kiln3d.com/pricing",
                code="REQUIRES_KILN_PRO",
                json_mode=json_mode,
            )
        )
        sys.exit(1)

    # Map the friendly CLI filter to the substrate enum.
    substrate_filter: str | None = {
        "all": None,
        "needs_questions": "needs_clarification",
        "ready_to_generate": "ready",
        "matches_what_you_asked_for": "honored",
    }[filter_status]

    try:
        briefs = list_briefs(filter_status=substrate_filter)[:max(1, min(limit, 1000))]
        if json_mode:
            click.echo(format_response(
                "success",
                data={
                    "sessions": [b.to_dict() for b in briefs],
                    "count": len(briefs),
                    "filter_status": filter_status,
                },
                json_mode=True,
            ))
            return
        if not briefs:
            click.echo(
                "No saved goals yet."
                if filter_status == "all"
                else f"No goals with status '{filter_status}'."
            )
            click.echo(
                "  Start one by telling the agent: "
                "'design_session(verb=\"start\", idea=\"...\")'"
            )
            return
        click.echo(f"Saved goals ({len(briefs)} shown):")
        for b in briefs:
            short = b.brief_id[:8]
            status_label = {
                "needs_clarification": "needs questions",
                "ready":               "ready",
                "honored":             "honored",
            }.get(b.status, b.status)
            env = ", ".join(b.environment) if b.environment else "-"
            click.echo(
                f"  {short}  [{status_label}]  {b.idea or '<no idea>'}"
            )
            click.echo(
                f"           duty: {b.duty or '-'} | environment: {env}"
            )
    except Exception as exc:
        click.echo(format_error(str(exc), json_mode=json_mode))
        sys.exit(1)


# ---------------------------------------------------------------------------
# ams
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def ams(ctx: click.Context, json_mode: bool) -> None:
    """Show AMS filament status (trays, colors, material types)."""
    try:
        adapter = _get_adapter_from_ctx(ctx)
        if not hasattr(adapter, "get_ams_status"):
            click.echo(
                format_error(
                    "AMS status is only available on Bambu Lab printers with AMS.",
                    code="UNSUPPORTED",
                    json_mode=json_mode,
                )
            )
            sys.exit(1)
        result = adapter.get_ams_status()
        if json_mode:
            click.echo(format_response("success", data=result, json_mode=True))
        else:
            ams_units = result.get("units") or result.get("ams", [])
            if not ams_units:
                click.echo("No AMS units detected.")
            else:
                untracked = False
                for unit in ams_units:
                    click.echo(f"AMS #{unit.get('unit_id', unit.get('id', '?'))}:")
                    for tray in unit.get("trays", unit.get("tray", [])):
                        slot = tray.get("slot", tray.get("id", "?"))
                        raw_color = tray.get("tray_color", tray.get("color", ""))
                        color = f"#{raw_color[:6]}" if isinstance(raw_color, str) and len(raw_color) >= 6 else (raw_color or "unknown")
                        material = tray.get("tray_type", tray.get("type", "unknown")) or "unknown"
                        # `remaining_known` (set by the adapter) is False when
                        # a spool has no RFID tag — Bambu's AMS has no scale, so
                        # `remain` is only meaningful for tagged spools.
                        remain = tray.get("remain", tray.get("remaining"))
                        if tray.get("remaining_known") and isinstance(remain, (int, float)):
                            level = f"{remain}% remaining"
                        else:
                            level = "remaining not reported"
                            untracked = True
                        click.echo(f"  Slot {slot}: {material} ({color}) — {level}")
                if untracked:
                    click.echo(
                        "  Tip: remaining % is only known for spools with a "
                        "Bambu RFID tag."
                    )
            tray_now = result.get("tray_now")
            if tray_now and tray_now != "255":
                click.echo(f"Active tray: {tray_now}")
            elif ams_units:
                selected = result.get("tray_pre") or result.get("tray_tar")
                if selected not in (None, "", "255"):
                    click.echo(f"Selected AMS tray: {selected}")
                else:
                    click.echo("Active tray not reported; AMS trays are loaded.")
    except click.ClickException:
        raise
    except PrinterError as exc:
        click.echo(
            format_error(
                f"Failed to get AMS status: {exc}. Verify the printer is online.",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to get AMS status: {exc}",
                json_mode=json_mode,
            )
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# speed
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("profile", required=False)
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def speed(ctx: click.Context, profile: str | None, json_mode: bool) -> None:
    """Get or set printer speed profile (silent/standard/sport/ludicrous)."""
    try:
        adapter = _get_adapter_from_ctx(ctx)

        if profile is not None:
            # --- set speed profile ---
            if not hasattr(adapter, "set_speed_profile"):
                click.echo(
                    format_error(
                        "Speed profile control is only available on Bambu Lab printers.",
                        code="UNSUPPORTED",
                        json_mode=json_mode,
                    )
                )
                sys.exit(1)
            ok = adapter.set_speed_profile(profile)
            data = {
                "action": "set_speed_profile",
                "profile": profile.strip().lower(),
                "accepted": ok,
            }
            if json_mode:
                click.echo(format_response("success", data=data, json_mode=True))
            else:
                click.echo(f"Speed profile set to '{profile.strip().lower()}'.")
        else:
            # --- get speed profile ---
            if not hasattr(adapter, "get_speed_profile"):
                click.echo(
                    format_error(
                        "Speed profile is only available on Bambu Lab printers.",
                        code="UNSUPPORTED",
                        json_mode=json_mode,
                    )
                )
                sys.exit(1)
            result = adapter.get_speed_profile()
            if json_mode:
                click.echo(format_response("success", data=result, json_mode=True))
            else:
                name = result.get("name", "unknown")
                level = result.get("level", "?")
                magnitude = result.get("speed_magnitude", "?")
                click.echo(f"Speed profile: {name} (level {level}, {magnitude}%)")
    except click.ClickException:
        raise
    except PrinterError as exc:
        click.echo(
            format_error(
                f"Failed to manage speed profile: {exc}. Verify the printer is online.",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to manage speed profile: {exc}",
                json_mode=json_mode,
            )
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
@click.pass_context
def health(ctx: click.Context, json_mode: bool) -> None:
    """Check printer and system health status."""
    try:
        checks: dict[str, Any] = {}

        # Printer connectivity
        try:
            adapter = _get_adapter_from_ctx(ctx)
            state = adapter.get_state()
            checks["printer_online"] = True
            checks["printer_state"] = state.status.value if hasattr(state.status, "value") else str(state.status)
        except (PrinterError, click.ClickException, Exception) as exc:
            checks["printer_online"] = False
            checks["printer_error"] = str(exc)

        # Slicer availability
        try:
            from kiln.slicer import find_slicer

            slicer_info = find_slicer()
            checks["slicer_available"] = True
            checks["slicer_name"] = slicer_info.name
            checks["slicer_version"] = slicer_info.version
        except Exception:
            checks["slicer_available"] = False

        # Kiln package version
        try:
            import kiln

            checks["kiln_version"] = kiln.__version__
        except Exception:
            checks["kiln_version"] = "unknown"

        # MCP client config drift.  ``kiln install-mcp`` writes the
        # client configs but nothing watches them afterwards — a
        # renamed venv or a hand-edit silently leaves a client
        # pointed at a binary that no longer exists, and the user's
        # first signal is the MCP host's "Server disconnected"
        # banner with no actionable detail.  Audit reports drift,
        # then the repair pass rewrites the ``command:`` field on
        # any broken ``kiln`` entry so the next launch of the MCP
        # host just works — making ``kiln health`` the single
        # entry-point users go to when something's wrong.
        _mcp_repairs: list[Any] = []
        try:
            from kiln.cli.mcp_config_audit import (
                audit_all_mcp_clients,
                to_json_payload,
            )
            from kiln.cli.mcp_config_repair import (
                repair_drifted_kiln_entries,
            )
            from kiln.cli.mcp_config_repair import (
                to_json_payload as _repair_to_json_payload,
            )

            _mcp_results = audit_all_mcp_clients()
            _mcp_repairs = repair_drifted_kiln_entries(_mcp_results)
            if _mcp_repairs:
                # Re-audit so the rendered status reflects the
                # post-repair state; otherwise the user sees a stale
                # "x command_missing" line right after the "Repaired"
                # line for the same entry.
                _mcp_results = audit_all_mcp_clients()
            checks["mcp_clients"] = to_json_payload(_mcp_results)
            checks["mcp_clients_ok"] = not any(
                r.has_drift or r.parse_error for r in _mcp_results
            )
            checks["mcp_clients_repaired"] = _repair_to_json_payload(_mcp_repairs)
        except Exception as exc:
            # Auditor must never break ``kiln health`` itself.  If it
            # raises (impossible by design, but defensive against
            # future regressions), surface the failure as a soft
            # warning rather than crashing the whole command.
            _mcp_results = []
            _mcp_repairs = []
            checks["mcp_clients"] = []
            checks["mcp_clients_ok"] = None
            checks["mcp_clients_repaired"] = []
            checks["mcp_clients_error"] = str(exc)

        healthy = checks.get("printer_online", False)
        checks["healthy"] = healthy

        if json_mode:
            click.echo(format_response("success", data=checks, json_mode=True))
        else:
            mark_ok = "+"
            mark_fail = "x"
            click.echo("System Health:")
            # Printer
            if checks.get("printer_online"):
                click.echo(f"  [{mark_ok}] Printer: online ({checks.get('printer_state', 'unknown')})")
            else:
                click.echo(f"  [{mark_fail}] Printer: offline ({checks.get('printer_error', 'unknown')})")
            # Slicer
            if checks.get("slicer_available"):
                sname = checks.get("slicer_name", "unknown")
                sver = checks.get("slicer_version") or "unknown"
                click.echo(f"  [{mark_ok}] Slicer: {sname} ({sver})")
            else:
                click.echo(f"  [{mark_fail}] Slicer: not found")
            # Kiln version
            click.echo(f"  [*] Kiln: v{checks.get('kiln_version', 'unknown')}")
            # Self-heal results, if any.  One line per rewrite.
            for _action in _mcp_repairs:
                click.echo(
                    f"Repaired {_action.client}: {_action.old} → {_action.new}",
                )
            # MCP client configs — one line per installed client.
            # Skip clients whose config doesn't exist (the user didn't
            # install Kiln there, no need to nag).  Show entry-level
            # drift with a copy-paste recovery command so the user
            # goes from "yellow banner with no clue what's wrong" to
            # "one line tells me exactly what to do" in one step.
            for _r in _mcp_results:
                if not _r.config_exists:
                    continue
                if _r.parse_error:
                    click.echo(
                        f"  [{mark_fail}] {_r.client}: config unparseable "
                        f"({_r.parse_error}). Run `kiln install-mcp` to regenerate.",
                    )
                    continue
                if not _r.entries:
                    # File exists but has no Kiln/MCP entry — nothing
                    # to verify; the user installed Kiln elsewhere or
                    # never ran ``kiln install-mcp`` on this client.
                    continue
                for _entry in _r.entries:
                    if _entry.is_ok:
                        click.echo(
                            f"  [{mark_ok}] {_r.client} → {_entry.name}: "
                            f"{_entry.command}",
                        )
                    else:
                        click.echo(
                            f"  [{mark_fail}] {_r.client} → {_entry.name}: "
                            f"{_entry.detail}. Run `kiln install-mcp` to regenerate.",
                        )
            if checks.get("mcp_clients_error"):
                click.echo(
                    f"  [{mark_fail}] MCP config audit failed: "
                    f"{checks['mcp_clients_error']}",
                )
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to check system health: {exc}",
                json_mode=json_mode,
            )
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# preview
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("file_path", type=click.Path(exists=True))
@click.option("--open/--no-open", default=True, help="Open preview image after rendering.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def preview(file_path: str, open: bool, json_mode: bool) -> None:
    """Render a visual preview of a 3D model (STL/3MF)."""
    try:
        from kiln.model_visualizer import visualize_model as _visualize

        result = _visualize(file_path)
        if not result.get("success"):
            click.echo(
                format_error(
                    result.get("error", "Preview rendering failed."),
                    json_mode=json_mode,
                )
            )
            sys.exit(1)

        views = result.get("views", [])
        image_paths = [v["path"] for v in views if v.get("path")]

        if json_mode:
            click.echo(
                format_response(
                    "success",
                    data={
                        "images": image_paths,
                        "output_dir": result.get("output_dir", ""),
                        "rendered": result.get("rendered", 0),
                        "failed": result.get("failed", 0),
                    },
                    json_mode=True,
                )
            )
        else:
            click.echo(f"Rendered {result.get('rendered', 0)} preview(s):")
            for p in image_paths:
                click.echo(f"  {p}")

        if open and image_paths:
            import subprocess

            subprocess.run(["open", image_paths[0]], check=False)  # noqa: S603, S607
    except Exception as exc:
        click.echo(
            format_error(
                f"Failed to preview '{file_path}': {exc}",
                json_mode=json_mode,
            )
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("file_path", type=click.Path(exists=True))
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def validate(file_path: str, json_mode: bool) -> None:
    """Check if a model is printable (geometry, overhangs, thin walls)."""
    try:
        from kiln.printability import analyze_printability as _analyze

        report = _analyze(file_path)
        if json_mode:
            click.echo(
                format_response("success", data={"report": report.to_dict()}, json_mode=True)
            )
        else:
            status = "PASS" if report.printable else "FAIL"
            click.echo(f"Printability: {status}  Score: {report.score}/100  Grade: {report.grade}")
            if report.recommendations:
                click.echo("Issues:")
                for rec in report.recommendations:
                    click.echo(f"  - {rec}")
    except ValueError as exc:
        click.echo(
            format_error(
                f"Validation failed for '{file_path}': {exc}",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            format_error(
                f"Validation failed for '{file_path}': {exc}",
                json_mode=json_mode,
            )
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# repair
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("file_path", type=click.Path(exists=True))
@click.option("--output", "-o", default=None, help="Output path (default: <name>_repaired.stl).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def repair(file_path: str, output: str | None, json_mode: bool) -> None:
    """Repair a mesh (fix holes, normals, degenerate faces)."""
    try:
        import os as _os

        from kiln.generation.validation import repair_stl

        if not output:
            base, ext = _os.path.splitext(file_path)
            output = f"{base}_repaired{ext or '.stl'}"

        result = repair_stl(file_path, output_path=output)
        if json_mode:
            click.echo(
                format_response("success", data=result, json_mode=True)
            )
        else:
            click.echo(f"Repaired: {result.get('path', output)}")
            click.echo(
                f"  Triangles: {result.get('original_triangles', '?')} -> "
                f"{result.get('cleaned_triangles', '?')}  "
                f"(removed {result.get('degenerate_removed', 0)} degenerate, "
                f"recomputed {result.get('normals_recomputed', 0)} normals)"
            )
    except ValueError as exc:
        click.echo(
            format_error(
                f"Repair failed for '{file_path}': {exc}",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(
            format_error(
                f"Repair failed for '{file_path}': {exc}",
                json_mode=json_mode,
            )
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# `kiln events` — local event log inspection.
#
# Free-tier friendly: the event log file is JSONL at
# ``~/.kiln/events.log`` (override with ``KILN_EVENT_LOG``).  Whether
# the writer is the kiln-pro VCS event bus, a CI hook script, or a
# user's own ``echo >> ~/.kiln/events.log`` cron job, the reader is
# pure stdlib JSON parsing — no kiln-pro dependency.  If the file
# doesn't exist, both commands print a friendly empty result instead
# of erroring; that's the expected state on a fresh install.
#
# Mirrors the MCP tools ``tail_event_log`` and ``summarize_events``
# (kiln-pro plugins/notification_tools.py); the MCP surface stays the
# canonical agent interface and these CLI commands are the
# operator-at-terminal version for ``tail -f``-style monitoring and
# quick "what happened in the last hour" diagnostics in scripts.
# ---------------------------------------------------------------------------

_EVENT_LOG_DEFAULT = os.path.expanduser("~/.kiln/events.log")


def _events_log_path() -> str:
    """Resolve the path to the JSONL event log.

    Honors ``KILN_EVENT_LOG`` (matches kiln-pro's writer + MCP
    reader).  Falls back to ``~/.kiln/events.log``.
    """
    return os.environ.get("KILN_EVENT_LOG") or _EVENT_LOG_DEFAULT


def _read_event_lines(path: str) -> list[str]:
    """Read JSONL lines, return list (empty if missing)."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return fh.readlines()


def _parse_event_lines(lines: list[str]) -> list[dict[str, object]]:
    """Parse JSONL → dict; preserve raw text for unparseable lines.

    Lines that fail to parse are returned as ``{"raw": <line>}`` so
    the operator sees them rather than silently losing entries — the
    common cause is a truncated write at the tail.
    """
    out: list[dict[str, object]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"raw": line})
    return out


@cli.group("events")
def events_group() -> None:
    """Inspect the local event log (``~/.kiln/events.log``).

    Subcommands:
      tail     — show the most recent N events (operator's ``tail``)
      summary  — aggregate counts over a time window
    """


@events_group.command("tail")
@click.option(
    "-n", "--limit", type=int, default=50, show_default=True,
    help="Number of trailing events to display.",
)
@click.option(
    "--json", "json_mode", is_flag=True,
    help="Emit raw JSONL (one event per line) instead of a table.",
)
def events_tail(limit: int, json_mode: bool) -> None:
    """Show the last N events from the local event log.

    Operator analogue of the ``tail_event_log`` MCP tool — useful for
    grep pipelines, quick last-state checks, and shell automation
    that doesn't have an agent in the loop.
    """
    if limit < 1:
        raise click.ClickException("--limit must be >= 1")

    path = _events_log_path()
    lines = _read_event_lines(path)
    tail = lines[-limit:]
    events = _parse_event_lines(tail)

    if json_mode:
        for ev in events:
            click.echo(json.dumps(ev))
        return

    if not events:
        click.echo(f"No events at {path} (file empty or missing).")
        return

    # Plain-text table; column widths kept terminal-friendly.  We
    # deliberately don't use Rich here because operators commonly pipe
    # this into `grep`, `awk`, or `less` — Rich's box-drawing breaks
    # those pipelines.  Operators who want a fancier view get
    # ``--json | jq`` instead.
    click.echo(f"# {path}  (showing last {len(events)} of {len(lines)})")
    click.echo("# created_at\tevent_type\tartifact_type\toperator")
    for ev in events:
        if "raw" in ev:
            click.echo(f"# unparseable: {ev['raw']}")
            continue
        ts = ev.get("created_at", "")
        if isinstance(ts, (int, float)) and ts:
            try:
                ts = _dt.fromtimestamp(float(ts), tz=_tz.utc).isoformat()
            except (ValueError, OSError):
                ts = str(ts)
        click.echo(
            f"{ts}\t{ev.get('event_type', '?')}\t"
            f"{ev.get('artifact_type', '?')}\t{ev.get('operator', '?')}"
        )


_DURATION_UNITS = {
    "s": 1.0 / 3600,
    "m": 1.0 / 60,
    "h": 1.0,
    "d": 24.0,
    "w": 24.0 * 7,
}


def _parse_window(spec: str) -> float:
    """Parse a duration like ``"1h"``, ``"30m"``, ``"7d"`` into hours.

    Bare numbers are treated as hours (matches the MCP tool's
    ``window_hours`` parameter).  Raises ``ClickException`` on invalid
    input rather than silently defaulting — a typo'd ``--since 2hr``
    should produce a visible error, not 24h of data.
    """
    spec = spec.strip().lower()
    if not spec:
        raise click.ClickException("--since cannot be empty")
    if spec[-1].isdigit():
        try:
            return float(spec)
        except ValueError as exc:
            raise click.ClickException(f"invalid --since value: {spec!r}") from exc
    unit = spec[-1]
    if unit not in _DURATION_UNITS:
        raise click.ClickException(
            f"unknown --since unit {unit!r}; use s/m/h/d/w (e.g. 30m, 2h, 7d)"
        )
    try:
        n = float(spec[:-1])
    except ValueError as exc:
        raise click.ClickException(f"invalid --since value: {spec!r}") from exc
    return n * _DURATION_UNITS[unit]


@events_group.command("summary")
@click.option(
    "--since", "since_spec", default="24h", show_default=True,
    help="Time window (e.g. 30m, 2h, 7d).  Bare numbers = hours.",
)
@click.option(
    "--json", "json_mode", is_flag=True,
    help="Emit aggregate as a single JSON object.",
)
def events_summary(since_spec: str, json_mode: bool) -> None:
    """Aggregate event counts over a time window.

    Counts events by type and artifact, plus the top 10 operators.
    Reads the same JSONL file as ``kiln events tail``.  Operator
    analogue of the ``summarize_events`` MCP tool — drop into a
    daily cron and pipe to a notifier for cheap fleet dashboards.
    """
    window_hours = _parse_window(since_spec)
    path = _events_log_path()
    lines = _read_event_lines(path)

    import time as _time
    cutoff = _time.time() - (window_hours * 3600)

    by_event: dict[str, int] = {}
    by_artifact: dict[str, int] = {}
    by_operator: dict[str, int] = {}
    total = 0
    unparseable = 0

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            unparseable += 1
            continue
        try:
            if float(ev.get("created_at") or 0) < cutoff:
                continue
        except (TypeError, ValueError):
            continue
        total += 1
        et = str(ev.get("event_type", "?"))
        at = str(ev.get("artifact_type", "?"))
        op = str(ev.get("operator", "?"))
        by_event[et] = by_event.get(et, 0) + 1
        by_artifact[at] = by_artifact.get(at, 0) + 1
        by_operator[op] = by_operator.get(op, 0) + 1

    top_ops = dict(sorted(by_operator.items(), key=lambda kv: -kv[1])[:10])

    if json_mode:
        click.echo(json.dumps({
            "window_hours": window_hours,
            "path": path,
            "total": total,
            "by_event_type": by_event,
            "by_artifact_type": by_artifact,
            "top_operators": top_ops,
            "unparseable_lines": unparseable,
        }))
        return

    click.echo(f"# {path}  (window: {since_spec} = {window_hours}h)")
    click.echo(f"Total events: {total}")
    if unparseable:
        click.echo(f"Unparseable lines: {unparseable}")
    if not total:
        return
    click.echo("\nBy event type:")
    for et, n in sorted(by_event.items(), key=lambda kv: -kv[1]):
        click.echo(f"  {n:>5}  {et}")
    click.echo("\nBy artifact type:")
    for at, n in sorted(by_artifact.items(), key=lambda kv: -kv[1]):
        click.echo(f"  {n:>5}  {at}")
    if top_ops:
        click.echo("\nTop operators:")
        for op, n in top_ops.items():
            click.echo(f"  {n:>5}  {op}")


# Pro-CLI registration — MUST run after every @cli.group decorator above so
# kiln-pro can extend public groups (e.g. graft `versions alerts`, `versions
# record-outcome`, `versions best` onto the public `versions` group).
#
# Pro registers FIRST (before register_auth_cli) so that two pieces of
# bookkeeping work correctly: (1) kiln-pro adds its own `identity` group
# which `register_auth_cli` then relocates a legacy top-level `login`
# onto (`kiln identity login`); (2) on a public-only install this
# block is a no-op via the ImportError guard, so the auth CLI still
# wires up unconditionally below.
try:
    from kiln_pro.cli.pro_commands import register_pro_cli

    register_pro_cli(cli)
except ImportError:
    pass  # kiln-pro not installed — pro CLI commands not available


# Auth commands — `kiln signin` / `kiln signout` / `kiln whoami` / `kiln pair`.
# Registered unconditionally so ``pip install kiln3d && kiln pair <code>``
# works on a clean machine without private-registry access.  These commands
# call only the public Kiln REST API; no proprietary logic.  Runs AFTER
# register_pro_cli so kiln-pro's legacy top-level `login` (identity-linking)
# can be relocated to `kiln identity login` and the canonical `kiln signin`
# (OAuth device flow) takes the name everyone actually reaches for first.
register_auth_cli(cli)


# MCP install command — public because a clean ``pip install kiln3d`` should
# be enough to pair this machine and expose the hosted tool surface.
register_install_mcp_cli(cli)
register_install_openscad_cli(cli)
register_install_step_backend_cli(cli)


# Spend-cap subcommand — `kiln spend-caps {show,raise,approve-order}`.
# Registered unconditionally for the same reason as the auth commands:
# the CLI talks only to the public Kiln REST API, and the agent-flow
# opt-in lives server-side, so a free-tier or paid user with the
# opt-in enabled can drive cap changes from the CLI without kiln-pro
# being installed locally.
register_spend_caps_cli(cli)

# `kiln bridge {status,start,stop,enable,disable}` — run the web->printer bridge
# as an opt-in background service.  Public surface; no kiln-pro dependency.
register_bridge_cli(cli)


def main() -> None:
    """CLI entry point.

    Forces stdout/stderr to UTF-8 before Click parses ``argv`` so help
    text and status glyphs render on legacy-code-page Windows consoles.
    """
    # This process entered through the CLI door.  Declared HERE, at the
    # console-script entry point, not guessed at call time — the surface
    # is a fact about how the process started (see kiln/surface.py).
    # ``kiln serve`` also passes through here; kiln.server.main()
    # re-declares "mcp" before any tool can dispatch.
    with contextlib.suppress(Exception):
        from kiln.surface import set_surface

        set_surface("cli")
    _ensure_utf8_streams()
    cli()


if __name__ == "__main__":
    main()
