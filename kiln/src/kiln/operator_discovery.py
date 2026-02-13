"""Customer acquisition / marketplace discovery engine.

Helps operators register their print-farm capabilities and lets agents
(or external callers) search for the best-matching operator for a given
job.  Listings are stored in-memory and exposed through a thread-safe
:class:`DiscoveryEngine` singleton.

Usage::

    from kiln.operator_discovery import get_discovery_engine, OperatorListing

    engine = get_discovery_engine()
    engine.register_listing(OperatorListing(
        operator_id="op-42",
        display_name="FarmCo",
        materials=["PLA", "PETG"],
        printer_models=["Prusa MK4"],
        capabilities=["enclosure"],
        min_lead_time_hours=2.0,
        avg_quality_score=4.5,
        success_rate=0.97,
    ))
    result = engine.search(DiscoveryQuery(material="PLA"))
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_OPERATOR_ID_LEN = 100
_MAX_MATERIAL_LEN = 50
_MAX_DISPLAY_NAME_LEN = 200
_MIN_QUALITY_SCORE = 0.0
_MAX_QUALITY_SCORE = 5.0
_MIN_SUCCESS_RATE = 0.0
_MAX_SUCCESS_RATE = 1.0


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DiscoveryValidationError(ValueError):
    """Raised when an :class:`OperatorListing` fails input validation."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class OperatorListing:
    """A single operator's advertised capabilities and stats."""

    operator_id: str
    display_name: str
    materials: List[str]
    printer_models: List[str]
    capabilities: List[str] = field(default_factory=list)
    location: Optional[str] = None
    min_lead_time_hours: float = 0.0
    avg_quality_score: float = 0.0
    success_rate: float = 0.0
    verified: bool = False
    accepting_orders: bool = True

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "operator_id": self.operator_id,
            "display_name": self.display_name,
            "materials": list(self.materials),
            "printer_models": list(self.printer_models),
            "capabilities": list(self.capabilities),
            "location": self.location,
            "min_lead_time_hours": self.min_lead_time_hours,
            "avg_quality_score": self.avg_quality_score,
            "success_rate": self.success_rate,
            "verified": self.verified,
            "accepting_orders": self.accepting_orders,
        }


@dataclass
class DiscoveryQuery:
    """Search criteria for finding operators."""

    material: Optional[str] = None
    capability: Optional[str] = None
    location: Optional[str] = None
    max_lead_time_hours: Optional[float] = None
    min_quality_score: Optional[float] = None
    min_success_rate: Optional[float] = None
    verified_only: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "material": self.material,
            "capability": self.capability,
            "location": self.location,
            "max_lead_time_hours": self.max_lead_time_hours,
            "min_quality_score": self.min_quality_score,
            "min_success_rate": self.min_success_rate,
            "verified_only": self.verified_only,
        }


@dataclass
class DiscoveryResult:
    """Results from a discovery search."""

    operators: List[OperatorListing]
    total_matches: int
    query: DiscoveryQuery
    searched_at: float

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "operators": [op.to_dict() for op in self.operators],
            "total_matches": self.total_matches,
            "query": self.query.to_dict(),
            "searched_at": self.searched_at,
        }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_listing(listing: OperatorListing) -> None:
    """Validate every field of an :class:`OperatorListing`.

    :raises DiscoveryValidationError: If any field is invalid.
    """
    # -- operator_id --
    if not isinstance(listing.operator_id, str) or not listing.operator_id.strip():
        raise DiscoveryValidationError("operator_id must be a non-empty string")
    if len(listing.operator_id) > _MAX_OPERATOR_ID_LEN:
        raise DiscoveryValidationError(
            f"operator_id exceeds max length of {_MAX_OPERATOR_ID_LEN} characters"
        )

    # -- display_name --
    if not isinstance(listing.display_name, str) or not listing.display_name.strip():
        raise DiscoveryValidationError("display_name must be a non-empty string")
    if len(listing.display_name) > _MAX_DISPLAY_NAME_LEN:
        raise DiscoveryValidationError(
            f"display_name exceeds max length of {_MAX_DISPLAY_NAME_LEN} characters"
        )

    # -- materials --
    if not listing.materials:
        raise DiscoveryValidationError("materials must be a non-empty list")
    for mat in listing.materials:
        if not isinstance(mat, str) or not mat.strip():
            raise DiscoveryValidationError(
                "each material must be a non-empty string"
            )
        if len(mat) > _MAX_MATERIAL_LEN:
            raise DiscoveryValidationError(
                f"material exceeds max length of {_MAX_MATERIAL_LEN} characters"
            )

    # -- printer_models --
    if not listing.printer_models:
        raise DiscoveryValidationError("printer_models must be a non-empty list")

    # -- avg_quality_score --
    if not (_MIN_QUALITY_SCORE <= listing.avg_quality_score <= _MAX_QUALITY_SCORE):
        raise DiscoveryValidationError(
            f"avg_quality_score must be between {_MIN_QUALITY_SCORE} "
            f"and {_MAX_QUALITY_SCORE}"
        )

    # -- success_rate --
    if not (_MIN_SUCCESS_RATE <= listing.success_rate <= _MAX_SUCCESS_RATE):
        raise DiscoveryValidationError(
            f"success_rate must be between {_MIN_SUCCESS_RATE} "
            f"and {_MAX_SUCCESS_RATE}"
        )

    # -- min_lead_time_hours --
    if listing.min_lead_time_hours < 0:
        raise DiscoveryValidationError("min_lead_time_hours must be >= 0")


