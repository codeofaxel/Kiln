"""A refusal has two readers, and neither one should get the other's half.

The defect this pins is not a typo, it is a shape.  Fourteen refusals across
``server.py`` and the plugin surface each hand-wrote their own copy, and every
one of them ended by telling a PERSON to run a terminal command — a person who
had asked for a coaster, through an assistant that could have run the command
itself.  Half of them called the command ``kiln login`` and half called it
``kiln signin``, for the identical action, because there were fourteen
definitions and so nothing to disagree with.

So these tests are deliberately about the CONTRACT rather than the wording:

* the half a person reads names the situation and carries no command syntax,
* the half the agent reads carries the command and says to RUN it,
* one command name, everywhere,

which leaves the copy free to be rewritten without touching this file, while a
fifteenth refusal that reintroduces the shape fails.  They exercise the real
registered tools and the real fallback gates, not the string builders alone —
the builders being right proves nothing about whether the tools call them.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from kiln.tiers_and_terms import (
    AGENT_ACCOUNT_NUDGE,
    AGENT_SIGNIN_HINT,
    ALREADY_SUBSCRIBED_LINE,
    SIGNIN_COMMAND,
    account_required_message,
    free_allowance_phrase,
    session_expired_message,
    signed_out_message,
    signin_hint_fields,
    tier_required_message,
)

# Shapes that mean "somebody is being handed a shell".  Backticks are the tell
# that matters most: every one of the original fourteen used them to quote a
# command, and nothing else in refusal copy needs code formatting.
_COMMAND_SHAPES = ("`", "python3 -m", "kiln signin", "kiln login", "kiln pair", "$ ")


def _assert_reads_as_prose(text: str, label: str) -> None:
    """Fail if *text* is a person-facing string carrying a command."""
    assert text, f"{label} is empty"
    for shape in _COMMAND_SHAPES:
        assert shape not in text, f"{label} hands the user a command: {shape!r} in {text!r}"


def _capture_tools(plugin_module: str) -> dict:
    """Register a plugin against a fake MCP and return its real tool callables."""
    plugin = importlib.import_module(plugin_module).plugin
    tools: dict = {}

    class FakeMCP:
        def tool(self_mcp):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

    plugin.register(FakeMCP())
    return tools


# ---------------------------------------------------------------------------
# The person-facing half
# ---------------------------------------------------------------------------


_TEXTURE_ALLOWANCE = {
    "bucket": "decoration",
    "limit": 3,
    "noun": "textures",
    "period": "month",
}


@pytest.mark.parametrize(
    "message",
    [
        tier_required_message("decorate_surface", "pro"),
        tier_required_message("This feature", "business"),
        tier_required_message("The 'x' profile", "pro", "Free profiles: default"),
        session_expired_message(),
        session_expired_message("user@example.com"),
        signed_out_message(),
        ALREADY_SUBSCRIBED_LINE,
        account_required_message("apply_image_texture", "", _TEXTURE_ALLOWANCE),
        account_required_message("cloud_push_branch", "pro"),
        account_required_message("some_free_tool"),
    ],
)
def test_user_facing_copy_carries_no_command(message):
    _assert_reads_as_prose(message, "refusal copy")


def test_tier_message_names_the_tier_and_where_to_read_more():
    msg = tier_required_message("decorate_surface", "pro")
    assert "decorate_surface" in msg
    assert "Kiln Pro" in msg
    assert "kiln3d.com/pricing" in msg


def test_tier_message_puts_the_free_alternative_before_the_pricing_link():
    """The only sentence that helps a user who will not upgrade goes first.

    A reader who has already reached a pricing URL has stopped reading, so an
    alternative appended after it is an alternative nobody sees.
    """
    msg = tier_required_message("SVG decoration", "pro", "PNG and JPG still work")
    assert msg.index("PNG and JPG still work") < msg.index("kiln3d.com/pricing")


# ---------------------------------------------------------------------------
# The free allowance — say the real number, or say nothing
# ---------------------------------------------------------------------------
#
# The account wall is decided LOCALLY, before any request goes out, so the
# server's "free includes 3 textures a month" was written where almost nobody
# could reach it.  The number now travels in the pro tool manifest, which makes
# the failure mode a stated-but-wrong figure — worse than the silence it
# replaced, because a person told a number believes it.  Hence the shape these
# pin: a real declared allowance is stated, and anything less than a complete,
# well-formed one produces no allowance sentence at all.


def test_allowance_sentence_states_the_declared_number():
    msg = account_required_message("apply_image_texture", "", _TEXTURE_ALLOWANCE)
    assert "3 textures a month" in msg
    # Free is the headline; the number reassures.  A refusal for a free tool
    # that reads as an upsell is a lie about the price.
    assert "free to use" in msg
    assert "pricing" not in msg.lower()


@pytest.mark.parametrize(
    "allowance",
    [
        None,
        {},
        {"noun": "textures", "period": "month"},          # no limit
        {"limit": 3, "period": "month"},                  # no noun
        {"limit": 0, "noun": "textures"},                 # nothing included
        {"limit": -1, "noun": "textures"},
        {"limit": True, "noun": "textures"},              # bool is an int
        {"limit": "3", "noun": "textures"},               # string from bad JSON
        {"limit": 3, "noun": "   "},
        {"limit": 3, "noun": "textures", "period": "  "},
        {"limit": 3, "noun": 7},
        "not a dict",
        3,
    ],
)
def test_an_allowance_we_cannot_read_is_never_invented(allowance):
    """No number, no partial number, no placeholder — no sentence."""
    assert free_allowance_phrase(allowance) == ""
    msg = account_required_message("apply_image_texture", "", allowance)
    assert "free includes" not in msg
    # A digit would mean some fragment of an unreadable block still rendered.
    assert not any(ch.isdigit() for ch in msg), msg


def test_allowance_period_comes_from_the_declaration():
    phrase = free_allowance_phrase(
        {"limit": 2, "noun": "print-ready fixes", "period": "week"}
    )
    assert phrase == "2 print-ready fixes a week"


def test_allowance_period_defaults_to_month_when_unstated():
    assert free_allowance_phrase({"limit": 2, "noun": "part splits"}) == (
        "2 part splits a month"
    )


def test_a_paid_tier_answers_instead_of_the_free_allowance():
    """What it costs is the answer to the question a paid tool raises.

    Naming a free monthly figure next to a tier requirement leaves a reader
    unable to tell which one applies to them.
    """
    msg = account_required_message("some_pro_tool", "pro", _TEXTURE_ALLOWANCE)
    assert "Kiln Pro" in msg
    assert "kiln3d.com/pricing" in msg
    assert "3 textures" not in msg


# ---------------------------------------------------------------------------
# The agent-facing half
# ---------------------------------------------------------------------------


def test_agent_hint_carries_the_command_and_says_to_run_it():
    fields = signin_hint_fields()
    assert fields["setup_hint"] == SIGNIN_COMMAND == "kiln signin"
    assert SIGNIN_COMMAND in fields["agent_hint"]
    lowered = fields["agent_hint"].lower()
    assert "run" in lowered
    # The instruction that stops the agent relaying the command onward.
    assert "do not ask them to type" in lowered


def test_agent_hint_fields_are_not_a_shared_mutable():
    first = signin_hint_fields()
    first["agent_hint"] = "clobbered"
    assert signin_hint_fields()["agent_hint"] == AGENT_SIGNIN_HINT


def test_account_nudge_points_the_agent_at_the_tool_not_the_terminal():
    assert "kiln_signin" in AGENT_ACCOUNT_NUDGE
    assert "rather than asking them to type a command" in AGENT_ACCOUNT_NUDGE
    # Nothing is blocked by being signed out, so the pacing survives edits.
    assert "never block work on it" in AGENT_ACCOUNT_NUDGE


# ---------------------------------------------------------------------------
# The real refusal paths
# ---------------------------------------------------------------------------


def _kiln_pro_licensing_present() -> bool:
    """True when kiln-pro supplies ``kiln.licensing`` on this machine.

    Public Kiln ships no ``licensing`` module, so on a free install — and in
    this repo's CI — ``server.py``'s fallback stubs ARE the live tier gate.
    With kiln-pro installed (a developer machine) the real implementation wins
    and there is nothing local left to assert.
    """
    try:
        import kiln.licensing  # noqa: F401
    except ImportError:
        return False
    return True


_needs_free_install = pytest.mark.skipif(
    _kiln_pro_licensing_present(),
    reason="kiln-pro supplies kiln.licensing here; the free-install fallback "
    "under test is only reachable without it (see the structural test below, "
    "which pins the same wiring on every machine)",
)


@_needs_free_install
def test_free_tier_gate_splits_its_refusal_by_audience():
    """``requires_tier`` is the gate a free install actually hits."""
    import kiln.server as server

    @server.requires_tier("pro")
    def some_pro_tool():  # pragma: no cover - refused before the body runs
        return {"success": True}

    result = some_pro_tool()
    assert result["success"] is False
    assert result["code"] == "TIER_REQUIRED"
    _assert_reads_as_prose(result["error"], "requires_tier error")
    assert "some_pro_tool" in result["error"]
    assert SIGNIN_COMMAND in result["agent_hint"]
    assert result["setup_hint"] == SIGNIN_COMMAND


@_needs_free_install
def test_free_tier_check_returns_prose_for_the_caller_to_show():
    import kiln.server as server

    ok, message = server.check_tier("business")
    assert ok is False
    _assert_reads_as_prose(message, "check_tier message")
    assert "Business" in message


def test_free_tier_fallback_gates_delegate_to_the_shared_copy():
    """Pin the WIRING on every machine, including where kiln-pro shadows it.

    A shared builder nobody calls is the same bug with extra steps, and the
    behavioural tests above cannot run on a developer machine — so this reads
    the fallback block itself and proves both gates route through the one
    definition instead of re-typing a refusal.
    """
    import inspect

    import kiln.server as server

    src = inspect.getsource(server)
    head = src[: src.index("from kiln.log_config import")]
    assert head.count("tier_required_message(") == 2, (
        "both fallback gates (check_tier, requires_tier) must build their "
        "message from the shared builder"
    )
    assert "**signin_hint_fields()" in head, (
        "the requires_tier fallback must carry the agent-addressed fields"
    )


@pytest.mark.parametrize(
    ("plugin_module", "tool_name", "error_key"),
    [
        ("kiln.plugins.consumer_tools", "tax_estimate", "error"),
        ("kiln.plugins.consumer_tools", "tax_jurisdictions", "error"),
        ("kiln.plugins.consumer_tools", "tax_jurisdiction_lookup", "error"),
    ],
)
def test_pro_only_tools_refuse_without_handing_over_a_terminal(
    plugin_module, tool_name, error_key, monkeypatch
):
    """Exercise the registered tool, not the builder underneath it.

    These refuse on ``ImportError`` for kiln-pro, which is the state of every
    free install, so the refusal is reached without any mocking beyond making
    the import fail on a machine that happens to have kiln-pro present.
    """
    import builtins

    real_import = builtins.__import__

    def _no_kiln_pro(name, *args, **kwargs):
        if name.startswith("kiln_pro"):
            raise ImportError("kiln-pro not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_kiln_pro)

    tools = _capture_tools(plugin_module)
    tool = tools[tool_name]
    result = tool("US-CA") if tool_name == "tax_jurisdiction_lookup" else tool()

    assert result["status"] == "error"
    _assert_reads_as_prose(result[error_key], f"{tool_name} error")
    assert SIGNIN_COMMAND in result["agent_hint"]
    assert result["setup_hint"] == SIGNIN_COMMAND


@pytest.fixture
def registered_pro_stubs():
    """Register the real manifest's stubs and yield its metered tools.

    ``_register_pro_tool_stubs`` normally runs only on a free install (the
    ``ImportError`` branch), so on a machine with kiln-pro present the
    allowance registry would sit empty and a test that merely read it would
    pass by being vacuous.  Driving the real function against a throwaway MCP
    reads the SHIPPED manifest on every machine, then puts the module globals
    back.
    """
    import kiln.server as server

    tiers = dict(server._PRO_TOOL_TIERS)
    quota = dict(server._PRO_TOOL_QUOTA)

    class FakeMCP:
        def tool(self_mcp):
            return lambda fn: fn

    try:
        server._register_pro_tool_stubs(FakeMCP())
        yield server._PRO_TOOL_QUOTA
    finally:
        server._PRO_TOOL_TIERS.clear()
        server._PRO_TOOL_TIERS.update(tiers)
        server._PRO_TOOL_QUOTA.clear()
        server._PRO_TOOL_QUOTA.update(quota)


def test_shipped_manifest_declares_an_allowance_for_the_metered_tools(
    registered_pro_stubs,
):
    """The mirrored manifest is a build artifact; a stale one goes silent.

    Without this, regenerating from a kiln-pro that had lost its quota
    registries would drop every allowance and the copy would quietly revert to
    the vaguer sentence — no error anywhere.  ``apply_image_texture`` is named
    because it is the tool this whole path was reported against.
    """
    assert "apply_image_texture" in registered_pro_stubs, (
        "the mirrored pro_tool_manifest.json carries no allowance for "
        "apply_image_texture — regenerate it from kiln-pro"
    )
    for name, block in registered_pro_stubs.items():
        assert free_allowance_phrase(block), f"{name} has an unreadable {block!r}"
        assert block.get("bucket"), f"{name} names no allowance pool"


def test_account_wall_states_each_tool_its_own_declared_allowance(
    registered_pro_stubs, tmp_path, monkeypatch
):
    """The real refusal, through the real manifest — not the builder alone.

    Asserts the sentence carries the number the manifest declares rather than
    any specific figure: what the free tier includes is kiln-pro's decision to
    change, and a public test that hardcoded "3" would fail the next time it
    did.
    """
    from kiln.server import _pro_api_call

    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_API_URL", raising=False)
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)

    for name, block in registered_pro_stubs.items():
        result = _pro_api_call(name)
        assert result["code"] == "KILN_ACCOUNT_NOT_PAIRED"
        assert free_allowance_phrase(block) in result["error"], name
        _assert_reads_as_prose(result["error"], f"{name} account wall")
        # The machine-readable twin, for a caller that would rather render the
        # allowance itself than parse the sentence.
        assert result["quota"] == block
        assert SIGNIN_COMMAND in result["agent_hint"]


def test_account_wall_says_nothing_about_an_allowance_it_was_never_given(
    tmp_path, monkeypatch
):
    """A tool with no declared allowance gets no number and no ``quota`` key.

    An empty dict would be indistinguishable from "metered at zero" to a
    caller reading the payload; absence cannot be misread that way.
    """
    from kiln.server import _pro_api_call

    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_API_URL", raising=False)
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)

    result = _pro_api_call("cloud_remote_list")

    assert result["code"] == "KILN_ACCOUNT_NOT_PAIRED"
    assert "quota" not in result
    assert "free includes" not in result["error"]


def test_license_status_expired_session_splits_by_audience(monkeypatch):
    from kiln.server import _annotate_session_liveness

    class _Session:
        token = ""
        state = "signed_out"
        detail = ""

    monkeypatch.setattr(
        "kiln.auth_session.resolve_session_bearer", lambda *a, **k: _Session()
    )
    payload = {"source": "oauth", "tier": "pro", "is_valid": True}
    _annotate_session_liveness(payload)

    assert payload["is_valid"] is False
    _assert_reads_as_prose(payload["action_required"], "action_required")
    assert SIGNIN_COMMAND in payload["agent_hint"]


# ---------------------------------------------------------------------------
# One command, one name
# ---------------------------------------------------------------------------


def test_no_shipping_source_says_kiln_login():
    """``kiln login`` is a real alias and keeps working; it is not what we SAY.

    Scoped to the surfaces a person meets through an agent.  ``cli/`` is
    excluded on purpose — those messages address someone already at a prompt,
    where naming a command is correct — and ``tiers_and_terms`` is excluded
    because it documents the alias and this history on purpose.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "kiln"
    offenders = []
    for path in src.rglob("*.py"):
        rel = path.relative_to(src)
        if rel.parts[0] == "cli" or rel.name == "tiers_and_terms.py":
            continue
        for lineno, line in enumerate(
            path.read_text(errors="replace").splitlines(), start=1
        ):
            if "kiln login" in line:
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, "say `kiln signin`, not `kiln login`:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# The nudge an agent relays — said once, or not at all
# ---------------------------------------------------------------------------


