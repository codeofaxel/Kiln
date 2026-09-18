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
guide Kiln can step through".  Pictures are never proxied or cached: the
maker's server serves them from the URL in the answer.  Before a step is
shown, Kiln asks the maker's server whether the picture is still there
(one HEAD request, a short timeout, remembered for the process): a
picture that is gone is not offered with confidence -- the step says so
and points at the maker's page instead -- and a server Kiln cannot reach
right now leaves the picture in place, marked unverified.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins`.  The
tool body is a module-level function so ``kiln machine repair-guide`` runs the very same
code the MCP tool runs.
"""

from __future__ import annotations

import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_logger = logging.getLogger(__name__)

#: Each maker's PUBLIC landing page, one per maker -- the honest answer when
#: kiln-pro is not installed.  A link to the maker's own site is a funnel,
#: not a leak, and this is deliberately no more than that: WHICH of the
#: maker's pages is the maintenance index for WHICH model is curated work
#: and lives in kiln-pro, served for the caller's own model.  Every URL
#: here answered 200 in-session on 2026-09-17.  Creality's public ids carry
#: no vendor prefix (k1, ender3, cr10), so each maker names the id prefixes
#: it answers for.
MAKER_LANDING: dict[str, dict[str, str]] = {
    "bambu": {"_maker": "Bambu Lab", "_prefixes": "bambu_", "_fallback": "https://wiki.bambulab.com/en/home"},
    "prusa": {"_maker": "Prusa Research", "_prefixes": "prusa_", "_fallback": "https://help.prusa3d.com/en"},
    "elegoo": {"_maker": "Elegoo", "_prefixes": "elegoo_", "_fallback": "https://wiki.elegoo.com/fdm-printers"},
    "qidi": {"_maker": "QIDI", "_prefixes": "qidi_", "_fallback": "https://wiki.qidi3d.com/en/home"},
    "creality": {"_maker": "Creality", "_prefixes": "creality_,k1,k2,ender3,ender5,cr10,sparkx", "_fallback": "https://wiki.creality.com/en/home"},
    "aon3d": {"_maker": "AON3D", "_prefixes": "aon3d_,aon_", "_fallback": "https://docs.aon3d.com/"},
    "visionminer": {"_maker": "Vision Miner", "_prefixes": "visionminer_,vision_miner_", "_fallback": "https://wiki.visionminer.com/docs"},
    # The gap-fill reading (2026-09-18): every remaining maker Kiln supports,
    # plus the three projects whose own docs stand in for a maker's.  Each
    # URL answered 200 in-session that day.
    "intamsys": {"_maker": "INTAMSYS", "_prefixes": "intamsys_", "_fallback": "https://help.intamsys.com/en/home"},
    "ankermake": {"_maker": "AnkerMake", "_prefixes": "anker_,ankermake_", "_fallback": "https://support.ankermake.com/s/"},
    "flashforge": {"_maker": "Flashforge", "_prefixes": "flashforge_", "_fallback": "https://wiki.flashforge.com/en/home"},
    "sovol": {"_maker": "Sovol", "_prefixes": "sovol_", "_fallback": "https://wiki.sovol3d.com/en/HOME"},
    "artillery": {"_maker": "Artillery", "_prefixes": "artillery_", "_fallback": "https://www.artillery3d.com/pages/support"},
    "voron": {"_maker": "Voron Design", "_prefixes": "voron_", "_fallback": "https://docs.vorondesign.com/"},
    "ratrig": {"_maker": "Rat Rig", "_prefixes": "ratrig_", "_fallback": "https://wiki.ratrig.com/"},
    "klipper": {"_maker": "Klipper", "_prefixes": "klipper_", "_fallback": "https://www.klipper3d.org/"},
}
#: Kept under the old name for the doctor and the tests.
MAKER_MAINTENANCE_INDEX = MAKER_LANDING

NO_GUIDE_SENTENCE = "your maker publishes no guide Kiln can step through"


def _model_key(printer_id: str) -> str:
    return str(printer_id or "").strip().lower().replace("-", "_").replace(" ", "_")


def _vendor_for(printer_id: str) -> str | None:
    key = _model_key(printer_id)
    for vendor, table in MAKER_MAINTENANCE_INDEX.items():
        for prefix in table["_prefixes"].split(","):
            if key.startswith(prefix) or key == prefix.rstrip("_"):
                return vendor
    return None


def maker_index(printer_id: str) -> tuple[str | None, str | None]:
    """``(maker name, landing URL)`` public Kiln knows for this model's maker, or ``(None, None)``."""
    vendor = _vendor_for(printer_id)
    if vendor is None:
        return None, None
    table = MAKER_MAINTENANCE_INDEX[vendor]
    return table.get("_maker"), table.get("_fallback")


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
            "Kiln does not copy, proxy or cache them. Each step says whether its picture was "
            "confirmed reachable, could not be checked, or is gone from the maker's site."
        ),
    }


#: A browser's user agent: some makers' wikis answer a bare client with 402.
_PICTURE_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
_PICTURE_TIMEOUT_S = 3.0
_picture_status_cache: dict[str, str] = {}


def _probe_picture(url: str) -> str:
    """``"live"``, ``"gone"`` or ``"unverified"`` for the maker's picture at *url*.

    One HEAD request with a browser user agent.  ``gone`` only on the
    answers that mean the picture is no longer there (404, 410); anything
    else that is not a 2xx -- a timeout, no network, a bot wall -- is
    ``unverified``, because Kiln not reaching a server is not the same as
    the maker having removed the picture.  Set ``KILN_REPAIR_GUIDE_PROBE=0``
    to skip the request (air-gapped installs; the answer then says
    unverified).
    """
    if os.environ.get("KILN_REPAIR_GUIDE_PROBE", "1") == "0":
        return "unverified"
    try:
        # A maker may name a picture in its own language; the wire wants it percent-encoded.
        parts = urllib.parse.urlsplit(url)
        ascii_url = urllib.parse.urlunsplit((
            parts.scheme, parts.netloc.encode("idna").decode("ascii"),
            urllib.parse.quote(parts.path, safe="/%:@+,;=~"),
            urllib.parse.quote(parts.query, safe="=&%+/:@?,;~"), "",
        ))
        request = urllib.request.Request(ascii_url, method="HEAD", headers={"User-Agent": _PICTURE_UA})
        with urllib.request.urlopen(request, timeout=_PICTURE_TIMEOUT_S) as response:  # noqa: S310 -- maker host from the curated table
            return "live" if 200 <= int(response.status) < 300 else "unverified"
    except urllib.error.HTTPError as exc:
        return "gone" if exc.code in (404, 410) else "unverified"
    except Exception:  # noqa: BLE001 -- any transport failure is "could not check"
        return "unverified"


def picture_status(url: str) -> str:
    """:func:`_probe_picture`, remembered for the life of the process."""
    status = _picture_status_cache.get(url)
    if status is None:
        status = _probe_picture(url)
        _picture_status_cache[url] = status
    return status


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
        status = picture_status(image_url)
        if status == "gone":
            # Never offered with confidence: the maker's server says the
            # picture is no longer at the address Kiln has.  No prose in
            # place of the photo; the maker's page (in the header) has it.
            block["no_image"] = True
            block["picture_gone"] = True
            block["kiln_note"] = (
                f"{maker}'s picture for this step is no longer at the address Kiln has; "
                f"open {maker}'s page for this guide (maker_page_url in the header) to see it."
            )
        else:
            block["image_url"] = image_url
            block["image_alt"] = step.get("image_alt")
            block["image_attribution"] = f"Image: {maker}, {source_url}" if source_url else f"Image: {maker}"
            block["image_status"] = status
            if status == "unverified":
                block["kiln_note"] = (
                    f"Kiln could not reach {maker}'s server to confirm this picture just now; "
                    f"if it does not load, open {maker}'s page for this guide (maker_page_url in the header)."
                )
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
        elif (cover and cover.get("maker_index_url")) or (maker_name and index_url):
            # kiln-pro knows the maker's maintenance page for THIS model;
            # public Kiln alone knows only the maker's landing page.
            payload["maker"] = (cover or {}).get("maker") or maker_name
            payload["maker_index_url"] = (cover or {}).get("maker_index_url") or index_url
            payload["kiln_note"] = (
                f"{NO_GUIDE_SENTENCE}. {payload['maker']}'s own maintenance pages are at maker_index_url."
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
    if cover and cover.get("maker_index_url"):
        return f"{NO_GUIDE_SENTENCE}; {cover.get('maker')}'s own maintenance pages for this model are at {cover['maker_index_url']}", True
    if maker_name and index_url:
        return (
            f"{NO_GUIDE_SENTENCE}; {maker_name}'s own site is {index_url}"
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
