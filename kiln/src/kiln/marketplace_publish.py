"""Publish-to-marketplace pipeline.

Enables users to publish 3D models to marketplaces (Thingiverse,
MyMiniFactory, Thangs) with validated print settings, optimal orientation,
and a print "birth certificate" — proven settings from successful prints.

The pipeline: validate model → attach print DNA → generate metadata →
publish to one or more marketplaces.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from typing import Any

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Valid licenses
# ---------------------------------------------------------------------------

_VALID_LICENSES = {"cc-by", "cc-by-sa", "cc-by-nc", "gpl", "public_domain"}

# ---------------------------------------------------------------------------
# Kiln attribution / watermark
# ---------------------------------------------------------------------------

_KILN_ATTRIBUTION = (
    "\n\n---\nPowered by [Kiln](https://kiln3d.com) — the open-source bridge between AI and 3D printing.\n"
)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class PrintCertificate:
    """Birth certificate for a 3D model — proven print settings."""

    file_hash: str
    printer_models_tested: list[str]
    materials_tested: list[str]
    recommended_settings: dict[str, Any]
    success_rate: float
    total_prints: int
    best_orientation: dict[str, float] | None  # rotation x/y/z
    print_time_range: tuple[int, int]  # min/max seconds
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PublishRequest:
    """Request to publish a model to one or more marketplaces."""

    file_path: str
    title: str
    description: str
    tags: list[str]
    category: str
    license: str  # "cc-by", "cc-by-sa", "cc-by-nc", "gpl", "public_domain"
    target_marketplaces: list[str]  # ["thingiverse", "myminifactory", "thangs"]
    include_certificate: bool = True
    include_print_settings: bool = True
    images: list[str] | None = None  # paths to preview images

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PublishResult:
    """Result of publishing to a single marketplace."""

    marketplace: str
    success: bool
    listing_url: str | None
    listing_id: str | None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PublishPipelineResult:
    """Aggregate result of the full publish pipeline."""

    results: list[PublishResult]
    certificate: PrintCertificate | None
    successful_count: int
    failed_count: int

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "results": [r.to_dict() for r in self.results],
            "certificate": self.certificate.to_dict() if self.certificate else None,
            "successful_count": self.successful_count,
            "failed_count": self.failed_count,
        }
        return data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _file_hash(file_path: str) -> str:
    """Compute SHA-256 hash of a file."""
    try:
        sha = hashlib.sha256()
        with open(file_path, "rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                sha.update(chunk)
        return sha.hexdigest()
    except OSError as exc:
        raise ValueError(f"Failed to hash file {file_path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def validate_publish_request(request: PublishRequest) -> list[str]:
    """Validate a publish request, returning a list of error strings.

    Returns an empty list if the request is valid.
    """
    errors: list[str] = []

    if not request.file_path:
        errors.append("file_path is required")
    elif not os.path.isfile(request.file_path):
        errors.append(f"File not found: {request.file_path}")

    if not request.title or not request.title.strip():
        errors.append("title is required")

    if not request.description or not request.description.strip():
        errors.append("description is required")

    if not request.tags:
        errors.append("At least one tag is required")

    if not request.category or not request.category.strip():
        errors.append("category is required")

    if request.license not in _VALID_LICENSES:
        errors.append(f"Invalid license {request.license!r}. Valid options: {', '.join(sorted(_VALID_LICENSES))}")

    if not request.target_marketplaces:
        errors.append("At least one target marketplace is required")

    if request.images:
        for img_path in request.images:
            if not os.path.isfile(img_path):
                errors.append(f"Image not found: {img_path}")

    return errors


def generate_print_certificate(file_path: str) -> PrintCertificate | None:
    """Query print DNA for this model's history and build a certificate.

    Returns ``None`` if there is no print history for this file.
    """
    from kiln.persistence import get_db

    if not os.path.isfile(file_path):
        _logger.warning("File not found for certificate generation: %s", file_path)
        return None

    fhash = _file_hash(file_path)
    db = get_db()

    # Query print outcomes for this file hash.
    rows = db._conn.execute(
        "SELECT * FROM print_outcomes WHERE file_hash = ?",
        (fhash,),
    ).fetchall()

    if not rows:
        return None

    outcomes = [dict(r) for r in rows]
    total = len(outcomes)
    successes = sum(1 for o in outcomes if o.get("outcome") == "success")
    success_rate = successes / total if total > 0 else 0.0

    # Collect printer models and materials.
    printers = sorted({o["printer_name"] for o in outcomes if o.get("printer_name")})
    materials = sorted({o["material_type"] for o in outcomes if o.get("material_type")})

    # Collect settings from the most recent successful print.
    recommended: dict[str, Any] = {}
    for o in reversed(outcomes):
        if o.get("outcome") == "success" and o.get("settings"):
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                recommended = json.loads(o["settings"])
            break

    # Query print history for time ranges.
    history_rows = db._conn.execute(
        "SELECT duration_seconds FROM print_history WHERE file_hash = ? AND duration_seconds IS NOT NULL",
        (fhash,),
    ).fetchall()

    durations = [int(dict(r)["duration_seconds"]) for r in history_rows if dict(r).get("duration_seconds")]
    time_range = (min(durations), max(durations)) if durations else (0, 0)

    return PrintCertificate(
        file_hash=fhash,
        printer_models_tested=printers,
        materials_tested=materials,
        recommended_settings=recommended,
        success_rate=round(success_rate, 3),
        total_prints=total,
        best_orientation=None,
        print_time_range=time_range,
        created_at=time.time(),
    )


def format_certificate_markdown(cert: PrintCertificate) -> str:
    """Format a print certificate as Markdown for marketplace descriptions."""
    lines = [
        "## Print Certificate",
        "",
        f"**Tested on {cert.total_prints} print(s)** with a **{cert.success_rate * 100:.0f}% success rate**.",
        "",
    ]

    if cert.printer_models_tested:
        lines.append(f"**Printers:** {', '.join(cert.printer_models_tested)}")

    if cert.materials_tested:
        lines.append(f"**Materials:** {', '.join(cert.materials_tested)}")

    if cert.print_time_range and cert.print_time_range != (0, 0):
        lo_min = cert.print_time_range[0] // 60
        hi_min = cert.print_time_range[1] // 60
        lines.append(f"**Print time:** {lo_min}–{hi_min} minutes")

    if cert.recommended_settings:
        lines.append("")
        lines.append("**Recommended settings:**")
        for k, v in cert.recommended_settings.items():
            lines.append(f"- {k}: {v}")

    lines.append("")
    lines.append("Powered by [Kiln](https://kiln3d.com) — the open-source bridge between AI and 3D printing.")

    return "\n".join(lines)


def publish_model(request: PublishRequest) -> PublishPipelineResult:
    """Publish a model to target marketplaces.

    Validates the request, optionally generates a print certificate,
    and attempts to upload to each target marketplace.
    """
    errors = validate_publish_request(request)
    if errors:
        return PublishPipelineResult(
            results=[
                PublishResult(
                    marketplace="validation",
                    success=False,
                    listing_url=None,
                    listing_id=None,
                    error="; ".join(errors),
                )
            ],
            certificate=None,
            successful_count=0,
            failed_count=1,
        )

    # Generate print certificate if requested.
    certificate: PrintCertificate | None = None
    if request.include_certificate:
        certificate = generate_print_certificate(request.file_path)

    # Build enhanced description with certificate + Kiln attribution.
    description = request.description
    if certificate and request.include_certificate:
        description += "\n\n" + format_certificate_markdown(certificate)
    description += _KILN_ATTRIBUTION

    # Attempt to publish to each marketplace.
    results: list[PublishResult] = []
    fhash = _file_hash(request.file_path)

    for marketplace_name in request.target_marketplaces:
        result = _publish_to_marketplace(
            marketplace_name=marketplace_name,
            request=request,
            description=description,
        )
        results.append(result)

        # Record successful publish in DB.
        if result.success:
            _record_published_model(
                file_hash=fhash,
                marketplace=marketplace_name,
                listing_id=result.listing_id,
                listing_url=result.listing_url,
                title=request.title,
                certificate=certificate,
            )

    successful = sum(1 for r in results if r.success)
    failed = sum(1 for r in results if not r.success)

    return PublishPipelineResult(
        results=results,
        certificate=certificate,
        successful_count=successful,
        failed_count=failed,
    )


def _publish_to_marketplace(
    *,
    marketplace_name: str,
    request: PublishRequest,
    description: str,
) -> PublishResult:
    """Attempt to publish to a single marketplace."""
    import kiln.server as _srv

    try:
        adapter = _srv._get_marketplace(marketplace_name)
    except Exception:
        return PublishResult(
            marketplace=marketplace_name,
            success=False,
            listing_url=None,
            listing_id=None,
            error=f"Marketplace {marketplace_name!r} is not configured or not available.",
        )

    # Check if the adapter supports upload.
    if not getattr(adapter, "supports_upload", False):
        return PublishResult(
            marketplace=marketplace_name,
            success=False,
            listing_url=None,
            listing_id=None,
            error=f"{adapter.display_name} does not support direct uploads.",
        )

    try:
        # Use the adapter's upload method if available.
        upload_fn = getattr(adapter, "upload_model", None)
        if upload_fn is None:
            return PublishResult(
                marketplace=marketplace_name,
                success=False,
                listing_url=None,
                listing_id=None,
                error=f"{adapter.display_name} adapter does not implement upload_model.",
            )

        result = upload_fn(
            file_path=request.file_path,
            title=request.title,
            description=description,
            tags=request.tags,
            category=request.category,
            license_type=request.license,
        )

        return PublishResult(
            marketplace=marketplace_name,
            success=True,
            listing_url=result.get("url"),
            listing_id=result.get("id"),
            error=None,
        )
    except Exception as exc:
        _logger.exception("Failed to publish to %s", marketplace_name)
        return PublishResult(
            marketplace=marketplace_name,
            success=False,
            listing_url=None,
            listing_id=None,
            error=str(exc),
        )


def _record_published_model(
    *,
    file_hash: str,
    marketplace: str,
    listing_id: str | None,
    listing_url: str | None,
    title: str,
    certificate: PrintCertificate | None,
) -> None:
    """Record a successful publish in the database."""
    from kiln.persistence import get_db

    db = get_db()
    cert_json = json.dumps(certificate.to_dict()) if certificate else None

    with db._write_lock:
        db._conn.execute(
            """
            INSERT INTO published_models
                (file_hash, marketplace, listing_id, listing_url, title,
                 published_at, certificate)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (file_hash, marketplace, listing_id, listing_url, title, time.time(), cert_json),
        )
        db._conn.commit()


def list_published_models(
    *,
    marketplace: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List published models from the database."""
    from kiln.persistence import get_db

    db = get_db()
    if marketplace:
        rows = db._conn.execute(
            "SELECT * FROM published_models WHERE marketplace = ? ORDER BY published_at DESC LIMIT ?",
            (marketplace, limit),
        ).fetchall()
    else:
        rows = db._conn.execute(
            "SELECT * FROM published_models ORDER BY published_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    return [dict(r) for r in rows]