@pytest.fixture
def registered_stub_descriptions():
    """The descriptions the stubs are actually REGISTERED with.

    The fixture above reads the allowance registries; this one captures what
    an agent receives, which is a different artifact — the registries can be
    perfectly populated while the sentence built from them reads badly.
    """
    import kiln.server as server

    tiers = dict(server._PRO_TOOL_TIERS)
    quota = dict(server._PRO_TOOL_QUOTA)
    seen: dict[str, str] = {}

    class CapturingMCP:
        def tool(self_mcp):
            def _decorator(fn):
                doc = fn.__doc__ or ""
                seen[getattr(fn, "__name__", "?")] = doc
                return fn
            return _decorator

    try:
        server._register_pro_tool_stubs(CapturingMCP())
        yield seen, server._PRO_TOOL_TIERS
    finally:
        server._PRO_TOOL_TIERS.clear()
        server._PRO_TOOL_TIERS.update(tiers)
        server._PRO_TOOL_QUOTA.clear()
        server._PRO_TOOL_QUOTA.update(quota)


def test_a_paid_tool_states_its_paywall_exactly_once(registered_stub_descriptions):
    """It used to say it twice, in two wordings, with two link labels.

    The manifest generator writes "Requires Kiln Business. / Upgrade: <url>"
    into the description, and the stub registrar appended "Requires Kiln
    Business. Pricing: <url>" on top of it unconditionally.  Nothing was
    factually wrong, which is why it survived — but this is the one sentence
    whose whole job is to be relayed to a person deciding whether to pay, and
    it arrived stuttering.  Neither half is wrong alone; only together.
    """
    seen, tiers = registered_stub_descriptions
    if not seen:
        pytest.skip("no pro tool manifest bundled in this checkout")
    # Not vacuous: the real bundled manifest, with real paid tools in it.
    assert len(seen) > 100 and tiers, "the stub surface came back empty"
    # And the detector really detects — the shape as it shipped, verbatim.
    _shipped_bug = (
        "Modify the design.\n\nRequires Kiln Business.\nUpgrade: u"
        "\n\nRequires Kiln Business. Pricing: u"
    )
    assert _shipped_bug.lower().count("requires kiln") > 1

    doubled = [
        name for name, desc in seen.items()
        if desc.lower().count("requires kiln") > 1
    ]
    assert not doubled, f"paid tools stating the paywall twice: {sorted(doubled)[:5]}"

    unstated = [
        name for name, tier in tiers.items()
        if f"requires kiln {tier}" not in seen.get(name, "").lower()
    ]
    assert not unstated, (
        f"paid tools an agent cannot see the price of: {sorted(unstated)[:5]}"
    )


