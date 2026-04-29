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
                    "in printer_intelligence.json.  Safety gates will "
                    "soft-pass.  Keys are case-sensitive (bambu_a1, not "
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
            "The safety stack (bed-fit, gcode bounds, temperature limits) "
            "will SOFT-PASS — unsafe prints can reach the printer.  Ask "
            "the user which printer model they have and add "
            "`printer_model: <model>` under `printers.%s` in the config "
            "file (e.g. bambu_a1, prusa_mk4, ender3).",
            ptype, _CONFIG_PATH, active_printer,
        )
    return None


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

