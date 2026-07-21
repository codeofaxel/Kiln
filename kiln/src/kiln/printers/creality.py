"""Creality FDM adapter backed by Moonraker/Klipper when available."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests

from kiln.printers.base import (
    FirmwareStatus,
    FirmwareUpdateResult,
    JobProgress,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterError,
    PrinterFile,
    PrinterState,
    PrintResult,
    UploadResult,
)
from kiln.printers.moonraker import MoonrakerAdapter

_MOONRAKER_PROBE_PATH = "/server/info"
_DEFAULT_CREALITY_MOONRAKER_PORTS: tuple[int, ...] = (7125, 80, 4408)
_MODEL_ALIASES: dict[str, str] = {
    "creality_k1_max": "k1_max",
    "k1 max": "k1_max",
    "k1-max": "k1_max",
    "creality_k1c": "k1c",
    "k1 c": "k1c",
    "creality_k1_se": "k1_se",
    "k1 se": "k1_se",
    "k1-se": "k1_se",
    "creality_k2": "k2",
    "creality_k2_pro": "k2_pro",
    "k2 pro": "k2_pro",
    "k2-pro": "k2_pro",
    "creality_k2_plus": "k2_plus",
    "k2 plus": "k2_plus",
    "k2-plus": "k2_plus",
    "creality_k2_se": "k2_se",
    "k2 se": "k2_se",
    "k2-se": "k2_se",
    "creality hi": "creality_hi",
    "creality-hi": "creality_hi",
    "ender 3 v3": "ender3_v3",
    "ender-3 v3": "ender3_v3",
    "ender-3-v3": "ender3_v3",
    "ender 3 v3 ke": "ender3_v3_ke",
    "ender-3 v3 ke": "ender3_v3_ke",
    "ender-3-v3-ke": "ender3_v3_ke",
    "ender 3 v3 plus": "ender3_v3_plus",
    "ender-3 v3 plus": "ender3_v3_plus",
    "ender-3-v3-plus": "ender3_v3_plus",
    "ender 3 v4": "ender3_v4",
    "ender-3 v4": "ender3_v4",
    "ender-3-v4": "ender3_v4",
    "ender 5 max": "ender5_max",
    "ender-5 max": "ender5_max",
    "ender-5-max": "ender5_max",
    "cr-10 se": "cr10_se",
    "cr10 se": "cr10_se",
    "sparkx i7": "sparkx_i7",
    "sparkx-i7": "sparkx_i7",
}
_OFFICIAL_ROOT_SERVICE_MODELS: frozenset[str] = frozenset(
    {"k1", "k1_max", "k1c", "ender3_v3_ke", "cr10_se"}
)
_OFFICIAL_STOCK_FLUIDD_MODELS: frozenset[str] = frozenset({"ender3_v3"})
_COMMUNITY_FLUIDD_MODELS: frozenset[str] = frozenset(
    {"k2", "k2_pro", "k2_plus", "creality_hi"}
)
_UNKNOWN_STOCK_MOONRAKER_MODELS: frozenset[str] = frozenset(
    {"sparkx_i7", "k1_se", "k2_se", "ender3_v4", "ender3_v3_plus", "ender5_max"}
)
_LEGACY_SERIAL_MODELS: frozenset[str] = frozenset(
    {"ender3", "ender3_v2", "ender3_s1", "ender3_s1_pro", "ender5", "cr10",
     "ender3_v3_se"}
)
_CFS_OBJECT_KEYWORDS: tuple[str, ...] = (
    "cfs",
    "filament_box",
    "filament box",
    "boxsinfo",
    "boxesinfo",
    "box_info",
    "rfid",
    "spool",
    "material",
)
_CFS_COMMAND_KEYWORDS: tuple[str, ...] = (
    "cfs",
    "filament",
    "load",
    "unload",
    "retract",
    "extrude",
    "mapping",
    "box",
)


@dataclass(frozen=True)
class CrealityMoonrakerProbe:
    """One non-destructive Moonraker probe attempt."""

    url: str
    ok: bool
    detail: str
    failure_kind: str | None = None
    status_code: int | None = None
    moonraker: bool = False
    auth_required: bool = False
    klippy_state: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "ok": self.ok,
            "detail": self.detail,
            "failure_kind": self.failure_kind,
            "status_code": self.status_code,
            "moonraker": self.moonraker,
            "auth_required": self.auth_required,
            "klippy_state": self.klippy_state,
        }


@dataclass(frozen=True)
class CrealityMoonrakerDiagnostics:
    """Creality Moonraker reachability summary for CLI and tests."""

    host: str
    candidates: list[str]
    checks: list[CrealityMoonrakerProbe] = field(default_factory=list)
    ok: bool = False
    resolved_url: str | None = None
    klippy_state: str | None = None
    auth_required: bool = False
    likely_cause: str | None = None
    user_message: str | None = None
    firmware_lockdown_possible: bool = False
    connection_checklist: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)

    @property
    def browser_test_url(self) -> str | None:
        """URL users can paste into a browser to verify local access."""
        if not self.resolved_url:
            return None
        return f"{self.resolved_url}{_MOONRAKER_PROBE_PATH}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "candidates": self.candidates,
            "checks": [check.to_dict() for check in self.checks],
            "ok": self.ok,
            "resolved_url": self.resolved_url,
            "klippy_state": self.klippy_state,
            "auth_required": self.auth_required,
            "likely_cause": self.likely_cause,
            "user_message": self.user_message,
            "firmware_lockdown_possible": self.firmware_lockdown_possible,
            "connection_checklist": self.connection_checklist,
            "browser_test_url": self.browser_test_url,
            "next_steps": self.next_steps,
        }


def _has_explicit_port(parsed_url: Any) -> bool:
    """Return whether a parsed URL includes a valid explicit port."""
    try:
        return parsed_url.port is not None
    except ValueError:
        return True


def _normalise_model_hint(model: str | None) -> str | None:
    """Return Kiln's local profile key for a Creality model hint."""
    if not model:
        return None
    cleaned = model.strip().lower().replace("/", " ").replace("-", "_")
    cleaned = "_".join(cleaned.split())
    if not cleaned:
        return None
    return _MODEL_ALIASES.get(model.strip().lower(), _MODEL_ALIASES.get(cleaned, cleaned))