def test_the_agent_guidance_names_the_real_access_classes():
    """The guidance tells agents the four classes and the exact field values.

    Prose teaching a vocabulary the data does not use is worse than no prose:
    the agent applies a rule that never matches, silently. So the class names
    it promises are checked against the values the manifest actually carries.

    2026-09-03: the prose taught THREE classes and defined 'free' as "FREE,
    UNLIMITED … silence is never ambiguous", while five kiln-pro tools sat
    under access='free' with a free DOOR and a tier-banded ANSWER (the safety
    floor free, the depth paid).  An agent read the prose and told a user
    magnet intelligence was free.  The fourth class — free door, tiered
    answer, access='free_banded' — carries a `band` block naming what each
    tier gets, and 'free' goes back to meaning flat.  Both halves are pinned
    here at the artifact an agent reads.
    """
    import json
    from pathlib import Path

    from dataclasses import asdict

    from kiln.skill_manifest import generate_manifest

    manifest = asdict(generate_manifest())

    guidance = None
    for value in _walk_strings(manifest):
        if "access classes" in value:
            guidance = value
            break
    assert guidance, "the access-class guidance is gone from the skill manifest"
    assert "four access classes" in guidance, (
        "the guidance still teaches three classes; 'free door, tiered answer' "
        "(access='free_banded') is the fourth, and without it an agent reads a "
        "banded free door as FREE, UNLIMITED"
    )
    assert "'free_banded'" in guidance

    bundled = Path(__file__).resolve().parents[1] / "src" / "kiln" / "pro_tool_manifest.json"
    if not bundled.exists():
        pytest.skip("no bundled pro manifest in this checkout")
    tools = json.loads(bundled.read_text())["tools"]
    real = {t.get("access") for t in tools}
    assert real, "the bundled manifest carries no access field at all"
    for value in sorted(real):
        assert f"'{value}'" in guidance, (
            f"the manifest uses access={value!r} but the agent guidance never "
            f"names it — an agent cannot classify what it was not told about"
        )

    # The fourth class, at the artifact: the label is DERIVED from the band
    # block (present <=> free_banded), the block names every tier, and the
    # description says it in prose for an agent that never parses JSON.
    # 'free' keeps meaning flat — no block, no line.
    tiers = ("free", "pro", "business", "enterprise")
    for t in tools:
        banded = t.get("access") == "free_banded"
        assert banded == bool(t.get("band")), (
            f"{t['name']}: access={t.get('access')!r} disagrees with its band block"
        )
        if banded:
            band = t["band"]
            assert band.get("domain"), f"{t['name']}: band block names no domain"
            for tier in tiers:
                assert str(band.get(tier, "")).strip(), f"{t['name']}: band has no {tier} line"
            assert "Free door, tiered answer" in t.get("description", ""), (
                f"{t['name']}: banded, but the description an agent reads is bare"
            )
        elif t.get("access") == "free":
            assert "Free door, tiered answer" not in t.get("description", ""), (
                f"{t['name']}: labelled flat free while its description says banded"
            )

    _assert_flat_free_tools_have_no_band_resolver(tools)


def _assert_flat_free_tools_have_no_band_resolver(tools):
    """Every tool still labelled plain 'free' must really be flat.

    Otherwise the four-class prose above is decoration again: the label
    would be right for the tools somebody remembered to classify and wrong
    for the next one.  This needs kiln-pro SOURCE (the scan reads tool
    bodies), which public CI guarantees is absent — so the half runs on an
    install that has it, and kiln-pro's own suite pins it unconditionally
    (kiln-pro tests/test_band_claims.py).  It is not a silent skip: the
    consumer-level checks above run everywhere.
    """
    try:
        from kiln_pro import band_scan
    except ImportError:
        return
    located = band_scan.locate_tools()
    banded_but_flat = []
    for t in tools:
        name = t["name"]
        if t.get("access") != "free" or band_scan.exemption(name):
            continue
        path = located.get(name)
        if path is None:
            continue
        scan = band_scan.scan_tool(name, path)
        if scan is not None and scan.banded:
            banded_but_flat.append((name, sorted(scan.signals | scan.policies)))
    assert not banded_but_flat, (
        "tools labelled access='free' whose bodies band the answer by tier — "
        f"the prose says free means flat, and for these it is a lie: {banded_but_flat}"
    )


def _walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk_strings(v)
