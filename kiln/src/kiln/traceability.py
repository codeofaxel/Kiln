"""Part traceability system for regulatory compliance.

Provides full chain-of-custody tracking for every printed part, linking
the part back to its source file, material batch, slicer settings, printer,
and operator.  Records are designed to satisfy ISO 9001 / 21 CFR 820
traceability requirements for additive manufacturing.

Usage::

    from kiln.traceability import TraceabilityEngine

    engine = TraceabilityEngine()
    record = engine.start_record(
        printer_id="prusa-mk4-01",
        file_name="bracket.gcode",
        file_hash="sha256:abc123...",
        material="PLA",
        safety_profile_version="1.2.0",
    )
    engine.complete_record(record.part_id, status="completed")
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TraceabilityError(Exception):
    """Raised when a traceability operation fails."""

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.cause = cause


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class PartTraceabilityRecord:
    """Full traceability record for a single printed part.

    :param part_id: UUID generated at print start.
    :param printer_id: Identifier of the printer that produced the part.
    :param operator_id: Optional human operator identifier.
    :param file_hash: SHA-256 hash of the G-code file.
    :param file_name: Name of the G-code file.
    :param material: Material type used (e.g. ``"PLA"``, ``"ABS"``).
    :param material_batch: Batch/lot number if known.
    :param slicer_settings_hash: Hash of slicer profile used.
    :param started_at: ISO 8601 timestamp of print start.
    :param completed_at: ISO 8601 timestamp of print completion.
    :param status: One of ``"printing"``, ``"completed"``, ``"failed"``,
        ``"cancelled"``.
    :param safety_profile_version: Which safety profile was active.
    :param quality_metrics: Temperature deviations, layer adhesion, etc.
    """

    part_id: str
    printer_id: str
    file_hash: str
    file_name: str
    material: str
    started_at: str
    status: str
    safety_profile_version: str
    operator_id: str | None = None
    material_batch: str | None = None
    slicer_settings_hash: str | None = None
    completed_at: str | None = None
    quality_metrics: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return asdict(self)


_VALID_STATUSES = frozenset({"printing", "completed", "failed", "cancelled"})


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class TraceabilityEngine:
    """Creates, stores, and queries part traceability records.

    By default records are stored in an in-memory dict.  Pass a custom
    *storage* backend to persist records externally.
    """

    def __init__(self, *, storage: Any | None = None) -> None:
        self._records: dict[str, PartTraceabilityRecord] = {}
        self._storage = storage

    # -- creation -----------------------------------------------------------

    def start_record(
        self,
        *,
        printer_id: str,
        file_name: str,
        file_hash: str,
        material: str,
        safety_profile_version: str,
        operator_id: str | None = None,
        material_batch: str | None = None,
        slicer_settings_hash: str | None = None,
    ) -> PartTraceabilityRecord:
        """Create and store a new traceability record for a print job.

        :returns: The newly created :class:`PartTraceabilityRecord`.
        :raises TraceabilityError: If required fields are empty.
        """
        if not printer_id:
            raise TraceabilityError("printer_id must not be empty")
        if not file_name:
            raise TraceabilityError("file_name must not be empty")
        if not file_hash:
            raise TraceabilityError("file_hash must not be empty")
        if not material:
            raise TraceabilityError("material must not be empty")
        if not safety_profile_version:
            raise TraceabilityError("safety_profile_version must not be empty")

        part_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        record = PartTraceabilityRecord(
            part_id=part_id,
            printer_id=printer_id,
            file_name=file_name,
            file_hash=file_hash,
            material=material,
            safety_profile_version=safety_profile_version,
            started_at=now,
            status="printing",
            operator_id=operator_id,
            material_batch=material_batch,
            slicer_settings_hash=slicer_settings_hash,
        )

        self._records[part_id] = record
        logger.info("Traceability record created: part_id=%s", part_id)
        return record

    # -- update -------------------------------------------------------------

    def complete_record(
        self,
        part_id: str,
        *,
        status: str,
        quality_metrics: dict[str, Any] | None = None,
    ) -> PartTraceabilityRecord:
        """Mark a record as completed, failed, or cancelled.

        :returns: The updated record.
        :raises TraceabilityError: If the part_id is not found or the status
            is invalid.
        """
        if status not in _VALID_STATUSES:
            raise TraceabilityError(f"Invalid status '{status}'; must be one of {sorted(_VALID_STATUSES)}")
        if status == "printing":
            raise TraceabilityError(
                "Cannot complete a record with status 'printing'; use 'completed', 'failed', or 'cancelled'"
            )

        record = self._records.get(part_id)
        if record is None:
            raise TraceabilityError(f"No record found for part_id '{part_id}'")

        record.completed_at = datetime.now(timezone.utc).isoformat()
        record.status = status
        if quality_metrics is not None:
            record.quality_metrics = quality_metrics

        logger.info("Traceability record updated: part_id=%s status=%s", part_id, status)
        return record

    # -- queries ------------------------------------------------------------

    def get_record(self, part_id: str) -> PartTraceabilityRecord | None:
        """Retrieve a single record by part ID."""
        return self._records.get(part_id)

    def get_records_by_printer(
        self,
        printer_id: str,
        *,
        limit: int = 100,
    ) -> list[PartTraceabilityRecord]:
        """Return records for a specific printer, newest first."""
        matches = [r for r in self._records.values() if r.printer_id == printer_id]
        matches.sort(key=lambda r: r.started_at, reverse=True)
        return matches[:limit]

    def get_records_by_material(
        self,
        material: str,
        *,
        limit: int = 100,
    ) -> list[PartTraceabilityRecord]:
        """Return records using a specific material, newest first."""
        matches = [r for r in self._records.values() if r.material.upper() == material.upper()]
        matches.sort(key=lambda r: r.started_at, reverse=True)
        return matches[:limit]

    # -- compliance ---------------------------------------------------------

    def export_compliance_report(
        self,
        part_ids: list[str],
    ) -> dict[str, Any]:
        """Generate a compliance report for the given parts.

        :returns: A dict with ``generated_at``, ``parts`` (list of record
            dicts), ``total``, ``verified``, and ``unverified`` counts.
        :raises TraceabilityError: If any part_id is not found.
        """
        parts: list[dict[str, Any]] = []
        verified_count = 0

        for pid in part_ids:
            record = self._records.get(pid)
            if record is None:
                raise TraceabilityError(f"Cannot generate report: no record for part_id '{pid}'")
            is_verified = self.verify_chain_of_custody(pid)
            entry = record.to_dict()
            entry["chain_of_custody_verified"] = is_verified
            parts.append(entry)
            if is_verified:
                verified_count += 1

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total": len(parts),
            "verified": verified_count,
            "unverified": len(parts) - verified_count,
            "parts": parts,
        }

    def verify_chain_of_custody(self, part_id: str) -> bool:
        """Verify that a record has all required fields and valid timestamps.

        :returns: ``True`` if the chain of custody is intact.
        """
        record = self._records.get(part_id)
        if record is None:
            return False

        # Required fields must be non-empty strings
        required = [
            record.part_id,
            record.printer_id,
            record.file_hash,
            record.file_name,
            record.material,
            record.started_at,
            record.status,
            record.safety_profile_version,
        ]
        if not all(required):
            return False

        # started_at must be a valid ISO 8601 timestamp
        try:
            start_dt = datetime.fromisoformat(record.started_at)
        except (ValueError, TypeError):
            return False

        # If completed, completed_at must exist and be after started_at
        if record.status in ("completed", "failed", "cancelled"):
            if not record.completed_at:
                return False
            try:
                end_dt = datetime.fromisoformat(record.completed_at)
            except (ValueError, TypeError):
                return False
            if end_dt < start_dt:
                return False

        return True
