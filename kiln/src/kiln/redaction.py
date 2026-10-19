"""Shared redaction patterns — the ONE home for Kiln's scrub rules.

Three surfaces redact text, and before this module each carried its own
pattern set, which drifted (the log scrubber had no private-IP rule; the
LLM redactor had richer token patterns than the log scrubber; neither
touched home directories):

* the rotating log file (``kiln.log_config.ScrubFilter``) — secrets must
  never land on disk, but the file stays on the user's own machine, so
  local addresses and paths remain readable for local debugging;
* the agent loop (``kiln.agent_loop._redact_sensitive_data``) — text sent
  to an external LLM drops secrets and private IPs, but keeps real file
  paths because the model must echo them back into tool calls;
* the bug-report boundary (:func:`redact_for_report`) — a report's text
  and its log tail leave the machine for Kiln's servers without secrets,
  private IPs or home-directory names (a ``/Users/<name>`` component is the
  user's real name).  A link the person wrote stays: a working URL form is
  often what a report is for;
* what Kiln reads off a printer (:mod:`kiln.outbound_guard`) — the report
  rules plus every place (:func:`redact_places`): web, network and hardware
  addresses, local host names, device paths and email addresses.

The surfaces intentionally differ in WHAT they redact; this module makes
them share HOW, so a pattern added here reaches all of them and the sets
can no longer drift apart.

Only stdlib modules are used.
"""

from __future__ import annotations

import re

DEFAULT_MARKER = "[REDACTED]"

# Value characters for labelled key/value secrets: stop at whitespace,
# quotes, commas, and closing braces/brackets so a secret at the end of a
# JSON value doesn't swallow the document's structural characters.
_VALUE = r"[^\"\x27\s,}{\]]+"

# Labelled key/value secrets.  Compound labels first so ``secret_key=``
# binds the full label rather than the bare ``secret`` alternative.
# Matches as a substring (``printer_token=x`` redacts at ``token=x``) —
# over-redacting a labelled value is always safe.
_SECRET_KV_RE = re.compile(
    r"((?:api[_-]?key|apikey|secret[_-]?key|access[_-]?token|auth[_-]?token"
    r"|access_code|credential|password|token|secret)"
    r"[\"\x27]?\s*[:=]\s*[\"\x27]?)(" + _VALUE + r")",
    re.IGNORECASE,
)

# Bearer tokens — bare form also covers ``Authorization: Bearer <t>``.
_BEARER_RE = re.compile(r"(Bearer\s+)(\S+)", re.IGNORECASE)
_BASIC_AUTH_RE = re.compile(r"(Authorization:\s*Basic\s+)(\S+)", re.IGNORECASE)
_AUTH_HEADER_RE = re.compile(r"(Authorization\s*:\s*)(\S+)", re.IGNORECASE)

# KILN_*_KEY= / KILN_*_SECRET= environment variable values.
_KILN_SECRET_RE = re.compile(r"(KILN_\w*(?:KEY|SECRET)\s*=\s*)\S+", re.IGNORECASE)

# Long hex / base64 tokens behind key-like labels (incl. the bare ``key``
# label the k/v pattern deliberately omits — ``key: value`` is too common
# in prose to redact unconditionally, but ``key: <32 hex chars>`` is not).
_HEX_TOKEN_RE = re.compile(
    r"((?:key|token|secret|password|credential|api_key|apikey)"
    r"\s*[:=]\s*[\"']?)([0-9a-fA-F]{32,})([\"']?)",
    re.IGNORECASE,
)
_BASE64_TOKEN_RE = re.compile(
    r"((?:key|token|secret|password|credential|api_key|apikey)"
    r"\s*[:=]\s*[\"']?)([A-Za-z0-9+/=]{20,})([\"']?)",
    re.IGNORECASE,
)

# RFC-1918 private address space.
_PRIVATE_IP_RE = re.compile(
    r"\b(?:"
    r"192\.168\.\d{1,3}\.\d{1,3}"
    r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2[0-9]|3[01])\.\d{1,3}\.\d{1,3}"
    r")\b"
)

