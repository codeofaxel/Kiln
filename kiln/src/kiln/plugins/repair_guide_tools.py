"""Repair-guide plugin -- the maker's own maintenance guide, one step at a time.

``repair_guide`` walks a person through the printer maker's published
procedure (a cutter blade, a nozzle, a PTFE tube) the way ``home_axes``
walks a homing sequence: ``plan_only`` returns the header and the step
titles, ``step=N`` returns ONE step -- the maker's words, the maker's
picture as a URL with attribution, the maker's own check where the page
has one, and what the next step is.  Kiln's own words appear only where
the picture cannot say it.

Step 0 is ALWAYS the maker's power-off warning, verbatim, and every step
after it requires ``power_off_confirmed=True`` -- a PERSON's statement that
the printer is off and unplugged, never set on their behalf.  A step asked
for without it is refused, and the refusal itself carries step 0, so the
warning is served before anything is unscrewed however the door was
knocked on.  Where the maker's page has no power-off sentence (a cold
pull, a screen-driven tramming -- jobs the maker runs powered) Kiln does
not invent one: step 0 says so in Kiln's words, marked as Kiln's, and
rides with every step of that guide instead of gating them.

The guides live in kiln-pro (``kiln._pro_guide_bridge``); public Kiln keeps
the mechanism and a tiny map of each maker's public maintenance index per
model, so an install without kiln-pro still answers honestly: the maker's
own index page where Kiln knows it, otherwise "your maker publishes no
guide Kiln can step through".  Pictures are never fetched, proxied or
cached: the maker's server serves them from the URL in the answer.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins`.  The
tool body is a module-level function so ``kiln machine repair-guide`` runs the very same
code the MCP tool runs.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)

#: Each maker's PUBLIC maintenance index, per public printer id.  A link to
#: the maker's own page for the caller's own machine is a funnel, not a
#: leak: it carries no step, no picture and no code mapping, and it is the
#: honest answer when kiln-pro is not installed or has no guide for a
#: topic.  Every URL was fetched and answered 200 in-session on 2026-09-17
#: (Bambu: curl; Prusa: read in the browser).  Per model where the maker
#: publishes per model; the vendor's landing page otherwise.
MAKER_MAINTENANCE_INDEX: dict[str, dict[str, str]] = {
    "bambu": {
        "_maker": "Bambu Lab",
        "_fallback": "https://wiki.bambulab.com/en/home",
        "bambu_a1_mini": "https://wiki.bambulab.com/en/a1-mini/maintenance",
        "bambu_a1": "https://wiki.bambulab.com/en/a1/maintenance",
        "bambu_p1p": "https://wiki.bambulab.com/en/p1/maintenance",
        "bambu_p1s": "https://wiki.bambulab.com/en/p1/maintenance",
        "bambu_x1c": "https://wiki.bambulab.com/en/x1/maintenance",
        "bambu_x1e": "https://wiki.bambulab.com/en/x1/maintenance",
        "bambu_h2d": "https://wiki.bambulab.com/en/h2/maintenance",
        "bambu_h2d_pro": "https://wiki.bambulab.com/en/h2/maintenance",
        "bambu_h2s": "https://wiki.bambulab.com/en/h2/maintenance",
        "bambu_h2c": "https://wiki.bambulab.com/en/h2/maintenance",
    },
    "prusa": {
        "_maker": "Prusa Research",
        "_fallback": "https://help.prusa3d.com/en",
        "prusa_mk4": "https://help.prusa3d.com/product/mk4s/printer-maintenance_247",
        "prusa_mini": "https://help.prusa3d.com/product/mini-2/printer-maintenance_247",
        "prusa_xl": "https://help.prusa3d.com/product/xl-plus/printer-maintenance_247",
        "prusa_mk3s": "https://help.prusa3d.com/product/mk3s-plus/printer-maintenance_247",
    },
}

NO_GUIDE_SENTENCE = "your maker publishes no guide Kiln can step through"


def _model_key(printer_id: str) -> str:
    return str(printer_id or "").strip().lower().replace("-", "_").replace(" ", "_")


def _vendor_for(printer_id: str) -> str | None:
    key = _model_key(printer_id)
    for vendor in MAKER_MAINTENANCE_INDEX:
        if key.startswith(vendor + "_") or key == vendor:
            return vendor
    return None


def maker_index(printer_id: str) -> tuple[str | None, str | None]:
    """``(maker name, index URL)`` public Kiln knows for this model, or ``(None, None)``."""
    vendor = _vendor_for(printer_id)
    if vendor is None:
        return None, None
    table = MAKER_MAINTENANCE_INDEX[vendor]
    url = table.get(_model_key(printer_id)) or table.get("_fallback")
    return table.get("_maker"), url


def _resolve_printer_id(printer_id: str, printer_name: str | None) -> str:
    """The model to look up: an explicit id, else the named printer's declared model."""
    if str(printer_id or "").strip():
        return str(printer_id).strip()
    from kiln.printer_model_resolver import resolve_printer_model_for

    declared = resolve_printer_model_for(printer_name or None)
    if declared:
        from kiln.printer_profile_ids import map_printer_hint_to_profile_id

        return map_printer_hint_to_profile_id(declared) or declared
    return ""


