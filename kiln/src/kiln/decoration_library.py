"""Decoration library — save and reuse proven decorations across models.

Decorations are stored as JSON manifests in a local library directory
(``~/.kiln/decorations/<slug>/manifest.json``).  Each decoration captures
the content file (heightmap, SVG, QR data), proven settings (depth, mode,
material, image_style), and metadata (tags, content_type) so that a
decoration that worked once can be reliably applied to new models.
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
    "procedural_texture", "ai_texture",
}

# Decoration categories — groups content_types for filtering/display.
DECORATION_CATEGORIES: dict[str, list[str]] = {
    "surface": ["photo", "svg", "qr", "text"],
    "texture": ["procedural_texture", "ai_texture"],
}

# Default settings per content type
_DEFAULTS: dict[str, dict[str, Any]] = {
    "photo": {"depth_mm": 0.6, "mode": "emboss", "image_style": "posterize"},
    "svg": {"depth_mm": 0.5, "mode": "deboss", "image_style": "auto"},
    "qr": {"depth_mm": 0.5, "mode": "emboss", "image_style": "auto"},
    "text": {"depth_mm": 0.4, "mode": "deboss", "image_style": "auto"},
    "procedural_texture": {"depth_mm": 0.0, "mode": "multicolor", "image_style": "auto"},
    "ai_texture": {"depth_mm": 0.0, "mode": "multicolor", "image_style": "auto"},
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
    """A print setting combination that has been proven to work.

    ``success_count`` and ``failure_count`` are auto-incremented from
    :func:`record_print_outcome` when a print job carries a
    ``decoration_slug``, so proven status reflects actual field data
    rather than self-reports.  ``last_failure_mode`` captures the most
    recent failure category (warping, adhesion, etc.) so later runs can
    avoid the same pitfall.
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
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _write_manifest(slug: str, data: dict[str, Any]) -> None:
    p = _manifest_path(slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))


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
            data = json.loads(manifest.read_text())
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


def record_decoration_success(
    name_or_slug: str,
    *,
    material: str,
    depth_mm: float,
    mode: str = "emboss",
    image_style: str = "auto",
) -> Decoration:
    """Record a successful print, updating proven settings.

    :raises ValueError: if the decoration is not found.
    """
    dec = get_decoration(name_or_slug)
    if dec is None:
        raise ValueError(f"Decoration not found: {name_or_slug}")

    now = _now_iso()

    if material in dec.proven_settings:
        ps = dec.proven_settings[material]
        ps.success_count += 1
        ps.last_printed = now
        # Update depth/mode/style if they changed
        ps.depth_mm = depth_mm
        ps.mode = mode
        ps.image_style = image_style
    else:
        dec.proven_settings[material] = ProvenSetting(
            depth_mm=depth_mm,
            mode=mode,
            image_style=image_style,
            success_count=1,
            last_printed=now,
        )

    dec.print_count += 1
    _write_manifest(dec.slug, dec.to_dict())
    return dec


def record_decoration_failure(
    name_or_slug: str,
    *,
    material: str,
    depth_mm: float,
    mode: str = "emboss",
    image_style: str = "auto",
    failure_mode: str | None = None,
) -> Decoration:
    """Record a failed print for this decoration + material combination.

    Mirror of :func:`record_decoration_success` — increments
    ``failure_count`` on the matching :class:`ProvenSetting` and stamps
    ``last_failed`` / ``last_failure_mode``.  When the combination has
    never been printed before, a new :class:`ProvenSetting` is created
    with ``success_count=0`` so the record reflects the failure without
    falsely claiming a proven pairing.

    :raises ValueError: if the decoration is not found.
    """
    dec = get_decoration(name_or_slug)
    if dec is None:
        raise ValueError(f"Decoration not found: {name_or_slug}")

    now = _now_iso()

    if material in dec.proven_settings:
        ps = dec.proven_settings[material]
        ps.failure_count += 1
        ps.last_failed = now
        ps.last_failure_mode = failure_mode
        # Depth/mode/style reflect the MOST RECENT attempt — useful when
        # diagnosing whether a setting drift caused the failure.
        ps.depth_mm = depth_mm
        ps.mode = mode
        ps.image_style = image_style
    else:
        dec.proven_settings[material] = ProvenSetting(
            depth_mm=depth_mm,
            mode=mode,
            image_style=image_style,
            success_count=0,
            failure_count=1,
            last_failed=now,
            last_failure_mode=failure_mode,
        )

    dec.print_count += 1
    _write_manifest(dec.slug, dec.to_dict())
    return dec


