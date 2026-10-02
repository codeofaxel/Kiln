"""Turn a mesh on this machine into a link to Kiln's 3D stage.

WHY THIS EXISTS
---------------
Kiln's interactive stage — drag to rotate, look underneath, check the back —
was reachable only through Kiln's hosted connection.  A locally installed
Kiln, which is how nearly every user runs it, ended a design at a flat PNG:
the mesh was right there on disk and there was no way to turn it over.

The stage capability itself cannot be minted here.  It is scoped to a
verified account and signed with a key that only Kiln's API holds, so a
client can never mint its own.  What a client CAN do is hand the bytes over
and be given a link back, which is all this module does:

    stage_link_for("/path/to/part.stl") -> {"viewer_url": ..., "expires_at": ...}

DESIGN NOTES
------------
* **Never raises, never blocks for long.**  A preview that would otherwise
  have shipped must still ship if the network is down, the user is signed
  out, or the API is having a bad day.  Every failure returns ``None``.

* **The wait is the door's, not the caller's.**  The upload runs on its own
  thread and a caller waits :data:`_INLINE_WAIT_S` for it at most, counted
  from when the upload STARTED — so every caller in one tool call shares
  one wait and one upload, however many of them ask.  An upload that
  outlasts the wait keeps going and its link is in the cache for the next
  result that names the same bytes.  (2026-09-30: a make that built in four
  seconds and rendered in ten never reached its caller.  Two doors inside
  the same tool call each uploaded the mesh and each sat out the full
  transfer timeout while the servers were slow — forty-seven seconds of
  waiting for a link, past the minute an MCP client allows a call, so the
  mesh and its pictures were thrown away with it.  The bound had been put
  on one caller of this door rather than on the door.)

* **Content-addressed cache.**  A single tool call can render the same mesh
  from sixteen camera angles.  Keying on the file's own bytes means that
  costs one upload, not sixteen, and re-rendering an unchanged design costs
  none at all.  Bytes are the key rather than the path because a design
  iterated in place keeps its filename while becoming a different object.

* **Signed out is not an error.**  There is no account to scope a link to,
  so there is no link — and no scary message about it either.  The preview
  image is still there.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Opt out entirely (air-gapped installs, tests, anyone who would rather not
#: have geometry leave the machine for a preview link).
_OPT_OUT_ENV = "KILN_NO_STAGE_LINKS"

#: Matches the API route's own ceiling.  A mesh past it gets no link rather
#: than a doomed multi-minute upload.
_MAX_UPLOAD_BYTES = 64 * 1024 * 1024

#: The upload's own ceiling, per socket operation.  Generous, because it runs
#: on its own thread and holds up nobody: a large mesh on a slow line gets
#: the time it needs to arrive.  What a CALLER waits is :data:`_INLINE_WAIT_S`.
_TIMEOUT_S = 60.0

#: How long a caller waits for the link before answering without it, counted
#: from the start of the upload it is waiting on.  A small part's link is
#: back well inside this; a large one's lands in the cache afterwards.
_INLINE_WAIT_S = 8.0

#: After a wait has run out, the servers are known to be slow: for
#: :data:`_SLOW_MEMORY_S` a NEW upload is waited on only this long, so a tool
#: call that makes several meshes pays the full wait once rather than once
#: per mesh.  The uploads still run, and an upload that comes back quickly
#: ends the memory early.  Time-bounded on purpose — a bad minute must not
#: switch links off for the rest of the process.
_SLOW_WAIT_S = 1.0
_SLOW_MEMORY_S = 90.0

#: Uploads allowed to be running at once.  Past this a new mesh gets no
#: upload (reason ``busy``) rather than another thread holding megabytes:
#: someone iterating a large design while the servers crawl would otherwise
#: stack one transfer per revision.
_MAX_IN_FLIGHT = 3

#: Extensions the stage can open.  Checked before reading the file so an
#: unrelated artifact never gets uploaded looking for a link.
_MESH_SUFFIXES = frozenset({".stl", ".3mf", ".obj"})

#: The one bearer value the server refused with 401/403 this process.
#: Compared by VALUE: a fresh sign-in mints a different token and uploads
#: again; the same stale token skips the upload it already paid for once.
_REFUSED_BEARER: str | None = None

#: sha256 -> (viewer_url, expires_at_epoch).  Bounded; oldest evicted first.
_cache: dict[str, tuple[str, float]] = {}
_CACHE_MAX = 64

#: A link is only reused while it has this much life left, so a caller never
#: hands a user a URL that dies while they are looking at it.
_REUSE_FLOOR_S = 120.0


class _Upload:
    """One upload of one mesh, shared by every caller that wants its link."""

    __slots__ = ("started", "allowance", "done", "link", "reason", "evidence", "recorded", "said")

    def __init__(self, allowance: float) -> None:
        self.started = time.monotonic()
        self.allowance = allowance
        self.done = threading.Event()
        self.link: dict[str, Any] | None = None
        self.reason = ""
        #: Some caller wants the outcome on the preview record ...
        self.evidence = False
        #: ... and it has been written, once, by whoever got there first.
        self.recorded = False
        #: Held while the outcome is set or read, so "still uploading" is
        #: never recorded after the link it would contradict.
        self.said = threading.Lock()


#: cache key -> the upload running for it.  Guarded by ``_inflight_lock``.
_inflight: dict[str, _Upload] = {}
_inflight_lock = threading.Lock()

#: ``time.monotonic()`` until which the servers count as slow.
_slow_until = 0.0


def _api_base() -> str:
    """The hosted API base — ``KILN_API_URL`` override else the default.

    Same convention as ``terms._hosted_api_base`` and ``usage_ledger``; the
    server import is lazy so this module stays cheap to import.
    """
    override = (os.environ.get("KILN_API_URL") or "").strip()
    if override:
        return override.rstrip("/")
    try:
        from kiln.server import _HOSTED_KILN_API_URL

        return _HOSTED_KILN_API_URL.rstrip("/")
    except Exception:
        return "https://api.kiln3d.com"


def _cache_get(sha: str, min_life_s: float = _REUSE_FLOOR_S) -> tuple[str, float] | None:
    """The live entry for these bytes, or ``None``.

    Returns the whole entry rather than just the URL so a caller never has to
    read ``_cache`` a second time: tools run in a thread pool, and a second
    lookup can find the key already evicted by another thread.

    *min_life_s* is how long the link must still have for THIS caller.  An
    entry below :data:`_REUSE_FLOOR_S` is no use to anyone and is dropped;
    one merely short of a longer ask is left for the callers it still suits.
    """
    hit = _cache.get(sha)
    if not hit:
        return None
    url, expires_at = hit
    left = expires_at - time.time()
    if left <= _REUSE_FLOOR_S:
        _cache.pop(sha, None)
        return None
    if left < min_life_s:
        return None
    return url, expires_at


def _cache_put(sha: str, url: str, expires_at: float) -> None:
    if len(_cache) >= _CACHE_MAX:
        # Drop whatever expires soonest — it is the least useful to keep.
        oldest = min(_cache, key=lambda k: _cache[k][1])
        _cache.pop(oldest, None)
    _cache[sha] = (url, expires_at)


def _stage_printer_id() -> str | None:
    """The canonical printer id this install can honestly claim, or ``None``.

    Routed through :mod:`kiln.stage_plate` — the same resolver the inline
    stage's payload uses — so the two surfaces can never disagree about whose
    bed a design stands on.  ``None`` covers every unknown (no configured
    model, unrecognised model, hosted process), and none of them are worth a
    log line: the generic plate is the designed answer there.
    """
    try:
        from kiln.stage_plate import resolve_stage_plate

        plate = resolve_stage_plate()
        if plate.get("source") == "printer":
            return plate.get("printer_id") or None
    except Exception:  # noqa: BLE001 — furniture, never a failed link
        pass
    return None


def _slice_identity(path: Path) -> str:
    """Which slice belongs to *path*, as a cheap cache tag — the G-code's
    path and mtime, or ``""`` for a mesh nobody sliced.  A ledger read,
    never a parse."""
    try:
        from kiln.stage_plate import resolve_sliced_gcode

        gcode = resolve_sliced_gcode(str(path))
        if not gcode:
            return ""
        return f"{gcode}@{int(os.path.getmtime(gcode))}"
    except Exception:  # noqa: BLE001
        return ""


def _slicer_sidecar(path: Path) -> bytes | None:
    """The slicer-added-geometry sidecar for *path*, or ``None``.

    Built by :func:`kiln.slicer_geometry.sidecar_for_mesh` — the one place
    that decides which slice belongs to a mesh and what the block looks
    like.  Wrapped here so a link never fails over its furniture."""
    try:
        from kiln.slicer_geometry import sidecar_for_mesh

        return sidecar_for_mesh(path)
    except Exception:  # noqa: BLE001
        logger.debug("stage link: slicer sidecar skipped", exc_info=True)
        return None


def _stage_arrival(path: Path) -> str:
    """Where *path* came from, as the JSON the upload carries — the same
    ``kiln.arrival.v1`` block the inline stage's payload does — or ``""``
    for a file Kiln made.  The /view page says it under "Your model"."""
    try:
        import json

        from kiln.arrival import stage_block

        block = stage_block(str(path))
        return json.dumps(block, sort_keys=True) if block else ""
    except Exception:  # noqa: BLE001 — furniture, never a failed link
        return ""


def _stage_key(sha: str, printer_id: str | None, slice_tag: str) -> str:
    """What the stage draws, as one tag: the file's bytes, the bed it stands
    on, and the slice this machine holds for it.  The link cache files a
    stage under this, and the inline stage compares results by it, so the
    two can never disagree about when a drawing changed."""
    return f"{sha}:{printer_id or ''}:{slice_tag}"


def stage_identity(path: Path) -> str:
    """:func:`_stage_key` for *path* — ``""`` when the file cannot be read,
    which never equals anything."""
    sha = _sha256_of(path)
    if not sha:
        return ""
    return _stage_key(sha, _stage_printer_id(), _slice_identity(path))


def _sha256_of(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _issued(path: Path, url: str, expires_at: float) -> None:
    """A link exists for these bytes: on record, for the print gate."""
    try:
        from kiln.preview_evidence import record

        record("url", path, viewer_url=url, expires_at=float(expires_at))
    except Exception:  # noqa: BLE001 — furniture, never a failed link
        logger.debug("stage link evidence not recorded", exc_info=True)


def _refused(path: Path, reason: str, evidence: bool = True) -> None:
    """No link, and this door says why — the record a PNG-only sign-off
    needs, written by the door and never by the caller.

    The one place a refusal is said: every early return in
    :func:`stage_link_for` comes through here, so the log names every
    reason exactly once and :func:`last_refusal` reads the same record.
    A ``None`` with no trace anywhere is how a whole day of missing links
    went unexplained (2026-09-19).  *evidence* False logs the reason and
    records nothing (see :func:`stage_link_for`).
    """
    logger.debug("stage link refused (%s): %s", reason, path.name)
    if not evidence:
        return
    try:
        from kiln.preview_evidence import record_url_refusal

        record_url_refusal(path, reason)
    except Exception:  # noqa: BLE001
        logger.debug("stage link refusal not recorded", exc_info=True)


#: Refusals that are this install's own doing, as clauses.  The four that
#: are Kiln's servers' doing — offline, signed out, didn't answer, said no —
#: are worded by :mod:`kiln.served_answer`, in the one voice every served
#: door uses, so a person who hits them here reads the same sentence as at
#: any other door.  A result carries the clause; the code stays in the
#: evidence record for anything that branches on it.
_LOCAL_REFUSALS: dict[str, str] = {
    "opted_out": "browser links are switched off on this install (KILN_NO_STAGE_LINKS is set)",
    "too_large": "the part file is over the size a browser link accepts",
    "empty": "the part file is empty",
    "no_httpx": "this install is missing the httpx library, so it can't issue a browser link",
    "pending": "the browser link is still uploading — it rides the next result for this file",
    "busy": (
        "earlier browser links are still uploading, so this one was not started — "
        "it is issued the next time this file is shown"
    ),
}

#: Recorded reasons that map onto one of the four served causes.
#: ``transport`` is the word older records carry from before the split
#: into offline / unanswered; it reads as unanswered, the claim that asks
#: the least of the person.
_SERVED_CAUSE_OF: dict[str, str] = {
    "offline": "offline",
    "unanswered": "unanswered",
    "transport": "unanswered",
    "bad_response": "unanswered",
    "signed_out": "signed_out",
    "session_expired": "signed_out",
    "session_refused": "signed_out",
}

_CANNOT = "issue a browser link"


def refusal_sentence(reason: str | None) -> str:
    """Why no browser link was issued, as a clause a person can act on
    (it follows a colon or "because" in the result that carries it).
    ``None`` — no refusal on record — reads as no link having been asked
    for."""
    from kiln.served_answer import SESSION_EXPIRED_CODE, Miss, clause

    if not reason:
        return "no browser link was asked for"
    local = _LOCAL_REFUSALS.get(reason)
    if local:
        return local
    cause = _SERVED_CAUSE_OF.get(reason)
    if cause:
        miss = Miss(cause, SESSION_EXPIRED_CODE if reason == "session_expired" else "")
        return clause(miss, feature="servers", cannot=_CANNOT)
    if reason.startswith("http_"):
        try:
            status = int(reason[len("http_"):])
        except ValueError:
            status = 0
        if status == 401:
            return clause(Miss("signed_out"), feature="servers", cannot=_CANNOT)
        if status >= 500:
            return clause(Miss("unanswered"), feature="servers", cannot=_CANNOT)
        return clause(Miss("refused"), feature="servers", cannot=_CANNOT)
    return f"no browser link was issued ({reason})"


def last_refusal(mesh_path: str | os.PathLike[str]) -> str | None:
    """Why :func:`stage_link_for` last returned ``None`` for *mesh_path*, in
    the door's own word (``opted_out``, ``signed_out``, ``too_large``,
    ``offline``, ``unanswered``, ``http_503``, ...) — or ``None`` when no refusal is on
    record, or a link was issued for these bytes since.

    A read of the evidence record, so a caller that got ``None`` can say
    why without the ``None`` contract changing.  Never raises.
    """
    try:
        from kiln.preview_evidence import evidence_for

        ev = evidence_for(mesh_path)
        refusal = ev.get("url_refusal")
        if not isinstance(refusal, dict):
            return None
        link = ev.get("url")
        if isinstance(link, dict) and link.get("at", 0) >= refusal.get("at", 0):
            return None  # the newer fact is a link
        reason = refusal.get("reason")
        return reason if isinstance(reason, str) and reason else None
    except Exception:  # noqa: BLE001
        logger.debug("stage link refusal not readable", exc_info=True)
        return None



def stage_link_for(
    mesh_path: str | os.PathLike[str],
    *,
    evidence: bool = True,
    bearer: str | None = None,
    min_life_s: float | None = None,
) -> dict[str, Any] | None:
    """Return ``{"viewer_url", "expires_at"}`` for a local mesh, or ``None``.

    ``None`` covers every ordinary reason there is no link — opted out, not
    signed in, file missing or not a mesh, too large, network down, API
    unhappy.  None of those are worth interrupting a caller over: the
    preview image the caller already has is the floor.

    *evidence* False asks for the same link without writing the preview
    record (:mod:`kiln.preview_evidence`) — neither the link nor why there
    is none.  For a link handed somewhere other than the agent's own
    result, such as the card a print's ask puts on the account: nobody on
    this machine has been shown it, and the print gate reads that record
    as proof the file WAS shown, or as the reason a picture may stand in.

    *bearer* uploads as that credential rather than the one
    :func:`kiln.auth_session.resolve_api_bearer` picks, for a caller whose
    link must belong to one account: the stage on an ask's card is kept
    only when its link was minted for the account asking, and a license
    key in the environment would mint it for another.  A link cached under
    any other credential is never handed back for it.

    *min_life_s* is how long a cached link must still have to be reused
    (:data:`_REUSE_FLOOR_S` unless given): a caller that hands the link to
    something that lives longer asks for more, and gets a fresh upload —
    cheap, since the upload is content-addressed — when the cached one
    would die first.

    Waits :data:`_INLINE_WAIT_S` at most (see the module's design notes).
    ``None`` with ``pending`` on record means the upload is still running
    and the link will be in the cache when it lands.
    """
    path = Path(mesh_path)
    if (os.environ.get(_OPT_OUT_ENV) or "").strip().lower() in {"1", "true", "yes"}:
        _refused(path, "opted_out", evidence)
        return None

    try:
        if path.suffix.lower() not in _MESH_SUFFIXES or not path.is_file():
            return None
        size = path.stat().st_size
    except OSError:
        return None
    if size <= 0 or size > _MAX_UPLOAD_BYTES:
        _refused(path, "too_large" if size > 0 else "empty", evidence)
        return None

    sha = _sha256_of(path)
    if sha is None:
        return None
    # This install's printer rides along so the staged page can draw the
    # maker's real bed.  Resolved the same way the inline stage's payload
    # is (kiln.stage_plate): a machine we can actually name, or nothing.
    printer_id = _stage_printer_id()
    # So does the slice this machine made of the mesh — skirt, brim, prime
    # tower, supports — as a small sidecar in the mesh's own frame, so the
    # hosted page can offer the same "show what the slicer added" toggle
    # the inline panel does.  Only WHICH slice is resolved here (a ledger
    # read); the sidecar itself — a full parse of the G-code — is built
    # only on a cache miss, after the sign-in check, so sixteen still poses
    # of one mesh parse it once and a signed-out install never does.
    slice_tag = _slice_identity(path)
    # The printer is part of the link's identity: the token carries it, so a
    # config change between calls must not serve a link claiming the old bed.
    # The slice is too: a re-slice between calls must not serve a link
    # still wearing the previous slice's tower.
    cache_key = _stage_key(sha, printer_id, slice_tag)
    # Where the file came from rides the token too: a listing read after the
    # first link must not leave the page crediting nobody for half an hour.
    arrival = _stage_arrival(path)
    if arrival:
        cache_key += ":from:" + hashlib.sha256(arrival.encode("utf-8")).hexdigest()[:16]
    if bearer is not None:
        # Filed under who minted it as well, so a link made for another
        # credential is never handed back to a caller that named its own.
        cache_key += ":as:" + hashlib.sha256(bearer.encode("utf-8")).hexdigest()[:16]
    if min_life_s is None:
        cached = _cache_get(cache_key)
    else:
        cached = _cache_get(cache_key, max(_REUSE_FLOOR_S, float(min_life_s)))
    if cached:
        # Same bytes already staged — the sixteen-pose case, and the
        # re-render-an-unchanged-design case, both land here.
        if evidence:
            _issued(path, cached[0], cached[1])
        return {"viewer_url": cached[0], "expires_at": cached[1], "cached": True}

    state = ""
    if bearer is not None:
        token = bearer.strip()
    else:
        try:
            from kiln.auth_session import resolve_api_bearer

            resolved = resolve_api_bearer()
            token = getattr(resolved, "token", "") or ""
            state = getattr(resolved, "state", "") or ""
        except Exception:
            _refused(path, "signed_out", evidence)
            return None
    if not token:
        # Nothing to scope a link to; not a failure.  A sign-in this machine
        # can no longer renew is told to sign in AGAIN, which is what every
        # other surface tells the same person.
        _refused(
            path, "session_expired" if state == "needs_signin" else "signed_out", evidence
        )
        return None
    if token == _REFUSED_BEARER:
        # The server already refused THIS bearer this process (expired or
        # revoked session).  Without this memory every render re-uploaded
        # the full mesh just to collect the same 401 — measured 2026-08-19:
        # four multi-megabyte uploads refused inside one decorate call,
        # each spending upload time inside a live tool request.  A fresh
        # sign-in mints a different token and clears the skip by value.
        _refused(path, "session_refused", evidence)
        return None

    try:
        import httpx  # noqa: F401 — only whether it is there; the upload thread uses it
    except ImportError:
        _refused(path, "no_httpx", evidence)
        return None

    global _slow_until

    with _inflight_lock:
        upload = _inflight.get(cache_key)
        if upload is None and len(_inflight) < _MAX_IN_FLIGHT:
            slow = time.monotonic() < _slow_until
            upload = _Upload(_SLOW_WAIT_S if slow else _INLINE_WAIT_S)
            _inflight[cache_key] = upload
            threading.Thread(
                target=_run_upload,
                args=(upload, cache_key, path, token, printer_id, slice_tag, arrival),
                name="kiln-stage-link",
                daemon=True,
            ).start()
        if upload is not None and evidence:
            upload.evidence = True
    if upload is None:
        _refused(path, "busy", evidence)
        return None

    # One wait per upload, shared: a second caller in the same tool call is
    # owed only what is left of it, and one that arrives after it ran out
    # is answered at once.
    left = upload.allowance - (time.monotonic() - upload.started)
    if left > 0:
        upload.done.wait(left)
    with upload.said:
        if upload.done.is_set():
            if evidence and not upload.recorded:
                _record_outcome(path, upload.link, upload.reason)
                upload.recorded = True
            return dict(upload.link) if upload.link else None
        _refused(path, "pending", evidence)
    with _inflight_lock:
        _slow_until = time.monotonic() + _SLOW_MEMORY_S
    return None


def _record_outcome(path: Path, link: dict[str, Any] | None, reason: str) -> None:
    """Put an upload's outcome on the preview record: the link, or why not."""
    if link:
        _issued(path, link["viewer_url"], link["expires_at"])
    else:
        _refused(path, reason)


def _run_upload(
    upload: _Upload, cache_key: str, path: Path, token: str,
    printer_id: str | None, slice_tag: str, arrival: str = "",
) -> None:
    """The thread behind one :class:`_Upload`.  Never raises."""
    global _slow_until

    link: dict[str, Any] | None = None
    reason = "unanswered"
    try:
        link, reason = _upload(path, token, printer_id, slice_tag, arrival)
    except Exception:  # noqa: BLE001 — a link is furniture, never a crash
        logger.debug("stage link upload failed", exc_info=True)
    quick = time.monotonic() - upload.started < _INLINE_WAIT_S
    with _inflight_lock:
        if link:
            # Cached BEFORE the upload leaves the in-flight table, so a
            # caller arriving now finds one or the other, never neither.
            _cache_put(cache_key, link["viewer_url"], link["expires_at"])
            if quick:
                _slow_until = 0.0
        _inflight.pop(cache_key, None)
    with upload.said:
        upload.link, upload.reason = link, reason
        if upload.evidence:
            _record_outcome(path, link, reason)
            upload.recorded = True
        upload.done.set()


def _upload(
    path: Path, token: str, printer_id: str | None, slice_tag: str, arrival: str = "",
) -> tuple[dict[str, Any] | None, str]:
    """Hand the bytes over; ``(link, "")`` or ``(None, reason)``.

    The blocking half of :func:`stage_link_for`.  Writes nothing to the
    cache or the preview record — its caller owns both.
    """
    global _REFUSED_BEARER

    import httpx

    # A real upload, so the sidecar is worth building now: one parse of the
    # slice, keyed by the caller so the next call for the same mesh and
    # slice never pays it again.
    sidecar = _slicer_sidecar(path) if slice_tag else None

    try:
        with path.open("rb") as fh:
            files: dict[str, Any] = {
                "file": (path.name, fh, "application/octet-stream"),
            }
            if sidecar:
                files["slicer"] = ("slicer.json", sidecar, "application/json")
            resp = httpx.post(
                f"{_api_base()}/api/view/mesh",
                headers={"Authorization": f"Bearer {token}"},
                files=files,
                # The server canonicalises the claim and bakes it into the
                # signed link, so the /view page draws THIS machine's bed.
                # Where the file came from rides the same way, so the page
                # can say it.  A server that predates the field ignores it.
                data={
                    **({"printer": printer_id} if printer_id else {}),
                    **({"arrival": arrival} if arrival else {}),
                }
                or None,
                timeout=_TIMEOUT_S,
            )
    except Exception as exc:  # noqa: BLE001 — any transport failure is a no-link
        logger.debug("stage link unavailable: %s", exc)
        # Which of two things it was decides the fix the person is told:
        # no route (offline) or a server that did not answer.  No probe
        # here -- a preview never opens a second socket to find out.
        from kiln.served_answer import classify_transport_error

        return None, classify_transport_error(exc, probe=False).cause

    if resp.status_code in (401, 403):
        # An auth refusal is a property of the BEARER, not of this mesh —
        # remember it so the next render skips the upload instead of
        # paying for the same refusal again.
        _REFUSED_BEARER = token
        logger.debug("stage link refused: HTTP %s (bearer remembered)", resp.status_code)
        return None, f"http_{resp.status_code}"
    if resp.status_code != 200:
        logger.debug("stage link refused: HTTP %s", resp.status_code)
        return None, f"http_{resp.status_code}"
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return None, "bad_response"
    url = (body or {}).get("viewer_url")
    if not isinstance(url, str) or not url:
        return None, "bad_response"

    expires_at = body.get("viewer_expires_at")
    if not isinstance(expires_at, (int, float)):
        expires_in = body.get("expires_in")
        expires_at = time.time() + (
            expires_in if isinstance(expires_in, (int, float)) else 1800
        )
    return {"viewer_url": url, "expires_at": float(expires_at), "cached": False}, ""


# ---------------------------------------------------------------------------
# Attaching to a tool result
# ---------------------------------------------------------------------------

#: Result keys that name a renderable artifact.  Same word-segment
#: convention the preview-autofire gate matches on, so a tool that satisfies
#: that gate is automatically visible to this one.
_MESH_KEY_SEGMENTS = ("stl", "3mf", "mesh", "obj")

#: A mesh-changing tool reports BOTH the mesh it was handed and the mesh it
#: made.  Dict order decides nothing here: a key that names the input is
#: never the answer, or a repair would hand the user a link to the broken
#: version and call it the fix.
_INPUT_MARKERS = (
    "input", "source", "original", "before", "parent", "prev", "previous",
    "from", "base", "src",
)

#: ...and when several candidates remain, the one that names itself as the
#: product wins over a bare ``mesh``.
_OUTPUT_MARKERS = (
    "output", "result", "produced", "final", "new", "repaired", "decorated",
    "textured", "merged", "split", "generated", "exported", "written",
    # A preview is something the tool MADE, and the stage exists to show
    # what was made.  Without this a preview-only paint named only its
    # input, and the panel showed a grey jar beside a painted PNG.
    "preview",
)


def _looks_like_mesh_key(key: str) -> bool:
    k = key.lower()
    if k.endswith("_path"):
        k = k[: -len("_path")]
    parts = k.split("_")
    if any(seg == part for part in parts for seg in _MESH_KEY_SEGMENTS):
        return True
    # A key that names itself the PRODUCT does not also have to say "stl".
    # Every caller checks the value's suffix against _MESH_SUFFIXES, and that
    # suffix is ground truth — the key name only disambiguates WHICH mesh a
    # result means, so letting it veto a verified .stl is backwards.  Without
    # this, a tool reporting its mesh under a generic ``output_path`` is
    # invisible here: no token is minted, no geometry reaches the inline
    # stage, and because the tool is still stamped as stage-bearing the panel
    # opens EMPTY.  (2026-08-01: apply_geometric_texture, live.)
    return any(part in _OUTPUT_MARKERS for part in parts)


#: ...and a key that names THE STAGE says exactly which file the stage
#: shows, over any inference from the rest.  A slice result names the mesh
#: it was handed (an input, disqualified) and the file it wrote — raw
#: G-code, or a Bambu ``.gcode.3mf`` that ranks as the product — and
#: neither is the plate as it will print: that is the sliced mesh, dressed
#: in the slice's own skirt and tower.  So ``slice_file`` says which
#: (``stage_mesh_path``), and the door that knows is believed.
_STAGE_MARKERS = ("stage", "staged")


def _key_rank(key: str) -> int | None:
    """Preference for a mesh-shaped key: higher wins, ``None`` disqualifies."""
    parts = set(key.lower().split("_"))
    if parts & set(_INPUT_MARKERS):
        return None
    if parts & set(_STAGE_MARKERS):
        return 2
    return 1 if parts & set(_OUTPUT_MARKERS) else 0


def _stage_named(d: dict) -> tuple[bool, str | None]:
    """``(named, file)``: whether *d* names the stage's file outright, and
    that file when the stage can draw it.  A named file the stage cannot
    draw (a STEP the slicer took as-is) answers ``(True, None)``: nothing
    else in the result may stand in for it.  A wrapped ``.gcode.3mf`` is a
    print artifact, not the part the person is deciding about — and on a
    Bambu it can carry a 1 mm placeholder cube where the picture goes, so
    falling through to it stages a cube and calls the panel proven."""
    for key, value in d.items():
        if not (isinstance(value, str) and value):
            continue
        if set(key.lower().split("_")) & set(_STAGE_MARKERS):
            return True, (value if Path(value).suffix.lower() in _MESH_SUFFIXES else None)
    return False, None


def find_mesh_path(result: Any) -> str | None:
    """The renderable mesh a tool result points at, if any.

    Looks one level into nested dicts (a produced file is often reported
    under ``artifact`` or ``preview``) but no deeper — a deep crawl starts
    finding inputs and neighbours rather than the thing just made.

    A result that names the stage's file (``stage_mesh_path``) is believed
    absolutely, including when the file it names cannot be staged: see
    :func:`_stage_named`.
    """
    if not isinstance(result, dict):
        return None

    named, file = _stage_named(result)
    if named:
        return file
    for value in result.values():
        if isinstance(value, dict):
            named, file = _stage_named(value)
            if named:
                return file

    def _scan(d: dict) -> tuple[int, str] | None:
        best: tuple[int, str] | None = None
        for key, value in d.items():
            if not (isinstance(value, str) and value and _looks_like_mesh_key(key)):
                continue
            if Path(value).suffix.lower() not in _MESH_SUFFIXES:
                continue
            rank = _key_rank(key)
            if rank is None:
                continue
            if best is None or rank > best[0]:
                best = (rank, value)
        return best

    direct = _scan(result)
    if direct:
        return direct[1]
    for value in result.values():
        if isinstance(value, dict):
            nested = _scan(value)
            if nested:
                return nested[1]
    return None


def attach_stage_link(result: Any, mesh_path: str | os.PathLike[str] | None = None) -> Any:
    """Attach ``viewer_url`` to a dict tool result, in place.  Never raises.

    Idempotent: a result that already carries a ``viewer_url`` is left alone,
    so a tool that attached its own is never second-guessed and a backstop
    caller costs nothing.

    Returns ``result`` for chaining.  Non-dict results pass through
    untouched — by the time the MCP layer has serialised a result into
    content blocks there is no dict left to add a key to, and rewriting
    serialised text to sneak one in is how a wire format gets corrupted.
    """
    try:
        if not isinstance(result, dict) or result.get("viewer_url"):
            return result
        if result.get("success") is False:
            return result
        target = mesh_path or find_mesh_path(result)
        if not target:
            return result
        link = stage_link_for(target)
        if not link:
            return result
        result["viewer_url"] = link["viewer_url"]
        result["viewer_expires_at"] = link["expires_at"]
        # The agent needs to be told to hand this over; a URL sitting in a
        # payload that nobody mentions is the same as no URL.
        result.setdefault(
            "viewer_hint",
            "Give the user this viewer_url so they can turn the model over in "
            "3D — drag to rotate, scroll to zoom. The link is temporary.",
        )
    except Exception as exc:  # noqa: BLE001 — a preview must never die for a link
        logger.debug("stage link not attached: %s", exc)
    return result


async def attach_stage_link_async(
    result: Any, mesh_path: str | os.PathLike[str] | None = None
) -> Any:
    """``attach_stage_link`` for a caller that is already on an event loop.

    The upload is a blocking socket call.  Run straight from a coroutine it
    would stall the loop for as long as the transfer takes — on a local
    stdio server that is the WHOLE server: no other tool call, no
    heartbeat, nothing, while a mesh uploads.  So the work goes to a
    thread and the loop keeps serving.

    ``mesh_path`` names the mesh when the caller already knows it (the
    stage's result hook does — it just minted a token for it); otherwise
    the result is searched, as ``attach_stage_link`` searches it.

    Never raises.  Returns ``result`` for chaining.
    """
    import asyncio

    try:
        if not isinstance(result, dict) or result.get("viewer_url"):
            return result
        if result.get("success") is False:
            return result
        target = mesh_path or find_mesh_path(result)
        if not target:
            return result
        await asyncio.to_thread(attach_stage_link, result, target)
    except Exception as exc:  # noqa: BLE001
        logger.debug("stage link not attached: %s", exc)
    return result
