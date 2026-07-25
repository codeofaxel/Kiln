"""Anonymous daily usage heartbeat.

Sends one row per install per day to Supabase: installation UUID,
device fingerprint (anonymous salted hash of the OS-level machine ID
— see ``kiln/device.py``), Kiln version, printer model, and daily
event counts.  No PII, no file paths, no user identity.  Runs in a
daemon thread on server startup — never blocks, never errors
visibly, never delays anything.

Disable with ``KILN_TELEMETRY=false`` in environment.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import date
from pathlib import Path

_logger = logging.getLogger(__name__)

_SUPABASE_URL = "https://nomzokpscfshjjzezplr.supabase.co"
_SUPABASE_ANON_KEY = "sb_publishable_ZCJyEL0qeveSwgqv7dry3A_YI26Yw6S"

_LAST_BEAT_PATH = Path.home() / ".kiln" / ".last_heartbeat"
_lock = threading.Lock()
_sent_today = False


def _telemetry_enabled() -> bool:
    """Check if telemetry is enabled (default: yes)."""
    val = os.environ.get("KILN_TELEMETRY", "true").strip().lower()
    return val not in ("false", "0", "no", "off")


# Environment variables that indicate a CI / build / sandbox runner.
# Heartbeats are for installed-user reachability, not for ephemeral
# CI jobs that get a fresh ``$HOME`` per invocation — those generate
# a brand-new ``installation_id`` per run and inflate active-install
# counts by two orders of magnitude (the founder dashboard saw 462
# "active installs" in 30d when the real number was ~4).  Suppressing
# the heartbeat at the source is the only honest fix.
_CI_ENV_VARS: tuple[str, ...] = (
    "CI",                  # generic flag set by GitHub Actions, GitLab, CircleCI, Travis, Buildkite, Vercel, Netlify, Cloudflare Pages, Render
    "GITHUB_ACTIONS",      # GitHub Actions
    "GITLAB_CI",           # GitLab CI
    "CIRCLECI",            # CircleCI
    "TRAVIS",              # Travis
    "BUILDKITE",           # Buildkite
    "JENKINS_URL",         # Jenkins
    "TEAMCITY_VERSION",    # TeamCity
    "TF_BUILD",            # Azure DevOps
    "BITBUCKET_BUILD_NUMBER",  # Bitbucket Pipelines
    "DRONE",               # Drone CI
    "APPVEYOR",            # AppVeyor
    "CODEBUILD_BUILD_ID",  # AWS CodeBuild
    "RUNNER_OS",           # GitHub Actions runner shim — present even in some self-hosted setups where ``CI`` got unset
    "PYTEST_CURRENT_TEST", # pytest sets this during test execution; a unit test importing kiln must not phone home
)


def _is_ci_environment() -> bool:
    """True if any well-known CI / build / test env var is set."""
    return any(os.environ.get(name) for name in _CI_ENV_VARS)


def _already_sent_today() -> bool:
    """File-based guard — avoid duplicate pings on restarts."""
    try:
        if _LAST_BEAT_PATH.is_file():
            stored = _LAST_BEAT_PATH.read_text(encoding="utf-8").strip()
            return stored == str(date.today())
    except OSError:
        pass
    return False


def _mark_sent() -> None:
    """Record that today's heartbeat was sent."""
    try:
        _LAST_BEAT_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _LAST_BEAT_PATH.write_text(str(date.today()), encoding="utf-8")
    except OSError:
        pass


def _adapter_model(adapter: object) -> str | None:
    """Best-effort model name for one adapter.

    Resolution order matters — this function replaces a call to
    ``adapter.get_printer_info()``, a method NO adapter has ever
    implemented: every call raised AttributeError into a bare except,
    so production heartbeats carried a NULL model for every install
    whose registry was otherwise fine (2026-07-25: 630 of 670 rows
    NULL while adapter_type resolved for hundreds of them).

    1. ``get_printer_info()`` — kept first, guarded, so an adapter that
       grows a live-probe method someday wins automatically.
    2. The ``printer_model`` / ``_printer_model`` attribute — the same
       source the registry's own fleet view reads (``_model_for`` in
       kiln/registry.py); Bambu carries it when configured.
    """
    try:
        info = adapter.get_printer_info()  # type: ignore[attr-defined]
        model = getattr(info, "model", None) or getattr(info, "printer_model", None)
        model = str(model or "").strip()
        if model:
            return model
    except Exception:
        pass
    model = getattr(adapter, "printer_model", None) or getattr(
        adapter, "_printer_model", None
    )
    model = str(model or "").strip()
    return model or None