def compute_decoration_scale(
    decoration: Decoration,
    *,
    target_face_width_mm: float,
    target_face_height_mm: float,
) -> float:
    """Compute the scale factor to fit a decoration onto a target face.

    Applies a ~10% margin and respects min_detail_mm if set.
    """
    scaling = decoration.scaling
    if not scaling:
        return 1.0

    # Apply 10% margin
    usable_w = target_face_width_mm * 0.9
    usable_h = target_face_height_mm * 0.9

    scale_w = usable_w / scaling.native_width_mm if scaling.native_width_mm else 1.0
    scale_h = usable_h / scaling.native_height_mm if scaling.native_height_mm else 1.0
    scale = min(scale_w, scale_h)

    # Clamp so min_detail stays above minimum printable threshold for FDM
    _min_printable_detail_mm = 0.4
    if scaling.min_detail_mm > 0 and scale < 1.0:
        effective_detail = scaling.min_detail_mm * scale
        if effective_detail < _min_printable_detail_mm:
            scale = _min_printable_detail_mm / scaling.min_detail_mm

    return scale


def resolve_decoration_settings(
    decoration: Decoration,
    *,
    material: str = "PLA",
) -> dict[str, Any]:
    """Resolve the best print settings for this decoration + material.

    Uses proven settings if available, otherwise falls back to per-type defaults.
    """
    # First: exact material match from proven settings
    if material in decoration.proven_settings:
        ps = decoration.proven_settings[material]
        if ps.success_count > 0:
            return {
                "depth_mm": ps.depth_mm,
                "mode": ps.mode,
                "image_style": ps.image_style,
                "material": material,
                "source": "proven",
                "success_count": ps.success_count,
            }

    # Second: any proven setting with success > 0
    for mat, ps in decoration.proven_settings.items():
        if ps.success_count > 0:
            return {
                "depth_mm": ps.depth_mm,
                "mode": ps.mode,
                "image_style": ps.image_style,
                "material": material,
                "source": "proven_other",
                "success_count": ps.success_count,
                "proven_material": mat,
            }

    # Third: content-type defaults
    defaults = _DEFAULTS.get(decoration.content_type, _DEFAULTS["photo"])
    return {
        "depth_mm": defaults["depth_mm"],
        "mode": defaults["mode"],
        "image_style": defaults["image_style"],
        "material": material,
        "source": "default",
    }


# ---------------------------------------------------------------------------
# Versioning helpers
# ---------------------------------------------------------------------------


def _max_version(slug: str) -> int:
    """Return the highest version number across all archived and current manifests.

    Scans ``manifest.v*.json`` files and the current ``manifest.json`` to
    determine the maximum version in use.
    """
    deco_dir = _decoration_dir(slug)
    if not deco_dir.exists():
        return 0

    highest = 0

    # Check current manifest
    current = _read_manifest(slug)
    if current:
        highest = max(highest, current.get("version", 1))

    # Check versioned archives
    for child in deco_dir.iterdir():
        m = re.match(r"^manifest\.v(\d+)\.json$", child.name)
        if m:
            highest = max(highest, int(m.group(1)))

    return highest


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
) -> Decoration:
    """Create a new version of a decoration, archiving the current one.

    :raises ValueError: if the decoration is not found.
    """
    current = get_decoration(name_or_slug)
    if current is None:
        raise ValueError(f"Decoration not found: {name_or_slug}")

    slug = current.slug
    deco_dir = _decoration_dir(slug)
    old_version = current.version

    # Archive current manifest
    archive_path = deco_dir / f"manifest.v{old_version}.json"
    src_manifest = _manifest_path(slug)
    if src_manifest.exists():
        shutil.copy2(src_manifest, archive_path)

    # Build changes dict
    changes: dict[str, str] = {}
    if depth_mm is not None:
        # Compare against first proven setting's depth or note the change
        for ps in current.proven_settings.values():
            if ps.depth_mm != depth_mm:
                changes["depth_mm"] = f"{ps.depth_mm} -> {depth_mm}"
            break
    if mode is not None and mode != "":
        for ps in current.proven_settings.values():
            if ps.mode != mode:
                changes["mode"] = f"{ps.mode} -> {mode}"
            break
    if image_style is not None and image_style != "":
        for ps in current.proven_settings.values():
            if ps.image_style != image_style:
                changes["image_style"] = f"{ps.image_style} -> {image_style}"
            break
    if content_path is not None:
        changes["content_file"] = "updated"
    if processing is not None:
        changes["processing"] = "updated"
    if scaling is not None:
        changes["scaling"] = "updated"

    new_version = old_version + 1

    # Handle new content file
    new_content_file = current.content_file
    if content_path:
        cp = Path(content_path)
        if not cp.exists():
            raise FileNotFoundError(f"Content file not found: {content_path}")
        ext = cp.suffix
        versioned_name = f"content.v{new_version}{ext}"
        shutil.copy2(cp, deco_dir / versioned_name)
        new_content_file = versioned_name

    # Update proven settings if depth/mode/image_style changed
    new_proven = {}
    for mat, ps in current.proven_settings.items():
        new_proven[mat] = ProvenSetting(
            depth_mm=depth_mm if depth_mm is not None else ps.depth_mm,
            mode=mode if mode is not None and mode != "" else ps.mode,
            image_style=image_style if image_style is not None and image_style != "" else ps.image_style,
            success_count=ps.success_count,
            last_printed=ps.last_printed,
        )

    dec = Decoration(
        name=current.name,
        slug=slug,
        content_type=current.content_type,
        created=_now_iso(),
        source_file=current.source_file,
        content_file=new_content_file,
        content_data=current.content_data,
        processing=processing if processing is not None else current.processing,
        scaling=scaling if scaling is not None else current.scaling,
        proven_settings=new_proven if new_proven else current.proven_settings,
        tags=list(current.tags),
        print_count=current.print_count,
        version=new_version,
        parent_version=old_version,
        changes=changes if changes else None,
    )
    _write_manifest(slug, dec.to_dict())
    _logger.debug("Iterated decoration %r v%d -> v%d", slug, old_version, new_version)
    return dec