def _display_name(printer_id: str) -> str:
    try:
        from kiln.printer_intelligence import get_printer_intel

        return get_printer_intel(printer_id).display_name
    except Exception:  # noqa: BLE001 -- a name is decoration
        return printer_id


def _maker_of(guide: dict[str, Any], printer_id: str) -> str:
    vendor = str(guide.get("vendor") or _vendor_for(printer_id) or "").strip()
    table = MAKER_MAINTENANCE_INDEX.get(vendor, {})
    return table.get("_maker") or (vendor.title() if vendor else "the maker")


def _step_zero(guide: dict[str, Any], maker: str) -> dict[str, Any]:
    """Step 0: the maker's power-off sentence, verbatim, where the page has one.

    The table keeps the maker's power-off sentence under its own key and
    the maker's other page-level cautions under ``warnings``; both are the
    maker's words and both ride here.  Where the maker's page has NO
    power-off sentence (a cold pull, an unclog, a screen-driven tramming --
    jobs the maker runs powered) Kiln does not invent one: step 0 says so,
    in Kiln's words and marked as Kiln's, and the printer is not required
    to be off.
    """
    others = [w for w in guide.get("warnings") or [] if isinstance(w, str) and w.strip()]
    power_off = guide.get("power_off_warning")
    if isinstance(power_off, str) and power_off.strip():
        return {
            "step": 0,
            "title": "Power off the printer",
            "text": power_off.strip(),
            "also": others,
            "maker_words": True,
            "printer_must_be_off": True,
        }
    return {
        "step": 0,
        "title": "Before you start",
        "text": (
            f"{maker}'s page for this job has no power-off sentence: the maker runs it with the "
            "printer on. Kiln's own note, not the maker's: keep your hands clear of the hot end and "
            "of anything that moves, and stop if the printer does something the step did not describe."
        ),
        "also": others,
        "maker_words": False,
        "printer_must_be_off": False,
    }


def _header(slug: str, guide: dict[str, Any], printer_id: str, start: int) -> dict[str, Any]:
    maker = _maker_of(guide, printer_id)
    return {
        "printer_id": printer_id,
        "printer": _display_name(printer_id),
        "maker": maker,
        "guide": slug,
        "topic": guide.get("topic"),
        "maker_page_title": guide.get("source_title"),
        "maker_page_url": guide.get("source_url"),
        "applies_also_to": guide.get("applies_also_to"),
        "tools": list(guide.get("tools") or []),
        "interval": guide.get("interval"),
        "parts": list(guide.get("parts") or []),
        "fetched_at": guide.get("fetched_at"),
        "printer_must_be_off": bool(str(guide.get("power_off_warning") or "").strip()),
        "step_count": len(guide.get("steps") or []),
        "start_at_step": start,
        "image_note": (
            f"Pictures are {maker}'s, served from {maker}'s own site by URL at answer time; "
            "Kiln does not copy, proxy or cache them."
        ),
    }


