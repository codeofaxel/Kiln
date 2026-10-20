"""Decoration library — save and reuse proven decorations across models.

Decorations are stored as JSON manifests in a local library directory
(``~/.kiln/decorations/<slug>/manifest.json``).  Each decoration captures
the content file (heightmap, SVG, QR data), proven settings (depth, mode,
material, image_style), and metadata (tags, content_type) so that a
decoration that worked once can be reliably applied to new models.

Saving, listing, looking up, applying and deleting are free.  The
decoration's memory -- recording what printed well, replaying it, versions,
history and rollback -- is Kiln Pro, worked out on Kiln's servers and written
back into the decoration's own folder (:mod:`kiln._pro_decoration_bridge`).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

_MANIFEST_FILENAME = "manifest.json"
_LIBRARY_DIR_NAME = "decorations"

_VALID_CONTENT_TYPES = {
    "photo", "svg", "qr", "text",
    "procedural_texture", "ai_texture", "geometric_texture",
}

# Decoration categories — groups content_types for filtering/display.
DECORATION_CATEGORIES: dict[str, list[str]] = {
    "surface": ["photo", "svg", "qr", "text"],
    "texture": ["procedural_texture", "ai_texture", "geometric_texture"],
}

# Default settings per content type
_DEFAULTS: dict[str, dict[str, Any]] = {
    "photo": {"depth_mm": 0.6, "mode": "emboss", "image_style": "posterize"},
    "svg": {"depth_mm": 0.5, "mode": "deboss", "image_style": "auto"},
    "qr": {"depth_mm": 0.5, "mode": "emboss", "image_style": "auto"},
    "text": {"depth_mm": 0.4, "mode": "deboss", "image_style": "auto"},
    "procedural_texture": {"depth_mm": 0.0, "mode": "multicolor", "image_style": "auto"},
    "ai_texture": {"depth_mm": 0.0, "mode": "multicolor", "image_style": "auto"},
    # A single-color physical relief (no colour split) — the real carve
    # depth lives in the decoration's own texture_params, so this is only
    # the "nobody's ever printed this one before" fallback; 1.2mm/deboss
    # mirrors apply_geometric_texture's own catalog default for every
    # texture except carbon fiber (0.6mm) and brushed metal (0.8mm).
    "geometric_texture": {"depth_mm": 1.2, "mode": "deboss", "image_style": "auto"},
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class DecorationScaling:
    """Native size and detail limits for a decoration asset."""

    native_width_mm: float
    native_height_mm: float
    min_detail_mm: float = 0.0
    aspect_ratio: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "native_width_mm": self.native_width_mm,
            "native_height_mm": self.native_height_mm,
            "min_detail_mm": self.min_detail_mm,
            "aspect_ratio": self.aspect_ratio,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DecorationScaling:
        return cls(
            native_width_mm=d["native_width_mm"],
            native_height_mm=d["native_height_mm"],
            min_detail_mm=d.get("min_detail_mm", 0.0),
            aspect_ratio=d.get("aspect_ratio", 1.0),
        )


@dataclass
class ProvenSetting:
    """A print setting combination this decoration has printed with.

    ``success_count`` / ``failure_count`` count the prints reported with this
    decoration on this material, including through ``record_print_outcome``;
    ``last_failure_mode`` is the most recent failure category.
    """

    depth_mm: float
    mode: str = "emboss"
    image_style: str = "auto"
    success_count: int = 0
    failure_count: int = 0
    last_printed: str | None = None
    last_failed: str | None = None
    last_failure_mode: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "depth_mm": self.depth_mm,
            "mode": self.mode,
            "image_style": self.image_style,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "last_printed": self.last_printed,
            "last_failed": self.last_failed,
            "last_failure_mode": self.last_failure_mode,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ProvenSetting:
        return cls(
            depth_mm=d["depth_mm"],
            mode=d.get("mode", "emboss"),
            image_style=d.get("image_style", "auto"),
            success_count=d.get("success_count", 0),
            failure_count=d.get("failure_count", 0),
            last_printed=d.get("last_printed"),
            last_failed=d.get("last_failed"),
            last_failure_mode=d.get("last_failure_mode"),
        )


def category_for(content_type: str) -> str:
    """Return the decoration category for a given content_type.

    :returns: ``"texture"`` for procedural/AI textures, ``"surface"`` for
        photo/svg/qr/text, ``"unknown"`` for anything else.
    """
    for cat, types in DECORATION_CATEGORIES.items():
        if content_type in types:
            return cat
    return "unknown"


@dataclass
class Decoration:
    """A saved surface decoration with metadata and proven settings."""

    name: str
    slug: str
    content_type: str
    created: str
    source_file: str | None = None
    content_file: str | None = None
    content_data: str | None = None
    processing: dict[str, Any] | None = None
    scaling: DecorationScaling | None = None
    proven_settings: dict[str, ProvenSetting] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    print_count: int = 0
    version: int = 1
    parent_version: int | None = None
    changes: dict[str, str] | None = None
    texture_params: dict[str, Any] | None = None
    #: Prints already counted into ``proven_settings``, so a print reported
    #: twice counts once.  Kept by decoration memory (Kiln Pro).
    counted_jobs: list[str] = field(default_factory=list)

    @property
    def category(self) -> str:
        """The decoration category: ``"surface"`` or ``"texture"``."""
        return category_for(self.content_type)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "slug": self.slug,
            "content_type": self.content_type,
            "category": self.category,
            "created": self.created,
            "source_file": self.source_file,
            "content_file": self.content_file,
            "content_data": self.content_data,
            "processing": self.processing,
            "scaling": self.scaling.to_dict() if self.scaling else None,
            "proven_settings": {
                material: s.to_dict()
                for material, s in self.proven_settings.items()
            },
            "tags": self.tags,
            "print_count": self.print_count,
            "version": self.version,
        }
        if self.parent_version is not None:
            d["parent_version"] = self.parent_version
        if self.changes is not None:
            d["changes"] = self.changes
        if self.texture_params is not None:
            d["texture_params"] = self.texture_params
        if self.counted_jobs:
            d["counted_jobs"] = self.counted_jobs
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Decoration:
        scaling_raw = d.get("scaling")
        scaling = DecorationScaling.from_dict(scaling_raw) if scaling_raw else None
        proven_raw = d.get("proven_settings", {})
        if isinstance(proven_raw, list):
            # Legacy format: list of ProvenSetting dicts without material key
            proven = {}
            for p in proven_raw:
                proven.setdefault("PLA", ProvenSetting.from_dict(p))
        else:
            proven = {
                k: ProvenSetting.from_dict(v) for k, v in proven_raw.items()
            }
        return cls(
            name=d["name"],
            slug=d["slug"],
            content_type=d["content_type"],
            created=d.get("created", ""),
            source_file=d.get("source_file"),
            content_file=d.get("content_file"),
            content_data=d.get("content_data"),
            processing=d.get("processing"),
            scaling=scaling,
            proven_settings=proven,
            tags=d.get("tags", []),
            print_count=d.get("print_count", 0),
            version=d.get("version", 1),
            parent_version=d.get("parent_version"),
            changes=d.get("changes"),
            texture_params=d.get("texture_params"),
            counted_jobs=[
                str(j) for j in (d.get("counted_jobs") or []) if isinstance(j, str)
            ],
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slugify(name: str) -> str:
    """Convert a human name to a filesystem-safe slug.

    :raises ValueError: if the result would be empty.
    """
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    if not slug:
        raise ValueError("Slug is empty after sanitizing name")
    return slug


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Library directory
# ---------------------------------------------------------------------------


def get_library_dir() -> Path:
    """Return (and create) the decoration library directory."""
    env = os.environ.get("KILN_DECORATIONS_DIR")
    if env:
        lib = Path(env)
    else:
        home = Path(os.environ.get("HOME", str(Path.home())))
        lib = home / ".kiln" / _LIBRARY_DIR_NAME
    lib.mkdir(parents=True, exist_ok=True)
    return lib


def _detect_content_type(file_path: str) -> str:
    """Detect content type from file extension."""
    ext = Path(file_path).suffix.lower()
    if ext in (".heic", ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".dat"):
        return "photo"
    if ext == ".svg":
        return "svg"
    return "photo"  # default


def _decoration_dir(slug: str) -> Path:
    """Return the directory for a specific decoration."""
    return get_library_dir() / slug


def get_content_file_path(decoration: Decoration) -> str | None:
    """Return absolute path to the content file, or None if not found."""
    if not decoration.content_file:
        return None
    p = get_library_dir() / decoration.slug / decoration.content_file
    return str(p) if p.exists() else None


def get_source_file_path(decoration: Decoration) -> str | None:
    """Return absolute path to the source file, or None if not found."""
    if not decoration.source_file:
        return None
    p = get_library_dir() / decoration.slug / decoration.source_file
    return str(p) if p.exists() else None


# ---------------------------------------------------------------------------
# Manifest I/O
# ---------------------------------------------------------------------------


def _manifest_path(slug: str) -> Path:
    return get_library_dir() / slug / _MANIFEST_FILENAME


def _read_manifest(slug: str) -> dict[str, Any] | None:
    p = _manifest_path(slug)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_manifest(slug: str, data: dict[str, Any]) -> None:
    p = _manifest_path(slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def save_decoration(
    name: str,
    *,
    content_type: str,
    source_path: str | None = None,
    content_path: str | None = None,
    content_data: str | None = None,
    processing: dict[str, Any] | None = None,
    scaling: DecorationScaling | None = None,
    depth_mm: float = 0.0,
    mode: str = "emboss",
    image_style: str = "auto",
    material: str | None = None,
    tags: list[str] | None = None,
    texture_params: dict[str, Any] | None = None,
) -> Decoration:
    """Save a decoration to the library.

    :raises ValueError: on invalid inputs.
    :raises FileNotFoundError: if *source_path* or *content_path* does not exist.
    """
    if not name or not name.strip():
        raise ValueError("Decoration name must not be empty")
    if content_type not in _VALID_CONTENT_TYPES:
        raise ValueError(
            f"Invalid content_type '{content_type}'; "
            f"must be one of {sorted(_VALID_CONTENT_TYPES)}"
        )
    if not source_path and not content_path and not content_data:
        raise ValueError(
            "At least one of source_path, content_path, or content_data "
            "is required to provide content for the decoration"
        )

    slug = _slugify(name)
    lib = get_library_dir()
    deco_dir = lib / slug
    deco_dir.mkdir(parents=True, exist_ok=True)

    # Copy source file
    source_file: str | None = None
    if source_path:
        sp = Path(source_path)
        if not sp.exists():
            raise FileNotFoundError(f"Source file not found: {source_path}")
        dest = deco_dir / sp.name
        shutil.copy2(sp, dest)
        source_file = sp.name

    # Copy content file
    content_file: str | None = None
    if content_path:
        cp = Path(content_path)
        if not cp.exists():
            raise FileNotFoundError(f"Content file not found: {content_path}")
        dest = deco_dir / cp.name
        shutil.copy2(cp, dest)
        content_file = cp.name

    # Build initial proven settings if material provided
    proven: dict[str, ProvenSetting] = {}
    if material and depth_mm > 0:
        proven[material] = ProvenSetting(
            depth_mm=depth_mm,
            mode=mode,
            image_style=image_style,
            success_count=0,
            last_printed=None,
        )

    dec = Decoration(
        name=name,
        slug=slug,
        content_type=content_type,
        created=_now_iso(),
        source_file=source_file,
        content_file=content_file,
        content_data=content_data,
        processing=processing,
        scaling=scaling,
        proven_settings=proven,
        tags=tags or [],
        print_count=0,
        texture_params=texture_params,
    )
    _write_manifest(slug, dec.to_dict())
    _logger.debug("Saved decoration %r to %s", name, deco_dir)
    return dec


def list_decorations(
    *,
    content_type: str | None = None,
    category: str | None = None,
    tag: str | None = None,
) -> list[Decoration]:
    """List all saved decorations, optionally filtered.

    :param content_type: Filter by content type (None = all).
    :param category: Filter by category — ``"surface"`` or ``"texture"``
        (None = all).
    :param tag: Filter by tag (None = all).
    :returns: List of Decoration objects, sorted by last-printed descending.
    """
    lib = get_library_dir()
    results: list[Decoration] = []

    if not lib.exists():
        return results

    for entry in sorted(lib.iterdir()):
        manifest = entry / _MANIFEST_FILENAME
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            dec = Decoration.from_dict(data)
        except (json.JSONDecodeError, KeyError) as exc:
            _logger.debug("Skipping invalid manifest %s: %s", manifest, exc)
            continue

        if content_type and dec.content_type != content_type:
            continue
        if category and dec.category != category:
            continue
        if tag and tag not in dec.tags:
            continue
        results.append(dec)

    # Sort: decorations with last_printed come first (most recent first),
    # then those without by created date.
    def _sort_key(d: Decoration) -> str:
        for ps in d.proven_settings.values():
            if ps.last_printed:
                return ps.last_printed
        return d.created

    results.sort(key=_sort_key, reverse=True)
    return results


def get_decoration(name_or_slug: str) -> Decoration | None:
    """Look up a decoration by name or slug.

    :returns: The Decoration, or None if not found / corrupt manifest.
    """
    # Try as slug directly
    slug_attempt = _slugify(name_or_slug) if " " in name_or_slug else name_or_slug
    raw = _read_manifest(slug_attempt)
    if raw is not None:
        try:
            return Decoration.from_dict(raw)
        except (KeyError, TypeError):
            return None

    # Fallback: scan all manifests for matching name
    lib = get_library_dir()
    for child in lib.iterdir():
        m = child / _MANIFEST_FILENAME
        if not m.exists():
            continue
        r = _read_manifest(child.name)
        if r and r.get("name") == name_or_slug:
            try:
                return Decoration.from_dict(r)
            except (KeyError, TypeError):
                continue
    return None


def delete_decoration(name_or_slug: str) -> bool:
    """Delete a decoration directory.

    :returns: True if deleted, False if not found.
    """
    slug = _slugify(name_or_slug) if " " in name_or_slug else name_or_slug
    lib = get_library_dir()
    target = lib / slug
    if not target.exists():
        return False
    shutil.rmtree(target)
    return True


def default_settings(content_type: str) -> dict[str, Any]:
    """The standard print settings for a kind of decoration: what any
    decoration starts from before it has printed, and what a plan without
    decoration memory uses."""
    return dict(_DEFAULTS.get(content_type, _DEFAULTS["photo"]))


# ---------------------------------------------------------------------------
# Decoration memory (Kiln Pro, worked out on Kiln's servers)
#
# What printed well, versions, history and rollback.  Each function below
# keeps its contract; the change itself comes from Kiln's servers through
# :mod:`kiln._pro_decoration_bridge` and is written into the decoration's own
# folder here.  With no answer (offline, signed out, a plan without decoration
# memory) nothing on disk changes and
# :class:`kiln._pro_decoration_bridge.MemoryUnavailable` says why.
# ---------------------------------------------------------------------------


def _found(name_or_slug: str) -> Decoration:
    dec = get_decoration(name_or_slug)
    if dec is None:
        raise ValueError(f"Decoration not found: {name_or_slug}")
    return dec


def _record_outcome(
    outcome: str,
    name_or_slug: str,
    *,
    material: str,
    depth_mm: float,
    mode: str,
    image_style: str,
    failure_mode: str | None,
    source_job_id: str | None,
    asked_by_user: bool,
) -> Decoration:
    from kiln import _pro_decoration_bridge as bridge

    dec = _found(name_or_slug)
    deco_dir = _decoration_dir(dec.slug)
    answer = bridge.ask(
        "record", deco_dir, asked_by_user=asked_by_user, outcome=outcome,
        material=material, depth_mm=float(depth_mm), mode=mode,
        image_style=image_style, failure_mode=failure_mode or "",
        source_job_id=source_job_id or "",
    )
    plan = answer.get("plan")
    if isinstance(plan, dict) and plan.get("already_counted"):
        return dec
    return Decoration.from_dict(bridge.carry_out(deco_dir, dec.slug, plan))


def record_decoration_success(
    name_or_slug: str,
    *,
    material: str,
    depth_mm: float,
    mode: str = "emboss",
    image_style: str = "auto",
    source_job_id: str | None = None,
    asked_by_user: bool = True,
) -> Decoration:
    """Record a successful print with this decoration (Kiln Pro).

    :param source_job_id: The print this outcome came from.  Pass it and the
        same print reported twice counts once.
    :raises ValueError: if the decoration is not found.
    :raises kiln._pro_decoration_bridge.MemoryUnavailable: no answer from
        Kiln's servers; nothing was recorded.
    """
    return _record_outcome(
        "success", name_or_slug, material=material, depth_mm=depth_mm,
        mode=mode, image_style=image_style, failure_mode=None,
        source_job_id=source_job_id, asked_by_user=asked_by_user,
    )


def record_decoration_failure(
    name_or_slug: str,
    *,
    material: str,
    depth_mm: float,
    mode: str = "emboss",
    image_style: str = "auto",
    failure_mode: str | None = None,
    source_job_id: str | None = None,
    asked_by_user: bool = True,
) -> Decoration:
    """Record a failed print with this decoration (Kiln Pro).

    :param source_job_id: As on :func:`record_decoration_success`.
    :raises ValueError: if the decoration is not found.
    :raises kiln._pro_decoration_bridge.MemoryUnavailable: no answer from
        Kiln's servers; nothing was recorded.
    """
    return _record_outcome(
        "failed", name_or_slug, material=material, depth_mm=depth_mm,
        mode=mode, image_style=image_style, failure_mode=failure_mode,
        source_job_id=source_job_id, asked_by_user=asked_by_user,
    )


def resolve_decoration_settings(
    decoration: Decoration,
    *,
    material: str = "PLA",
    asked_by_user: bool = False,
) -> dict[str, Any]:
    """The settings to print this decoration with on *material*.

    Kiln Pro replays the settings it printed well with, from Kiln's servers.
    A decoration that has not printed well yet, and any call that gets no
    answer, uses the standard settings for its kind (``source: "default"``);
    a decoration that HAS printed well and was not replayed says why in
    ``note``.  Never raises.
    """
    from kiln import _pro_decoration_bridge as bridge

    standard = {**default_settings(decoration.content_type), "material": material, "source": "default"}
    if not any(ps.success_count > 0 for ps in decoration.proven_settings.values()):
        return standard
    try:
        answer = bridge.ask(
            "resolve", _decoration_dir(decoration.slug),
            asked_by_user=asked_by_user, material=material,
        )
    except bridge.MemoryUnavailable as miss:
        return {**standard, **_replay_note(miss.answer)}
    except (ValueError, OSError) as exc:
        _logger.debug("decoration settings not replayed: %s", exc)
        return standard
    settings = answer.get("settings")
    if not isinstance(settings, dict) or not {"depth_mm", "mode", "image_style"} <= set(settings):
        return standard
    return {**settings, "material": material}


def _replay_note(answer: dict[str, Any]) -> dict[str, Any]:
    """What a decoration that printed well is told when its settings were
    not replayed: the plan, said once, or why the servers did not answer."""
    from kiln.served_answer import classify_answer, clause, fields

    if str(answer.get("code") or "") == "TIER_REQUIRED":
        note: dict[str, Any] = {
            "note": (
                "Used the standard settings. Kiln Pro replays the settings "
                "this decoration printed well with."
            ),
        }
        if answer.get("upgrade_url"):
            note["upgrade_url"] = answer["upgrade_url"]
        return note
    miss = classify_answer(answer)
    if miss is None:
        return {}
    return {
        "note": "Used the standard settings: " + clause(
            miss, feature="servers",
            cannot="replay the settings this decoration printed well with",
            then="apply it again",
        ) + ".",
        **fields(miss),
    }


def iterate_decoration(
    name_or_slug: str,
    *,
    content_path: str | None = None,
    depth_mm: float | None = None,
    mode: str | None = None,
    image_style: str | None = None,
    processing: dict[str, Any] | None = None,
    scaling: DecorationScaling | None = None,
    notes: str = "",
    asked_by_user: bool = True,
) -> Decoration:
    """Create a new version of a decoration, archiving the current one (Kiln Pro).

    :raises ValueError: if the decoration is not found.
    :raises FileNotFoundError: if *content_path* does not exist.
    :raises kiln._pro_decoration_bridge.MemoryUnavailable: no answer from
        Kiln's servers; nothing changed.
    """
    from kiln import _pro_decoration_bridge as bridge

    current = _found(name_or_slug)
    if content_path and not Path(content_path).is_file():
        raise FileNotFoundError(f"Content file not found: {content_path}")
    deco_dir = _decoration_dir(current.slug)
    answer = bridge.ask(
        "iterate", deco_dir, asked_by_user=asked_by_user,
        depth_mm=float(depth_mm) if depth_mm else 0.0,
        mode=mode or "", image_style=image_style or "",
        content_suffix=Path(content_path).suffix if content_path else "",
        processing_changed=processing is not None,
        scaling_changed=scaling is not None,
    )
    return Decoration.from_dict(bridge.carry_out(
        deco_dir, current.slug, answer.get("plan"), content_path=content_path,
        overrides={
            "processing": processing,
            "scaling": scaling.to_dict() if scaling is not None else None,
        },
    ))


def rollback_decoration(
    name_or_slug: str,
    *,
    version: int,
    asked_by_user: bool = True,
) -> Decoration:
    """Restore an archived version as a NEW version (Kiln Pro); history is
    never rewritten.

    :raises ValueError: if the decoration or archived version is not found.
    :raises kiln._pro_decoration_bridge.MemoryUnavailable: no answer from
        Kiln's servers; nothing changed.
    """
    from kiln import _pro_decoration_bridge as bridge

    current = _found(name_or_slug)
    deco_dir = _decoration_dir(current.slug)
    if not (deco_dir / f"manifest.v{int(version)}.json").is_file():
        raise ValueError(
            f"Archived version {version} not found for decoration {name_or_slug!r}"
        )
    answer = bridge.ask("rollback", deco_dir, asked_by_user=asked_by_user, version=int(version))
    return Decoration.from_dict(bridge.carry_out(deco_dir, current.slug, answer.get("plan")))


def decoration_history(name_or_slug: str, *, asked_by_user: bool = True) -> list[dict[str, Any]]:
    """Every version of a decoration, oldest first (Kiln Pro).  An empty
    list when the decoration is not found.

    :raises kiln._pro_decoration_bridge.MemoryUnavailable: no answer from
        Kiln's servers.
    """
    from kiln import _pro_decoration_bridge as bridge

    slug = _slugify(name_or_slug) if " " in name_or_slug else name_or_slug
    deco_dir = _decoration_dir(slug)
    if not (deco_dir / _MANIFEST_FILENAME).is_file():
        return []
    history = bridge.ask("history", deco_dir, asked_by_user=asked_by_user).get("history")
    return history if isinstance(history, list) else []