def _get_printer_info() -> tuple[str | None, str | None, int]:
    """Best-effort resolve of printer model, adapter type, and printer count."""
    model: str | None = None
    adapter_type: str | None = None
    printer_count = 0
    try:
        from kiln.registry import get_registry

        reg = get_registry()
        printer_count = reg.count
        adapter = reg.get("default")
        if adapter is not None:
            model = _adapter_model(adapter)
            # config.yaml is the canonical model source when the
            # adapter doesn't track one (see printer_model_resolver).
            if not model:
                try:
                    from kiln.printer_model_resolver import resolve_printer_model

                    model = resolve_printer_model()
                except Exception:
                    pass
            # Legacy env override — last resort only.
            if not model:
                model = os.environ.get("KILN_PRINTER_MODEL", None)
            # Derive adapter type from class name
            cls_name = type(adapter).__name__.lower()
            if "creality" in cls_name:
                adapter_type = "creality"
            elif "bambu" in cls_name:
                adapter_type = "bambu"
            elif "octoprint" in cls_name:
                adapter_type = "octoprint"
            elif "moonraker" in cls_name:
                adapter_type = "moonraker"
            elif "serial" in cls_name:
                adapter_type = "serial"
            else:
                adapter_type = cls_name.replace("adapter", "").strip("_") or None
    except Exception:
        pass
    # Last resort: derive adapter type from env
    if not adapter_type:
        adapter_type = os.environ.get("KILN_PRINTER_TYPE", None)
    return model, adapter_type, printer_count


# Bound the per-heartbeat model list: home fleets are 1-3 printers;
# anything past 8 is a farm and the fleet tools already cover it.
_MAX_HEARTBEAT_PRINTER_MODELS = 8


def _get_all_printer_models() -> list[str]:
    """Best-effort model names of EVERY registered printer.

    ``_get_printer_info`` reports only the "default" printer, so a
    two-printer setup was invisible beyond its default machine.  This
    walks the whole registry (deduped, order-stable, capped) so the
    aggregate telemetry reflects the real fleet.  Model names only —
    no printer ids, no addresses, no serials.
    """
    models: list[str] = []

    def _add(model: str | None) -> None:
        model = str(model or "").strip()[:60]
        if model and model not in models and len(models) < _MAX_HEARTBEAT_PRINTER_MODELS:
            models.append(model)

    try:
        from kiln.registry import get_registry

        reg = get_registry()
        for name in reg.list_names():
            if len(models) >= _MAX_HEARTBEAT_PRINTER_MODELS:
                break
            try:
                _add(_adapter_model(reg.get(name)))
            except Exception:
                continue
    except Exception:
        pass
    # config.yaml carries a printer_model per configured printer — the
    # canonical source when adapters don't track a model themselves.
    try:
        from kiln.printer_model_resolver import resolve_all_printer_models

        for model in resolve_all_printer_models():
            _add(model)
    except Exception:
        pass
    return models


def _get_daily_counts() -> dict[str, int]:
    """Read today's event counters from daily_stats."""
    try:
        from kiln.daily_stats import get_daily_stats

        return get_daily_stats()
    except Exception:
        return {"prints": 0, "generations": 0, "decorations": 0, "textures": 0}


def _top_n(mapping: object, n: int) -> dict:
    """Return the ``n`` highest-count entries of a ``{name: count}`` map.

    Bounds the heartbeat payload for high-cardinality breakdowns (the
    per-tool counter): a busy install can touch many tools in a day, but
    only the busiest ``n`` need to travel.  Non-dict / bad input → ``{}``.
    """
    if not isinstance(mapping, dict) or not mapping:
        return {}
    try:
        items = [(k, int(v)) for k, v in mapping.items() if isinstance(v, (int, float))]
    except Exception:
        return {}
    items.sort(key=lambda kv: kv[1], reverse=True)
    return {k: v for k, v in items[:n]}


def _is_pro_installed() -> bool:
    """Check if kiln-pro is installed."""
    try:
        import kiln_pro  # noqa: F401

        return True
    except ImportError:
        return False