def _step_block(guide: dict[str, Any], n: int, *, maker: str, source_url: str | None) -> dict[str, Any] | None:
    steps = [s for s in guide.get("steps") or [] if isinstance(s, dict)]
    by_n = {int(s.get("n", 0)): s for s in steps}
    step = by_n.get(n)
    if step is None:
        return None
    block: dict[str, Any] = {
        "step": n,
        "title": step.get("title"),
        "do": step.get("do"),
    }
    warning = step.get("warning")
    if isinstance(warning, str) and warning.strip():
        block["warning"] = warning
    image_url = step.get("image_url")
    if isinstance(image_url, str) and image_url.strip():
        block["image_url"] = image_url
        block["image_alt"] = step.get("image_alt")
        block["image_attribution"] = f"Image: {maker}, {source_url}" if source_url else f"Image: {maker}"
    else:
        block["no_image"] = True
        # Kiln's own words, only where the picture cannot say it.
        block["kiln_note"] = f"{maker}'s page has no picture for this step; follow the words."
    verify = step.get("verify")
    if isinstance(verify, str) and verify.strip():
        block["verify"] = verify
    following = by_n.get(n + 1)
    block["next_step"] = {"step": n + 1, "title": following.get("title")} if following else None
    block["done"] = following is None
    return block


