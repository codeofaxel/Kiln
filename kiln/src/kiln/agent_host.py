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

* Claude's desktop app introduces its chat as ``claude-ai`` and its
  Cowork mode as ``local-agent-mode-`` followed by the name the user gave
  the server in the app's settings, both with the Apps extension
  declared; a server the app launched carried no CLAUDE variables.  The
  user's label is dropped from the host name (see
  ``_USER_SUFFIXED_NAME_PREFIXES``): it says what the person called
  Kiln, not which app is asking.
* Claude Code — the terminal CLI and the desktop app's Code tab — is
  ``claude-code`` with its own version, declares elicitation, and
  exports ``CLAUDECODE=1`` plus ``CLAUDE_CODE_ENTRYPOINT`` to every
  process it starts (``claude-desktop`` from the Code tab; unset in the
  terminal, which its own code reads as ``cli``; ``sdk-ts`` /
  ``sdk-py`` / ``sdk-cli`` from the Agent SDK).  The name alone cannot
  tell the terminal from the Code tab, so the entry point is folded into
  the host label — but only for a host that names itself ``claude-code``.
  Every process Claude Code starts inherits the marker, shells included,
  so another app launched from inside a Claude Code session can carry it
  too; the marker is Claude Code's statement about itself, never about
  whichever host happens to hold it.

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
#: code reads as ``cli``.  Only consulted when the marker is present AND
#: the host names itself Claude Code (``_is_claude_code``), so another
#: app that inherited the variable is not mislabelled.
_CLAUDE_CODE_MARKER = "CLAUDECODE"
#: The ``clientInfo.name`` Claude Code gives at the handshake.  The markers
#: here are read only when the connected host names itself this.
_CLAUDE_CODE_CLIENT_NAME = "claude-code"
_CLAUDE_CODE_ENTRYPOINT = "CLAUDE_CODE_ENTRYPOINT"
_CLAUDE_CODE_DEFAULT_ENTRYPOINT = "cli"
#: Claude Code's documented model override; when set it names the model
#: the session was configured to start with, which is the one thing the
#: environment can say about the model.
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

#: Hosts that append a label of the USER's to their own name.  Claude
#: desktop's Cowork mode names its client ``local-agent-mode-<the name the
#: user gave this server>``: the suffix says what the person called Kiln,
#: not which app is asking, and a person's own label is not ours to send.
#: The name collapses to the prefix, without its trailing dash.
_USER_SUFFIXED_NAME_PREFIXES = ("local-agent-mode-",)

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


def _host_name(info: Any) -> str:
    """The host's own name as a token, minus any label of the user's."""
    name = token(getattr(info, "name", None))
    for prefix in _USER_SUFFIXED_NAME_PREFIXES:
        if name.startswith(prefix):
            return prefix.rstrip("-")
    return name


def _client_info(mcp: Any, ctx: Any) -> Any | None:
    from kiln.mcp_compat import current_session

    session = current_session(mcp, ctx)
    params = getattr(session, "client_params", None)
    return getattr(params, "clientInfo", None)


def _is_claude_code(name: str, env: Any) -> bool:
    """Claude Code's markers apply: the host names itself Claude Code AND
    the marker is present.  Either alone is not enough — the name without
    the marker is a Claude Code build that does not export it, and the
    marker without the name is some other app that inherited it."""
    return name == _CLAUDE_CODE_CLIENT_NAME and bool(
        str(env.get(_CLAUDE_CODE_MARKER, "") or "").strip()
    )


def _entrypoint(name: str, env: Any) -> str:
    if not _is_claude_code(name, env):
        return ""
    return token(env.get(_CLAUDE_CODE_ENTRYPOINT) or _CLAUDE_CODE_DEFAULT_ENTRYPOINT)


def _model_hint(info: Any, name: str, env: Any) -> str:
    """A model the host volunteered, or ``unknown``.

    ``clientInfo`` is an open record (the SDK keeps unknown fields as
    extras), so a host that adds ``model`` there is read; no host examined
    does today.  Claude Code's ``ANTHROPIC_MODEL`` override counts only
    for Claude Code itself, for the same reason as the entry point, and
    it is the model the session was configured to START with: a switch
    made inside the session never reaches the server's environment.
    """
    extra = getattr(info, "model_extra", None) or {}
    volunteered = extra.get("model") if isinstance(extra, dict) else None
    if not volunteered:
        volunteered = getattr(info, "model", None)
    if volunteered:
        return token(volunteered)
    if _is_claude_code(name, env):
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

        name = _host_name(info)
        return AgentHost(
            name=name,
            version=token(getattr(info, "version", None)),
            entrypoint=_entrypoint(name, env),
            apps=bool(local_stage.host_declares_apps(mcp, ctx)),
            elicitation=bool(host_can_ask_the_user(mcp, ctx)),
            model=_model_hint(info, name, env),
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