def rollback_decoration(
    name_or_slug: str,
    *,
    version: int,
) -> Decoration:
    """Roll back a decoration to a previous archived version.

    Creates a NEW version (not the old version number) that restores the
    settings from the archived version.

    :raises ValueError: if the decoration or archived version is not found.
    """
    current = get_decoration(name_or_slug)
    if current is None:
        raise ValueError(f"Decoration not found: {name_or_slug}")

    slug = current.slug
    deco_dir = _decoration_dir(slug)
    archive_path = deco_dir / f"manifest.v{version}.json"
    if not archive_path.exists():
        raise ValueError(
            f"Archived version {version} not found for decoration {name_or_slug!r}"
        )

    # Load the archived version
    try:
        archived_data = json.loads(archive_path.read_text())
        archived = Decoration.from_dict(archived_data)
    except (json.JSONDecodeError, KeyError) as exc:
        raise ValueError(f"Corrupt archive manifest.v{version}.json: {exc}") from exc

    # Archive the CURRENT manifest before overwriting
    current_version = current.version
    current_archive = deco_dir / f"manifest.v{current_version}.json"
    src_manifest = _manifest_path(slug)
    if src_manifest.exists() and not current_archive.exists():
        shutil.copy2(src_manifest, current_archive)

    # New version = max of all versions + 1
    new_version = _max_version(slug) + 1

    # Build the rolled-back decoration
    dec = Decoration(
        name=archived.name,
        slug=slug,
        content_type=archived.content_type,
        created=_now_iso(),
        source_file=archived.source_file,
        content_file=archived.content_file,
        content_data=archived.content_data,
        processing=archived.processing,
        scaling=archived.scaling,
        proven_settings=archived.proven_settings,
        tags=list(archived.tags),
        print_count=archived.print_count,
        version=new_version,
        parent_version=current_version,
        changes={"rollback": f"restored from v{version}"},
    )
    _write_manifest(slug, dec.to_dict())
    _logger.debug(
        "Rolled back decoration %r to v%d as new v%d", slug, version, new_version
    )
    return dec


def decoration_history(name_or_slug: str) -> list[dict[str, Any]]:
    """Return version history for a decoration, sorted by version ascending.

    Each entry includes version, created, changes, proven materials, and
    print count.  Returns an empty list if the decoration is not found.
    """
    slug = _slugify(name_or_slug) if " " in name_or_slug else name_or_slug
    deco_dir = _decoration_dir(slug)
    if not deco_dir.exists():
        return []

    entries: list[dict[str, Any]] = []

    # Gather all manifests (versioned + current)
    for child in deco_dir.iterdir():
        data: dict[str, Any] | None = None
        if child.name == _MANIFEST_FILENAME:
            data = _read_manifest(slug)
        elif re.match(r"^manifest\.v\d+\.json$", child.name):
            try:
                data = json.loads(child.read_text())
            except (json.JSONDecodeError, OSError):
                continue

        if data is None:
            continue

        try:
            dec = Decoration.from_dict(data)
        except (KeyError, TypeError):
            continue

        proven_materials = sorted(dec.proven_settings.keys())
        entries.append({
            "version": dec.version,
            "created": dec.created,
            "changes": dec.changes,
            "proven_materials": proven_materials,
            "print_count": dec.print_count,
        })

    # Deduplicate by version (current manifest may duplicate a versioned archive)
    seen: set[int] = set()
    unique: list[dict[str, Any]] = []
    for entry in entries:
        v = entry["version"]
        if v not in seen:
            seen.add(v)
            unique.append(entry)

    unique.sort(key=lambda e: e["version"])
    return unique