def _model_local_access_notes(model: str | None) -> list[str]:
    """Return conservative model-specific local-control guidance."""
    normalised = _normalise_model_hint(model)
    if normalised in _OFFICIAL_STOCK_FLUIDD_MODELS:
        return [
            "Official Creality Wiki guidance for Ender-3 V3 documents local Fluidd at http://<printer-ip>:4408.",
            "Fluidd reachability is not enough for Kiln by itself; Kiln still needs a Moonraker /server/info response on a probed port.",
        ]
    if normalised in _OFFICIAL_ROOT_SERVICE_MODELS:
        return [
            "Official Creality firmware/Annex notes for this family point to a root or service-enabled Fluidd/Mainsail/Moonraker path, not a stock Moonraker guarantee.",
            "If Fluidd/Mainsail or Moonraker was installed, verify it after firmware updates because official notes say configuration files may be overwritten.",
        ]
    if normalised in _COMMUNITY_FLUIDD_MODELS:
        return [
            "Official Creality sources confirm Klipper/LAN-capable firmware for this family, but Fluidd/Moonraker port behavior is community or third-party confirmed.",
            "Try http://<printer-ip>:4408 as a diagnostic hint, then save the printer only after /server/info returns Moonraker JSON.",
        ]
    if normalised in _UNKNOWN_STOCK_MOONRAKER_MODELS:
        return [
            "No official stock local Moonraker evidence is known for this Creality profile yet.",
            "Use the Creality adapter only if /server/info is reachable; otherwise keep using Creality Print/cloud/local app paths or another local backend when applicable.",
        ]
    if normalised in _LEGACY_SERIAL_MODELS:
        return [
            "This is a Marlin-era or serial-first Creality profile; use type 'serial' or 'octoprint' unless a separate Moonraker host is installed.",
        ]
    return []


