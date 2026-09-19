"""Which door showed the person the thing they are signing off on.

The print gate has always accepted a preview token as proof that a
preview happened.  It never recorded what KIND of preview: a PNG render
and an inline 3D stage minted the same token, so a print signed off from
six stills was indistinguishable from one the person had turned over in
the panel.  Measured 2026-09-19, live, on a sliced multicolour jar — the
stage never opened, no link was issued, and the file was accepted.

The rule is a preference order, and this module is what makes the order
enforceable rather than advisory:

* ``stage`` — the inline MCP Apps panel.  Evidence: this machine served a
  viewer payload for the file's bytes (the panel fetched it, or the
  geometry rode the result to a host that draws it).
* ``url`` — a hosted stage link the person can open.  Evidence: a link was
  issued for the file's bytes and has not expired.
* ``png`` — static renders.  The floor.  Evidence: a render happened, AND
  the stage was unavailable for a reason the server can name, AND the
  link door refused for a reason it recorded itself.

Every fact here is written by the door that did the thing — the panel's
fetch verb, the link issuer, the renderer — never by the caller of
``issue_preview_token``.  A caller says which door it used; this module
checks that claim against the record and refuses a claim the record does
not support.

WHY A FILE, KEYED BY BYTES.  A desktop host runs one Kiln server per
open session and routes a panel's fetch over whichever connection it
holds, so the server that served the stage is often not the one issuing
the token (measured 2026-09-01, on the stage-token ledger this mirrors).
The record therefore lives under ``~/.kiln`` where every sibling server
reads it.  It is keyed by the file's content hash, not its path: a
re-sliced file keeps its name and becomes a different object, and the old
sign-off must not follow it.

NOT ON THE HOSTED DEPLOY.  The shared multi-tenant server has one disk for
every account, and a served render there would record "this file was
shown" under a hash another account's identical file resolves to.  Nothing
on that box can start a print or consume a token, so the record is inert,
but it is still one tenant's fact on another tenant's answer.  So on the
hosted deploy nothing is recorded and nothing is read: every door reads as
"no evidence", the same as a fresh install.  Judged 2026-09-18 by the
tenant-state ledger in kiln-pro; the same skip safety_profiles.py uses.

A print file is usually not the thing the stage showed.  The stage shows
the DESIGN mesh; the printer receives the SLICE.  The slice ledger
(:mod:`kiln.monitor_twin`) is the one place that knows which mesh a
slice came from, so evidence recorded for ``jar.stl`` counts for the
``jar.gcode.3mf`` sliced from it — and for nothing else.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DOOR_STAGE = "stage"
DOOR_URL = "url"
DOOR_PNG = "png"
DOORS: tuple[str, ...] = (DOOR_STAGE, DOOR_URL, DOOR_PNG)

#: How long a record vouches for a file.  A stage opened this morning does
#: not sign off a print started this evening; opening it again costs one
#: tool call, and the person gets to look at what is actually about to
#: print.  Longer than the token TTL on purpose: the token is the yes, the
#: evidence is what the yes was about.
EVIDENCE_TTL_S = 3600.0

#: Per-door refusal codes, so an agent can branch without parsing prose.
CODE_UNKNOWN_DOOR = "PREVIEW_DOOR_UNKNOWN"
CODE_NOT_USED = "PREVIEW_DOOR_NOT_USED"
CODE_SKIPPED = "PREVIEW_DOOR_SKIPPED"

_MAX_ENTRIES = 256
_mem: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()

_MESH_SUFFIXES = frozenset({".stl", ".3mf", ".obj"})


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _ledger_path() -> Path:
    home = Path(os.environ.get("KILN_HOME", "").strip() or (Path.home() / ".kiln"))
    return home / "preview_evidence.json"


def _shared_disk() -> bool:
    """True on the hosted multi-tenant deploy, where the ledger is skipped.

    Called at each door's entry, outside the best-effort ``try`` blocks, so
    the skip is a decision the door makes and never something a broad
    handler swallows.
    """
    from kiln.runtime_env import is_hosted_multitenant

    return is_hosted_multitenant()


def _file_hash(path: str | os.PathLike[str] | None) -> str | None:
    """The gate's own content hash, or ``None`` for a file that is not there."""
    if not path:
        return None
    try:
        from kiln.preview_gate import hash_file

        digest = hash_file(str(path))
    except Exception:  # noqa: BLE001
        return None
    return None if digest.startswith("NO_FILE:") else digest


