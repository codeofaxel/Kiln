"""Tests for kiln.print_service -- Print-as-a-Service API.

Covers:
- PrintServiceRequest, PrintServiceQuote, PrintServiceOrder dataclasses
- _validate_request validation logic
- _estimate_local_cost and _estimate_fulfillment_cost
- create_print_order full pipeline
- confirm_print_order state transitions
- get_order_status retrieval
- cancel_order logic
- list_orders filtering
- _generate_order_id uniqueness
- Edge cases: invalid requests, budget limits, missing orders
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiln.persistence import KilnDB
from kiln.print_service import (
    PrintServiceOrder,
    PrintServiceQuote,
    PrintServiceRequest,
    _estimate_fulfillment_cost,
    _estimate_local_cost,
    _generate_order_id,
    _validate_request,
    cancel_order,
    confirm_print_order,
    create_print_order,
    get_order_status,
    list_orders,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path):
    """Return a KilnDB backed by a temporary file."""
    db_path = str(tmp_path / "test_kiln.db")
    instance = KilnDB(db_path=db_path)
    yield instance
    instance.close()


@pytest.fixture()
def model_file(tmp_path):
    """Create a temporary STL file."""
    p = tmp_path / "test_model.stl"
    p.write_bytes(b"solid test\nendsolid test\n")
    return str(p)


def _make_request(**overrides) -> PrintServiceRequest:
    """Return a valid PrintServiceRequest with optional overrides."""
    defaults = {
        "material": "pla",
        "quantity": 1,
        "prefer_local": True,
    }
    defaults.update(overrides)
    return PrintServiceRequest(**defaults)


# ---------------------------------------------------------------------------
# Dataclass tests
# ---------------------------------------------------------------------------


class TestPrintServiceRequest:
    """PrintServiceRequest dataclass."""

    def test_to_dict(self, model_file):
        req = _make_request(model_path=model_file)
        d = req.to_dict()
        assert d["model_path"] == model_file
        assert d["material"] == "pla"
        assert d["quantity"] == 1

    def test_defaults(self):
        req = PrintServiceRequest()
        assert req.model_path is None
        assert req.model_url is None
        assert req.prompt is None
        assert req.material == "pla"
        assert req.quantity == 1
        assert req.prefer_local is True
        assert req.max_budget_usd is None


class TestPrintServiceQuote:
    """PrintServiceQuote dataclass."""

    def test_to_dict(self):
        q = PrintServiceQuote(
            order_id="pso_test123",
            local_option={"cost_usd": 1.0},
            fulfillment_option={"cost_usd": 4.0},
            recommended="local",
            reasoning="test",
            total_cost_usd=1.0,
            estimated_time_hours=2.0,
            printability_score=90,
        )
        d = q.to_dict()
        assert d["order_id"] == "pso_test123"
        assert d["recommended"] == "local"
        assert d["printability_score"] == 90


class TestPrintServiceOrder:
    """PrintServiceOrder dataclass."""

    def test_to_dict(self):
        o = PrintServiceOrder(
            order_id="pso_abc",
            status="received",
            model_path="/tmp/test.stl",
            material="pla",
            provider="local",
            printer_name=None,
            tracking_url=None,
            cost_usd=1.5,
            created_at=1000.0,
            updated_at=1000.0,
            steps_completed=["received"],
            current_step="received",
            error=None,
        )
        d = o.to_dict()
        assert d["status"] == "received"
        assert d["steps_completed"] == ["received"]
        assert d["error"] is None


# ---------------------------------------------------------------------------
# _validate_request
# ---------------------------------------------------------------------------


class TestValidateRequest:
    """_validate_request validation."""

    def test_no_source_provided(self):
        req = _make_request()
        errors = _validate_request(req)
        assert any("model_path" in e or "model_url" in e or "prompt" in e for e in errors)

    def test_multiple_sources(self, model_file):
        req = _make_request(model_path=model_file, prompt="make a cube")
        errors = _validate_request(req)
        assert any("Only one" in e for e in errors)

    def test_valid_model_path(self, model_file):
        req = _make_request(model_path=model_file)
        errors = _validate_request(req)
        assert errors == []

    def test_valid_model_url(self):
        req = _make_request(model_url="https://example.com/model.stl")
        errors = _validate_request(req)
        assert errors == []

    def test_valid_prompt(self):
        req = _make_request(prompt="a small cube with rounded edges")
        errors = _validate_request(req)
        assert errors == []

    def test_missing_model_file(self, tmp_path):
        req = _make_request(model_path=str(tmp_path / "nonexistent.stl"))
        errors = _validate_request(req)
        assert any("not found" in e for e in errors)

    def test_quantity_zero(self, model_file):
        req = _make_request(model_path=model_file, quantity=0)
        errors = _validate_request(req)
        assert any("quantity" in e for e in errors)

    def test_negative_budget(self, model_file):
        req = _make_request(model_path=model_file, max_budget_usd=-5.0)
        errors = _validate_request(req)
        assert any("max_budget_usd" in e for e in errors)


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------


class TestEstimateLocalCost:
    """_estimate_local_cost estimates."""

    def test_pla_cost(self):
        result = _estimate_local_cost("pla")
        assert result["cost_usd"] > 0
        assert "estimated_time_hours" in result

    def test_quantity_multiplier(self):
        single = _estimate_local_cost("pla", quantity=1)
        double = _estimate_local_cost("pla", quantity=2)
        assert double["cost_usd"] == single["cost_usd"] * 2

    def test_unknown_material_uses_default(self):
        result = _estimate_local_cost("unknown_material")
        assert result["cost_usd"] > 0

    def test_different_materials(self):
        pla = _estimate_local_cost("pla")
        nylon = _estimate_local_cost("nylon")
        assert nylon["cost_usd"] > pla["cost_usd"]


class TestEstimateFulfillmentCost:
    """_estimate_fulfillment_cost estimates."""

    def test_more_expensive_than_local(self):
        local = _estimate_local_cost("pla")
        fulfillment = _estimate_fulfillment_cost("pla")
        assert fulfillment["cost_usd"] > local["cost_usd"]

    def test_includes_shipping_time(self):
        result = _estimate_fulfillment_cost("pla")
        assert result["estimated_time_hours"] > 24  # includes shipping


# ---------------------------------------------------------------------------
# _generate_order_id
# ---------------------------------------------------------------------------


class TestGenerateOrderId:
    """_generate_order_id uniqueness."""

    def test_prefix(self):
        oid = _generate_order_id()
        assert oid.startswith("pso_")

    def test_unique(self):
        ids = {_generate_order_id() for _ in range(100)}
        assert len(ids) == 100


# ---------------------------------------------------------------------------
# create_print_order
# ---------------------------------------------------------------------------


class TestCreatePrintOrder:
    """create_print_order pipeline."""

    def test_valid_request(self, db, model_file):
        req = _make_request(model_path=model_file)
        with patch("kiln.persistence.get_db", return_value=db):
            quote = create_print_order(req)

        assert quote.order_id.startswith("pso_")
        assert quote.local_option is not None
        assert quote.fulfillment_option is not None
        assert quote.recommended == "local"
        assert quote.total_cost_usd > 0

    def test_prefer_fulfillment(self, db, model_file):
        req = _make_request(model_path=model_file, prefer_local=False)
        with patch("kiln.persistence.get_db", return_value=db):
            quote = create_print_order(req)

        assert quote.recommended == "fulfillment"

    def test_budget_constraint_switches_option(self, db, model_file):
        # Set a very low budget that only local can satisfy.
        req = _make_request(
            model_path=model_file,
            prefer_local=False,
            max_budget_usd=2.0,
        )
        with patch("kiln.persistence.get_db", return_value=db):
            quote = create_print_order(req)

        assert quote.recommended == "local"

    def test_invalid_request_raises(self, db):
        req = _make_request()  # no source
        with patch("kiln.persistence.get_db", return_value=db), pytest.raises(ValueError):
            create_print_order(req)

    def test_printability_score_stl(self, db, model_file):
        req = _make_request(model_path=model_file)
        with patch("kiln.persistence.get_db", return_value=db):
            quote = create_print_order(req)
        assert quote.printability_score == 90

    def test_printability_score_prompt(self, db):
        req = _make_request(prompt="a cube")
        with patch("kiln.persistence.get_db", return_value=db):
            quote = create_print_order(req)
        assert quote.printability_score == 60

    def test_order_saved_to_db(self, db, model_file):
        req = _make_request(model_path=model_file)
        with patch("kiln.persistence.get_db", return_value=db):
            quote = create_print_order(req)

        row = db._conn.execute(
            "SELECT * FROM print_service_orders WHERE id = ?",
            (quote.order_id,),
        ).fetchone()
        assert row is not None
        assert dict(row)["status"] == "received"


# ---------------------------------------------------------------------------
# confirm_print_order
# ---------------------------------------------------------------------------


class TestConfirmPrintOrder:
    """confirm_print_order state transitions."""

    def test_confirm_received_order(self, db, model_file):
        req = _make_request(model_path=model_file)
        with patch("kiln.persistence.get_db", return_value=db):
            quote = create_print_order(req)
            order = confirm_print_order(quote.order_id)

        assert order.status == "validating"
        assert "received" in order.steps_completed

    def test_confirm_already_confirmed(self, db, model_file):
        req = _make_request(model_path=model_file)
        with patch("kiln.persistence.get_db", return_value=db):
            quote = create_print_order(req)
            confirm_print_order(quote.order_id)
            with pytest.raises(ValueError, match="cannot be confirmed"):
                confirm_print_order(quote.order_id)

    def test_confirm_with_fulfillment_option(self, db, model_file):
        req = _make_request(model_path=model_file)
        with patch("kiln.persistence.get_db", return_value=db):
            quote = create_print_order(req)
            order = confirm_print_order(quote.order_id, option="fulfillment")

        assert order.provider == "fulfillment"


# ---------------------------------------------------------------------------
# get_order_status
# ---------------------------------------------------------------------------


class TestGetOrderStatus:
    """get_order_status retrieval."""

    def test_existing_order(self, db, model_file):
        req = _make_request(model_path=model_file)
        with patch("kiln.persistence.get_db", return_value=db):
            quote = create_print_order(req)
            order = get_order_status(quote.order_id)

        assert order.order_id == quote.order_id
        assert order.status == "received"

    def test_nonexistent_order(self, db):
        with patch("kiln.persistence.get_db", return_value=db), pytest.raises(ValueError, match="not found"):
            get_order_status("pso_nonexistent")


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------


class TestCancelOrder:
    """cancel_order logic."""

    def test_cancel_received_order(self, db, model_file):
        req = _make_request(model_path=model_file)
        with patch("kiln.persistence.get_db", return_value=db):
            quote = create_print_order(req)
            result = cancel_order(quote.order_id)

        assert result["success"] is True
        assert "cancelled" in result["message"]

    def test_cancel_already_cancelled(self, db, model_file):
        req = _make_request(model_path=model_file)
        with patch("kiln.persistence.get_db", return_value=db):
            quote = create_print_order(req)
            cancel_order(quote.order_id)
            result = cancel_order(quote.order_id)

        assert result["success"] is False

    def test_cancel_nonexistent_order(self, db):
        with patch("kiln.persistence.get_db", return_value=db), pytest.raises(ValueError, match="not found"):
            cancel_order("pso_nonexistent")


# ---------------------------------------------------------------------------
# list_orders
# ---------------------------------------------------------------------------


class TestListOrders:
    """list_orders filtering."""

    def test_empty_list(self, db):
        with patch("kiln.persistence.get_db", return_value=db):
            orders = list_orders()
        assert orders == []

    def test_list_all_orders(self, db, model_file):
        req = _make_request(model_path=model_file)
        with patch("kiln.persistence.get_db", return_value=db):
            create_print_order(req)
            create_print_order(req)
            orders = list_orders()

        assert len(orders) == 2

    def test_filter_by_status(self, db, model_file):
        req = _make_request(model_path=model_file)
        with patch("kiln.persistence.get_db", return_value=db):
            quote = create_print_order(req)
            create_print_order(req)
            cancel_order(quote.order_id)
            cancelled = list_orders(status="cancelled")
            received = list_orders(status="received")

        assert len(cancelled) == 1
        assert len(received) == 1

    def test_limit(self, db, model_file):
        req = _make_request(model_path=model_file)
        with patch("kiln.persistence.get_db", return_value=db):
            for _ in range(5):
                create_print_order(req)
            orders = list_orders(limit=3)

        assert len(orders) == 3

    def test_order_includes_steps(self, db, model_file):
        req = _make_request(model_path=model_file)
        with patch("kiln.persistence.get_db", return_value=db):
            create_print_order(req)
            orders = list_orders()

        assert orders[0].steps_completed == ["received"]
        assert orders[0].current_step == "received"
