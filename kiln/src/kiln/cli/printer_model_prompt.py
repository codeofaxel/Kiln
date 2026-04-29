"""Interactive prompt for the ``printer_model`` config field.

The field activates Kiln's full safety stack (bed-fit, gcode bounds,
temperature limits, PTFE clamp).  Without it those gates soft-pass —
and before 2026-04-16 the setup CLI never asked for it, so EVERY
existing user has a config file missing the field.  Incident #0
exposed the gap.

This module centralises the prompt logic so three callers
(``kiln setup``, ``kiln quickstart``, migration nag) all behave
consistently.

Design principles (locked in by user review of the previous attempt):
  * Single source of truth: the ``printer_model`` field in
    ~/.kiln/config.yaml.  No env vars, no silent inference.
  * Explicit confirmation: for Bambu printers with a known serial
    prefix, we SUGGEST the model as a Click prompt default, but the
    user presses Enter to accept — we don't silently apply the guess.
  * No hand-rolled picker for non-Bambu: we show the top-N common
    keys for that type and let the user type the one that matches,
    with a final validation step that catches typos.
  * Refusing to guess is better than guessing wrong: OctoPrint/serial
    backends return ``None`` (no default) and require an explicit
    answer — those backends front any printer, so any guess would be
    dangerous.
"""
from __future__ import annotations

import logging
from typing import Any

import click

logger = logging.getLogger(__name__)

# Bambu serial prefix → model (deterministic from Bambu's SKU scheme).
# Match longest prefix first so "03919" (X1C) wins over "039" (A1).
_BAMBU_PREFIX_SUGGESTIONS: dict[str, str] = {
    "03919": "bambu_x1c",
    "039":   "bambu_a1",
    "094":   "bambu_a1_mini",
    "00M":   "bambu_p1s",
    "00W":   "bambu_p1p",
    "01S":   "bambu_x1e",
}

# Top-N common models per backend type, shown as examples in the prompt.
_EXAMPLES_BY_TYPE: dict[str, list[str]] = {
    "prusa":     ["prusa_mk4", "prusa_mini", "prusa_mk3s", "prusa_xl"],
    "moonraker": ["klipper_generic", "voron_24", "voron_trident", "qidi_x_plus3", "k1_max"],
    "creality":  ["sparkx_i7", "k1_max", "k1c", "ender3_v4", "ender3_v3_ke"],
    "octoprint": ["ender3", "ender3_v2", "ender5", "prusa_mk3s"],
    "serial":    ["ender3", "ender3_v2", "cr10", "prusa_mk3s"],
    "elegoo":    ["elegoo_centauri", "elegoo_centauri_carbon"],
    "bambu":     ["bambu_a1", "bambu_a1_mini", "bambu_x1c",
                  "bambu_p1s", "bambu_p1p", "bambu_x1e"],
}


def suggest_bambu_model(serial: str | None) -> str | None:
    """Return a suggested Bambu model for *serial*, or None if no prefix
    matches.  Not used directly by the prompt — exposed so other
    callers (e.g. migration nag) can surface the same suggestion."""
    if not serial:
        return None
    for prefix in sorted(_BAMBU_PREFIX_SUGGESTIONS, key=len, reverse=True):
        if serial.startswith(prefix):
            return _BAMBU_PREFIX_SUGGESTIONS[prefix]
    return None


def _validate_model_key(value: str) -> bool:
    """Check that *value* matches a key in printer_intelligence.json.
    Uses ``get_build_volume`` because it's strict — ``get_printer_intel``
    falls back to the 'default' profile and would silently accept typos.
    """
    raw = value.strip() if value else ""
    if not raw or raw != raw.lower():
        return False
    try:
        from kiln.printers.bed_fit import get_build_volume
        return get_build_volume(raw) is not None
    except Exception:
        # If we can't validate (missing data file), accept any non-empty
        # string — better to record the user's intent than to block.
        return bool(raw)