def run_repair_guide(
    *,
    printer_id: str = "",
    topic: str = "",
    hms_code: str = "",
    step: int = 0,
    plan_only: bool = False,
    power_off_confirmed: bool = False,
    printer_name: str | None = None,
    guide: str = "",
) -> dict[str, Any]:
    """The one door every surface calls."""
    import kiln.server as _srv
    from kiln import _pro_guide_bridge as bridge
    from kiln.printer_intelligence import extract_codes

    model = _resolve_printer_id(printer_id, printer_name)
    if not model:
        return _srv._error_dict(
            "Say which printer: pass printer_id (e.g. bambu_a1_mini), or name a registered printer "
            "whose config declares printer_model.",
            code="PRINTER_MODEL_REQUIRED", extra={"printer_model_required": True},
        )
    # A code anywhere in ``topic`` is read as the fault code it is.
    codes = list(extract_codes(hms_code)) + list(extract_codes(topic))
    code = codes[0].replace("_", "") if codes else ""
    if not topic.strip() and not code and not guide.strip():
        return _srv._error_dict(
            "Say what to repair: a topic (cutter, nozzle, hotend, ptfe_tube, extruder_gears, belts, "
            "bed_level, lubrication, firmware_recovery, filament_sensor) or the fault code on the screen.",
            code="INVALID_INPUT",
        )
    maker_name, index_url = maker_index(model)
    found = bridge.find_guide(model, topic=topic, code=code, guide=guide.strip())
    if found is not None and "choices" in found:
        # More than one of the maker's guides answers this topic for this
        # model -- two machines behind one printer id, or two pages for one
        # job.  Never a silent first pick: the person says which, by slug.
        choices = found["choices"]
        return {
            "success": True,
            "printer_id": model,
            "printer": _display_name(model),
            "topic": topic or None,
            "guide": None,
            "choices": choices,
            "kiln_note": (
                f"{len(choices)} of the maker's guides match this topic for this model. Ask which one "
                "applies (machine is the maker's own product name for the page; section is the "
                "maker's page section), then call again with guide=<the chosen slug>."
            ),
        }
    if found is None:
        cover = bridge.coverage(model)
        payload: dict[str, Any] = {
            "success": True,
            "printer_id": model,
            "printer": _display_name(model),
            "guide": None,
            "topic": topic or None,
            "coverage": cover,
        }
        if code:
            payload["hms_code"] = code
        if cover and cover.get("count"):
            payload["maker"] = cover.get("maker")
            payload["kiln_note"] = (
                f"{cover.get('maker')} publishes {cover.get('count')} guide(s) Kiln can step through for this "
                f"model ({', '.join(cover.get('topics') or [])}), none for this topic."
            )
        elif maker_name and index_url:
            payload["maker"] = maker_name
            payload["maker_index_url"] = index_url
            payload["kiln_note"] = (
                f"{NO_GUIDE_SENTENCE}. {maker_name}'s own maintenance index for this model is at maker_index_url."
            )
        else:
            payload["kiln_note"] = f"{NO_GUIDE_SENTENCE}."
        return payload

    slug, guide, start = found["slug"], found["guide"], found["start_at_step"]
    header = _header(slug, guide, model, start)
    if code:
        header["hms_code"] = code
    maker = header["maker"]
    zero = _step_zero(guide, maker)
    if plan_only:
        return {
            "success": True,
            **header,
            "step_zero": zero,
            "steps": [
                {"step": int(s.get("n", 0)), "title": s.get("title")}
                for s in guide.get("steps") or [] if isinstance(s, dict)
            ],
            # Two audiences, two fields: the person hears plain words; the
            # agent driving the tool reads which argument carries their word.
            "how_to_step": (
                "Power off the printer and unplug it, then say so; Kiln then gives one step at a "
                "time and names the next one after each."
            ),
            "agent_note": (
                f"When the person says the printer is off and unplugged, call repair_guide(step={start}, "
                "power_off_confirmed=True); never set that argument on their behalf."
            ),
        }
    n = int(step or 0)
    if n < 0 or n > header["step_count"]:
        return _srv._error_dict(
            f"This guide has steps 0 to {header['step_count']}; step {n} does not exist.",
            code="INVALID_INPUT", extra={"step_count": header["step_count"]},
        )
    if n == 0:
        return {
            "success": True,
            **header,
            **zero,
            "next_step": {
                "step": start,
                "title": next((s.get("title") for s in guide.get("steps") or [] if isinstance(s, dict) and int(s.get("n", 0)) == start), None),
                "requires": "the person's word that the printer is off and unplugged",
                "agent_note": "pass power_off_confirmed=True only once the person has said so",
            },
            "done": False,
        }
    if header["printer_must_be_off"] and not power_off_confirmed:
        # The maker says power off first; a person confirms it, never Kiln.
        # The refusal carries step 0 itself, so the warning is served before
        # any step however the door was knocked on.
        return _srv._error_dict(
            f"Before step {n}: power off the printer and unplug it, then say so. "
            f"{maker}'s warning, verbatim: {zero['text']}",
            code="POWER_OFF_REQUIRED",
            extra={
                "power_off_required": True, "step_zero": zero, "guide": slug, "printer_id": model,
                "agent_note": (
                    "Ask the person; when they say the printer is off and unplugged, call again with "
                    "power_off_confirmed=True. Never set it on their behalf."
                ),
            },
        )
    block = _step_block(guide, n, maker=maker, source_url=header.get("maker_page_url"))
    if block is None:
        return _srv._error_dict(f"Step {n} is not in this guide.", code="INVALID_INPUT")
    answer = {"success": True, **header, **block}
    if not header["printer_must_be_off"]:
        # No power-off gate to pass through, so step 0 rides with every step:
        # the maker's cautions are read before the step, not instead of it.
        answer["step_zero"] = zero
    return answer


