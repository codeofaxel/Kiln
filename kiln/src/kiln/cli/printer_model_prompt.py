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

# Bambu serial-number prefix → model.  Verified 2026-06-01 against Bambu's
# OFFICIAL find-sn wiki (wiki.bambulab.com/en/general/find-sn), page read
# directly.  The prior table was unsourced and wrong on 5 of 6 entries — most
# dangerously it mapped 094→a1_mini when 094 is actually the H2D, and
# 01S→x1e when 01S is the P1P.  All Bambu prefixes are 3 chars.
#
# Full official table (only the models Kiln has profiles for are mapped below;
# the rest are recorded for when those models are added — see
# kiln_pro device intelligence):
#   039 A1 · 030 A1 mini · 01P P1S · 01S P1P · 00M X1C · 03W X1E · 22E P2S · 26A A2L
#   094 H2D · 239 H2D Pro · 093 H2S · 31B H2C · 20P X2D
#
# This is only the PRE-CONNECTION suggestion shown at CLI setup.  Authoritative
# runtime model detection uses the MQTT product_name (see the Bambu adapter
# model-map in printers/bambu.py), which self-corrects on connect.
_BAMBU_PREFIX_SUGGESTIONS: dict[str, str] = {
    "039": "bambu_a1",
    "030": "bambu_a1_mini",
    "01P": "bambu_p1s",
    "01S": "bambu_p1p",
    "00M": "bambu_x1c",
    "03W": "bambu_x1e",
    "22E": "bambu_p2s",
    "26A": "bambu_a2l",
    "093": "bambu_h2s",
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
    click.echo(click.style("  Printer model (turns on Kiln's safety checks)", bold=True))
    click.echo()
    click.echo(
        "  Kiln uses the printer_model to look up build volume, temperature\n"
        "  limits, and mechanical constraints.  Without it, Kiln can't check\n"
        "  that prints fit the bed or stay within safe temperatures — those\n"
        "  checks are skipped, so an unsafe print could reach the printer."
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
                "  printer_model (press Enter to skip — leaves safety checks off)",
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
                    "~/.kiln/config.yaml later to turn on safety checks.",
                    fg="yellow",
                ))
                return None
            click.echo(click.style(
                "  printer_model is required — Kiln's safety checks need it.",
                fg="yellow",
            ))
            continue
        if _validate_model_key(raw):
            click.echo(click.style(
                f"  \u2713 {raw} found in printer_intelligence.json — "
                f"safety checks will turn on.",
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
                "  Recorded — but safety checks stay off for this unknown "
                "model.  Fix the value later to turn them on.",
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
