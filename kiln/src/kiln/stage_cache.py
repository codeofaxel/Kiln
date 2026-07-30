"""The 3D stage document, cached on this machine.

WHY THIS EXISTS
---------------
Kiln's inline 3D stage — the panel a conversation renders so you can turn a
part over instead of squinting at a still — is one self-contained HTML
document.  Shipping it inside the package would freeze it at whatever the
release day looked like; fetching it on every render would make the first
design of a session wait on a download.  So it is fetched once, cached on
disk, and revalidated cheaply against the server's ETag.

    warm()      -> pull it in the background at server start
    document()  -> the document, or None if there has never been one

DESIGN NOTES
------------
* **Never raises, never blocks the server.**  Every failure path ends in
  "no stage this session," which is exactly the behaviour of every install
  before the stage existed.  A cold cache with no network is not an error
  worth a traceback.

* **The cached copy outlives the network.**  Once the document is on disk it
  is served forever, revalidated when it happens to be convenient.  An
  offline machine keeps the stage it already has; it does not lose a feature
  because a laptop woke up on a plane.

* **Not caller-scoped.**  This is the same static document for every user on
  every install — a downloaded asset, like the model cache next to it.  It
  holds no design, no account, and no geometry; losing the whole directory
  costs one download.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

#: Opt out of the network fetch entirely (air-gapped installs, tests).  A
#: document already on disk is still served — this stops the fetch, not the
#: stage.
_OPT_OUT_ENV = "KILN_NO_STAGE_FETCH"

#: Long enough for 750 KB on a slow line, short enough that a dead API never
#: turns into a hung server start.
_TIMEOUT_S = 20.0

#: The document is one inlined bundle (three.js + the stage).  Well past what
#: it plausibly grows to, and small enough that a redirect to something else
#: entirely never lands in the cache.
_MAX_BYTES = 16 * 1024 * 1024

#: A stage document is HTML.  Anything else is a captive portal, an error
#: page, or a misconfigured proxy — none of which belong in the cache.
_HTML_SNIFF = b"<!doctype html"

_DOC_NAME = "mesh_viewer.html"
_ETAG_NAME = "mesh_viewer.etag"

#: Read once per process after the first hit — the document is ~750 KB and a
#: mesh result must not pay a disk read to find that out.
_memo: str | None = None
_lock = threading.Lock()


def _api_base() -> str:
    """The hosted API base — ``KILN_API_URL`` override else the default.

    Same convention as ``stage_link`` and ``usage_ledger``; the server import
    is lazy so this module stays cheap to import.
    """
    override = (os.environ.get("KILN_API_URL") or "").strip()
    if override:
        return override.rstrip("/")
    try:
        from kiln.server import _HOSTED_KILN_API_URL

        return _HOSTED_KILN_API_URL.rstrip("/")
    except Exception:
        return "https://api.kiln3d.com"


def cache_dir() -> Path:
    """``~/.kiln/stage_cache`` (``KILN_HOME`` respected), created on demand."""
    home = Path(os.environ.get("KILN_HOME", "").strip() or (Path.home() / ".kiln"))
    d = home / "stage_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_disk() -> str | None:
    try:
        return (cache_dir() / _DOC_NAME).read_text(encoding="utf-8")
    except OSError:
        return None


def _read_etag() -> str | None:
    try:
        etag = (cache_dir() / _ETAG_NAME).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return etag or None


def _write(document: str, etag: str | None) -> None:
    """Write the pair atomically enough that a crash can't pair a new ETag
    with an old document — the shape that makes a stale stage permanent."""
    d = cache_dir()
    tmp = d / f"{_DOC_NAME}.tmp"
    tmp.write_text(document, encoding="utf-8")
    tmp.replace(d / _DOC_NAME)
    etag_path = d / _ETAG_NAME
    if etag:
        etag_path.write_text(etag, encoding="utf-8")
    else:
        # No ETag from the server: drop any old one rather than let it
        # revalidate a document it no longer describes.
        etag_path.unlink(missing_ok=True)


def _fetch(etag: str | None) -> tuple[str, str | None] | None:
    """``(document, etag)`` from the API, ``None`` for "keep what you have".

    ``None`` covers both a 304 (the cached copy is current, which is the
    point of sending the ETag) and every failure — offline, DNS, a 500, a
    body that isn't an HTML document.  A caller that already has a document
    keeps it; a caller that doesn't gets no stage, quietly.
    """
    try:
        import httpx
    except ImportError:
        return None

    headers = {"If-None-Match": etag} if etag else {}
    try:
        resp = httpx.get(
            f"{_api_base()}/api/mcp-apps/mesh-viewer",
            headers=headers,
            timeout=_TIMEOUT_S,
            follow_redirects=True,
        )
    except Exception as exc:  # noqa: BLE001 — any transport failure is a no-op
        logger.debug("stage document not fetched: %s", exc)
        return None

    if resp.status_code == 304:
        return None
    if resp.status_code != 200:
        logger.debug("stage document refused: HTTP %s", resp.status_code)
        return None

    raw = resp.content
    if not raw or len(raw) > _MAX_BYTES:
        logger.debug("stage document rejected: %d bytes", len(raw))
        return None
    if raw.lstrip()[: len(_HTML_SNIFF)].lower() != _HTML_SNIFF:
        # A captive portal or an error page dressed as a 200.  Caching it
        # would replace the stage with someone else's login form.
        logger.debug("stage document rejected: not an HTML document")
        return None
    try:
        return raw.decode("utf-8"), resp.headers.get("ETag")
    except UnicodeDecodeError:
        return None


def refresh() -> str | None:
    """Bring the cache up to date if the network allows.  Never raises.

    Returns **the document this machine now has** — which after a failed
    fetch is the one it already had, and after a first-ever failure is
    ``None``.  Answering "what do you have" rather than "did the fetch
    succeed" is the honest shape: a 500 from the API is not a loss when the
    stage is already on disk, and reporting it as one would be a lie in the
    log and a trap for a caller.
    """
    global _memo
    if (os.environ.get(_OPT_OUT_ENV) or "").strip().lower() in {"1", "true", "yes"}:
        return document()
    try:
        on_disk = _read_disk()
        fresh = _fetch(_read_etag() if on_disk else None)
        if fresh is None:
            return document()
        doc, etag = fresh
        _write(doc, etag)
        with _lock:
            _memo = doc
        return doc
    except Exception:  # noqa: BLE001 — a stage refresh must never break a server
        logger.debug("stage cache refresh failed", exc_info=True)
        return None


def warm() -> threading.Thread | None:
    """Start the refresh on a daemon thread.  Returns it, or ``None``.

    Called at server start so the first design of a session finds the
    document already there.  On a background thread because a cold cache is
    a 750 KB download and no user should watch a server boot behind it.
    """
    try:
        t = threading.Thread(target=refresh, daemon=True, name="kiln-stage-cache-warm")
        t.start()
        return t
    except Exception:  # noqa: BLE001
        logger.debug("stage cache warm not started", exc_info=True)
        return None


def document() -> str | None:
    """The cached stage document, or ``None`` if this machine has never had one.

    Reads disk at most once per process.  Deliberately does NOT fetch: this
    is called while a host waits on a ``resources/read``, and a synchronous
    download there would hang the panel on a slow line — the warm at server
    start is what fills the cache.
    """
    global _memo
    with _lock:
        if _memo is not None:
            return _memo
    doc = _read_disk()
    if doc is None:
        return None
    with _lock:
        _memo = doc
    return _memo


def _reset_for_tests() -> None:
    global _memo
    with _lock:
        _memo = None
