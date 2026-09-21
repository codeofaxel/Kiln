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

#: Generous enough for a large mesh on a slow line, short enough that a dead
#: API never becomes a hung tool call.
_TIMEOUT_S = 20.0

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


def _cache_get(sha: str) -> tuple[str, float] | None:
    """The live entry for these bytes, or ``None``.

    Returns the whole entry rather than just the URL so a caller never has to
    read ``_cache`` a second time: tools run in a thread pool, and a second
    lookup can find the key already evicted by another thread.
    """
    hit = _cache.get(sha)
    if not hit:
        return None
    url, expires_at = hit
    if expires_at - time.time() <= _REUSE_FLOOR_S:
        _cache.pop(sha, None)
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


def _refused(path: Path, reason: str) -> None:
    """No link, and this door says why — the record a PNG-only sign-off
    needs, written by the door and never by the caller.

    The one place a refusal is said: every early return in
    :func:`stage_link_for` comes through here, so the log names every
    reason exactly once and :func:`last_refusal` reads the same record.
    A ``None`` with no trace anywhere is how a whole day of missing links
    went unexplained (2026-09-19).
    """
    logger.debug("stage link refused (%s): %s", reason, path.name)
    try:
        from kiln.preview_evidence import record_url_refusal

        record_url_refusal(path, reason)
    except Exception:  # noqa: BLE001
        logger.debug("stage link refusal not recorded", exc_info=True)


#: The link door's refusal codes, as sentences.  A result carries the
#: sentence; the code stays in the evidence record for anything that
#: branches on it.
_REFUSAL_SENTENCES: dict[str, str] = {
    "opted_out": "browser stage links are switched off on this install (KILN_NO_STAGE_LINKS)",
    "signed_out": (
        "Kiln is signed out on this install, so no browser stage link can be "
        "issued — run kiln_signin"
    ),
    "session_refused": (
        "Kiln's sign-in on this install has expired or was revoked, so no "
        "browser stage link can be issued — run kiln_signin"
    ),
    "too_large": "the mesh is over the link door's upload limit",
    "empty": "the mesh file is empty",
    "no_httpx": "the httpx library is missing on this install, so no browser stage link can be issued",
    "transport": "the link service could not be reached",
    "bad_response": "the link service answered with something that was not a link",
}


def refusal_sentence(reason: str | None) -> str:
    """The link door's refusal *reason* as a sentence a person can act on.
    ``None`` — no refusal on record — reads as the door never having been
    asked."""
    if not reason:
        return "the link door was not asked"
    known = _REFUSAL_SENTENCES.get(reason)
    if known:
        return known
    if reason.startswith("http_"):
        return f"the link service answered HTTP {reason[len('http_'):]}"
    return f"the link door refused ({reason})"


def last_refusal(mesh_path: str | os.PathLike[str]) -> str | None:
    """Why :func:`stage_link_for` last returned ``None`` for *mesh_path*, in
    the door's own word (``opted_out``, ``signed_out``, ``too_large``,
    ``transport``, ``http_503``, ...) — or ``None`` when no refusal is on
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



def stage_link_for(mesh_path: str | os.PathLike[str]) -> dict[str, Any] | None:
    """Return ``{"viewer_url", "expires_at"}`` for a local mesh, or ``None``.

    ``None`` covers every ordinary reason there is no link — opted out, not
    signed in, file missing or not a mesh, too large, network down, API
    unhappy.  None of those are worth interrupting a caller over: the
    preview image the caller already has is the floor.
    """
    global _REFUSED_BEARER

    path = Path(mesh_path)
    if (os.environ.get(_OPT_OUT_ENV) or "").strip().lower() in {"1", "true", "yes"}:
        _refused(path, "opted_out")
        return None

    try:
        if path.suffix.lower() not in _MESH_SUFFIXES or not path.is_file():
            return None
        size = path.stat().st_size
    except OSError:
        return None
    if size <= 0 or size > _MAX_UPLOAD_BYTES:
        _refused(path, "too_large" if size > 0 else "empty")
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
    cache_key = f"{sha}:{printer_id or ''}:{slice_tag}"
    cached = _cache_get(cache_key)
    if cached:
        # Same bytes already staged — the sixteen-pose case, and the
        # re-render-an-unchanged-design case, both land here.
        _issued(path, cached[0], cached[1])
        return {"viewer_url": cached[0], "expires_at": cached[1], "cached": True}

    try:
        from kiln.auth_session import resolve_api_bearer

        bearer = resolve_api_bearer()
    except Exception:
        _refused(path, "signed_out")
        return None
    token = getattr(bearer, "token", "") or ""
    if not token:
        # Signed out.  Nothing to scope a link to; not a failure.
        _refused(path, "signed_out")
        return None
    if token == _REFUSED_BEARER:
        # The server already refused THIS bearer this process (expired or
        # revoked session).  Without this memory every render re-uploaded
        # the full mesh just to collect the same 401 — measured 2026-08-19:
        # four multi-megabyte uploads refused inside one decorate call,
        # each spending upload time inside a live tool request.  A fresh
        # sign-in mints a different token and clears the skip by value.
        _refused(path, "session_refused")
        return None

    try:
        import httpx
    except ImportError:
        _refused(path, "no_httpx")
        return None

    # A real upload, so the sidecar is worth building now: one parse of the
    # slice, keyed above so the next call for the same mesh and slice never
    # pays it again.
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
                data={"printer": printer_id} if printer_id else None,
                timeout=_TIMEOUT_S,
            )
    except Exception as exc:  # noqa: BLE001 — any transport failure is a no-link
        logger.debug("stage link unavailable: %s", exc)
        _refused(path, "transport")
        return None

    if resp.status_code in (401, 403):
        # An auth refusal is a property of the BEARER, not of this mesh —
        # remember it so the next render skips the upload instead of
        # paying for the same refusal again.
        _REFUSED_BEARER = token
        logger.debug("stage link refused: HTTP %s (bearer remembered)", resp.status_code)
        _refused(path, f"http_{resp.status_code}")
        return None
    if resp.status_code != 200:
        logger.debug("stage link refused: HTTP %s", resp.status_code)
        _refused(path, f"http_{resp.status_code}")
        return None
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        _refused(path, "bad_response")
        return None
    url = (body or {}).get("viewer_url")
    if not isinstance(url, str) or not url:
        _refused(path, "bad_response")
        return None

    expires_at = body.get("viewer_expires_at")
    if not isinstance(expires_at, (int, float)):
        expires_in = body.get("expires_in")
        expires_at = time.time() + (
            expires_in if isinstance(expires_in, (int, float)) else 1800
        )
    _cache_put(cache_key, url, float(expires_at))
    _issued(path, url, float(expires_at))
    return {"viewer_url": url, "expires_at": float(expires_at), "cached": False}


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
