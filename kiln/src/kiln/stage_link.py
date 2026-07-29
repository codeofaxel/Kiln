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


def _cache_get(sha: str) -> str | None:
    hit = _cache.get(sha)
    if not hit:
        return None
    url, expires_at = hit
    if expires_at - time.time() <= _REUSE_FLOOR_S:
        _cache.pop(sha, None)
        return None
    return url


def _cache_put(sha: str, url: str, expires_at: float) -> None:
    if len(_cache) >= _CACHE_MAX:
        # Drop whatever expires soonest — it is the least useful to keep.
        oldest = min(_cache, key=lambda k: _cache[k][1])
        _cache.pop(oldest, None)
    _cache[sha] = (url, expires_at)


def _sha256_of(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def stage_link_for(mesh_path: str | os.PathLike[str]) -> dict[str, Any] | None:
    """Return ``{"viewer_url", "expires_at"}`` for a local mesh, or ``None``.

    ``None`` covers every ordinary reason there is no link — opted out, not
    signed in, file missing or not a mesh, too large, network down, API
    unhappy.  None of those are worth interrupting a caller over: the
    preview image the caller already has is the floor.
    """
    if (os.environ.get(_OPT_OUT_ENV) or "").strip().lower() in {"1", "true", "yes"}:
        return None

    path = Path(mesh_path)
    try:
        if path.suffix.lower() not in _MESH_SUFFIXES or not path.is_file():
            return None
        size = path.stat().st_size
    except OSError:
        return None
    if size <= 0 or size > _MAX_UPLOAD_BYTES:
        return None

    sha = _sha256_of(path)
    if sha is None:
        return None
    cached = _cache_get(sha)
    if cached:
        # Same bytes already staged — the sixteen-pose case, and the
        # re-render-an-unchanged-design case, both land here.
        return {"viewer_url": cached, "expires_at": _cache[sha][1], "cached": True}

    try:
        from kiln.auth_session import resolve_api_bearer

        bearer = resolve_api_bearer()
    except Exception:
        return None
    token = getattr(bearer, "token", "") or ""
    if not token:
        # Signed out.  Nothing to scope a link to; not a failure.
        return None

    try:
        import httpx
    except ImportError:
        return None

    try:
        with path.open("rb") as fh:
            resp = httpx.post(
                f"{_api_base()}/api/view/mesh",
                headers={"Authorization": f"Bearer {token}"},
                files={"file": (path.name, fh, "application/octet-stream")},
                timeout=_TIMEOUT_S,
            )
    except Exception as exc:  # noqa: BLE001 — any transport failure is a no-link
        logger.debug("stage link unavailable: %s", exc)
        return None

    if resp.status_code != 200:
        logger.debug("stage link refused: HTTP %s", resp.status_code)
        return None
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return None
    url = (body or {}).get("viewer_url")
    if not isinstance(url, str) or not url:
        return None

    expires_at = body.get("viewer_expires_at")
    if not isinstance(expires_at, (int, float)):
        expires_in = body.get("expires_in")
        expires_at = time.time() + (
            expires_in if isinstance(expires_in, (int, float)) else 1800
        )
    _cache_put(sha, url, float(expires_at))
    return {"viewer_url": url, "expires_at": float(expires_at), "cached": False}


# ---------------------------------------------------------------------------
# Attaching to a tool result
# ---------------------------------------------------------------------------

#: Result keys that name a renderable artifact.  Same word-segment
#: convention the preview-autofire gate matches on, so a tool that satisfies
#: that gate is automatically visible to this one.
_MESH_KEY_SEGMENTS = ("stl", "3mf", "mesh", "obj")


def _looks_like_mesh_key(key: str) -> bool:
    k = key.lower()
    if k.endswith("_path"):
        k = k[: -len("_path")]
    return any(seg == part for part in k.split("_") for seg in _MESH_KEY_SEGMENTS)


def find_mesh_path(result: Any) -> str | None:
    """The renderable mesh a tool result points at, if any.

    Looks one level into nested dicts (a produced file is often reported
    under ``artifact`` or ``preview``) but no deeper — a deep crawl starts
    finding inputs and neighbours rather than the thing just made.
    """
    if not isinstance(result, dict):
        return None

    def _scan(d: dict) -> str | None:
        for key, value in d.items():
            if isinstance(value, str) and value and _looks_like_mesh_key(key):
                if Path(value).suffix.lower() in _MESH_SUFFIXES:
                    return value
        return None

    direct = _scan(result)
    if direct:
        return direct
    for value in result.values():
        if isinstance(value, dict):
            nested = _scan(value)
            if nested:
                return nested
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
