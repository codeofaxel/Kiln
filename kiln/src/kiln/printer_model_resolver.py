"""Live printer-model resolution.

Single source of truth: the ``printer_model`` field on the active
printer's entry in ``~/.kiln/config.yaml``.

Scrapped (intentionally, post-incident #0):
  - Bambu serial-prefix inference
  - Prusa host-pattern inference
  - Per-type generic fallbacks
  - ``KILN_PRINTER_MODEL`` environment variable fallback
  - Source tagging (explicit/inferred/guessed)

Rationale: inference was a well-intentioned convenience that spreads
state across multiple places where they can disagree.  The price —
maintenance burden, edge cases (new printer models = unknown prefix =
wrong safety limits applied silently), and user confusion when the
inferred model is wrong — outweighs the benefit.  One source, one
way to set it.  When ``printer_model`` is missing, the safety stack
emits a loud warning and soft-passes; the user is prompted to set it
via the ``set_printer_model`` MCP tool.

The resolver is cheap (reads + parses one yaml file, memoised by
file mtime) and safe to call from hot safety paths.  Unlike a
frozen module global, it always reflects the latest config.yaml
contents so agents that just wrote to the file see their changes.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path.home() / ".kiln" / "config.yaml"

# (mtime, model) — invalidate_cache() resets to (0.0, None).
_cache: tuple[float, str | None] = (0.0, None)


def invalidate_cache() -> None:
    """Reset the memoised resolution.  Call after writing to
    config.yaml if you want the next resolution to re-read immediately
    (otherwise the mtime check handles it)."""
    global _cache  # noqa: PLW0603
    _cache = (0.0, None)


def resolve_printer_model() -> str | None:
    """Return the active printer's ``printer_model`` field from
    ``~/.kiln/config.yaml``, or ``None`` when the field is absent /
    the config file is missing.

    Callers in the safety stack should prefer this function over any
    frozen module global.  It reflects the latest config.yaml contents
    on every call (mtime-cached, so the cost is one stat + one parse
    per config change).
    """
    global _cache  # noqa: PLW0603
    try:
        mtime = _CONFIG_PATH.stat().st_mtime if _CONFIG_PATH.is_file() else 0.0
    except OSError:
        mtime = 0.0
    cached_mtime, cached_value = _cache
    if cached_mtime == mtime and cached_mtime != 0.0:
        return cached_value

    model = _read_printer_model_from_config()
    _cache = (mtime, model)
    return model


def resolve_active_printer_name() -> str | None:
    """The NAME its owner gave the printer Kiln acts on by default.

    ``config.yaml`` records which printer is active and every entry is keyed
    by the name its owner chose, so this is a fact the machine already holds —
    not an inference. It is the answer to "which printer is this?" for every
    surface that reports on the default adapter without taking a printer
    argument, and those surfaces previously had no way to say.

    Returns ``None`` when no printer is configured, and when the only name on
    offer is the ``"default"`` placeholder the config falls back to: that is a
    label Kiln supplies in the absence of a choice, and echoing it back to
    someone as the name of their printer is worse than saying nothing.

    Reads the same file, through the same defensive loader, as
    :func:`resolve_printer_model` — one place understands which config entry
    is the active one, so a second reader cannot drift from it.
    """
    cfg = _load_yaml_config()
    if not cfg:
        return None
    printers = cfg.get("printers")
    if not isinstance(printers, dict) or not printers:
        return None
    active = cfg.get("active_printer")
    if not active:
        # No declared choice. One configured printer is still unambiguous;
        # several without a choice is genuinely unanswerable, and guessing
        # which is exactly the mistake this function exists to avoid.
        if len(printers) != 1:
            return None
        active = next(iter(printers))
    name = str(active).strip()
    if not name or name == "default" or name not in printers:
        return None
    return name


def _read_printer_model_from_config() -> str | None:
    """Parse ~/.kiln/config.yaml and return the active printer's
    ``printer_model`` field, or None if missing / malformed.

    When the field IS set but doesn't match any entry in
    ``printer_intelligence.json``, we log a loud warning (typo check)
    and still return the raw value — the safety gates will then
    soft-pass because ``get_build_volume`` / ``get_profile`` can't
    resolve it.  Loud warning catches typos early without adding a
    new MCP tool.
    """
    cfg = _load_yaml_config()
    if not cfg:
        return None
    active_printer = cfg.get("active_printer") or "default"
    printers = cfg.get("printers") or {}
    entry: dict[str, Any] = printers.get(active_printer) or {}
    model = entry.get("printer_model") or cfg.get("printer_model")
    if model:
        model_str = str(model).strip()
        # Typo check — warn loudly if the value isn't in our database.
        # get_build_volume returns None for unknown printers; unlike
        # get_printer_intel which silently falls back to "default".
        try:
            from kiln.printers.bed_fit import get_build_volume
            if model_str != model_str.lower() or get_build_volume(model_str) is None:
                logger.warning(
                    "printer_model=%r in %s doesn't match any known printer "
                    "in printer_intelligence.json.  Safety checks will be "
                    "skipped.  Keys are case-sensitive (bambu_a1, not "
                    "bambu_A1).  Check the JSON file for valid values.",
                    model_str, _CONFIG_PATH,
                )
        except Exception:
            pass
        return model_str
    # Emit ONE warning per config-mtime so users + agents see the gap.
    ptype = str(entry.get("type") or "").lower()
    if ptype:
        logger.warning(
            "No `printer_model` set for the active %s printer in %s. "
            "Kiln can't check that prints fit the bed or stay within safe "
            "temperatures — those checks are skipped, so an unsafe print "
            "could reach the printer.  Ask the user which printer model "
            "they have and add `printer_model: <model>` under `printers.%s` "
            "in the config file (e.g. bambu_a1, prusa_mk4, ender3).",
            ptype, _CONFIG_PATH, active_printer,
        )
    return None


def resolve_printer_model_for(printer_name: str | None) -> str | None:
    """The config-declared ``printer_model`` for one NAMED printer.

    :func:`resolve_printer_model` answers "what is the active printer",
    which is the right answer only for a tool that acts on the default
    connection.  A tool that can be aimed needs the model of the machine
    it was aimed at: bed-fit, temperature ceilings and the PTFE clamp all
    key off this value, so reading the active printer's model while
    talking to a different machine certifies geometry against the wrong
    bed — the failure the bed-fit gate exists to prevent.

    ``printer_name=None``, or the name of the active printer, resolves
    through :func:`resolve_printer_model` unchanged, so the default path
    keeps its legacy top-level fallback and its missing-model warning.

    For any OTHER name the top-level ``printer_model`` is deliberately
    NOT consulted: a legacy single-printer config states the default
    machine's model, and applying it to a second machine is exactly the
    wrong-bed answer above.  Returning ``None`` instead makes the safety
    gates soft-pass and say so, which is the posture this module already
    takes for an unknown model — a skipped check is recoverable, a check
    passed against the wrong hardware is not.
    """
    if not printer_name:
        return resolve_printer_model()
    cfg = _load_yaml_config()
    active_printer = (cfg.get("active_printer") or "default") if cfg else "default"
    if printer_name == active_printer:
        return resolve_printer_model()
    printers = cfg.get("printers") or {} if cfg else {}
    entry = printers.get(printer_name) or {}
    model = str(entry.get("printer_model") or "").strip() if isinstance(entry, dict) else ""
    if model:
        return model
    logger.warning(
        "No `printer_model` set for printer %r in %s.  Bed-fit and "
        "temperature checks for prints aimed at it will be skipped.  Add "
        "`printer_model: <model>` under `printers.%s` in the config file.",
        printer_name, _CONFIG_PATH, printer_name,
    )
    return None


def resolve_all_printer_models() -> list[str]:
    """Return the ``printer_model`` of EVERY printer entry in
    ``~/.kiln/config.yaml`` (active or not), deduped, order-stable.

    The single-model :func:`resolve_printer_model` answers "what is the
    ACTIVE printer" for the safety stack; this answers "what hardware
    does this install have" for fleet-shaped consumers (usage
    telemetry, fleet views).  Same single source of truth — the config
    file — same defensive posture: malformed config returns ``[]``,
    never raises.  Not memoised: callers are cold paths (a once-daily
    heartbeat), and skipping the cache avoids a second mtime ledger.
    """
    cfg = _load_yaml_config()
    if not cfg:
        return []
    models: list[str] = []
    printers = cfg.get("printers")
    entries = list(printers.values()) if isinstance(printers, dict) else []
    # Legacy single-printer configs carry a top-level printer_model.
    entries.append(cfg)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model = str(entry.get("printer_model") or "").strip()
        if model and model not in models:
            models.append(model)
    return models


def _load_yaml_config() -> dict[str, Any]:
    """Load ~/.kiln/config.yaml defensively.  Returns empty dict on any
    error — resolution must never raise just because the config is
    malformed (we'd rather soft-pass than crash the server)."""
    if not _CONFIG_PATH.is_file():
        return {}
    try:
        import yaml
        with open(_CONFIG_PATH) as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.debug("Failed to parse %s: %s", _CONFIG_PATH, exc)
        return {}

