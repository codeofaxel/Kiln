"""Shared "safety gap" warning helper for MCP tool responses.

Incident #0 follow-up: the CLI got a migration nag when it detects
``printer_model`` is missing from ``~/.kiln/config.yaml``.  MCP agents
never see the CLI, so they need the same warning surfaced inline in
tool responses.

This module provides a single function :func:`safety_gap_warning`
that every safety-gated MCP tool can call.  The returned value is
``None`` when everything is fine (or when the check itself fails —
never block a legitimate response on a broken warning) and a
structured dict when the safety stack is soft-passing.

Callers attach the dict to their response under a ``safety_warning``
key:

    result = {...}
    if warn := safety_gap_warning():
        result["safety_warning"] = warn
    return result

The warning is INFORMATIONAL — it doesn't block the tool.  We don't
want to refuse to print just because the gates are soft-passing;
the refusal should come from an agent noticing the warning and
fixing the config.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def safety_gap_warning() -> dict[str, Any] | None:
    """Return a structured safety-gap warning dict, or None when the
    safety stack is fully active.

    The dict shape is::

        {
            "code": "PRINTER_MODEL_NOT_SET",
            "severity": "warning",
            "message": "...",
            "remediation": "...",
            "suggestion": "bambu_a1",    # present only for known Bambu serials
        }
    """
    try:
        from kiln.printer_model_resolver import resolve_printer_model
        model = resolve_printer_model()
        if model:
            return None

        # Model is unset.  Build the warning.
        result: dict[str, Any] = {
            "code": "PRINTER_MODEL_NOT_SET",
            "severity": "warning",
            "message": (
                "Safety stack is DEGRADED: printer_model not set in "
                "~/.kiln/config.yaml.  Bed-fit checks, gcode bounds "
                "validation, and temperature limits are all soft-passing "
                "— unsafe prints can reach the printer."
            ),
            "remediation": (
                "Ask the user which printer model they have, then add "
                "`printer_model: <value>` under the printer entry in "
                "~/.kiln/config.yaml.  Examples: bambu_a1, bambu_x1c, "
                "prusa_mk4, prusa_mini, ender3, klipper_generic.  See "
                "kiln/data/printer_intelligence.json for the full list "
                "of valid keys."
            ),
        }

        # Best-effort Bambu serial suggestion — doesn't AUTO-APPLY
        # (user explicitly rejected silent inference), but surfacing
        # a suggestion is a convenience for the agent to propose to
        # the user when they ask what to fill in.
        try:
            from kiln.cli.printer_model_prompt import suggest_bambu_model
            import kiln.server as _srv
            if _srv._PRINTER_TYPE == "bambu" and _srv._PRINTER_SERIAL:
                suggestion = suggest_bambu_model(_srv._PRINTER_SERIAL)
                if suggestion:
                    result["suggestion"] = suggestion
                    result["remediation"] += (
                        f"  For this printer specifically, `printer_model: "
                        f"{suggestion}` matches the serial prefix — confirm "
                        f"with the user before applying."
                    )
        except Exception:
            pass

        return result
    except Exception as exc:
        logger.debug("safety_gap_warning check failed: %s", exc)
        return None


def attach_safety_warning(response: dict[str, Any]) -> dict[str, Any]:
    """Convenience: attach the warning to *response* under
    ``safety_warning`` when applicable, return the (mutated) response.

    Safe no-op when response is None or the warning is absent."""
    if not isinstance(response, dict):
        return response
    warn = safety_gap_warning()
    if warn:
        response["safety_warning"] = warn
    return response