def _send_heartbeat() -> None:
    """Send a single heartbeat to Supabase."""
    global _sent_today  # noqa: PLW0603

    if _is_ci_environment():
        return

    with _lock:
        if _sent_today or _already_sent_today():
            _sent_today = True
            return

    try:
        import json
        import platform
        import urllib.request

        from kiln.device import get_device_fingerprint
        from kiln.installation import get_installation_id

        installation_id = get_installation_id()
        # Anonymous, one-way salted hash of the OS-level machine ID
        # (or "" if we can't read it).  Lets the dashboard count
        # unique DEVICES separately from unique installs — see
        # PRIVACY.md §3.1 for the full data-flow disclosure.
        device_fingerprint = get_device_fingerprint() or None

        kiln_version: str | None = None
        try:
            import kiln

            kiln_version = getattr(kiln, "__version__", None)
        except Exception:
            pass

        printer_model, adapter_type, printer_count = _get_printer_info()
        stats = _get_daily_counts()

        rpc_url = f"{_SUPABASE_URL}/rest/v1/rpc/record_heartbeat"
        payload = json.dumps({
            "p_installation_id": installation_id,
            "p_kiln_version": kiln_version,
            "p_printer_model": printer_model,
            "p_adapter_type": adapter_type,
            "p_printer_count": printer_count,
            "p_prints_today": stats.get("prints", 0),
            "p_generations_today": stats.get("generations", 0),
            "p_decorations_today": stats.get("decorations", 0),
            "p_textures_today": stats.get("textures", 0),
            "p_slices_today": stats.get("slices", 0),
            "p_downloads_today": stats.get("downloads", 0),
            "p_print_hours_today": stats.get("print_hours", 0.0),
            "p_pro_installed": _is_pro_installed(),
            "p_os_platform": platform.system().lower(),
            "p_device_fingerprint": device_fingerprint,
            # NOT json.dumps'd here — the outer payload = json.dumps({...})
            # below already serializes this whole dict.  Encoding it twice
            # stored details as a jsonb STRING scalar server-side instead of
            # an object, which silently broke the ingest sanitizers and
            # crashed the founder dashboard's tool-usage tile the first time
            # anything tried to read it (kiln-pro migration 095, 2026-07-09).
            "p_details": {
                "texture_names": stats.get("texture_names", {}),
                "decoration_types": stats.get("decoration_types", {}),
                "slicer_profiles": stats.get("slicer_profiles", {}),
                "marketplace_sources": stats.get("marketplace_sources", {}),
                # Tier-denial telemetry — {tool_name: count_today}.
                # Every entry is a user who hit a paywall; if the same
                # user upgraded on the web but their local session
                # never synced, we'll see denials spike on one install
                # for a day or two and then either disappear (they
                # ran `kiln pair`) or persist (they gave up — a
                # support-ticket ticker).
                "tier_denials": stats.get("tier_denials", {}),
                # Per-tool call counts — {tool_name: count_today}.  The
                # anonymous view of what a not-signed-in local user
                # actually does.  Capped to the busiest tools so the
                # payload stays small; names + counts only, never args.
                "tool_calls": _top_n(stats.get("tool_calls", {}), 100),
                # Model names of EVERY registered printer (deduped,
                # capped) — the top-level printer_model field only ever
                # names the default machine, which made second printers
                # invisible in aggregate stats.  Names only: no ids,
                # addresses, or serials.
                "printer_models": _get_all_printer_models(),
                # The last COMPLETE day's activity counters.  The
                # ``*_today`` fields above are sampled when the server
                # starts — before that day's work has happened — so they
                # systematically read near-zero.  This carries a whole
                # finished day instead, one day behind.  Same counters,
                # same privacy posture; the date says which day it is.
                "previous_day": stats.get("previous_day", {}),
            },
        }).encode()

        req = urllib.request.Request(
            rpc_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "apikey": _SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {_SUPABASE_ANON_KEY}",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status < 300:
                _mark_sent()
                with _lock:
                    _sent_today = True
                _logger.debug("Heartbeat sent (install=%s)", installation_id[:8])

    except Exception as exc:
        _logger.debug("Heartbeat failed (non-fatal): %s", exc)


def send_heartbeat_async() -> None:
    """Fire the heartbeat in a daemon thread — never blocks startup."""
    if not _telemetry_enabled():
        return
    if _is_ci_environment():
        return
    if _sent_today or _already_sent_today():
        return
    t = threading.Thread(target=_send_heartbeat, daemon=True, name="kiln-heartbeat")
    t.start()