def repair_guide(
    printer_id: str = "",
    topic: str = "",
    hms_code: str = "",
    step: int = 0,
    plan_only: bool = False,
    power_off_confirmed: bool = False,
    printer_name: str = "",
    guide: str = "",
) -> dict[str, Any]:
    """Walk through the printer maker's own repair guide, one step per call.

    The guide is the maker's published page for THIS model, reproduced in the
    maker's order: their power-off warning first, their numbered steps in
    their words, their picture for each step (as a URL, served from their
    site with attribution -- Kiln never copies it), and their own check at
    the end where the page has one.  Kiln adds words only where the picture
    cannot say it.  Where the maker states them, the header carries the
    tools, the interval ("every 10-15 spools") and the parts.

    **Run it in steps, with the person at the machine.**  ``plan_only=True``
    returns the header (maker, page title and URL, the maker's own "also
    applies to" sentence, tools, interval, parts) plus step 0 and the step
    titles.  ``step=0`` is ALWAYS the maker's power-off warning, verbatim.
    ``step=N`` returns only step N and names step N+1.  Every step from 1 on
    requires ``power_off_confirmed=True`` -- a PERSON's statement that the
    printer is off and unplugged; without it the call is refused with
    ``POWER_OFF_REQUIRED`` and the warning itself.  Never set it on a
    person's behalf.  A person who is already partway in (the screw is out)
    skips ahead with ``step``; Kiln never assumes where they are.

    Where Kiln has no guide for the topic it says so: the maker's own
    maintenance index for this model where Kiln knows it (``maker_index_url``),
    otherwise "your maker publishes no guide Kiln can step through".  No
    generic procedure is ever presented as the maker's.

    Args:
        printer_id: Printer model identifier (e.g. ``bambu_a1_mini``).  Omit
            to use the declared ``printer_model`` of ``printer_name`` (or the
            default printer).
        topic: What to repair -- ``cutter``, ``nozzle``, ``hotend``,
            ``ptfe_tube``, ``extruder_gears``, ``belts``, ``bed_level``,
            ``lubrication``, ``firmware_recovery``, ``filament_sensor``.  A
            fault code written here is read as one.
        hms_code: The code on the printer's screen (``"1200-8001"``, any
            separators); where it maps to a guide, that guide is chosen and
            the answer says which step to start at.
        step: Which step to serve (0 = the power-off warning).  Ignored with
            ``plan_only``.
        plan_only: Return the header and step titles; serve no step.
        power_off_confirmed: A PERSON has powered the printer off and
            unplugged it.  Required for every ``step >= 1``.
        printer_name: Which registered printer's declared model to use when
            ``printer_id`` is omitted.
        guide: A guide's slug, from a ``choices`` answer.  When a topic
            matches more than one of the maker's guides for this model (one
            Kiln printer id can cover two machines; a maker often has two
            pages for one job) the answer lists them and nothing is picked
            for the person -- ask, then pass the chosen slug here.
    """
    import kiln.server as _srv

    if err := _srv._check_auth("intel"):
        return err
    try:
        return run_repair_guide(
            printer_id=printer_id, topic=topic, hms_code=hms_code, step=step,
            plan_only=plan_only, power_off_confirmed=power_off_confirmed,
            printer_name=printer_name or None, guide=guide,
        )
    except Exception as exc:
        _logger.exception("Unexpected error in repair_guide")
        return _srv._error_dict(f"Unexpected error in repair_guide: {exc}", code="INTERNAL_ERROR")


def coverage_line(printer_id: str) -> tuple[str, bool]:
    """``(detail, warn)`` for the ``kiln doctor`` ``repair_guides`` line.

    Reads the same bridge the tool reads, so doctor never promises a guide
    the tool would not serve.
    """
    from kiln import _pro_guide_bridge as bridge

    maker_name, index_url = maker_index(printer_id)
    cover = bridge.coverage(printer_id) if printer_id else None
    if cover and cover.get("count"):
        topics = ", ".join(cover.get("topics") or [])
        return (
            f"{cover.get('maker')}: {cover.get('count')} guide(s) for this model Kiln can step through "
            f"({topics}) -- repair_guide (kiln machine repair-guide <topic|code> --plan, then --step N)"
        ), False
    if maker_name and index_url:
        return (
            f"{NO_GUIDE_SENTENCE}; {maker_name}'s own maintenance index for this model is {index_url}"
            + ("" if cover is not None else " (Kiln Pro adds the step-by-step walkthroughs)")
        ), True
    return f"{NO_GUIDE_SENTENCE}", True


class _RepairGuideToolsPlugin:
    """The maker's repair guides, one step at a time.

    Tools:
        - repair_guide
    """

    @property
    def name(self) -> str:
        return "repair_guide_tools"

    @property
    def description(self) -> str:
        return "Walk through the printer maker's own repair guide, one step and one picture at a time"

    def register(self, mcp: Any) -> None:
        from kiln.tool_annotations import read_only

        mcp.tool(annotations=read_only("Repair guide"))(repair_guide)


plugin = _RepairGuideToolsPlugin()