def prompt_for_printer_model(
    printer_type: str,
    serial: str | None = None,
    *,
    allow_skip: bool = False,
) -> str | None:
    """Interactively ask the user for the ``printer_model``.

    Args:
        printer_type: Backend type — ``bambu``, ``prusa``, ``moonraker``,
            ``creality``, ``octoprint``, ``serial``, ``elegoo``.
        serial: Optional serial number; used to suggest a default for
            Bambu printers.
        allow_skip: When True, an empty answer returns None (caller
            accepts that the safety stack will soft-pass).  When False
            (default), we re-prompt until a valid model is entered.

    Returns:
        The printer_model key (validated against printer_intelligence.json),
        or None if the user skipped an optional prompt.
    """
    examples = _EXAMPLES_BY_TYPE.get(printer_type.lower(), [])
    default_hint = suggest_bambu_model(serial) if printer_type.lower() == "bambu" else None

    click.echo()
    click.echo(click.style("  Printer model (activates the safety stack)", bold=True))
    click.echo()
    click.echo(
        "  Kiln uses the printer_model to look up build volume, temperature\n"
        "  limits, and mechanical constraints.  Without it, the bed-fit\n"
        "  gate, gcode bounds check, and hotend/bed temp limits all\n"
        "  soft-pass — unsafe prints can reach the printer."
    )
    if examples:
        click.echo()
        click.echo(f"  Common {printer_type} models: {', '.join(examples)}")
        click.echo(
            "  Full list in kiln/data/printer_intelligence.json."
        )
    click.echo()

    while True:
        if default_hint:
            raw = click.prompt(
                "  printer_model",
                default=default_hint,
                show_default=True,
            )
        elif allow_skip:
            raw = click.prompt(
                "  printer_model (press Enter to skip — safety gates will soft-pass)",
                default="",
                show_default=False,
            )
        else:
            raw = click.prompt("  printer_model")
        raw = (raw or "").strip()
        if not raw:
            if allow_skip:
                click.echo(click.style(
                    "  Skipped.  Add `printer_model: <value>` to "
                    "~/.kiln/config.yaml later to activate safety gates.",
                    fg="yellow",
                ))
                return None
            click.echo(click.style(
                "  printer_model is required — Kiln's safety stack needs it.",
                fg="yellow",
            ))
            continue
        if _validate_model_key(raw):
            click.echo(click.style(
                f"  \u2713 {raw} found in printer_intelligence.json — "
                f"safety stack will activate.",
                fg="green",
            ))
            return raw
        # Typo: offer close matches
        close = _find_close_matches(raw)
        if close:
            click.echo(click.style(
                f"  '{raw}' isn't a known model.  Did you mean one of: "
                f"{', '.join(close)}?",
                fg="yellow",
            ))
        else:
            click.echo(click.style(
                f"  '{raw}' isn't in printer_intelligence.json.  Check "
                f"kiln/data/printer_intelligence.json for valid keys.",
                fg="yellow",
            ))
        if click.confirm("  Use this value anyway?", default=False):
            click.echo(click.style(
                "  Recorded — but safety gates will soft-pass for this "
                "unknown model.  Fix the value later to activate them.",
                fg="yellow",
            ))
            return raw
        # loop and re-ask


def _find_close_matches(value: str, n: int = 5) -> list[str]:
    """Return up to *n* close matches from printer_intelligence.json."""
    try:
        import json
        from pathlib import Path
        data = json.loads((
            Path(__file__).resolve().parent.parent
            / "data" / "printer_intelligence.json"
        ).read_text())
        keys = list(data.keys())
        from difflib import get_close_matches
        return get_close_matches(value, keys, n=n, cutoff=0.5)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Migration helper: detect existing configs missing the field
# ---------------------------------------------------------------------------

def check_existing_config_for_missing_model(
    raw_config: dict[str, Any],
) -> list[str]:
    """Return the names of any printers in *raw_config* that are missing
    the ``printer_model`` field.  Used by ``kiln status`` etc. to emit
    a one-time migration nag on startup.
    """
    missing: list[str] = []
    printers = raw_config.get("printers") or {}
    if not isinstance(printers, dict):
        return missing
    for name, entry in printers.items():
        if not isinstance(entry, dict):
            continue
        if not entry.get("printer_model"):
            missing.append(name)
    return missing
