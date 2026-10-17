"""Public-Kiln → Kiln's servers: how much weight one printed part can carry.

The load figure is Kiln Pro (https://kiln3d.com/pricing), and it is worked
out by kiln-pro for the one part asked about: the material, the part's
load-bearing cross-section, the length of the arm and which way the load
pulls the layers go in, and the figure for that part comes back with what it
assumes and the caution that goes with it.  Nothing about the material's own
figures is sent to this computer, and nothing is kept here.

Asked in one of two places, in order:

1. **kiln-pro on this computer, with its catalogue** (and the hosted server,
   which is one): answered here by the same function Kiln's servers run
   (imported by name in :func:`_local`, never by a string, so a static
   reader of the call graph sees the crossing).
2. **served**: Kiln's servers answer through :data:`WIRE_TOOL`, asked through
   the door every served tool uses (:func:`kiln.server._pro_api_call`).

No answer is a :class:`~kiln.served_answer.Miss` (offline, signed out, no
answer, or refused).  A load figure is a verdict, so the door that asked
words the miss to say the part is unchecked, never that it holds.  Nothing
here raises.
"""

from __future__ import annotations

import logging
from typing import Any

from kiln.served_answer import Miss, classify_answer, classify_transport_error

logger = logging.getLogger(__name__)

#: The served tool that answers for one part.
WIRE_TOOL = "estimate_load_figure"
#: Where the same answer is read from when kiln-pro IS installed here.
LOCAL_MODULE = "kiln_pro.load_capacity"
#: A miss with nothing asked of Kiln's servers: kiln-pro on this computer failed.
_NOT_ASKED = "NOT_ASKED"
#: The hosted boundary's own exception for a read below the record's tier
#: (kiln-pro's ``HostedIntelligencePolicyError``), known by name and wording
#: because public Kiln cannot import the type.
_TIER_REFUSAL = "not entitled"
_PRO_REQUIRED = "The load figure for a part is part of Kiln Pro."


def _local_available() -> bool:
    """True when kiln-pro here can answer: installed WITH its catalogue file
    (a pip-installed kiln-pro has the reader and no file)."""
    try:
        from kiln import _pro_material_bridge

        return _pro_material_bridge.available()
    except Exception:  # noqa: BLE001 -- the served door
        return False


def load_figure(
    material: str,
    cross_section_mm2: float,
    cantilever_length_mm: float,
    *,
    load_across_layers: bool = True,
) -> tuple[dict[str, Any] | None, Miss | None]:
    """The load figure for ONE part.

    ``(estimate, None)`` when kiln-pro sized the part -- the answer's own
    fields, ``limit_mode: "deflection"`` marking an elastomer's
    serviceability limit; ``(None, None)`` when Kiln has no load figures for
    *material* (a settled answer); ``(None, miss)`` when nothing answered.
    Never raises.
    """
    if _local_available():
        local = _local(material, cross_section_mm2, cantilever_length_mm, load_across_layers)
        if local is not None:
            return local
        # kiln-pro here predates the load module: ask the servers instead.
    return _served(material, cross_section_mm2, cantilever_length_mm, load_across_layers)


def _read(answer: Any) -> tuple[dict[str, Any] | None, Miss | None]:
    if isinstance(answer, dict) and answer.get("success") is True and answer.get("status") == "ok":
        if answer.get("known") is False:
            return None, None
        estimate = answer.get("estimate")
        if answer.get("known") is True and isinstance(estimate, dict):
            return dict(estimate), None
        return None, Miss("unanswered", detail="an answer with no figure in it")
    return None, classify_answer(answer) or Miss("unanswered", detail="an answer that did not say it succeeded")


def _local(
    material: str, cross_section_mm2: float, cantilever_length_mm: float, load_across_layers: bool,
) -> tuple[dict[str, Any] | None, Miss | None] | None:
    """kiln-pro's answer on this computer, or ``None`` when its kiln-pro
    has no load module to ask."""
    try:
        from kiln_pro.load_capacity import answer as local_answer
    except ImportError:
        return None
    try:
        out = local_answer(
            material, cross_section_mm2, cantilever_length_mm, load_across_layers=load_across_layers,
        )
    except Exception as exc:  # noqa: BLE001 -- a door never fails for want of the figure
        if type(exc).__name__ == "HostedIntelligencePolicyError" and _TIER_REFUSAL in str(exc):
            # On the hosted server the caller's plan is below the figure's:
            # a refusal, worded as one, never "kiln-pro stopped".
            return None, Miss("refused", "TIER_REQUIRED", _PRO_REQUIRED)
        logger.debug("load figure on this computer failed", exc_info=True)
        return None, Miss("unanswered", _NOT_ASKED, f"kiln-pro on this computer stopped: {exc}"[:400])
    return _read(out)


def _served(
    material: str, cross_section_mm2: float, cantilever_length_mm: float, load_across_layers: bool,
) -> tuple[dict[str, Any] | None, Miss | None]:
    try:
        from kiln.server import _pro_api_call
    except Exception:  # noqa: BLE001
        return None, Miss("unanswered", _NOT_ASKED, "this install could not open its connection to Kiln's servers")
    try:
        answer = _pro_api_call(
            WIRE_TOOL,
            material=material,
            cross_section_mm2=float(cross_section_mm2),
            cantilever_length_mm=float(cantilever_length_mm),
            load_across_layers=bool(load_across_layers),
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a miss to word, never a traceback
        logger.debug("load figure not served", exc_info=True)
        return None, classify_transport_error(exc)
    return _read(answer)


__all__ = ["LOCAL_MODULE", "WIRE_TOOL", "load_figure"]