# ---------------------------------------------------------------------------
# Discovery engine
# ---------------------------------------------------------------------------


class DiscoveryEngine:
    """Thread-safe operator discovery engine.

    Stores operator listings in-memory and provides filtered, ranked
    search results so agents can match print jobs to the best operator.
    """

    def __init__(self) -> None:
        self._listings: Dict[str, OperatorListing] = {}
        self._lock = threading.Lock()

    # -- write operations --------------------------------------------------

    def register_listing(self, listing: OperatorListing) -> None:
        """Validate and store (or update) an operator listing.

        :param listing: The listing to register.
        :raises DiscoveryValidationError: If any field is invalid.
        """
        _validate_listing(listing)
        with self._lock:
            self._listings[listing.operator_id] = listing
        logger.info("Registered listing for operator %s", listing.operator_id)

    def remove_listing(self, operator_id: str) -> bool:
        """Remove an operator listing.

        :returns: ``True`` if the listing existed and was removed.
        """
        with self._lock:
            if operator_id in self._listings:
                del self._listings[operator_id]
                logger.info("Removed listing for operator %s", operator_id)
                return True
            return False

    def update_accepting_orders(
        self, operator_id: str, *, accepting: bool
    ) -> None:
        """Toggle an operator's order-acceptance status.

        :raises KeyError: If the operator is not registered.
        """
        with self._lock:
            if operator_id not in self._listings:
                raise KeyError(f"Operator {operator_id!r} not found")
            self._listings[operator_id].accepting_orders = accepting
        logger.info(
            "Operator %s accepting_orders set to %s", operator_id, accepting
        )

    # -- read operations ---------------------------------------------------

    def get_listing(self, operator_id: str) -> Optional[OperatorListing]:
        """Return a single listing, or ``None`` if not found."""
        with self._lock:
            return self._listings.get(operator_id)

    def search(self, query: DiscoveryQuery) -> DiscoveryResult:
        """Search for operators matching *query*.

        Filters are applied conjunctively (all specified criteria must
        match).  Results are sorted by a composite relevance score
        (``quality_score * success_rate``), with verified operators
        ranked above unverified ones.
        """
        with self._lock:
            candidates = list(self._listings.values())

        matched = [op for op in candidates if self._matches(op, query)]

        # Sort: verified first, then by composite score descending
        matched.sort(
            key=lambda op: (
                op.verified,
                op.avg_quality_score * op.success_rate,
            ),
            reverse=True,
        )

        return DiscoveryResult(
            operators=matched,
            total_matches=len(matched),
            query=query,
            searched_at=time.time(),
        )

    def get_stats(self) -> Dict[str, Any]:
        """Return aggregate statistics across all registered operators."""
        with self._lock:
            listings = list(self._listings.values())

        total = len(listings)
        if total == 0:
            return {
                "total_operators": 0,
                "verified_count": 0,
                "accepting_count": 0,
                "material_coverage": {},
                "avg_quality": 0.0,
            }

        verified_count = sum(1 for op in listings if op.verified)
        accepting_count = sum(1 for op in listings if op.accepting_orders)

        material_coverage: Dict[str, int] = {}
        for op in listings:
            for mat in op.materials:
                material_coverage[mat] = material_coverage.get(mat, 0) + 1

        avg_quality = sum(op.avg_quality_score for op in listings) / total

        return {
            "total_operators": total,
            "verified_count": verified_count,
            "accepting_count": accepting_count,
            "material_coverage": material_coverage,
            "avg_quality": avg_quality,
        }

    # -- internal ----------------------------------------------------------

    @staticmethod
    def _matches(op: OperatorListing, query: DiscoveryQuery) -> bool:
        """Return ``True`` if *op* satisfies every criterion in *query*."""
        if query.material is not None:
            if query.material.upper() not in (m.upper() for m in op.materials):
                return False

        if query.capability is not None:
            if query.capability.lower() not in (
                c.lower() for c in op.capabilities
            ):
                return False

        if query.location is not None:
            if op.location is None:
                return False
            if query.location.lower() not in op.location.lower():
                return False

        if query.max_lead_time_hours is not None:
            if op.min_lead_time_hours > query.max_lead_time_hours:
                return False

        if query.min_quality_score is not None:
            if op.avg_quality_score < query.min_quality_score:
                return False

        if query.min_success_rate is not None:
            if op.success_rate < query.min_success_rate:
                return False

        if query.verified_only and not op.verified:
            return False

        return True


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_engine: Optional[DiscoveryEngine] = None
_engine_lock = threading.Lock()


def get_discovery_engine() -> DiscoveryEngine:
    """Return the module-level :class:`DiscoveryEngine` singleton.

    Thread-safe; the instance is created on first call.
    """
    global _engine
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is None:
            _engine = DiscoveryEngine()
        return _engine
