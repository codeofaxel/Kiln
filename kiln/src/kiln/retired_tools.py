"""Tool names Kiln took away, and the tool that does the job now.

When a tool is renamed or folded into another, its old name may stay
registered as an alias for a while so existing setups keep working.  Two
tables keep that honest:

* ``DEPRECATED`` -- old names still registered, each with the release that
  removes it.  ``kiln/tests/test_retired_tool_names.py`` fails a tool whose
  description calls itself deprecated with no row here, and a row whose
  release has arrived, so "kept for one release" is a date someone is held
  to rather than a sentence in a docstring.
* ``RETIRED`` -- names no tool carries any more.  A call to one is refused
  at the dispatch door with the call that replaces it, so an old setup gets
  a pointer instead of "unknown tool".
"""

from __future__ import annotations

#: Old name -> the call that does the job now.
RETIRED: dict[str, str] = {
    "print_status_lite": 'printer_status(detail="lite")',
    "repair_mesh_advanced": "repair_mesh(close_holes=True)",
    "render_model_preview": 'visualize_model(angles=["isometric"])',
    "recover_texture_detail": "keep_painted_detail",
    # Renamed: they hand back the file from before a mid-print
    # edit and the steps to go back to it, and change nothing themselves.
    "revert_mid_print": "get_print_before_edit",
    "revert_mid_print_fleet": "get_print_before_edit_fleet",
    # Retired: public tools whose only engine shipped with kiln-pro, so on
    # every customer's install they answered "No module named".  The job
    # each did lives where it always worked (the note
    # beside each pointer says where); the pointer names the nearest tool.
    "configure_sso": "kiln_signin",
    "sso_login_url": "kiln_signin",
    "sso_exchange_code": "kiln_signin",
    "sso_status": "kiln_signin",
    "manage_team_member": "list_org_members",
    "uptime_report": "kiln_health",
    "rotate_encryption_key": "encryption_status",
    "create_project": "print_cost_intelligence",
    "log_project_cost": "print_cost_intelligence",
    "project_cost_summary": "print_cost_intelligence",
    "client_cost_report": "print_cost_intelligence",
    # Folded: a one-printer and a named-printer tool for each firmware job
    # became one tool per job, for one machine or many.  The named-printer
    # three read a record nothing ever filled, so they answered
    # "not registered" for every real printer.
    "firmware_status": "check_machine_firmware",
    "check_firmware_status": "check_machine_firmware(machine=...)",
    "update_firmware": "update_machine_firmware",
    "update_printer_firmware": "update_machine_firmware(machine=...)",
    "rollback_firmware": "roll_back_machine_firmware",
    "rollback_printer_firmware": "roll_back_machine_firmware(machine=...)",
}

_SSO_NOTE = (
    "Signing in with your company's identity provider happens on the web sign-in "
    "(a work email goes to SSO); Kiln sets the provider up with you as part of an "
    "Enterprise plan, and nothing in chat configures it."
)
_PROJECT_COSTS_NOTE = (
    "Project cost tracking is not in this release; each print's cost is on its "
    "print record."
)
#: What a retired name's refusal adds after the pointer: where the job
#: lives now, when the nearest tool is only part of the answer.
RETIRED_NOTES: dict[str, str] = {
    "configure_sso": _SSO_NOTE,
    "sso_login_url": _SSO_NOTE,
    "sso_exchange_code": _SSO_NOTE,
    "sso_status": _SSO_NOTE,
    "manage_team_member": (
        "Who is on your team is managed on the Team page of your Kiln account; "
        "add_org_member and remove_org_member ask, and the person approves there."
    ),
    "uptime_report": "Kiln's measured uptime and incidents are at https://status.kiln3d.com.",
    "rotate_encryption_key": (
        "Files in your Kiln account are encrypted on Kiln's servers with a key held "
        "there; encryption_status says what is true of this computer. Nothing in chat "
        "rotates a key."
    ),
    "create_project": _PROJECT_COSTS_NOTE,
    "log_project_cost": _PROJECT_COSTS_NOTE,
    "project_cost_summary": _PROJECT_COSTS_NOTE,
    "client_cost_report": _PROJECT_COSTS_NOTE,
}

#: Old name still registered -> (the call that replaces it, the release that
#: removes it, as "X.Y.Z").
DEPRECATED: dict[str, tuple[str, str]] = {}


def retired_message(name: str) -> str | None:
    """The refusal for a retired tool name, or None for any other name."""
    replacement = RETIRED.get(name)
    if replacement is None:
        return None
    message = f"The {name} tool was removed from Kiln. Call {replacement} instead."
    note = RETIRED_NOTES.get(name)
    return f"{message} {note}" if note else message


def says_deprecated(description: str | None) -> bool:
    """True when a tool's description opens by calling the tool deprecated."""
    first_line = (description or "").lstrip().split("\n", 1)[0]
    return first_line.lstrip("*_ ").lower().startswith("deprecated")


def replacement_tool(call: str) -> str:
    """The tool name a replacement call names: ``repair_mesh(...)`` -> ``repair_mesh``."""
    return call.split("(", 1)[0].strip()
