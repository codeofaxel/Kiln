"""What a served-tool stub tells an agent, and what a missing manifest says.

The stubs registered from ``pro_tool_manifest.json`` are the only place a
pip-installed agent ever meets Kiln's served tools, so each description is
the whole pitch: what the tool does first, the tier once, and a link that
can be counted back to the tool that sent the person.  And when the manifest
is missing the install is broken -- it is in every release -- so the server
says so at WARNING, not DEBUG (1.1.3 to 1.4.1.1 shipped without it, and the
DEBUG line was the only trace).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest

from kiln import server
from kiln.tiers_and_terms import PRICING_URL, upgrade_link

_MANIFEST = Path(server.__file__).parent / "pro_tool_manifest.json"


class _FakeMCP:
    def __init__(self):
        self.tools: dict[str, object] = {}

    def tool(self, **_kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _register(tmp_path, monkeypatch, manifest: dict | None) -> _FakeMCP:
    if manifest is not None:
        (tmp_path / "pro_tool_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(server, "Path", lambda _p: tmp_path / "kiln")
    monkeypatch.setattr(server, "_PRO_TOOL_NUDGES", {})
    monkeypatch.setattr(server, "_PRO_TOOL_TIERS", {})
    monkeypatch.setattr(server, "_PRO_TOOL_QUOTA", {})
    mcp = _FakeMCP()
    server._register_pro_tool_stubs(mcp)
    return mcp


def test_upgrade_link_tags_surface_and_tool():
    assert upgrade_link("generate_coaster") == f"{PRICING_URL}?src=agent&tool=generate_coaster"
    assert upgrade_link() == f"{PRICING_URL}?src=agent"
    assert upgrade_link("x", src="web_home") == f"{PRICING_URL}?src=web_home&tool=x"


def test_a_missing_manifest_is_a_warning(tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.DEBUG, logger=server.logger.name):
        mcp = _register(tmp_path, monkeypatch, manifest=None)
    assert mcp.tools == {}
    warnings = [r for r in caplog.records if "pro tool manifest" in r.getMessage().lower()]
    assert warnings, caplog.text
    assert all(r.levelno >= logging.WARNING for r in warnings), [r.levelname for r in warnings]


def test_a_stub_link_names_the_tool_that_sent_the_person(tmp_path, monkeypatch):
    mcp = _register(tmp_path, monkeypatch, {"tools": [
        {"name": "paid_thing", "tier": "pro", "parameters": {},
         "description": f"Does the thing.\n\nRequires Kiln Pro.\nUpgrade: {PRICING_URL}"},
        {"name": "untagged_paid", "tier": "business", "parameters": {},
         "description": "Fleet-wide thing."},
    ]})
    doc = mcp.tools["paid_thing"].__doc__
    assert upgrade_link("paid_thing") in doc
    assert doc.count("Requires Kiln") == 1
    # A tool whose manifest entry says nothing about its tier gets one
    # sentence and one tagged link, never the bare page.
    doc = mcp.tools["untagged_paid"].__doc__
    assert doc.startswith("Fleet-wide thing.")
    assert "Requires Kiln Business." in doc
    assert upgrade_link("untagged_paid") in doc
    assert PRICING_URL + "\n" not in doc and not doc.endswith(PRICING_URL)


_BARE_LINK = re.compile(re.escape(PRICING_URL) + r"(?![?/\w])")


@pytest.mark.skipif(not _MANIFEST.is_file(), reason="no bundled manifest in this tree")
def test_every_shipped_paid_stub_leads_with_value_and_says_the_tier_once(tmp_path, monkeypatch):
    """Over the manifest this tree ships, not a toy."""
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    mcp = _register(tmp_path, monkeypatch, manifest)
    paid = [t for t in manifest["tools"] if str(t.get("tier", "")).lower() not in ("", "free")]
    assert len(paid) > 100, len(paid)

    problems = []
    for tool in paid:
        name = tool["name"]
        doc = mcp.tools[name].__doc__ or ""
        first_paragraph = doc.split("\n\n", 1)[0]
        if "requires kiln" in first_paragraph.lower():
            problems.append(f"{name}: the tier comes before what the tool does")
        if doc.lower().count("requires kiln") != 1:
            problems.append(f"{name}: says the paywall {doc.lower().count('requires kiln')} times")
        if upgrade_link(name) not in doc:
            problems.append(f"{name}: no link tagged with its own name")
        if _BARE_LINK.search(doc):
            problems.append(f"{name}: carries a bare, uncountable pricing link")
    assert not problems, "\n".join(problems)


def test_the_sign_in_refusal_link_names_the_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_API_URL", raising=False)
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
    monkeypatch.setattr(server, "_PRO_TOOL_TIERS", {"paid_thing": "pro"})
    monkeypatch.setattr(server, "_PRO_TOOL_QUOTA", {})
    monkeypatch.setattr(server, "_PRO_TOOL_NUDGES", {})
    result = server._pro_api_call("paid_thing")
    assert result.get("code") == "KILN_ACCOUNT_NOT_PAIRED", result
    assert result["upgrade_url"] == upgrade_link("paid_thing")


def test_every_public_agent_door_carries_the_offer_first_rule():
    """The rule that keeps a paid tool from being quietly rebuilt for free.

    It used to reach only the hosted connector.  A stub's tier is printed in
    its description, and an agent that reads a tier off the listing never
    calls the tool, so no refusal ever reaches it: the instructions are the
    only place this can be said.  Three public doors carry them.
    """
    from kiln.agent_loop import _get_default_system_prompt
    from kiln.tiers_and_terms import OFFER_FIRST_LINE, TIERS_AND_TERMS

    doors = {
        "MCP instructions (built per connect)": server._build_instructions(),
        "MCP instructions (static fallback)": server.mcp.instructions or "",
        "CLI agent default prompt": _get_default_system_prompt(),
    }
    for door, text in doors.items():
        assert TIERS_AND_TERMS in text, door
        assert OFFER_FIRST_LINE in text, door
        assert text.count("OFFER FIRST:") == 1, f"{door}: said more than once"


def test_a_sign_in_wall_tells_the_agent_to_retry_the_request(tmp_path, monkeypatch):
    """One ask, one moment: after signing in, the original request runs again.

    Without this the person signs in and then has to repeat themselves, and a
    free account learns only on that second ask that the tool is paid.
    """
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_API_URL", raising=False)
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
    monkeypatch.setattr(server, "_PRO_TOOL_TIERS", {"paid_thing": "business"})
    monkeypatch.setattr(server, "_PRO_TOOL_QUOTA", {})
    monkeypatch.setattr(server, "_PRO_TOOL_NUDGES", {})
    hint = server._pro_api_call("paid_thing")["agent_hint"]
    assert "kiln signin" in hint
    assert "retry the request" in hint
