"""Safety tools plugin.

Extracts safety-domain MCP tools from server.py into a focused plugin
module.  Provides tools for safety auditing, safety dashboard, safety
settings display, and safety profile management.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
import os
from typing import Any

_logger = logging.getLogger(__name__)


def _limits_proposal_key(printer_model: str, profile: dict) -> tuple[str, str]:
    """``(what the code is for, its fingerprint)``: the proposal's numbers,
    so a code answered for one set of limits cannot be spent on another."""
    import hashlib
    import json

    limits = {k: profile.get(k) for k in sorted(profile) if k != "notes"}
    digest = hashlib.sha256(json.dumps(limits, sort_keys=True, default=str).encode()).hexdigest()
    return f"limits for {printer_model}", digest


def _needs_a_sign_in(printer_model: str) -> dict | None:
    """The refusal a signed-out install gets for a limit above the floor on
    a printer Kiln has no number for, or ``None`` when it is signed in.

    The floor still prints; what a sign-in buys is Kiln standing behind a
    ceiling it will enforce.  Counted on the account wall like every other
    door a sign-in opens, so the dashboard sees it.
    """
    try:
        from kiln.auth_session import resolve_api_bearer

        if resolve_api_bearer().token:
            return None
    except Exception:  # noqa: BLE001 -- an unreadable session is not a sign-in
        pass
    try:
        from kiln.daily_stats import record_account_wall

        record_account_wall("add_safety_profile")
    except Exception:  # noqa: BLE001
        pass
    from kiln.tiers_and_terms import signin_hint_fields

    return {
        "success": False,
        "status": "error",
        "code": "KILN_ACCOUNT_NOT_PAIRED",
        "error": (
            f"Kiln has no verified limits for {printer_model!r}, so these numbers would be the only ceiling "
            "it enforces, and that takes a signed-in install. Sign in free (kiln_signin), then call this again: "
            "a person confirms the limits with a code on this screen, as a print start is."
        ),
        **signin_hint_fields(),
    }


def _limits_confirmed_by_a_person(printer_model: str, profile: dict) -> Any:
    """``True`` when a person has typed the code shown for exactly these
    limits; otherwise the refusal that says what to do, after showing the
    code where a screen can show one.

    The hosted server has no screen and no single owner, so it never
    writes a limit a person did not confirm on their own machine.
    """
    import kiln.server as _srv
    from kiln import screen_code
    from kiln.runtime_env import is_hosted_multitenant

    if is_hosted_multitenant():
        return _srv._error_dict(
            f"Kiln has no verified limits for {printer_model!r}; a limit above its generic floor "
            "is confirmed by the person on the machine the printer is set up on, and the hosted "
            "server is not that machine.",
            code="NOT_HERE",
        )
    key, digest = _limits_proposal_key(printer_model, profile)
    answered = screen_code.take_answer(key, printer_model, tool=screen_code.LIMITS_TOOL)
    if answered is not None and answered.issued.file_sha256 == digest:
        _srv._audit(
            "add_safety_profile", "limits_confirmed",
            details={"printer_model": printer_model, "proposal": digest, "words": answered.words},
        )
        return True
    shown = screen_code.issue(
        tool=screen_code.LIMITS_TOOL, file_name=key, file_sha256=digest,
        printer_name=printer_model, host=_srv._host_label(None),
        printer_label=str(profile.get("display_name") or printer_model),
    )
    outcome = shown.get("outcome")
    lead = (
        f"Kiln has no verified limits for {printer_model!r}, so these numbers would be the only "
        "ceiling it has. "
    )
    if outcome == screen_code.SHOWN:
        how = (
            "A code was just shown on the person's screen. Ask them to type it, pass their words "
            "to give_print_code, then call add_safety_profile again with the same numbers."
        )
    elif outcome == screen_code.COOLDOWN:
        how = f"Too many codes were shown recently; try again in {shown.get('seconds_left', 60)} seconds."
    else:
        how = (
            "This machine cannot show the person a code, so Kiln cannot take these limits here. "
            "Set printer_model to a printer Kiln knows, or state limits at or below the generic floor."
        )
    return _srv._error_dict(lead + how, code="CONFIRMATION_REQUIRED", extra={"code_shown": outcome})


class _SafetyToolsPlugin:
    """Safety audit, dashboard, settings, and profile management tools.

    Tools:
        - safety_audit
        - safety_status
        - safety_settings
        - list_safety_profiles
        - get_safety_profile
        - add_safety_profile
    """

    @property
    def name(self) -> str:
        return "safety_tools"

    @property
    def description(self) -> str:
        return "Safety audit, dashboard, settings, and profile management tools"

    def register(self, mcp: Any) -> None:  # noqa: PLR0915
        """Register safety tools with the MCP server."""

        import kiln.server as _srv

        # ------------------------------------------------------------------
        # safety_audit
        # ------------------------------------------------------------------

        @mcp.tool()
        def safety_audit(
            action: str | None = None,
            tool_name: str | None = None,
            limit: int = 25,
        ) -> dict:
            """Query the safety audit log.

            Returns a record of all safety-relevant operations: tool executions,
            blocked attempts, rate-limit violations, and preflight failures.

            Args:
                action: Filter by action type.  Options: ``"executed"``,
                    ``"blocked"``, ``"rate_limited"``, ``"auth_denied"``,
                    ``"preflight_failed"``, ``"dry_run"``.  Omit for all.
                tool_name: Filter by MCP tool name (e.g. ``"send_gcode"``).
                limit: Maximum number of records to return (default 25, max 100).
            """
            limit = min(max(1, limit), 100)
            try:
                db = _srv.get_db()
                entries = db.query_audit(action=action, tool_name=tool_name, limit=limit)
                summary = db.audit_summary()
                return {
                    "success": True,
                    "entries": entries,
                    "summary": summary,
                }
            except Exception as exc:
                _logger.exception("Unexpected error in safety_audit")
                return _srv._error_dict(
                    f"Unexpected error in safety_audit: {exc}", code="INTERNAL_ERROR"
                )

        # ------------------------------------------------------------------
        # safety_status
        # ------------------------------------------------------------------

        @mcp.tool()
        def safety_status() -> dict:
            """Get a comprehensive snapshot of all active safety measures.

            Returns a single summary showing: the active safety profile, temperature
            limits, rate-limit configuration, recent blocked actions, authentication
            status, and confirmation-mode status.  Use this to answer "is my printer
            safe right now?" in a single call.
            """
            try:
                # Active safety profile
                profile_info: dict[str, Any] = {
                    "printer_model": _srv._PRINTER_MODEL or "not configured"
                }
                max_tool, max_bed = _srv._get_temp_limits()
                profile_info["max_hotend_temp"] = max_tool
                profile_info["max_bed_temp"] = max_bed
                if _srv._PRINTER_MODEL:
                    try:
                        from kiln.safety_profiles import (
                            get_profile,
                            limits_provenance_note,
                        )

                        profile = get_profile(_srv._PRINTER_MODEL)
                        profile_info["profile_id"] = profile.id
                        profile_info["display_name"] = profile.display_name
                        profile_info["max_feedrate"] = profile.max_feedrate
                        if profile.build_volume:
                            profile_info["build_volume"] = profile.build_volume
                        # This snapshot quotes the limits in force, so it
                        # says whose numbers they are, same as every other
                        # surface that quotes them.
                        profile_info["limits_provenance"] = limits_provenance_note(profile)
                    except KeyError:
                        profile_info["profile_id"] = "default (no specific profile found)"

                # Rate limit configuration
                rate_limits = {}
                for t_name, (interval_ms, per_min) in _srv._TOOL_RATE_LIMITS.items():
                    rate_limits[t_name] = f"{interval_ms}ms cooldown, {per_min}/min"

                # Confirm-level tools (from tool_safety.json)
                confirm_tools = sorted(
                    name
                    for name, meta in _srv._TOOL_SAFETY.items()
                    if meta.get("level") in ("confirm", "emergency")
                )

                # Auth status
                auth_info = {
                    "enabled": (
                        _srv._get_auth().enabled
                        if hasattr(_srv._get_auth(), "enabled")
                        else False
                    ),
                }

                # Confirm mode
                confirm_mode = os.environ.get("KILN_CONFIRM_MODE", "").lower() in (
                    "1",
                    "true",
                    "yes",
                )

                # Recent blocked actions (from audit log)
                recent_blocked: list[dict[str, Any]] = []
                try:
                    db = _srv.get_db()
                    summary = db.audit_summary(window_seconds=3600.0)
                    recent_blocked = summary.get("recent_blocked", [])
                except Exception as exc:
                    _logger.debug(
                        "Failed to fetch audit summary for safety status: %s", exc
                    )

                # G-code blocked command list
                from kiln.gcode import _BLOCKED_COMMANDS  # noqa: E402

                blocked_gcode_commands = sorted(_BLOCKED_COMMANDS.keys())

                # Standing consent windows: prints inside one start without
                # asking, which is the one safety fact a person asking "is my
                # printer safe right now?" most needs to hear.
                standing_windows: list[dict[str, Any]] = []
                try:
                    from kiln import consent_windows

                    standing_windows = [consent_windows.describe(w) for w in consent_windows.standing_now()]
                except Exception as exc:
                    _logger.debug("Standing windows not read for safety status: %s", exc)

                return {
                    "success": True,
                    "safety_profile": profile_info,
                    "temperature_limits": {"max_hotend": max_tool, "max_bed": max_bed},
                    "rate_limits": rate_limits,
                    "confirm_level_tools": confirm_tools,
                    "auth": auth_info,
                    "confirm_mode_enabled": confirm_mode,
                    "blocked_gcode_commands": blocked_gcode_commands,
                    "recent_blocked_actions": recent_blocked,
                    "standing_windows": standing_windows,
                    "summary": (
                        f"Safety profile: {profile_info.get('display_name', _srv._PRINTER_MODEL or 'default')}. "
                        f"Temp limits: {max_tool}\u00b0C hotend / {max_bed}\u00b0C bed. "
                        f"{len(rate_limits)} rate-limited tools. "
                        f"{len(confirm_tools)} confirm-level tools. "
                        f"{len(recent_blocked)} blocked action(s) in last hour. "
                        + (
                            "Standing consent window(s) open: "
                            + "; ".join(
                                f"{w['id']} {w['summary']}" if w["always"]
                                else f"{w['id']} {w['scope']} until {w['until_clock']}"
                                for w in standing_windows
                            )
                            + " — prints there start without asking; revoke_consent_window closes one."
                            if standing_windows
                            else "No standing consent window: every print asks the person."
                        )
                    ),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in safety_status")
                return _srv._error_dict(
                    f"Unexpected error in safety_status: {exc}", code="INTERNAL_ERROR"
                )

        # ------------------------------------------------------------------
        # safety_settings
        # ------------------------------------------------------------------

        @mcp.tool()
        def safety_settings() -> dict:
            """Show current safety and auto-print settings.

            Displays whether auto-print is enabled for marketplace downloads
            and AI-generated models, and which printers have always allow on,
            along with guidance on how to change them.
            Call this early in a session to understand what safety protections
            are active.
            """
            # Always allow sits beside the auto-print switches because it
            # answers the same question -- where does a print start with
            # nobody asked -- for one printer at a time.
            always_on: list[str] = []
            try:
                from kiln import consent_windows

                always_on = [
                    consent_windows.describe_scope(w.scope) for w in consent_windows.standing_now() if w.always
                ]
            except Exception as exc:
                _logger.debug("Always-allow entries not read for safety settings: %s", exc)
            return {
                "success": True,
                "always_allow": {
                    "on_for": always_on,
                    "description": (
                        "Prints on a printer listed here start without asking the person. Each one "
                        "is still previewed first and still passes every start check, and where the "
                        "printer has a camera the bed is looked at first."
                    ),
                    "how_to_change": (
                        "Only the person turns it on: at a terminal (kiln consent window --always "
                        "--printer NAME) or on their Kiln account page with their authenticator. "
                        "Anyone can turn it off: revoke_consent_window, or kiln consent revoke."
                    ),
                },
                "auto_print_marketplace": {
                    "enabled": _srv._AUTO_PRINT_MARKETPLACE,
                    "env_var": "KILN_AUTO_PRINT_MARKETPLACE",
                    "risk_level": "moderate",
                    "description": (
                        "When enabled, marketplace models are auto-printed after "
                        "download+upload. When disabled (default), models are "
                        "uploaded but require explicit start_print call."
                    ),
                },
                "auto_print_generated": {
                    "enabled": _srv._AUTO_PRINT_GENERATED,
                    "env_var": "KILN_AUTO_PRINT_GENERATED",
                    "risk_level": "high",
                    "description": (
                        "When enabled, AI-generated models are auto-printed after "
                        "generation+validation+slicing+upload. When disabled "
                        "(default), models are uploaded but require explicit "
                        "start_print call. Higher risk than marketplace models."
                    ),
                },
                "recommendations": [
                    "Prefer downloading proven community models over generating new ones.",
                    "Always validate meshes before printing (validate_generated_mesh).",
                    "Review model dimensions against your printer's build volume.",
                    "Keep auto-print disabled unless you understand the risks.",
                    "AI model generation is experimental \u2014 generated geometry may "
                    "have thin walls, non-manifold faces, or impossible overhangs.",
                ],
                "how_to_change": (
                    "Set environment variables before starting the MCP server:\n"
                    "  export KILN_AUTO_PRINT_MARKETPLACE=true   # moderate risk\n"
                    "  export KILN_AUTO_PRINT_GENERATED=true     # higher risk\n"
                    "Or run 'kiln setup' to configure interactively."
                ),
            }

        # ------------------------------------------------------------------
        # list_safety_profiles
        # ------------------------------------------------------------------

        @mcp.tool()
        def list_safety_profiles() -> dict:
            """List the printers Kiln has a safety profile for, by id and name.

            The list names printers; it carries no limits.  Kiln reads a
            printer's limits one printer at a time: call
            ``get_safety_profile`` with one of these ids for its temperature,
            feedrate and flow limits, its build volume, and where those
            numbers came from.  ``validate_gcode_safe`` checks commands
            against one printer's limits.  A printer set up on this machine
            with limits its owner entered is listed too, marked
            ``set_up_on_this_machine``.
            """
            if err := _srv._check_auth("safety"):
                return err
            try:
                from kiln.safety_profiles import (
                    list_local_printer_overrides,
                    list_profiles,
                    roster_display_name,
                )

                # Names only, from the roster this install carries: asking for
                # every printer's limits here would be one catalogue fetch per
                # printer, and a bulk door to every ceiling Kiln holds.
                set_up_here = set(list_local_printer_overrides())
                profiles = []
                for pid in list_profiles():
                    entry: dict[str, Any] = {"id": pid, "display_name": roster_display_name(pid) or pid}
                    if pid in set_up_here:
                        entry["set_up_on_this_machine"] = True
                    profiles.append(entry)
                return {
                    "success": True,
                    "count": len(profiles),
                    "profiles": profiles,
                    "note": (
                        "Names only. For one printer's limits and where they came from, "
                        "call get_safety_profile(printer_id=<id>)."
                    ),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in list_safety_profiles")
                return _srv._error_dict(
                    f"Unexpected error in list_safety_profiles: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # get_safety_profile
        # ------------------------------------------------------------------

        @mcp.tool()
        def get_safety_profile(printer_id: str) -> dict:
            """Get the full safety profile for a specific printer model.

            Returns temperature limits, feedrate limits, volumetric flow,
            build volume, and safety notes.  Falls back to the default
            profile if the printer_id is not found.

            The profile also says where its numbers came from:
            ``owner_supplied`` lists any limit fields whose values were
            typed by this machine's owner rather than verified by Kiln,
            and ``limits_provenance`` is a ready-made sentence stating
            it.  Repeat that sentence when quoting a limit, so a
            verified number and a typed one are never presented with
            the same authority.

            Args:
                printer_id: Printer model identifier (e.g. ``"ender3"``,
                    ``"bambu_x1c"``, ``"prusa_mk4"``).
            """
            if err := _srv._check_auth("safety"):
                return err
            try:
                from kiln.safety_profiles import get_profile, profile_to_dict

                profile = get_profile(printer_id)
                return {"success": True, "profile": profile_to_dict(profile)}
            except KeyError:
                return _srv._error_dict(
                    f"No safety profile for '{printer_id}' and no default available.",
                    code="NOT_FOUND",
                )
            except Exception as exc:
                _logger.exception("Unexpected error in get_safety_profile")
                return _srv._error_dict(
                    f"Unexpected error in get_safety_profile: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # add_safety_profile
        # ------------------------------------------------------------------

        @mcp.tool()
        def add_safety_profile(printer_model: str, profile: dict) -> dict:
            """Add a local safety-profile override for a printer model.  Free at every tier.

            Validates the profile and saves it to this machine's override file
            (``~/.kiln/local_printer_overrides.json``; the older name
            ``community_profiles.json`` is still read).  Nothing saved here is
            uploaded, pooled or shared.

            An override may only TIGHTEN a curated limit.  A higher number is
            discarded in favour of Kiln's curated value, so this is the right
            tool for a printer Kiln has never heard of, or for holding your own
            machine BELOW the curated limits.

            It is the WRONG tool for "my hotend is upgraded".  Use
            ``select_printer_variant`` for that: it resolves to a ceiling Kiln
            has verified against the manufacturer, instead of one you typed.

            Values saved here are labelled owner-supplied in every profile
            readout — Kiln never presents them as its own verified numbers.

            For a printer Kiln has no number for, a limit above its generic
            floor needs two things.  A signed-in install (a free account:
            ``kiln_signin``), because Kiln vouches for ceilings it enforces
            and a ceiling nobody set is held to the floor.  And a PERSON's
            confirmation: Kiln shows a code on the screen of the machine it
            runs on, the person types it in the chat, you pass it to
            ``give_print_code``, then call this again with the same numbers.  Find the numbers on the maker's own spec
            page (maximum hotend and bed temperature, bed size) and say where
            they came from in ``notes``; never guess them, and never ask the
            person to type numbers they do not know.  The code is the
            confirmation; nothing you can call stands in for it.

            Args:
                printer_model: Short identifier for the printer (e.g.
                    ``"my_custom_corexy"``).
                profile: Dict containing at least ``max_hotend_temp``,
                    ``max_bed_temp``, ``max_feedrate``, and ``build_volume``
                    (a list of 3 positive numbers ``[X, Y, Z]``).  Optional
                    fields: ``display_name``, ``max_chamber_temp``, ``min_safe_z``,
                    ``max_volumetric_flow``, ``notes``.
            """
            if err := _srv._check_auth("safety"):
                return err
            try:
                from kiln.safety_profiles import (
                    limits_above_floor,
                    set_local_printer_override,
                    validate_safety_profile,
                )

                errors = validate_safety_profile(profile)
                if errors:
                    return _srv._error_dict(
                        f"Validation failed: {'; '.join(errors)}",
                        code="VALIDATION_ERROR",
                    )
                confirmed = False
                if limits_above_floor(printer_model, profile):
                    if (wall := _needs_a_sign_in(printer_model)) is not None:
                        return wall
                    answered = _limits_confirmed_by_a_person(printer_model, profile)
                    if answered is not True:
                        return answered
                    confirmed = True
                set_local_printer_override(printer_model, profile, confirmed=confirmed)
                return {
                    "success": True,
                    "printer_model": printer_model.lower().replace("-", "_").strip(),
                    "message": "Local printer override saved successfully."
                    + (" The person confirmed these limits." if confirmed else ""),
                }
            except ValueError as exc:
                return _srv._error_dict(
                    f"Failed to add safety profile: {exc}", code="VALIDATION_ERROR"
                )
            except Exception as exc:
                _logger.exception("Unexpected error in add_safety_profile")
                return _srv._error_dict(
                    f"Unexpected error in add_safety_profile: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # list_printer_variants / select_printer_variant
        # ------------------------------------------------------------------

        @mcp.tool()
        def list_printer_variants(printer_model: str) -> dict:
            """Show the curated hardware variants available for a printer.

            A curated profile describes a printer AS SHIPPED.  When Kiln has
            verified a documented hardware change — an Ender 3 whose PTFE-lined
            hotend has been replaced with an E3D Revo CR, say — that
            configuration is curated as a VARIANT, with its own limits, its
            manufacturer source, and the preconditions that make it true.

            Returns the as-shipped limits alongside each variant's, so you can
            see what selecting one would change before selecting it.  An empty
            ``variants`` map is the honest answer for a machine Kiln has not
            verified a modified configuration for.

            Args:
                printer_model: Printer identifier (e.g. ``"ender3"``).
            """
            if err := _srv._check_auth("safety"):
                return err
            try:
                from kiln.safety_profiles import list_printer_variants as _list

                return {"success": True, **_list(printer_model)}
            except Exception as exc:
                _logger.exception("Unexpected error in list_printer_variants")
                return _srv._error_dict(
                    f"Unexpected error in list_printer_variants: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def select_printer_variant(printer_model: str, variant_id: str = "") -> dict:
            """Declare which curated hardware variant your machine actually is.

            This is how an operator with a modified printer gets an accurate
            ceiling WITHOUT typing one.  You say which hardware you have; Kiln
            supplies the limit from curated, manufacturer-sourced data.  There
            is no argument here that accepts a temperature, which is the point:
            a limit Kiln enforces is always a limit Kiln verified.

            Check ``requires`` on the variant first — a ceiling is only true if
            its preconditions are met.  Several variants need a firmware change
            as well as the part, and selecting the variant is your statement
            that you have done both.  Kiln cannot check your hardware remotely.

            The declaration stays on this machine.  It is never uploaded or
            pooled, and it is ignored entirely on hosted multi-tenant
            deployments, where "this machine" has no single owner.

            Args:
                printer_model: Printer identifier (e.g. ``"ender3"``).
                variant_id: Variant to declare, from ``list_printer_variants``.
                    Pass ``""`` to go back to the as-shipped profile.
            """
            if err := _srv._check_auth("safety"):
                return err
            try:
                from kiln.safety_profiles import list_printer_variants as _list
                from kiln.safety_profiles import select_printer_variant as _select

                result = _select(printer_model, variant_id or None)
                if result.get("variant"):
                    spec = _list(printer_model)["variants"].get(result["variant"], {})
                    result["requires"] = spec.get("requires", [])
                return {"success": True, **result}
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="VALIDATION_ERROR")
            except Exception as exc:
                _logger.exception("Unexpected error in select_printer_variant")
                return _srv._error_dict(
                    f"Unexpected error in select_printer_variant: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # give_print_code
        #
        # The screen door.  When this host cannot draw the approval dialog,
        # Kiln shows a short code in a notification on the machine it runs
        # on and the person types it in the chat.  This is where the agent
        # relays those words.  It is not a tool that says yes: without the
        # code — which nothing an agent can call reveals — it says no, and
        # a wrong guess counts (see kiln.screen_code).
        # ------------------------------------------------------------------

        @mcp.tool()
        def give_print_code(words: str) -> dict:
            """Relay the code the person typed after Kiln showed one on their screen.

            When a print is refused with "a code was just shown in a notification",
            ask the person to type it here, then pass their exact words: the code
            alone approves that one print; the code followed by ``2h`` or ``today``
            also keeps printing on that printer without asking for a while. Then
            call the start tool again. Nothing you can call reveals the code; a
            wrong guess counts, and three wrong guesses void every code for a minute.
            When the reply carries ``phone_next_time``, pass that one line on once
            the print has started.

            Args:
                words: Exactly what the person typed, e.g. ``4821`` or ``4821 2h``.
            """
            from kiln import screen_code
            from kiln.runtime_env import is_hosted_multitenant

            if err := _srv._check_auth("write"):
                return err
            if is_hosted_multitenant():
                return _srv._error_dict(
                    "The hosted server has no screen to show a code on; the signed-in person "
                    "approves each print on the print page.", code="NOT_HERE",
                )
            out = screen_code.answer(words, host=_srv._host_label(None))
            outcome = out.get("outcome")
            if outcome == screen_code.ANSWERED:
                rec = out["record"]
                issued = rec.issued
                reply: dict = {
                    "success": True,
                    "approved": {
                        "file": issued.file_name, "printer": issued.printer_name or "default",
                        "choice": rec.choice, "typed_duration": rec.typed_duration,
                    },
                    "next": (
                        "Call add_safety_profile again with the same limits; this yes covers them."
                        if issued.tool == screen_code.LIMITS_TOOL
                        else f"Call the tool that starts the print again ({issued.tool}); this yes covers it."
                    ),
                }
                for_a_print = issued.tool != screen_code.LIMITS_TOOL
                if for_a_print and rec.answer.opens_window:
                    # The window opens where the dialog's does: when the print
                    # starts, in the wrapper — never from a tool.
                    reply["standing_window"] = {
                        "opens": "when the print is started",
                        "for": rec.answer.describe_window_ask(),
                    }
                # Someone who typed a code is someone the phone would help:
                # one line for after the print starts, once.  A limits code
                # starts nothing, so it earns no print offer.
                offer = _srv._phone_offer() if for_a_print else None
                if offer:
                    reply["phone_next_time"] = offer
                _srv._audit(
                    "give_print_code", "consent_code_answered",
                    details={
                        "file": issued.file_name, "printer": issued.printer_name, "words": rec.words,
                        "choice": rec.choice, "host": issued.host, "file_sha256": issued.file_sha256,
                        "code_shown_at": issued.issued_at, "answered_at": rec.answered_at,
                    },
                )
                return reply
            _srv._audit("give_print_code", "consent_code_refused", details={"outcome": outcome})
            if outcome == screen_code.WRONG:
                if out.get("nothing_live"):
                    return _srv._error_dict(
                        "No code is waiting to be answered. Start the print again so one is shown.",
                        code="NO_CODE",
                    )
                return _srv._error_dict(
                    f"That is not the code that was shown. {out.get('tries_left', 0)} tries left.",
                    code="WRONG_CODE",
                )
            if outcome == screen_code.VOIDED:
                return _srv._error_dict(
                    f"Too many wrong codes: every code is void for {out.get('seconds_left', 60)} seconds. "
                    "Tell the person, wait, then start the print again.", code="CODES_VOIDED",
                )
            if outcome == screen_code.TOO_FAST:
                return _srv._error_dict(
                    "That answer came back before a person could have read the notification; it was "
                    "not counted. The notification is shown again when the print is started again.",
                    code="TOO_FAST",
                )
            if outcome == screen_code.EVERY_PRINTER_NOT_HERE:
                return _srv._error_dict(
                    "A code covers the one printer the print was aimed at. Every printer at once is "
                    "opened at a terminal (`kiln consent window --for 2h --fleet`) or on the account page.",
                    code="NOT_HERE",
                )
            return _srv._error_dict(
                "Pass exactly what the person typed: the four-digit code, optionally followed by 2h, "
                "today, or a length like 45m.", code="VALIDATION_ERROR",
            )

        # ------------------------------------------------------------------
        # consent_window_status / revoke_consent_window
        #
        # A standing window is a person's "yes, for a while": prints may
        # start on a printer without asking each time.  A person opens one
        # through the host's approval dialog ("yes, and for the next 2
        # hours") or at a terminal (`kiln consent window`).  There is no
        # tool that opens or extends one, on purpose: an agent granting
        # itself standing permission is the hole the window exists to
        # close.  Closing is the safe direction, so that IS a tool.
        # ------------------------------------------------------------------

        @mcp.tool()
        def consent_window_status(printer_name: str | None = None) -> dict:
            """Which standing consent windows are open, for what, until when.

            A standing window is the person's "yes, for a while": inside it,
            prints start on the named printer without asking each time (each
            one still previewed first).  Only the person opens one — by
            picking "yes, and for the next…" in the approval dialog, or with
            ``kiln consent window`` at a terminal.  Nothing an agent can call
            opens or extends one.  Closing one is always allowed: see
            ``revoke_consent_window``.

            An entry with ``always: true`` is always allow: the person turned
            it on for one printer — at a terminal, or on their Kiln account
            page (``opened_via`` says which) — it has no end (``until`` is
            null), and prints there start without asking until it is turned
            off.  ``turned_off`` lists any that Kiln turned off itself in the
            last day because a different printer was set up under the name.

            Args:
                printer_name: Show only the window covering this printer.
                    Omit for every open window.
            """
            from kiln import consent_windows
            from kiln.runtime_env import is_hosted_multitenant

            if is_hosted_multitenant() and consent_windows.window_store() is None:
                return {
                    "success": True,
                    "windows": [],
                    "note": (
                        "The hosted server keeps no standing windows: the signed-in "
                        "account approves each print."
                    ),
                }
            # Always allow can be turned on or off on the account page.
            consent_windows.sync_account_always()
            live = consent_windows.standing_now()
            closed = consent_windows.turned_off_recently()
            if printer_name:
                always = consent_windows.always_for(printer_name)
                always_id = always.id if always is not None else ""
                live = [w for w in live if (w.id == always_id if w.always else w.covers(printer_name))]
                gone = consent_windows.turned_itself_off(printer_name)
                closed = [gone] if gone is not None else []
            rows = [consent_windows.describe(w) for w in live]
            from kiln.consent_window_note import turned_off_line

            return {
                "success": True,
                "windows": rows,
                "turned_off": [
                    {**consent_windows.describe(w), "note": turned_off_line(consent_windows.describe_scope(w.scope))}
                    for w in closed
                ],
                "note": (
                    "Prints inside a window start without asking; each is still previewed "
                    "first. To close one early, call revoke_consent_window. Only the person "
                    "can open or extend one — in the approval dialog (a length they pick or "
                    "type, 24 hours at most)"
                    + (
                        ", or on the Agent page in their Kiln settings."
                        if is_hosted_multitenant()
                        else ", or with `kiln consent window --for 2h --printer NAME` at a terminal."
                    )
                    if rows
                    else "No standing window is open: every print asks the person first."
                ),
            }

        @mcp.tool()
        def revoke_consent_window(
            window_id: str | None = None,
            printer_name: str | None = None,
            all_windows: bool = False,
        ) -> dict:
            """Close a standing consent window now.  Prints ask the person again.

            The person opened the window — in the approval dialog or at a
            terminal — and can close it here, through you, without touching
            a terminal: when they say to close it, call this.  Closing is
            the safe direction, so it needs no confirmation and no window
            can refuse it.  Jobs queued under the window will not start.

            This is also how always allow is turned off: when the person
            says "ask me first", call this with the printer's name (or the
            id on the print result) and show them the ``note`` as written.

            Args:
                window_id: The window to close (from ``consent_window_status``
                    or the ``standing_window`` block on a print result).
                printer_name: Close every open window covering this printer.
                all_windows: Close every open window.
            """
            if err := _srv._check_auth("print"):
                return err
            from kiln import consent_windows
            from kiln.runtime_env import is_hosted_multitenant

            if is_hosted_multitenant() and consent_windows.window_store() is None:
                return {"success": True, "revoked": [], "note": "The hosted server keeps no standing windows."}
            try:
                if all_windows:
                    closed = consent_windows.revoke_all()
                elif window_id:
                    closed = [consent_windows.revoke_window(window_id)]
                elif printer_name:
                    closed = consent_windows.revoke_covering(printer_name)
                else:
                    return _srv._error_dict(
                        "Say which: window_id, printer_name, or all_windows=true.",
                        code="VALIDATION_ERROR",
                    )
            except KeyError:
                return _srv._error_dict(f"No window {window_id}.", code="NOT_FOUND")
            _srv._audit(
                "revoke_consent_window", "consent_window_revoked",
                details={"revoked": [w.id for w in closed], "printer": printer_name, "all": all_windows},
            )
            return {
                "success": True,
                "revoked": [consent_windows.describe(w) for w in closed],
                "note": _closed_note(closed),
            }


def _closed_note(closed: list[Any]) -> str:
    """What the person is told a revoke did.  Always allow gets its own
    sentence, said per printer; a timed window the one it always had."""
    if not closed:
        return "No open window to close."
    from kiln.consent_windows import describe_scope

    always = [
        f"Always allow is off for {describe_scope(w.scope)}. Kiln will ask before each print."
        for w in closed if w.always
    ]
    if len(always) == len(closed):
        return " ".join(always)
    return " ".join([*always, "Closed. Prints ask the person again."])


plugin = _SafetyToolsPlugin()
