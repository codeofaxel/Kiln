"""Which agent host is driving this server, and what it can do.

An MCP client names itself once, at ``initialize`` — ``clientInfo``
carries a name and a version, and ``capabilities`` says what the host
can draw (an MCP Apps panel, an elicitation prompt).  That handshake is
the only thing a stdio server is TOLD about the app on the other end,
and it is a fact about the whole connection, so it is read once per
process and recorded once, the way ``kiln/surface.py`` treats the
door the process came in through.

Measured on real hosts, which is what the labels below are shaped
around:

* Claude's desktop chat spawns the server through its bundle launcher,
  passes NO environment of its own, and introduces itself as
  ``claude-ai`` with the Apps extension declared.
* Claude Code — the terminal CLI and the desktop app's Code tab — is
  ``claude-code`` with its own version, declares elicitation, and
  exports ``CLAUDECODE=1`` plus ``CLAUDE_CODE_ENTRYPOINT`` to every
  server it spawns (``claude-desktop`` from the Code tab; unset in the
  terminal, which its own code reads as ``cli``; ``sdk-ts`` /
  ``sdk-py`` / ``sdk-cli`` from the Agent SDK).  Those two names alone
  cannot tell the terminal from the Code tab, so the entry point is
  folded into the host label when that marker is present.

The MODEL is not part of the protocol.  No host examined sends it in
``clientInfo`` and none exports it to the server process by default;
it is recorded only when a host volunteers one — a non-standard
``model`` field on ``clientInfo``, or Claude Code's own
``ANTHROPIC_MODEL`` override, which names the model it will use — and
reads ``unknown`` otherwise.  An honest unknown beats a guess: the
dashboard counts the unknowns as unknowns rather than filing them
under whichever model was fashionable.

What leaves the machine: the host NAME and VERSION, whether it declared
Apps and elicitation, and the model hint or ``unknown``.  Never the
session id, the project directory, the working tree, or anything else
the environment happens to carry.  Same switch as the rest of the
heartbeat (``KILN_TELEMETRY=false``).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, NamedTuple

_logger = logging.getLogger(__name__)

#: The host label when no session, no ``clientInfo``, or no name.
UNKNOWN = "unknown"

#: Facts recorded beside a host, as ``"<host label> <fact>"`` keys of one
#: ``{key: count}`` map.  A closed set, so the dashboard can render each
#: kind on its own axis; a version rides as ``v:<version>``, a model hint
#: as ``model:<hint>``.
FACT_APPS = "apps"
FACT_ELICITATION = "elicitation"
FACT_VERSION_PREFIX = "v:"
FACT_MODEL_PREFIX = "model:"

#: Claude Code's own markers: it exports ``CLAUDECODE=1`` to every child,
#: and ``CLAUDE_CODE_ENTRYPOINT`` names the door it was started from.
#: The variable is unset for the plain terminal, which Claude Code's own
#: code reads as ``cli``.  Only consulted when the marker is present, so
#: a different host inheriting a stale shell variable is not mislabelled.
_CLAUDE_CODE_MARKER = "CLAUDECODE"
_CLAUDE_CODE_ENTRYPOINT = "CLAUDE_CODE_ENTRYPOINT"
_CLAUDE_CODE_DEFAULT_ENTRYPOINT = "cli"
#: Claude Code's documented model override; when set it names the model
#: the host will use, which is the one thing the environment can say
#: about the model.
_CLAUDE_CODE_MODEL_OVERRIDE = "ANTHROPIC_MODEL"

# A token as it lands in a heartbeat map key: lowercase, no whitespace,
# nothing the dashboard's read-side key rule would refuse.  Capped so a
# fuzzed clientInfo cannot grow a key without bound -- and so the longest
# key this module can compose (a name, a door and a fact, three tokens
# and two spaces plus the "model:" prefix) stays inside the 128
# characters the read side and the ingest filter accept.  Raising this
# cap without widening that rule would delete the longest keys silently,
# at both ends; ``KEY_BUDGET`` and its test pin the arithmetic.
_TOKEN_KEEP = re.compile(r"[^a-z0-9._-]+")
_TOKEN_MAX = 40
#: The longest key ``AgentHost.facts`` can produce; the dashboard's rule
#: accepts exactly this many characters.
KEY_BUDGET = 128

_recorded = False


class AgentHost(NamedTuple):
    """What one connected host said about itself, normalised."""

    #: ``clientInfo.name`` as a token — ``claude-ai``, ``claude-code``,
    #: ``cursor`` — or ``unknown``.
    name: str
    #: ``clientInfo.version`` as a token, or ``unknown``.
    version: str
    #: The door a multi-door host was started from (Claude Code's
    #: ``claude-desktop`` / ``cli`` / ``sdk-*``), or ``""`` when the host
    #: has one door or did not say.
    entrypoint: str
    #: Declared the MCP Apps extension at initialize.
    apps: bool
    #: Declared elicitation (it can put a question in front of a person).
    elicitation: bool
    #: A model the host volunteered, as a token, or ``unknown``.
    model: str

    @property
    def label(self) -> str:
        """The host as the dashboard groups it: the name, plus the entry
        point when the host has more than one door."""
        return f"{self.name} {self.entrypoint}" if self.entrypoint else self.name

    @property
    def facts(self) -> list[str]:
        """The ``agent_host_facts`` keys this host contributes."""
        facts = [f"{self.label} {FACT_VERSION_PREFIX}{self.version}",
                 f"{self.label} {FACT_MODEL_PREFIX}{self.model}"]
        if self.apps:
            facts.append(f"{self.label} {FACT_APPS}")
        if self.elicitation:
            facts.append(f"{self.label} {FACT_ELICITATION}")
        return facts


def token(value: Any) -> str:
    """A name, version or model hint as a map-key token, or ``unknown``.

    Lowercased, whitespace folded to ``-``, anything outside
    ``[a-z0-9._-]`` dropped, leading separators stripped, capped.  A
    value with nothing left after that is ``unknown``: the absence is a
    real answer and must stay distinguishable from every real host.
    """
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", "-", text)
    text = _TOKEN_KEEP.sub("", text).lstrip("._-")
    return text[:_TOKEN_MAX] or UNKNOWN


def _client_info(mcp: Any, ctx: Any) -> Any | None:
    from kiln.mcp_compat import current_session

    session = current_session(mcp, ctx)
    params = getattr(session, "client_params", None)
    return getattr(params, "clientInfo", None)


def _entrypoint(env: Any) -> str:
    if not str(env.get(_CLAUDE_CODE_MARKER, "") or "").strip():
        return ""
    return token(env.get(_CLAUDE_CODE_ENTRYPOINT) or _CLAUDE_CODE_DEFAULT_ENTRYPOINT)


def _model_hint(info: Any, env: Any) -> str:
    """A model the host volunteered, or ``unknown``.

    ``clientInfo`` is an open record (the SDK keeps unknown fields as
    extras), so a host that adds ``model`` there is read; no host examined
    does today.  Claude Code's ``ANTHROPIC_MODEL`` override counts only
    under Claude Code's marker, for the same reason as the entry point.
    """
    extra = getattr(info, "model_extra", None) or {}
    volunteered = extra.get("model") if isinstance(extra, dict) else None
    if not volunteered:
        volunteered = getattr(info, "model", None)
    if volunteered:
        return token(volunteered)
    if str(env.get(_CLAUDE_CODE_MARKER, "") or "").strip():
        override = env.get(_CLAUDE_CODE_MODEL_OVERRIDE)
        if override:
            return token(override)
    return UNKNOWN


def describe(mcp: Any, ctx: Any = None, env: Any = None) -> AgentHost | None:
    """What the connected host declared, or None when there is no session.

    ``ctx`` is the request context the handler was invoked with (the only
    place the session lives on SDK 2); ``env`` defaults to the process
    environment.  Never raises: a host that cannot be described is
    ``None``, and the caller records nothing rather than ``unknown`` —
    "no session" is not a host.
    """
    env = os.environ if env is None else env
    try:
        info = _client_info(mcp, ctx)
        if info is None:
            return None
        from kiln import local_stage
        from kiln.mcp_compat import host_can_ask_the_user

        return AgentHost(
            name=token(getattr(info, "name", None)),
            version=token(getattr(info, "version", None)),
            entrypoint=_entrypoint(env),
            apps=bool(local_stage.host_declares_apps(mcp, ctx)),
            elicitation=bool(host_can_ask_the_user(mcp, ctx)),
            model=_model_hint(info, env),
        )
    except Exception as exc:  # noqa: BLE001 — telemetry never breaks a call
        _logger.debug("agent_host.describe failed: %s", exc)
        return None


def record_once(mcp: Any, ctx: Any = None) -> AgentHost | None:
    """Count this process's host in today's stats, the first time a tool
    call lands.  Idempotent per process; never raises.

    The first TOOL CALL, not the handshake: a host that spawns the server
    at app start and never asks it anything is not a person using Kiln
    from that host.  So ``agent_hosts`` counts hosts that were USED, and
    is not the same number as ``surface_sessions["mcp"]``, which counts
    process starts.
    """
    global _recorded  # noqa: PLW0603
    if _recorded:
        return None
    host = describe(mcp, ctx)
    if host is None:
        return None
    try:
        from kiln.daily_stats import record_agent_host

        record_agent_host(host.label, host.facts)
        _recorded = True
    except Exception as exc:  # noqa: BLE001
        _logger.debug("agent_host.record_once failed: %s", exc)
    return host


def reset_recorded() -> None:
    """Forget that a host was recorded.  Test isolation only."""
    global _recorded  # noqa: PLW0603
    _recorded = False