def _candidate_moonraker_urls(host: str) -> list[str]:
    """Return Moonraker base URL candidates for a Creality host."""
    cleaned = host.strip().rstrip("/")
    if not cleaned:
        raise ValueError("host must not be empty")

    if cleaned.startswith("/dev/") or cleaned.upper().startswith("COM"):
        return []

    if not cleaned.startswith(("http://", "https://")):
        return [f"http://{cleaned}:{port}" for port in _DEFAULT_CREALITY_MOONRAKER_PORTS]

    parsed = urlparse(cleaned)
    if not parsed.hostname:
        return [cleaned]

    candidates = [cleaned]
    if not _has_explicit_port(parsed):
        for port in _DEFAULT_CREALITY_MOONRAKER_PORTS:
            netloc = parsed.hostname
            if ":" in netloc and not netloc.startswith("["):
                netloc = f"[{netloc}]"
            candidate = urlunparse(parsed._replace(netloc=f"{netloc}:{port}", path="", params="", query="", fragment=""))
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def _moonraker_result(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result")
    if isinstance(result, dict):
        return result
    return payload


def _extract_klippy_state(payload: dict[str, Any]) -> str | None:
    result = _moonraker_result(payload)
    state = result.get("klippy_state") or result.get("state")
    return str(state) if state is not None else None


def _looks_like_moonraker_info(payload: dict[str, Any]) -> bool:
    result = _moonraker_result(payload)
    return any(
        key in result
        for key in (
            "moonraker_version",
            "api_version",
            "api_version_string",
            "klippy_state",
            "klippy_connected",
            "components",
        )
    )


def _connection_checklist(host: str, model: str | None = None) -> list[str]:
    display_host = host.strip() or "<printer-ip>"
    return [
        "Keep the printer and this computer on the same Wi-Fi/LAN. Guest Wi-Fi, VPNs, VLANs, or client isolation can block local printer ports.",
        f"Confirm {display_host!r} is the printer's current IP address or local hostname on the printer screen or router.",
        "Check the Moonraker endpoint in a browser: http://<printer-ip>:7125/server/info. Kiln also probes http://<printer-ip>/server/info and http://<printer-ip>:4408/server/info.",
        "If Moonraker returns 401/403, pass --api-key or set KILN_PRINTER_API_KEY.",
        "If the printer answers HTTP but /server/info is not Moonraker, stock firmware on that version may not expose local Moonraker or may expose it on a different port.",
    ] + _model_local_access_notes(model)


def _request_failure_kind(exc: requests.RequestException) -> str:
    if isinstance(exc, requests.Timeout):
        return "network_timeout"
    if isinstance(exc, requests.ConnectionError):
        return "network_or_port_unreachable"
    return "request_failed"


def _classify_failed_probe(
    host: str,
    checks: list[CrealityMoonrakerProbe],
    model: str | None = None,
) -> tuple[str, str, bool, list[str]]:
    checklist = _connection_checklist(host, model=model)
    model_notes = _model_local_access_notes(model)
    if any(check.auth_required for check in checks):
        return (
            "moonraker_auth_required",
            "The printer did answer Moonraker, but Moonraker requires an API key before Kiln can connect.",
            False,
            [
                "Pass --api-key or set KILN_PRINTER_API_KEY, then run doctor-creality again.",
                "Paste http://<printer-ip>:7125/server/info into a browser on the same LAN to confirm the printer prompts or responds.",
            ]
            + model_notes,
        )

    if any(check.failure_kind == "moonraker_not_exposed" for check in checks):
        return (
            "firmware_locked_or_wrong_port",
            (
                "Something answered on the printer address, but /server/info was not Moonraker. "
                "That usually means the wrong port was used, a web UI answered instead, or stock firmware on this version does not expose local Moonraker."
            ),
            True,
            [
                "Verify the Moonraker endpoint in a browser: http://<printer-ip>:7125/server/info.",
                "If port 7125 fails, try http://<printer-ip>/server/info and http://<printer-ip>:4408/server/info.",
                "If the printer's regular web page loads but /server/info does not, check Creality firmware settings/release notes for local Moonraker or LAN API access.",
            ]
            + model_notes,
        )

    if any(check.status_code is not None and check.status_code >= 500 for check in checks):
        return (
            "moonraker_backend_unavailable",
            "Moonraker or the printer backend answered with a server error, so the printer may be booting, updating, or in a firmware error state.",
            False,
            [
                "Wait for the printer to finish booting or updating, then run doctor-creality again.",
                "Open http://<printer-ip>:7125/server/info in a browser on the same LAN and check whether it returns JSON.",
            ]
            + model_notes,
        )

    return (
        "network_or_port_unreachable",
        (
            "Kiln could not reach Moonraker on the Creality printer. "
            "Most misses are same-LAN, IP address, firewall, or Moonraker port issues; if those are correct, stock firmware may have local Moonraker disabled or locked down."
        ),
        False,
        [
            checklist[0],
            checklist[1],
            checklist[2],
            f"If all probes fail for {host!r} after LAN/IP/port checks, check whether that firmware exposes local Moonraker or configure the printer through serial/OctoPrint instead.",
        ]
        + model_notes,
    )


def _next_steps_for_failed_probe(
    host: str,
    checks: list[CrealityMoonrakerProbe],
    model: str | None = None,
) -> list[str]:
    _, _, _, next_steps = _classify_failed_probe(host, checks, model=model)
    if any(check.auth_required for check in checks):
        return [
            "Moonraker answered but rejected the request. Pass --api-key or set KILN_PRINTER_API_KEY if auth is enabled.",
            "Paste http://<printer-ip>:7125/server/info into a browser on the same LAN to confirm the printer prompts or responds.",
        ] + _model_local_access_notes(model)
    return next_steps


def diagnose_creality_moonraker(
    host: str,
    api_key: str | None = None,
    *,
    model: str | None = None,
    timeout: int = 5,
    verify_ssl: bool = True,
) -> CrealityMoonrakerDiagnostics:
    """Probe Creality Moonraker candidates and report the reachable URL."""
    try:
        candidates = _candidate_moonraker_urls(host)
    except ValueError:
        return CrealityMoonrakerDiagnostics(
            host=host,
            candidates=[],
            checks=[
                CrealityMoonrakerProbe(
                    url=host,
                    ok=False,
                    detail="host must not be empty",
                    failure_kind="host_missing",
                )
            ],
            likely_cause="host_missing",
            user_message="Kiln needs the printer IP address or local hostname before it can check Creality Moonraker access.",
            connection_checklist=_connection_checklist("<printer-ip>", model=model),
            next_steps=["Provide the printer IP address or local hostname."],
        )

    if not candidates:
        return CrealityMoonrakerDiagnostics(
            host=host,
            candidates=[],
            checks=[
                CrealityMoonrakerProbe(
                    url=host,
                    ok=False,
                    detail=(
                        "Creality serial/USB printers should use type 'serial' "
                        "or 'octoprint'; the Creality adapter expects local Moonraker."
                    ),
                    failure_kind="serial_or_usb_printer",
                )
            ],
            likely_cause="serial_or_usb_printer",
            user_message=(
                "This looks like a serial/USB printer path. The Creality adapter is for networked "
                "Creality FDM printers with local Moonraker."
            ),
            connection_checklist=_connection_checklist(host, model=model),
            next_steps=["Use type 'serial' or 'octoprint' for USB/Marlin Creality printers."]
            + _model_local_access_notes(model),
        )

    headers = {"X-Api-Key": api_key} if api_key else None
    checks: list[CrealityMoonrakerProbe] = []
    for base_url in candidates:
        probe_url = f"{base_url}{_MOONRAKER_PROBE_PATH}"
        try:
            response = requests.get(
                probe_url,
                headers=headers,
                timeout=min(timeout, 5),
                verify=verify_ssl,
            )
        except requests.RequestException as exc:
            checks.append(
                CrealityMoonrakerProbe(
                    url=probe_url,
                    ok=False,
                    detail=str(exc),
                    failure_kind=_request_failure_kind(exc),
                )
            )
            continue

        if response.status_code in (401, 403):
            checks.append(
                CrealityMoonrakerProbe(
                    url=probe_url,
                    ok=False,
                    status_code=response.status_code,
                    detail=(
                        "Moonraker answered but requires an API key"
                        if not api_key
                        else "Moonraker answered but rejected the API key"
                    ),
                    failure_kind="auth_required",
                    auth_required=True,
                )
            )
            continue

        if not response.ok:
            failure_kind = (
                "moonraker_backend_unavailable"
                if response.status_code >= 500
                else "moonraker_not_exposed"
            )
            checks.append(
                CrealityMoonrakerProbe(
                    url=probe_url,
                    ok=False,
                    status_code=response.status_code,
                    detail=f"returned HTTP {response.status_code}",
                    failure_kind=failure_kind,
                )
            )
            continue

        try:
            payload = response.json()
        except ValueError:
            checks.append(
                CrealityMoonrakerProbe(
                    url=probe_url,
                    ok=False,
                    status_code=response.status_code,
                    detail="returned HTTP 200 but not JSON",
                    failure_kind="moonraker_not_exposed",
                )
            )
            continue

        if not isinstance(payload, dict) or not _looks_like_moonraker_info(payload):
            checks.append(
                CrealityMoonrakerProbe(
                    url=probe_url,
                    ok=False,
                    status_code=response.status_code,
                    detail="returned JSON but not Moonraker /server/info",
                    failure_kind="moonraker_not_exposed",
                )
            )
            continue

        klippy_state = _extract_klippy_state(payload)
        checks.append(
            CrealityMoonrakerProbe(
                url=probe_url,
                ok=True,
                status_code=response.status_code,
                detail=f"/server/info reachable (klippy_state={klippy_state or 'unknown'})",
                moonraker=True,
                klippy_state=klippy_state,
            )
        )
        return CrealityMoonrakerDiagnostics(
            host=host,
            candidates=candidates,
            checks=checks,
            ok=True,
            resolved_url=base_url,
            klippy_state=klippy_state,
            likely_cause="moonraker_reachable",
            user_message="Moonraker is reachable from Kiln on the local network.",
            connection_checklist=_connection_checklist(host, model=model),
            next_steps=[
                "Save this printer as type 'creality' with this resolved Moonraker URL.",
                f"Browser test: {probe_url}",
            ],
        )

    likely_cause, user_message, firmware_lockdown_possible, next_steps = _classify_failed_probe(
        host, checks, model=model
    )
    return CrealityMoonrakerDiagnostics(
        host=host,
        candidates=candidates,
        checks=checks,
        auth_required=any(check.auth_required for check in checks),
        likely_cause=likely_cause,
        user_message=user_message,
        firmware_lockdown_possible=firmware_lockdown_possible,
        connection_checklist=_connection_checklist(host, model=model),
        next_steps=next_steps,
    )


def _contains_keyword(value: str, keywords: tuple[str, ...]) -> bool:
    lower = value.lower()
    return any(keyword in lower for keyword in keywords)


def _extract_object_names(payload: dict[str, Any]) -> list[str]:
    result = _moonraker_result(payload)
    objects = result.get("objects", [])
    if not isinstance(objects, list):
        return []
    return [str(obj) for obj in objects if isinstance(obj, str)]


def _extract_gcode_commands(payload: dict[str, Any]) -> list[str]:
    result = _moonraker_result(payload)
    if not isinstance(result, dict):
        return []
    return [str(key) for key in result if isinstance(key, str)]


def _case_insensitive_get(data: dict[str, Any], *keys: str) -> Any:
    lower_map = {str(key).lower(): value for key, value in data.items()}
    for key in keys:
        value = lower_map.get(key.lower())
        if value is not None:
            return value
    return None


def _looks_like_cfs_slot(data: dict[str, Any]) -> bool:
    keys = {str(key).lower() for key in data}
    slot_keys = {
        "boxid",
        "box_id",
        "slot",
        "slotid",
        "slot_id",
        "tray",
        "tray_id",
        "materialid",
        "filamentid",
    }
    material_keys = {
        "material",
        "materialid",
        "same_material",
        "filament",
        "filament_type",
        "type",
        "color",
        "colour",
        "rfid",
        "remain",
        "remaining",
    }
    return bool(keys & slot_keys) and bool(keys & material_keys)


def _normalise_cfs_slot(data: dict[str, Any], path: str) -> dict[str, Any]:
    slot = _case_insensitive_get(
        data,
        "boxId",
        "box_id",
        "slot",
        "slotId",
        "slot_id",
        "tray",
        "tray_id",
        "id",
    )
    material = _case_insensitive_get(
        data,
        "material",
        "materialId",
        "same_material",
        "filament",
        "filament_type",
        "type",
        "name",
    )
    color = _case_insensitive_get(
        data,
        "color",
        "colour",
        "colorCode",
        "tray_color",
        "filamentColor",
    )
    remaining = _case_insensitive_get(
        data,
        "remain",
        "remaining",
        "remaining_percent",
        "percent",
    )
    return {
        "slot": slot,
        "material": material,
        "color": color,
        "remaining": remaining,
        "rfid": _case_insensitive_get(data, "rfid", "tag", "tag_uid"),
        "loaded": _case_insensitive_get(data, "loaded", "selected", "active"),
        "source_path": path,
        "raw": data,
    }


def _extract_cfs_slots(status: dict[str, Any]) -> list[dict[str, Any]]:
    slots: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            if _looks_like_cfs_slot(value):
                normalised = _normalise_cfs_slot(value, path)
                key = repr((
                    normalised.get("slot"),
                    normalised.get("material"),
                    normalised.get("color"),
                    normalised.get("source_path"),
                ))
                if key not in seen:
                    seen.add(key)
                    slots.append(normalised)
            for child_key, child_value in value.items():
                _visit(child_value, f"{path}.{child_key}" if path else str(child_key))
        elif isinstance(value, list):
            for idx, item in enumerate(value):
                _visit(item, f"{path}[{idx}]")

    _visit(status, "")
    return slots


class CrealityAdapter(PrinterAdapter):
    """First-class Creality FDM adapter using the Moonraker API surface.

    Modern Creality FDM printers such as K1/K2/Hi/Ender-3 V3 KE run
    Klipper-derived firmware, but users know them as Creality printers.
    This adapter gives Kiln a Creality-branded entry point while delegating
    the hardware operations to the existing Moonraker adapter.
    """

    def __init__(
        self,
        host: str,
        api_key: str | None = None,
        *,
        model: str | None = None,
        timeout: int = 30,
        retries: int = 3,
        verify_ssl: bool = True,
    ) -> None:
        if not host:
            raise ValueError("host must not be empty")
        self._input_host = host.strip().rstrip("/")
        self._api_key = api_key or None
        self._model = model or None
        self._timeout = timeout
        self._verify_ssl = verify_ssl
        self._moonraker_url = self._resolve_moonraker_url()
        self._backend = MoonrakerAdapter(
            host=self._moonraker_url,
            api_key=self._api_key,
            timeout=timeout,
            retries=retries,
            verify_ssl=verify_ssl,
        )
        if self._model:
            self.set_safety_profile(self._model)

    @property
    def moonraker_url(self) -> str:
        """Resolved Moonraker base URL."""
        return self._moonraker_url

    @property
    def name(self) -> str:
        return "creality"

    @property
    def capabilities(self) -> PrinterCapabilities:
        return self._backend.capabilities

    def set_safety_profile(self, profile_id: str) -> None:
        super().set_safety_profile(profile_id)
        self._backend.set_safety_profile(profile_id)

    def _resolve_moonraker_url(self) -> str:
        diagnostics = diagnose_creality_moonraker(
            self._input_host,
            api_key=self._api_key,
            model=self._model,
            timeout=self._timeout,
            verify_ssl=self._verify_ssl,
        )
        if not diagnostics.candidates:
            raise PrinterError(
                "Creality serial/USB printers should be configured with type 'serial' or 'octoprint'. "
                "The 'creality' adapter is for networked Creality FDM printers with a Moonraker API."
            )

        if diagnostics.ok and diagnostics.resolved_url:
            return diagnostics.resolved_url

        failures = [f"{check.url}: {check.detail}" for check in diagnostics.checks]
        detail = "; ".join(failures[:3])
        if len(failures) > 3:
            detail += "; ..."
        cause = f" Likely cause: {diagnostics.likely_cause}." if diagnostics.likely_cause else ""
        guidance = f" {diagnostics.user_message}" if diagnostics.user_message else ""
        steps = " ".join(diagnostics.next_steps[:2])
        if steps:
            guidance = f"{guidance} Next steps: {steps}"
        raise PrinterError(
            "Could not find a Moonraker API on this Creality printer. "
            "For K1/K1 Max/K1C/Ender-3 V3/Ender-3 V3 KE/CR-10 SE/K2/Hi, verify the printer is on the LAN "
            "and Moonraker is reachable, then try the printer IP, http://<ip>:7125, or model-specific Fluidd port hints. "
            f"Probe results: {detail or 'no candidates reached'}.{cause}{guidance}"
        )

    def get_state(self) -> PrinterState:
        return self._backend.get_state()

    def get_job(self) -> JobProgress:
        return self._backend.get_job()

    def list_files(self) -> list[PrinterFile]:
        return self._backend.list_files()

    def upload_file(self, file_path: str) -> UploadResult:
        return self._backend.upload_file(file_path)

    def _start_print_impl(self, file_name: str, **kwargs: Any) -> PrintResult:
        # Delegate to the backend's IMPL, not its start_print(): the gate
        # already ran once at this adapter's start_print() (Template Method)
        # with the correct Creality printer id.  Calling the backend's
        # start_print() would re-run the gate with the backend's (often
        # unresolved) model and double-gate the print.
        return self._backend._start_print_impl(file_name, **kwargs)

    def cancel_print(self) -> PrintResult:
        return self._backend.cancel_print()

    def pause_print(self) -> PrintResult:
        return self._backend.pause_print()

    def _resume_print_impl(self) -> PrintResult:
        # Delegate to the backend's IMPL, not its resume_print(): the
        # not-paused gate already ran once at this adapter's resume_print()
        # (base Template Method) via this adapter's get_state.  Calling the
        # backend's resume_print() would re-run that gate redundantly.
        return self._backend._resume_print_impl()

    def emergency_stop(self) -> PrintResult:
        return self._backend.emergency_stop()

    def run_calibration(self, *, options: list[str] | None = None) -> PrintResult:
        return self._backend.run_calibration(options=options)

    def set_tool_temp(self, target: float) -> bool:
        return self._backend.set_tool_temp(target)

    def set_bed_temp(self, target: float) -> bool:
        return self._backend.set_bed_temp(target)

    def send_gcode(self, commands: list[str]) -> bool:
        return self._backend.send_gcode(commands)

    def set_fan(self, node: str, percent: int) -> bool:
        """Set the part-cooling fan speed via the Moonraker/Klipper backend.

        Only the single default part-cooling fan is supported — see
        :meth:`kiln.printers.base.PrinterAdapter._validate_part_fan`.
        """
        return self._backend.set_fan(node, percent)

    def skip_objects(self, object_names: list[str]) -> bool:
        """Abandon named objects mid-print via the Moonraker/Klipper backend.

        Creality's 2024+ machines run Klipper behind Moonraker, so this
        delegates to ``MoonrakerAdapter.skip_objects`` (``EXCLUDE_OBJECT``).
        Same precondition: the file must have been sliced with object
        labelling on.
        """
        return self._backend.skip_objects(object_names)

    def get_snapshot(self) -> bytes | None:
        return self._backend.get_snapshot()

    def get_stream_url(self) -> str | None:
        return self._backend.get_stream_url()

    def get_filament_status(self) -> dict[str, Any] | None:
        return self._backend.get_filament_status()

    def get_cfs_status(self) -> dict[str, Any]:
        """Read-only Creality CFS/CFS-C discovery through Moonraker.

        Creality documents CFS control in Creality Print and printer UI, but
        not a stable public Moonraker slot-control API. This method therefore
        discovers likely CFS-related Moonraker objects/macros and normalizes
        any visible slot-shaped data without sending load/unload commands.
        """
        warnings: list[str] = [
            "Creality CFS active slot control is hardware-unverified in Kiln; this call is read-only.",
        ]
        objects_payload = self._backend._get_json("/printer/objects/list")  # type: ignore[attr-defined]
        object_names = _extract_object_names(objects_payload)
        cfs_objects = [
            name
            for name in object_names
            if _contains_keyword(name, _CFS_OBJECT_KEYWORDS)
        ]

        status: dict[str, Any] = {}
        if cfs_objects:
            try:
                payload = self._backend._get_json(  # type: ignore[attr-defined]
                    "/printer/objects/query",
                    params={name: "" for name in cfs_objects},
                )
                result = _moonraker_result(payload)
                raw_status = result.get("status")
                if isinstance(raw_status, dict):
                    status = raw_status
            except PrinterError as exc:
                warnings.append(f"CFS candidate object query failed: {exc}")

        candidate_commands: list[str] = []
        try:
            help_payload = self._backend._get_json("/printer/gcode/help")  # type: ignore[attr-defined]
            candidate_commands = [
                command
                for command in _extract_gcode_commands(help_payload)
                if _contains_keyword(command, _CFS_COMMAND_KEYWORDS)
            ]
        except PrinterError as exc:
            warnings.append(f"G-code help query failed: {exc}")

        slots = _extract_cfs_slots(status)
        detected = bool(cfs_objects or slots or candidate_commands)
        if not detected:
            warnings.append(
                "No CFS-specific Moonraker objects or macros were discovered. "
                "If CFS/CFS-C is installed, verify the printer firmware exposes it locally."
            )

        return {
            "detected": detected,
            "source": "moonraker_discovery",
            "moonraker_url": self._moonraker_url,
            "hardware_unverified": True,
            "active_slot_control_supported": False,
            "control_mode": "firmware_gcode_or_creality_print",
            "candidate_objects": cfs_objects,
            "candidate_commands": candidate_commands,
            "slots": slots,
            "slot_count": len(slots) if slots else None,
            "raw_status": status,
            "warnings": warnings,
            "next_steps": [
                "Use Creality Print or printer UI to verify CFS slot mapping before unattended multicolor jobs.",
                "For K1 Max/K1/K1C/K1 SE, confirm the CFS-C retrofit firmware and compatible hotend path are installed.",
                "Capture /printer/objects/list, /printer/objects/query, and gcode/help output from real hardware before enabling active slot commands.",
            ],
        }

    def get_bed_mesh(self) -> dict[str, Any] | None:
        return self._backend.get_bed_mesh()

    def get_firmware_status(self) -> FirmwareStatus | None:
        return self._backend.get_firmware_status()

    def update_firmware(self, component: str | None = None) -> FirmwareUpdateResult:
        return self._backend.update_firmware(component=component)

    def rollback_firmware(self, component: str) -> FirmwareUpdateResult:
        return self._backend.rollback_firmware(component)

    def delete_file(self, file_path: str) -> bool:
        return self._backend.delete_file(file_path)

    def __repr__(self) -> str:
        return f"<CrealityAdapter host={self._input_host!r} moonraker_url={self._moonraker_url!r}>"
