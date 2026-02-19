"""Per-project cost tracking for manufacturing bureaus.

Allows fleet managers to allocate printer time, material costs, and
fulfillment fees to specific client projects.  Essential for manufacturing
bureaus that need accurate per-project P&L.

Usage::

    tracker = ProjectCostTracker(db=get_db())
    tracker.create_project("P-001", name="Widget Run", client="Acme Corp")
    tracker.log_cost("P-001", category="material", amount=12.50, description="PLA 1kg")
    tracker.log_cost("P-001", category="printer_time", amount=8.00, printer_name="voron-350", hours=2.0)
    summary = tracker.project_summary("P-001")
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiln.persistence import KilnDB

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ProjectStatus(str, Enum):
    """Lifecycle status of a client project."""

    ACTIVE = "active"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class CostCategory(str, Enum):
    """Recognised cost categories for project entries."""

    MATERIAL = "material"
    PRINTER_TIME = "printer_time"
    FULFILLMENT_FEE = "fulfillment_fee"
    LABOR = "labor"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ProjectInfo:
    """A client project that costs are allocated against.

    Attributes:
        id: Unique project identifier (e.g. ``"P-001"``).
        name: Human-readable project name.
        client: Client or company the project belongs to.
        created_at: Unix timestamp when the project was created.
        status: Lifecycle status (active / completed / archived).
        tags: Arbitrary key-value metadata.
        budget_usd: Optional spending budget in USD.
    """

    id: str
    name: str = ""
    client: str = ""
    created_at: float = 0.0
    status: ProjectStatus = ProjectStatus.ACTIVE
    tags: dict[str, str] = field(default_factory=dict)
    budget_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "client": self.client,
            "created_at": self.created_at,
            "status": self.status.value if isinstance(self.status, ProjectStatus) else self.status,
            "tags": dict(self.tags),
            "budget_usd": self.budget_usd,
        }


@dataclass
class CostEntry:
    """A single cost line item allocated to a project.

    Attributes:
        id: Unique entry identifier.
        project_id: The project this cost is allocated to.
        category: Cost category (material, printer_time, etc.).
        amount: Monetary amount of the cost.
        currency: Currency code (default ``"USD"``).
        description: Human-readable description of the cost.
        printer_name: Printer that incurred the cost (optional).
        job_id: Associated print job identifier (optional).
        hours: Machine or labour hours (optional).
        created_at: Unix timestamp when the entry was logged.
    """

    id: str
    project_id: str
    category: CostCategory
    amount: float
    currency: str = "USD"
    description: str = ""
    printer_name: str | None = None
    job_id: str | None = None
    hours: float | None = None
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "id": self.id,
            "project_id": self.project_id,
            "category": self.category.value if isinstance(self.category, CostCategory) else self.category,
            "amount": self.amount,
            "currency": self.currency,
            "description": self.description,
            "printer_name": self.printer_name,
            "job_id": self.job_id,
            "hours": self.hours,
            "created_at": self.created_at,
        }


@dataclass
class ProjectSummary:
    """Aggregate cost summary for a single project.

    Attributes:
        project: The project info.
        total_cost: Sum of all cost entries.
        cost_by_category: Breakdown of costs per category.
        entry_count: Number of cost entries logged.
        budget_remaining: Remaining budget (``None`` if no budget set).
    """

    project: ProjectInfo
    total_cost: float
    cost_by_category: dict[str, float]
    entry_count: int
    budget_remaining: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "project": self.project.to_dict(),
            "total_cost": round(self.total_cost, 2),
            "cost_by_category": {k: round(v, 2) for k, v in self.cost_by_category.items()},
            "entry_count": self.entry_count,
            "budget_remaining": round(self.budget_remaining, 2) if self.budget_remaining is not None else None,
        }


# ---------------------------------------------------------------------------
# Cost tracker
# ---------------------------------------------------------------------------


class ProjectCostTracker:
    """Tracks costs allocated to client projects.

    Thread-safe via :class:`threading.RLock`.  When no
    :class:`~kiln.persistence.KilnDB` is provided, all data is stored
    in-memory (useful for tests and single-session usage).
    """

    def __init__(self, db: KilnDB | None = None) -> None:
        self._db = db
        # In-memory storage.
        self._projects: dict[str, ProjectInfo] = {}
        self._entries: list[CostEntry] = []
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Project CRUD
    # ------------------------------------------------------------------

    def create_project(
        self,
        project_id: str,
        *,
        name: str = "",
        client: str = "",
        budget_usd: float | None = None,
        tags: dict[str, str] | None = None,
    ) -> ProjectInfo:
        """Create a new client project.

        Args:
            project_id: Unique project identifier.
            name: Human-readable project name.
            client: Client or company name.
            budget_usd: Optional spending budget in USD.
            tags: Arbitrary key-value metadata.

        Returns:
            The newly created :class:`ProjectInfo`.

        Raises:
            ValueError: If a project with the given ID already exists.
        """
        with self._lock:
            if project_id in self._projects:
                raise ValueError(f"Project '{project_id}' already exists")

            info = ProjectInfo(
                id=project_id,
                name=name,
                client=client,
                created_at=time.time(),
                status=ProjectStatus.ACTIVE,
                tags=dict(tags) if tags else {},
                budget_usd=budget_usd,
            )
            self._projects[project_id] = info

        logger.info(
            "Created project %s (client=%s, budget=%s)",
            project_id,
            client or "<none>",
            f"${budget_usd:.2f}" if budget_usd is not None else "unlimited",
        )
        return info

    def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        client: str | None = None,
        status: str | None = None,
        budget_usd: float | None = None,
    ) -> ProjectInfo:
        """Update fields on an existing project.

        Only provided (non-``None``) fields are updated.

        Args:
            project_id: Project to update.
            name: New project name.
            client: New client name.
            status: New status string (must be a valid :class:`ProjectStatus` value).
            budget_usd: New budget in USD.

        Returns:
            The updated :class:`ProjectInfo`.

        Raises:
            ValueError: If the project does not exist or the status value is invalid.
        """
        with self._lock:
            info = self._projects.get(project_id)
            if info is None:
                raise ValueError(f"Project '{project_id}' not found")

            if name is not None:
                info.name = name
            if client is not None:
                info.client = client
            if status is not None:
                info.status = ProjectStatus(status)
            if budget_usd is not None:
                info.budget_usd = budget_usd

        logger.info("Updated project %s", project_id)
        return info

    def get_project(self, project_id: str) -> ProjectInfo | None:
        """Look up a project by ID.

        Returns:
            The :class:`ProjectInfo` if found, otherwise ``None``.
        """
        with self._lock:
            return self._projects.get(project_id)

    def list_projects(
        self,
        *,
        status: str | None = None,
        client: str | None = None,
    ) -> list[ProjectInfo]:
        """List projects with optional filters.

        Args:
            status: Filter by project status value.
            client: Filter by client name (case-sensitive).

        Returns:
            List of matching :class:`ProjectInfo` objects.
        """
        with self._lock:
            results: list[ProjectInfo] = []
            for info in self._projects.values():
                if status is not None and info.status.value != status:
                    continue
                if client is not None and info.client != client:
                    continue
                results.append(info)
        return results

    # ------------------------------------------------------------------
    # Cost logging
    # ------------------------------------------------------------------

    def log_cost(
        self,
        project_id: str,
        *,
        category: str,
        amount: float,
        currency: str = "USD",
        description: str = "",
        printer_name: str | None = None,
        job_id: str | None = None,
        hours: float | None = None,
    ) -> CostEntry:
        """Log a cost entry against a project.

        Args:
            project_id: The project to allocate the cost to.
            category: Cost category (must be a valid :class:`CostCategory` value).
            amount: Monetary amount.
            currency: Currency code.
            description: Human-readable description.
            printer_name: Printer that incurred the cost.
            job_id: Associated print job identifier.
            hours: Machine or labour hours.

        Returns:
            The created :class:`CostEntry`.

        Raises:
            ValueError: If the project does not exist or the category is invalid.
        """
        cat = CostCategory(category)
        entry_id = secrets.token_hex(8)

        with self._lock:
            if project_id not in self._projects:
                raise ValueError(f"Project '{project_id}' not found")

            entry = CostEntry(
                id=entry_id,
                project_id=project_id,
                category=cat,
                amount=amount,
                currency=currency,
                description=description,
                printer_name=printer_name,
                job_id=job_id,
                hours=hours,
                created_at=time.time(),
            )
            self._entries.append(entry)

        logger.info(
            "Logged cost %s on project %s: %s %.2f %s",
            entry_id,
            project_id,
            cat.value,
            amount,
            currency,
        )
        return entry

    # ------------------------------------------------------------------
    # Summaries & reports
    # ------------------------------------------------------------------

    def project_summary(self, project_id: str) -> ProjectSummary:
        """Generate an aggregate cost summary for a project.

        Args:
            project_id: The project to summarise.

        Returns:
            A :class:`ProjectSummary` with totals and category breakdown.

        Raises:
            ValueError: If the project does not exist.
        """
        with self._lock:
            info = self._projects.get(project_id)
            if info is None:
                raise ValueError(f"Project '{project_id}' not found")

            total = 0.0
            by_category: dict[str, float] = {}
            count = 0

            for entry in self._entries:
                if entry.project_id != project_id:
                    continue
                count += 1
                total += entry.amount
                cat_key = entry.category.value if isinstance(entry.category, CostCategory) else entry.category
                by_category[cat_key] = by_category.get(cat_key, 0.0) + entry.amount

        budget_remaining: float | None = None
        if info.budget_usd is not None:
            budget_remaining = round(info.budget_usd - total, 2)

        return ProjectSummary(
            project=info,
            total_cost=round(total, 2),
            cost_by_category={k: round(v, 2) for k, v in by_category.items()},
            entry_count=count,
            budget_remaining=budget_remaining,
        )

    def client_summary(self, client: str) -> dict[str, Any]:
        """Aggregate costs across all projects for a client.

        Args:
            client: Client name to filter by.

        Returns:
            Dictionary with ``client``, ``project_count``,
            ``total_cost``, and per-project breakdown.
        """
        with self._lock:
            project_ids = [
                pid for pid, info in self._projects.items() if info.client == client
            ]

        summaries: list[dict[str, Any]] = []
        grand_total = 0.0

        for pid in project_ids:
            ps = self.project_summary(pid)
            grand_total += ps.total_cost
            summaries.append(ps.to_dict())

        return {
            "client": client,
            "project_count": len(project_ids),
            "total_cost": round(grand_total, 2),
            "projects": summaries,
        }

    def cost_report(
        self,
        *,
        start_date: float | None = None,
        end_date: float | None = None,
        client: str | None = None,
    ) -> dict[str, Any]:
        """Generate a cost report across projects for a date range.

        Args:
            start_date: Unix timestamp lower bound (inclusive).
            end_date: Unix timestamp upper bound (inclusive).
            client: Optional client filter.

        Returns:
            Dictionary with ``total_cost``, ``entry_count``,
            ``cost_by_category``, ``cost_by_project``, and filters applied.
        """
        with self._lock:
            # Determine which project IDs to include.
            if client is not None:
                project_ids = {
                    pid for pid, info in self._projects.items() if info.client == client
                }
            else:
                project_ids = set(self._projects.keys())

            total = 0.0
            count = 0
            by_category: dict[str, float] = {}
            by_project: dict[str, float] = {}

            for entry in self._entries:
                if entry.project_id not in project_ids:
                    continue
                if start_date is not None and entry.created_at < start_date:
                    continue
                if end_date is not None and entry.created_at > end_date:
                    continue

                count += 1
                total += entry.amount

                cat_key = entry.category.value if isinstance(entry.category, CostCategory) else entry.category
                by_category[cat_key] = by_category.get(cat_key, 0.0) + entry.amount
                by_project[entry.project_id] = by_project.get(entry.project_id, 0.0) + entry.amount

        return {
            "total_cost": round(total, 2),
            "entry_count": count,
            "cost_by_category": {k: round(v, 2) for k, v in by_category.items()},
            "cost_by_project": {k: round(v, 2) for k, v in by_project.items()},
            "filters": {
                "start_date": start_date,
                "end_date": end_date,
                "client": client,
            },
        }