def _read_ledger() -> dict[str, dict[str, Any]]:
    try:
        entries = json.loads(_ledger_path().read_text())
        return entries if isinstance(entries, dict) else {}
    except Exception:  # noqa: BLE001 — a corrupt ledger is an empty ledger
        return {}


def _write_ledger(entries: dict[str, dict[str, Any]]) -> None:
    """Atomic, private, bounded.  Never raises: evidence that cannot be
    written is a refused token later, not a failed preview now."""
    try:
        import tempfile

        path = _ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        while len(entries) > _MAX_ENTRIES:
            oldest = min(entries, key=lambda k: entries[k].get("touched", 0.0))
            entries.pop(oldest, None)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".preview_evidence_")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(entries, fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except Exception:  # noqa: BLE001
        logger.debug("preview evidence ledger write skipped", exc_info=True)


def _entry(digest: str) -> dict[str, Any] | None:
    """The record for these bytes: this process's copy first, then the
    ledger a sibling server may have written."""
    with _lock:
        hit = _mem.get(digest)
    if hit is not None:
        return hit
    hit = _read_ledger().get(digest)
    if isinstance(hit, dict):
        with _lock:
            _mem[digest] = hit
        return hit
    return None


def _update(digest: str, path: str, key: str, facts: dict[str, Any]) -> None:
    with _lock:
        entries = _read_ledger()
        entry = dict(entries.get(digest) or _mem.get(digest) or {})
        entry["path"] = os.path.abspath(path)
        entry["touched"] = time.time()
        entry[key] = facts
        entries[digest] = entry
        _mem[digest] = entry
        _write_ledger(entries)


# ---------------------------------------------------------------------------
# Recording — called by the doors, never by the caller
# ---------------------------------------------------------------------------


def record(door: str, file_path: str | os.PathLike[str], **facts: Any) -> str | None:
    """Note that *door* actually showed *file_path*.  Returns the hash it
    was recorded under, or ``None`` when the file could not be hashed.
    Never raises.  Records nothing on the hosted deploy."""
    if _shared_disk():
        return None
    try:
        if door not in DOORS:
            return None
        digest = _file_hash(file_path)
        if not digest:
            return None
        _update(digest, str(file_path), door, {"at": time.time(), **facts})
        return digest
    except Exception:  # noqa: BLE001
        logger.debug("preview evidence not recorded", exc_info=True)
        return None


def record_url_refusal(file_path: str | os.PathLike[str], reason: str) -> None:
    """The link door tried and could not: say why, in its own words.

    This is what lets a PNG-only sign-off be honest — the caller cannot
    assert "the link failed"; the link door has to have said so."""
    if _shared_disk():
        return
    try:
        digest = _file_hash(file_path)
        if digest:
            _update(digest, str(file_path), "url_refusal", {"at": time.time(), "reason": reason})
    except Exception:  # noqa: BLE001
        logger.debug("preview link refusal not recorded", exc_info=True)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def design_mesh_for(file_path: str | os.PathLike[str]) -> str | None:
    """The mesh this machine sliced *file_path* from, if the slice ledger
    knows one and it is still on disk.  ``None`` for a mesh itself, a file
    Kiln did not slice, or the hosted server (whose ledger is nobody's)."""
    if _shared_disk():
        return None
    try:
        from kiln.monitor_twin import sliced_entry_for

        entry = sliced_entry_for(os.path.basename(str(file_path)))
        if not entry:
            return None
        mesh = str(entry.get("input") or "")
        if mesh and os.path.isfile(mesh) and os.path.abspath(mesh) != os.path.abspath(str(file_path)):
            return os.path.abspath(mesh)
    except Exception:  # noqa: BLE001
        logger.debug("design mesh not resolved", exc_info=True)
    return None


def _fresh(facts: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(facts, dict):
        return None
    at = facts.get("at")
    if not isinstance(at, (int, float)) or time.time() - at > EVIDENCE_TTL_S:
        return None
    return facts


def evidence_for(file_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Everything on record for *file_path*, its design mesh included.

    Each door key holds the freshest record from the file itself or from
    the mesh it was sliced from, or ``None``.  Stale records read as
    absent — see :data:`EVIDENCE_TTL_S`.
    """
    path = str(file_path)
    out: dict[str, Any] = {
        "file": os.path.basename(path),
        "file_hash": _file_hash(path),
        "design_mesh": design_mesh_for(path),
        DOOR_STAGE: None,
        DOOR_URL: None,
        DOOR_PNG: None,
        "url_refusal": None,
    }
    if _shared_disk():
        return out
    sources: list[dict[str, Any]] = []
    if out["file_hash"]:
        own = _entry(out["file_hash"])
        if own:
            sources.append(own)
    if out["design_mesh"]:
        mesh_hash = _file_hash(out["design_mesh"])
        mesh_entry = _entry(mesh_hash) if mesh_hash else None
        if mesh_entry:
            sources.append(mesh_entry)
    for key in (DOOR_STAGE, DOOR_URL, DOOR_PNG, "url_refusal"):
        for src in sources:
            facts = _fresh(src.get(key))
            if facts and (out[key] is None or facts["at"] > out[key]["at"]):
                out[key] = facts
    return out


def _stageable(file_path: str, design_mesh: str | None) -> bool:
    if design_mesh:
        return True
    return Path(file_path).suffix.lower() in _MESH_SUFFIXES


def stage_unavailable_reason(
    file_path: str | os.PathLike[str], *, host_renders: bool | None, design_mesh: str | None = None,
) -> str | None:
    """Why the inline stage cannot show *file_path* right now — or ``None``
    when it can, which is the answer that refuses a PNG-only sign-off.

    *host_renders* is what the connected host declared (or proved by
    reading the stage document).  ``None`` means nobody asked — a CLI
    process, say — and reads as "no panel", because there is none.
    """
    try:
        from kiln import local_stage

        if not local_stage.enabled():
            return "the inline stage is switched off on this install (KILN_NO_LOCAL_STAGE)"
    except Exception:  # noqa: BLE001
        return "the inline stage is not installed here"
    if not host_renders:
        return "this host draws no MCP Apps panel"
    if not _stageable(str(file_path), design_mesh):
        return "neither this file nor a design mesh Kiln sliced it from can be staged"
    return None


# ---------------------------------------------------------------------------
# Judging a claim
# ---------------------------------------------------------------------------


def _refusal(message: str, code: str) -> dict[str, str]:
    return {"message": message, "code": code}


def _age(facts: dict[str, Any]) -> str:
    return f"{int(time.time() - facts['at'])}s ago"


def judge(
    file_path: str | os.PathLike[str], door: str, *, host_renders: bool | None,
) -> tuple[dict[str, str] | None, dict[str, Any]]:
    """Check a caller's claim that *door* previewed *file_path*.

    Returns ``(refusal, verdict)``.  ``refusal`` is ``None`` when the claim
    holds; otherwise a ``{message, code}`` whose message tells an agent,
    in one sentence, what to do next.  ``verdict`` carries the door, the
    evidence relied on, and — for a PNG sign-off — why each better door
    was unavailable, in the server's words.
    """
    path = str(file_path)
    name = os.path.basename(path) or path
    verdict: dict[str, Any] = {"door": door, "evidence": {}, "skipped": {}}
    if door not in DOORS:
        return _refusal(
            "issue_preview_token needs door='stage', 'url' or 'png': the inline stage "
            "first, the viewer link (url) if this host draws no panel, and PNG renders "
            "only when neither is possible.",
            CODE_UNKNOWN_DOOR,
        ), verdict

    ev = evidence_for(path)
    verdict["evidence"] = {
        "file_hash": ev["file_hash"],
        "design_mesh": ev["design_mesh"],
    }
    if ev["file_hash"] is None:
        return _refusal(
            f"{name} is not on this machine, so nothing can be verified about its preview; "
            "issue the token from the local file that was shown.",
            CODE_NOT_USED,
        ), verdict

    stage_line = (
        f"open Kiln's inline 3D stage on {name} (or the design mesh it was sliced from) "
        "so the panel fetches its geometry, then call issue_preview_token again with "
        "door='stage'"
    )
    url_line = (
        f"call visualize_model(file_path, share_link=True) on {name}, hand the user its "
        "viewer_url, then call issue_preview_token again with door='url'"
    )

    if door == DOOR_STAGE:
        if ev[DOOR_STAGE] is None:
            stale = _stale_note(path, DOOR_STAGE)
            return _refusal(
                f"No inline stage payload has been served for {name}{stale}: {stage_line}; "
                "if this host draws no panel, issue the link instead (door='url').",
                CODE_NOT_USED,
            ), verdict
        verdict["evidence"][DOOR_STAGE] = ev[DOOR_STAGE]
        return None, verdict

    if door == DOOR_URL:
        link = ev[DOOR_URL]
        if link is None:
            stale = _stale_note(path, DOOR_URL)
            return _refusal(f"No viewer link has been issued for {name}{stale}: {url_line}.", CODE_NOT_USED), verdict
        expires_at = link.get("expires_at")
        if isinstance(expires_at, (int, float)) and expires_at <= time.time():
            return _refusal(
                f"The viewer link for {name} has expired: issue a fresh one — {url_line}.",
                CODE_NOT_USED,
            ), verdict
        verdict["evidence"][DOOR_URL] = link
        return None, verdict

    # door == DOOR_PNG — the floor, and only when the floor is all there is.
    if ev[DOOR_STAGE] is not None:
        return _refusal(
            f"The inline stage was already served for {name} ({_age(ev[DOOR_STAGE])}); "
            "call issue_preview_token again with door='stage' — PNG renders are the "
            "fallback, not the record.",
            CODE_SKIPPED,
        ), verdict
    link = ev[DOOR_URL]
    if link is not None and (
        not isinstance(link.get("expires_at"), (int, float)) or link["expires_at"] > time.time()
    ):
        return _refusal(
            f"A viewer link is live for {name}; hand the user its viewer_url and call "
            "issue_preview_token again with door='url'.",
            CODE_SKIPPED,
        ), verdict
    no_stage = stage_unavailable_reason(path, host_renders=host_renders, design_mesh=ev["design_mesh"])
    if no_stage is None:
        return _refusal(
            f"PNG renders are the last resort and the inline 3D stage is available on this "
            f"host: {stage_line}.",
            CODE_SKIPPED,
        ), verdict
    refusal = ev["url_refusal"]
    if refusal is None:
        return _refusal(
            f"No link attempt is on record for {name}: {url_line}; when it returns no "
            "viewer_url the reason is recorded and door='png' is then accepted.",
            CODE_SKIPPED,
        ), verdict
    if ev[DOOR_PNG] is None:
        return _refusal(
            f"No PNG render is on record for {name}: call visualize_model(file_path) and "
            "show the user the renders, then call issue_preview_token again with door='png'.",
            CODE_NOT_USED,
        ), verdict
    verdict["skipped"] = {
        DOOR_STAGE: no_stage,
        DOOR_URL: f"the link door refused: {refusal.get('reason', 'unknown')}",
    }
    verdict["evidence"][DOOR_PNG] = ev[DOOR_PNG]
    return None, verdict


def _stale_note(path: str, door: str) -> str:
    """" (last served Ns ago)" when a record exists but has aged out, so
    the refusal says re-open rather than implying nothing ever happened."""
    digest = _file_hash(path)
    entry = _entry(digest) if digest else None
    facts = (entry or {}).get(door)
    if isinstance(facts, dict) and isinstance(facts.get("at"), (int, float)):
        return f" in the last hour (last {_age(facts)})"
    return ""


def _reset_for_tests() -> None:
    with _lock:
        _mem.clear()