# Home-directory components.  The USERNAME is the sensitive part (it is
# usually the user's real name); the rest of the path is kept because it
# is what makes a traceback debuggable.
_POSIX_HOME_RE = re.compile(r"((?:/Users|/home)/)([^/\\\s\"\x27:;,]+)")
_WINDOWS_HOME_RE = re.compile(
    r"((?:[A-Za-z]:)?\\Users\\)([^/\\\s\"\x27:;,]+)", re.IGNORECASE
)


# What locates a machine or a person, for text leaving for Kiln's servers.
# Web addresses first, so a host inside one is taken with it.
_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s\"\x27<>]+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_ANY_IPV4_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?![\w.])")
_IPV6_RE = re.compile(r"(?<![\w:])(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}(?![\w:])", re.IGNORECASE)
_MAC_RE = re.compile(r"(?<![\w:-])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![\w:-])", re.IGNORECASE)
_LOCAL_HOST_RE = re.compile(
    r"\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:local|lan|home|internal|localdomain)\b", re.IGNORECASE,
)
_DEVICE_PATH_RE = re.compile(r"/dev/[\w./-]+")


def redact_places(text: str) -> str:
    """Replace what locates a machine or a person in *text*: web addresses,
    email addresses, network and hardware addresses, local host names and
    device paths, each by a word saying what was there."""
    text = _URL_RE.sub("[a web address]", text)
    text = _EMAIL_RE.sub("[an email address]", text)
    text = _MAC_RE.sub("[a hardware address]", text)
    text = _ANY_IPV4_RE.sub("[a network address]", text)
    text = _IPV6_RE.sub("[a network address]", text)
    text = _LOCAL_HOST_RE.sub("[a network address]", text)
    return _DEVICE_PATH_RE.sub("[a device]", text)


def redact_secrets(text: str, marker: str = DEFAULT_MARKER) -> str:
    """Redact API keys, tokens, passwords, and auth headers from *text*."""
    text = _SECRET_KV_RE.sub(r"\1" + marker, text)
    text = _BEARER_RE.sub(r"\1" + marker, text)
    text = _BASIC_AUTH_RE.sub(r"\1" + marker, text)
    text = _AUTH_HEADER_RE.sub(r"\1" + marker, text)
    text = _KILN_SECRET_RE.sub(r"\1" + marker, text)
    text = _HEX_TOKEN_RE.sub(r"\1" + marker + r"\3", text)
    text = _BASE64_TOKEN_RE.sub(r"\1" + marker + r"\3", text)
    return text


def redact_private_ips(text: str, marker: str = DEFAULT_MARKER) -> str:
    """Redact RFC-1918 private IP addresses from *text*."""
    return _PRIVATE_IP_RE.sub(marker, text)


def redact_home_paths(text: str) -> str:
    """Replace the username component of home-directory paths.

    ``/Users/jane/x.stl`` → ``/Users/[USER]/x.stl`` (same for ``/home/``
    and ``\\Users\\``).  The path shape survives — only the account name,
    which is usually the user's real name, is dropped.
    """
    text = _POSIX_HOME_RE.sub(r"\1[USER]", text)
    text = _WINDOWS_HOME_RE.sub(r"\1[USER]", text)
    return text


def redact_for_report(text: str) -> str:
    """Full boundary redaction for text leaving the user's machine.

    Secrets + private IPs + home-directory usernames.  Links stay: a
    report is the person's own words, and the URL form that worked is often
    its whole point (:func:`redact_places` is for what Kiln reads off a
    printer, not for this).  UNCONDITIONAL —
    deliberately not gated by ``KILN_LLM_PRIVACY_MODE``, which controls
    LLM traffic only.  Turning that switch off must never quietly turn
    off bug-report redaction (they are unrelated privacy surfaces).
    """
    if not text:
        return text
    text = redact_secrets(text)
    text = redact_private_ips(text)
    return redact_home_paths(text)
