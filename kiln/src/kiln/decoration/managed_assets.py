"""Recognise a MANAGED decoration asset handed to the raw carve tool.

Kiln stores the artwork behind a saved decoration preset content-addressed
under ``~/.kiln/decoration_assets/`` as ``<prefix>.<sha16><ext>`` (see
:func:`kiln.asset_store.persist_asset`).  Those files are not ordinary user
content: each one is the image half of a preset that ALSO recorded a depth,
a surface selection, a pattern family and a carve mode.

``decorate_surface`` accepts any path, so passing one of those files works —
and produces a carve that uses none of the settings the preset recorded.
That is the failure this module exists to make visible: an agent that cannot
find the preset-apply door falls back to carving the preset's raw asset with
hand-chosen parameters, the call succeeds, and the result is described as
"the preset's settings" when it is nothing of the kind.  Nothing errored, so
nothing caught it.

Detection here is deliberately STRUCTURAL and dependency-free — the path
convention is public Kiln's own — so the warning survives with or without
kiln-pro installed.  When kiln-pro IS present it enriches the finding with
the actual preset name and recorded settings, so the warning can name the
drift instead of just flagging the file.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

# persist_asset's naming contract: "<prefix>.<sha256[:16]><ext>".
_MANAGED_NAME = re.compile(
    r"^(?P<prefix>[A-Za-z0-9_-]+)\.(?P<sha16>[0-9a-f]{16})(?P<ext>\.[A-Za-z0-9]+)?$"
)

_ASSET_DIRNAME = "decoration_assets"


def _decoration_assets_dir() -> str | None:
    """Absolute path of the managed decoration-asset store, or None."""
    try:
        from kiln.asset_store import kiln_root

        return os.path.join(os.path.abspath(kiln_root()), _ASSET_DIRNAME)
    except Exception:  # noqa: BLE001 — detection never breaks a decoration
        logger.debug("could not resolve decoration asset dir", exc_info=True)
        return None


def _preset_lineage(sha16: str) -> dict[str, Any] | None:
    """Ask kiln-pro which preset owns this asset.  None when unavailable."""
    try:
        from kiln_pro.decoration.preset_lineage import (  # type: ignore[import]
            lineage_for_asset_sha16,
        )

        return lineage_for_asset_sha16(sha16)
    except ImportError:
        return None
    except Exception:  # noqa: BLE001 — enrichment is best-effort
        logger.debug("preset lineage lookup failed", exc_info=True)
        return None


def describe_managed_asset(content: str | None) -> dict[str, Any] | None:
    """Return a lineage record when *content* is a managed preset asset.

    :param content: ``decorate_surface``'s ``content`` argument — a file
        path, a ``"text:..."`` string, or anything else.
    :returns: ``None`` for ordinary content (the overwhelmingly common
        case — a user's own image, a text spec).  Otherwise a dict with
        ``asset_sha16``, ``asset_path``, a human-readable ``warning``, and
        — when kiln-pro is installed and the asset resolves to a preset —
        ``preset`` carrying that preset's identifiers and recorded
        settings.
    """
    if not content or not isinstance(content, str):
        return None
    if content.startswith("text:"):
        return None

    try:
        abs_path = os.path.abspath(os.path.expanduser(content))
    except Exception:  # noqa: BLE001 — a malformed path is simply not managed
        return None

    assets_dir = _decoration_assets_dir()
    if not assets_dir or os.path.dirname(abs_path) != assets_dir:
        return None

    match = _MANAGED_NAME.match(os.path.basename(abs_path))
    if match is None:
        return None

    sha16 = match.group("sha16")
    record: dict[str, Any] = {
        "asset_sha16": sha16,
        "asset_path": abs_path,
    }

    lineage = _preset_lineage(sha16)
    if lineage:
        record["preset"] = lineage
        name = lineage.get("name") or "a saved preset"
        record["warning"] = (
            f"This is the stored artwork of {name!r} (a saved decoration "
            f"preset), carved here with the parameters you passed rather "
            f"than the ones the preset recorded. To apply the preset "
            f"itself — its own depth, surface selection and mode — call "
            f"apply_decoration_preset(preset_id="
            f"{lineage.get('preset_id')!r}, host_mesh_path=...). Describe "
            f"this result as the preset's settings only if you passed them."
        )
        drift = lineage.get("recorded_settings") or {}
        if drift:
            record["preset_recorded_settings"] = drift
    else:
        record["warning"] = (
            "This file is a managed decoration asset — the stored artwork "
            "of a saved decoration preset, which also recorded a depth, "
            "mode and surface selection not used by this call. Prefer the "
            "preset-apply tool (apply_decoration_preset) over carving the "
            "raw asset, and do not describe this result as the preset's "
            "recorded settings."
        )
    return record


__all__ = ["describe_managed_asset"]
