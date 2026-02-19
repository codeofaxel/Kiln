"""Tests for kiln.project_costs — per-project cost tracking."""

from __future__ import annotations

import threading
import time

import pytest

from kiln.project_costs import (
    CostCategory,
    CostEntry,
    ProjectCostTracker,
    ProjectInfo,
    ProjectStatus,
    ProjectSummary,
    get_project_cost_tracker,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tracker() -> ProjectCostTracker:
    return ProjectCostTracker()


@pytest.fixture()
def tracker_with_project(tracker: ProjectCostTracker) -> ProjectCostTracker:
    tracker.create_project("P-001", name="Widget Run", client="Acme Corp")
    return tracker


# =========================================================================
# TestEnums
# =========================================================================


class TestEnums:
    def test_project_status_values(self):
        assert ProjectStatus.ACTIVE.value == "active"
        assert ProjectStatus.COMPLETED.value == "completed"
        assert ProjectStatus.ARCHIVED.value == "archived"

    def test_cost_category_values(self):
        assert CostCategory.MATERIAL.value == "material"
        assert CostCategory.PRINTER_TIME.value == "printer_time"
        assert CostCategory.FULFILLMENT_FEE.value == "fulfillment_fee"
        assert CostCategory.LABOR.value == "labor"
        assert CostCategory.OTHER.value == "other"

    def test_project_status_is_string_enum(self):
        assert isinstance(ProjectStatus.ACTIVE, str)
        assert ProjectStatus.ACTIVE == "active"

    def test_cost_category_is_string_enum(self):
        assert isinstance(CostCategory.MATERIAL, str)
        assert CostCategory.MATERIAL == "material"


# =========================================================================
# TestProjectInfoDataclass
# =========================================================================


class TestProjectInfoDataclass:
    def test_defaults(self):
        info = ProjectInfo(id="P-001")
        assert info.name == ""
        assert info.client == ""
        assert info.status == ProjectStatus.ACTIVE
        assert info.tags == {}
        assert info.budget_usd is None

    def test_to_dict_serialises_enum(self):
        info = ProjectInfo(id="P-001", status=ProjectStatus.COMPLETED)
        d = info.to_dict()
        assert d["status"] == "completed"
        assert isinstance(d["status"], str)

    def test_to_dict_includes_all_fields(self):
        info = ProjectInfo(
            id="P-001",
            name="Test",
            client="Acme",
            created_at=1000.0,
            tags={"region": "us"},
            budget_usd=500.0,
        )
        d = info.to_dict()
        assert d["id"] == "P-001"
        assert d["name"] == "Test"
        assert d["client"] == "Acme"
        assert d["created_at"] == 1000.0
        assert d["tags"] == {"region": "us"}
        assert d["budget_usd"] == 500.0


# =========================================================================
# TestCostEntryDataclass
# =========================================================================


class TestCostEntryDataclass:
    def test_to_dict_serialises_enum(self):
        entry = CostEntry(
            id="e1",
            project_id="P-001",
            category=CostCategory.MATERIAL,
            amount=12.50,
        )
        d = entry.to_dict()
        assert d["category"] == "material"
        assert d["amount"] == 12.50

    def test_optional_fields_default_none(self):
        entry = CostEntry(
            id="e1",
            project_id="P-001",
            category=CostCategory.LABOR,
            amount=100.0,
        )
        assert entry.printer_name is None
        assert entry.job_id is None
        assert entry.hours is None


# =========================================================================
# TestProjectSummaryDataclass
# =========================================================================


class TestProjectSummaryDataclass:
    def test_to_dict_rounds_values(self):
        info = ProjectInfo(id="P-001", budget_usd=100.0)
        summary = ProjectSummary(
            project=info,
            total_cost=33.333,
            cost_by_category={"material": 33.333},
            entry_count=1,
            budget_remaining=66.667,
        )
        d = summary.to_dict()
        assert d["total_cost"] == 33.33
        assert d["cost_by_category"]["material"] == 33.33
        assert d["budget_remaining"] == 66.67

    def test_to_dict_no_budget(self):
        info = ProjectInfo(id="P-001")
        summary = ProjectSummary(
            project=info,
            total_cost=50.0,
            cost_by_category={},
            entry_count=0,
        )
        d = summary.to_dict()
        assert d["budget_remaining"] is None


# =========================================================================
# TestCreateProject
# =========================================================================


class TestCreateProject:
    def test_create_basic(self, tracker):
        info = tracker.create_project("P-001", name="Test", client="Acme")
        assert info.id == "P-001"
        assert info.name == "Test"
        assert info.client == "Acme"
        assert info.status == ProjectStatus.ACTIVE
        assert info.created_at > 0

    def test_create_with_budget_and_tags(self, tracker):
        info = tracker.create_project(
            "P-002",
            name="Budget Test",
            client="Acme",
            budget_usd=1000.0,
            tags={"region": "us-east"},
        )
        assert info.budget_usd == 1000.0
        assert info.tags == {"region": "us-east"}

    def test_create_duplicate_raises(self, tracker):
        tracker.create_project("P-001")
        with pytest.raises(ValueError, match="already exists"):
            tracker.create_project("P-001")

    def test_create_minimal(self, tracker):
        info = tracker.create_project("P-MIN")
        assert info.id == "P-MIN"
        assert info.name == ""
        assert info.client == ""


# =========================================================================
# TestUpdateProject
# =========================================================================


class TestUpdateProject:
    def test_update_name(self, tracker_with_project):
        updated = tracker_with_project.update_project("P-001", name="New Name")
        assert updated.name == "New Name"
        assert updated.client == "Acme Corp"  # Unchanged.

    def test_update_status(self, tracker_with_project):
        updated = tracker_with_project.update_project("P-001", status="completed")
        assert updated.status == ProjectStatus.COMPLETED

    def test_update_budget(self, tracker_with_project):
        updated = tracker_with_project.update_project("P-001", budget_usd=5000.0)
        assert updated.budget_usd == 5000.0

    def test_update_nonexistent_raises(self, tracker):
        with pytest.raises(ValueError, match="not found"):
            tracker.update_project("NOPE", name="X")

    def test_update_invalid_status_raises(self, tracker_with_project):
        with pytest.raises(ValueError):
            tracker_with_project.update_project("P-001", status="invalid")

    def test_update_multiple_fields(self, tracker_with_project):
        updated = tracker_with_project.update_project(
            "P-001", name="Updated", client="NewCorp", budget_usd=999.0,
        )
        assert updated.name == "Updated"
        assert updated.client == "NewCorp"
        assert updated.budget_usd == 999.0


# =========================================================================
# TestGetProject
# =========================================================================


class TestGetProject:
    def test_get_existing(self, tracker_with_project):
        info = tracker_with_project.get_project("P-001")
        assert info is not None
        assert info.name == "Widget Run"

    def test_get_nonexistent_returns_none(self, tracker):
        assert tracker.get_project("NOPE") is None


# =========================================================================
# TestListProjects
# =========================================================================


class TestListProjects:
    def test_list_all(self, tracker):
        tracker.create_project("P-001", client="Acme")
        tracker.create_project("P-002", client="Beta")
        assert len(tracker.list_projects()) == 2

    def test_list_filter_by_client(self, tracker):
        tracker.create_project("P-001", client="Acme")
        tracker.create_project("P-002", client="Beta")
        tracker.create_project("P-003", client="Acme")
        results = tracker.list_projects(client="Acme")
        assert len(results) == 2
        assert all(p.client == "Acme" for p in results)

    def test_list_filter_by_status(self, tracker):
        tracker.create_project("P-001")
        tracker.create_project("P-002")
        tracker.update_project("P-002", status="completed")
        results = tracker.list_projects(status="active")
        assert len(results) == 1
        assert results[0].id == "P-001"

    def test_list_empty(self, tracker):
        assert tracker.list_projects() == []


# =========================================================================
# TestLogCost
# =========================================================================


class TestLogCost:
    def test_log_basic(self, tracker_with_project):
        entry = tracker_with_project.log_cost(
            "P-001", category="material", amount=12.50, description="PLA 1kg",
        )
        assert entry.project_id == "P-001"
        assert entry.category == CostCategory.MATERIAL
        assert entry.amount == 12.50
        assert entry.description == "PLA 1kg"
        assert entry.id  # Has an auto-generated ID.
        assert entry.created_at > 0

    def test_log_with_printer_and_job(self, tracker_with_project):
        entry = tracker_with_project.log_cost(
            "P-001",
            category="printer_time",
            amount=8.00,
            printer_name="voron-350",
            job_id="job-abc",
            hours=2.0,
        )
        assert entry.printer_name == "voron-350"
        assert entry.job_id == "job-abc"
        assert entry.hours == 2.0

    def test_log_all_categories(self, tracker_with_project):
        for cat in CostCategory:
            entry = tracker_with_project.log_cost(
                "P-001", category=cat.value, amount=10.0,
            )
            assert entry.category == cat

    def test_log_nonexistent_project_raises(self, tracker):
        with pytest.raises(ValueError, match="not found"):
            tracker.log_cost("NOPE", category="material", amount=1.0)

    def test_log_invalid_category_raises(self, tracker_with_project):
        with pytest.raises(ValueError):
            tracker_with_project.log_cost(
                "P-001", category="invalid_category", amount=1.0,
            )

    def test_log_unique_entry_ids(self, tracker_with_project):
        e1 = tracker_with_project.log_cost("P-001", category="material", amount=1.0)
        e2 = tracker_with_project.log_cost("P-001", category="material", amount=2.0)
        assert e1.id != e2.id


# =========================================================================
# TestProjectSummary
# =========================================================================


class TestProjectSummary:
    def test_summary_empty_project(self, tracker_with_project):
        summary = tracker_with_project.project_summary("P-001")
        assert summary.total_cost == 0.0
        assert summary.entry_count == 0
        assert summary.cost_by_category == {}

    def test_summary_with_costs(self, tracker_with_project):
        tracker_with_project.log_cost("P-001", category="material", amount=10.0)
        tracker_with_project.log_cost("P-001", category="material", amount=5.0)
        tracker_with_project.log_cost("P-001", category="labor", amount=20.0)

        summary = tracker_with_project.project_summary("P-001")
        assert summary.total_cost == 35.0
        assert summary.entry_count == 3
        assert summary.cost_by_category == {"material": 15.0, "labor": 20.0}

    def test_summary_budget_remaining(self, tracker):
        tracker.create_project("P-BUD", budget_usd=100.0)
        tracker.log_cost("P-BUD", category="material", amount=30.0)
        tracker.log_cost("P-BUD", category="labor", amount=25.0)

        summary = tracker.project_summary("P-BUD")
        assert summary.budget_remaining == 45.0

    def test_summary_budget_exceeded(self, tracker):
        tracker.create_project("P-OVER", budget_usd=50.0)
        tracker.log_cost("P-OVER", category="material", amount=60.0)

        summary = tracker.project_summary("P-OVER")
        assert summary.budget_remaining == -10.0

    def test_summary_no_budget(self, tracker_with_project):
        summary = tracker_with_project.project_summary("P-001")
        assert summary.budget_remaining is None

    def test_summary_nonexistent_raises(self, tracker):
        with pytest.raises(ValueError, match="not found"):
            tracker.project_summary("NOPE")

    def test_summary_to_dict(self, tracker_with_project):
        tracker_with_project.log_cost("P-001", category="material", amount=10.0)
        summary = tracker_with_project.project_summary("P-001")
        d = summary.to_dict()
        assert d["project"]["id"] == "P-001"
        assert d["total_cost"] == 10.0
        assert d["entry_count"] == 1

    def test_summary_isolates_projects(self, tracker):
        tracker.create_project("P-A", client="X")
        tracker.create_project("P-B", client="X")
        tracker.log_cost("P-A", category="material", amount=100.0)
        tracker.log_cost("P-B", category="material", amount=50.0)

        summary_a = tracker.project_summary("P-A")
        summary_b = tracker.project_summary("P-B")
        assert summary_a.total_cost == 100.0
        assert summary_b.total_cost == 50.0


# =========================================================================
# TestClientSummary
# =========================================================================


class TestClientSummary:
    def test_client_summary_aggregates(self, tracker):
        tracker.create_project("P-A", client="Acme")
        tracker.create_project("P-B", client="Acme")
        tracker.log_cost("P-A", category="material", amount=100.0)
        tracker.log_cost("P-B", category="labor", amount=200.0)

        result = tracker.client_summary("Acme")
        assert result["client"] == "Acme"
        assert result["project_count"] == 2
        assert result["total_cost"] == 300.0
        assert len(result["projects"]) == 2

    def test_client_summary_excludes_other_clients(self, tracker):
        tracker.create_project("P-A", client="Acme")
        tracker.create_project("P-B", client="Beta")
        tracker.log_cost("P-A", category="material", amount=100.0)
        tracker.log_cost("P-B", category="material", amount=999.0)

        result = tracker.client_summary("Acme")
        assert result["total_cost"] == 100.0
        assert result["project_count"] == 1

    def test_client_summary_no_projects(self, tracker):
        result = tracker.client_summary("Unknown")
        assert result["project_count"] == 0
        assert result["total_cost"] == 0.0


# =========================================================================
# TestCostReport
# =========================================================================


class TestCostReport:
    def test_report_all(self, tracker):
        tracker.create_project("P-001", client="Acme")
        tracker.log_cost("P-001", category="material", amount=10.0)
        tracker.log_cost("P-001", category="labor", amount=20.0)

        report = tracker.cost_report()
        assert report["total_cost"] == 30.0
        assert report["entry_count"] == 2
        assert report["cost_by_category"]["material"] == 10.0
        assert report["cost_by_category"]["labor"] == 20.0
        assert report["cost_by_project"]["P-001"] == 30.0

    def test_report_filters_by_client(self, tracker):
        tracker.create_project("P-A", client="Acme")
        tracker.create_project("P-B", client="Beta")
        tracker.log_cost("P-A", category="material", amount=100.0)
        tracker.log_cost("P-B", category="material", amount=50.0)

        report = tracker.cost_report(client="Acme")
        assert report["total_cost"] == 100.0
        assert report["entry_count"] == 1

    def test_report_filters_by_date_range(self, tracker):
        tracker.create_project("P-001")
        # Log cost, then check time-based filtering.
        tracker.log_cost("P-001", category="material", amount=10.0)

        now = time.time()
        report = tracker.cost_report(start_date=now - 60, end_date=now + 60)
        assert report["entry_count"] == 1

        report_future = tracker.cost_report(start_date=now + 3600)
        assert report_future["entry_count"] == 0

    def test_report_empty(self, tracker):
        report = tracker.cost_report()
        assert report["total_cost"] == 0.0
        assert report["entry_count"] == 0


# =========================================================================
# TestSingleton
# =========================================================================


class TestSingleton:
    def test_get_project_cost_tracker_returns_same_instance(self):
        import kiln.project_costs as mod

        original = mod._tracker
        mod._tracker = None
        try:
            t1 = get_project_cost_tracker()
            t2 = get_project_cost_tracker()
            assert t1 is t2
        finally:
            mod._tracker = original

    def test_singleton_thread_safety(self):
        import kiln.project_costs as mod

        original = mod._tracker
        mod._tracker = None
        try:
            instances: list[ProjectCostTracker] = []
            barrier = threading.Barrier(4)

            def worker():
                barrier.wait()
                instances.append(get_project_cost_tracker())

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert len(instances) == 4
            assert all(inst is instances[0] for inst in instances)
        finally:
            mod._tracker = original


# =========================================================================
# TestThreadSafety
# =========================================================================


class TestThreadSafety:
    def test_concurrent_cost_logging(self, tracker_with_project):
        """Multiple threads logging costs concurrently should not lose data."""
        barrier = threading.Barrier(8)
        errors: list[str] = []

        def log_costs(thread_id: int):
            try:
                barrier.wait()
                for i in range(10):
                    tracker_with_project.log_cost(
                        "P-001",
                        category="material",
                        amount=1.0,
                        description=f"thread-{thread_id}-{i}",
                    )
            except Exception as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=log_costs, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        summary = tracker_with_project.project_summary("P-001")
        assert summary.entry_count == 80  # 8 threads x 10 entries.
        assert summary.total_cost == 80.0
