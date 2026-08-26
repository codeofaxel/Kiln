"""Canonical asset persistence — every saved asset lives under ``~/.kiln``.

The metadata for a saved artifact (a design's recipe, a decoration preset row,
a feature record) has always persisted durably.  The **asset** it points at —
the mesh, the image — historically stayed wherever it was first written, which
straight out of a generator is a **temporary directory**.  The metadata then
recorded that temp path, so a routine cleanup could orphan a saved artifact:
the recipe survives, the file it references evaporates.

This module is the single chokepoint that closes that hole.  ``persist_asset``
copies an asset **into** its durable ``~/.kiln`` home the moment it is saved —
content-addressed, atomic, idempotent — and returns the durable path so the
caller can rewrite its reference before persisting.  Route every save through
it and a saved artifact can never again point at ephemeral storage.

The rule (enforced by :mod:`kiln.asset_store` + the ``audit_asset_durability``
gate): a persisted design/decoration/feature asset MUST resolve under
``~/.kiln``.  If a user asks to *also* keep a copy elsewhere, that is additive —
the ``~/.kiln`` copy is never skipped.
"""

from __future__ import annotations

import hashlib
import os
import shutil

_CHUNK = 1 << 20  # 1 MiB streaming hash/copy

#: Provenance companions that must FOLLOW an asset into its durable home.
#: Each is a sidecar suffix appended to the asset's own filename.  The copy
#: is a dumb byte copy on purpose: a companion keyed to the asset's content
#: hash (decoration faces are keyed to the mesh's sha256) stays valid at the
#: new path because ``persist_asset`` copies content unchanged, and one that
#: was already stale keeps failing its own read-time hash gate at the new
#: home — validity is the reader's job, transport is ours.
_COMPANION_SUFFIXES = (".decoration_faces.json",)


def kiln_root() -> str:
    """Absolute path to the durable Kiln home (``~/.kiln``)."""
    return os.path.abspath(os.path.expanduser(os.path.join("~", ".kiln")))


def is_durable(path: str | None) -> bool:
    """True when *path* resolves inside the durable ``~/.kiln`` root.

    A durable asset survives temp-dir cleanup, reboots, and the scratch-dir
    pruner.  Anything under ``/tmp``, ``/var/folders`` (macOS), or any other
    location outside ``~/.kiln`` is NOT durable.
    """
    if not path:
        return False
    try:
        ap = os.path.abspath(os.path.expanduser(path))
    except (OSError, ValueError):
        return False
    root = kiln_root()
    return ap == root or ap.startswith(root + os.sep)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def persist_asset(
    src_path: str | None,
    dest_dir: str,
    *,
    prefix: str = "asset",
) -> str | None:
    """Copy *src_path* into *dest_dir* content-addressed; return the durable path.

    Repair-on-save + idempotent:

    * ``None`` / empty in → returned unchanged (nothing to persist).
    * Already durable (already under ``~/.kiln``) → returned unchanged; no copy.
    * Source missing / not a regular file → returned unchanged.  Nothing to
      copy — the caller keeps its honest (if dangling) reference rather than
      inventing one.
    * Otherwise the bytes are copied to
      ``<dest_dir>/<prefix>.<sha16>.<ext>`` (deduplicated by content hash,
      written atomically) and that absolute path is returned.

    ``dest_dir`` should itself be under ``~/.kiln`` (e.g. the design's own
    directory, or ``~/.kiln/decoration_assets``); callers pass the durable
    home for their artifact kind.

    Provenance companions (see :data:`_COMPANION_SUFFIXES`) sitting beside
    the source follow the asset to its durable home — copied even when the
    asset itself deduplicated, so a sidecar updated after the first save
    (a carve painted later) refreshes at the destination too.
    """
    if not src_path:
        return src_path
    abs_src = os.path.abspath(os.path.expanduser(src_path))
    if is_durable(abs_src):
        return abs_src
    if not os.path.isfile(abs_src):
        return src_path
    os.makedirs(dest_dir, exist_ok=True)
    sha = _sha256_file(abs_src)
    ext = os.path.splitext(abs_src)[1] or ""
    dest = os.path.join(os.path.abspath(dest_dir), f"{prefix}.{sha[:16]}{ext}")
    if not os.path.exists(dest):
        tmp = f"{dest}.{os.getpid()}.part"
        shutil.copy2(abs_src, tmp)
        os.replace(tmp, dest)  # atomic publish
    _persist_companions(abs_src, dest)
    return dest


def _persist_companions(abs_src: str, dest: str) -> None:
    """Carry an asset's provenance sidecars along with it.

    Runs even when the asset itself deduplicated (``dest`` already
    existed): the same mesh content can gain provenance later — a carve
    painted after the first save — and the latest sidecar must win at the
    durable home too.  Best-effort: losing a sidecar copy must never fail
    the asset persist that succeeded.
    """
    for suffix in _COMPANION_SUFFIXES:
        src_side = abs_src + suffix
        if not os.path.isfile(src_side):
            continue
        try:
            dest_side = dest + suffix
            tmp = f"{dest_side}.{os.getpid()}.part"
            shutil.copy2(src_side, tmp)
            os.replace(tmp, dest_side)
        except OSError:
            pass
