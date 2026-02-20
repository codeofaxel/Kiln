"""Print-as-a-Service API.

Exposes Kiln as a public API endpoint: POST a 3D model, get a physical
object shipped to you. Orchestrates the full pipeline: upload → validate →
analyze printability → select provider (local or fulfillment) → quote →
print/order → track → deliver.

This is the "one API call to physical object" layer.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Order statuses
# ---------------------------------------------------------------------------

_VALID_STATUSES = {
    "received",
    "validating",
    "generating",
    "slicing",
    "printing",
    "shipping",
    "delivered",
    "failed",
    "cancelled",
}

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class PrintServiceRequest:
    """Request to create a print-as-a-service order."""

    model_path: str | None = None  # local file path
    model_url: str | None = None  # remote URL to download
    prompt: str | None = None  # text-to-3D generation prompt
    material: str = "pla"
    intent: str | None = None  # "strong", "pretty", "cheap"
    quantity: int = 1
    color: str | None = None
    shipping_address: dict[str, str] | None = None  # for fulfillment
    prefer_local: bool = True  # prefer local printer over fulfillment
    printer_name: str | None = None  # specific local printer
    callback_url: str | None = None  # webhook for status updates
    max_budget_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PrintServiceQuote:
    """Quote for a print service order."""

    order_id: str
    local_option: dict[str, Any] | None  # cost, time, printer
    fulfillment_option: dict[str, Any] | None  # cost, time, provider
    recommended: str  # "local" or "fulfillment"
    reasoning: str
    total_cost_usd: float
    estimated_time_hours: float
    printability_score: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PrintServiceOrder:
    """A print service order with its current status."""

    order_id: str
    status: str  # see _VALID_STATUSES
    model_path: str | None
    material: str
    provider: str  # "local" or fulfillment provider name
    printer_name: str | None
    tracking_url: str | None
    cost_usd: float
    created_at: float
    updated_at: float
    steps_completed: list[str]
    current_step: str
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_order_id() -> str:
    """Generate a unique order ID."""
    return f"pso_{uuid.uuid4().hex[:16]}"


def _validate_request(request: PrintServiceRequest) -> list[str]:
    """Validate a print service request, returning a list of errors."""
    errors: list[str] = []

    sources = sum(
        [
            request.model_path is not None,
            request.model_url is not None,
            request.prompt is not None,
        ]
    )

    if sources == 0:
        errors.append("One of model_path, model_url, or prompt is required")
    elif sources > 1:
        errors.append("Only one of model_path, model_url, or prompt should be provided")

    if request.model_path and not os.path.isfile(request.model_path):
        errors.append(f"Model file not found: {request.model_path}")

    if request.quantity < 1:
        errors.append("quantity must be at least 1")

    if request.max_budget_usd is not None and request.max_budget_usd <= 0:
        errors.append("max_budget_usd must be positive")

    return errors


def _estimate_local_cost(material: str, *, quantity: int = 1) -> dict[str, Any]:
    """Estimate cost for local printing."""
    # Base cost estimates per material (USD per gram, approximate).
    material_costs = {
        "pla": 0.02,
        "abs": 0.025,
        "petg": 0.03,
        "tpu": 0.04,
        "nylon": 0.05,
        "asa": 0.03,
    }

    cost_per_gram = material_costs.get(material.lower(), 0.03)
    # Assume ~50g average model weight for estimation.
    estimated_grams = 50
    unit_cost = cost_per_gram * estimated_grams
    total_cost = unit_cost * quantity

    return {
        "cost_usd": round(total_cost, 2),
        "estimated_time_hours": round(2.0 * quantity, 1),
        "material_cost_per_gram": cost_per_gram,
    }


def _estimate_fulfillment_cost(
    material: str,
    *,
    quantity: int = 1,
) -> dict[str, Any]:
    """Estimate cost for fulfillment printing (Craftcloud, Sculpteo, etc.)."""
    # Fulfillment is typically 3-5x more expensive than local.
    local = _estimate_local_cost(material, quantity=quantity)
    multiplier = 4.0

    return {
        "cost_usd": round(local["cost_usd"] * multiplier, 2),
        "estimated_time_hours": round(48.0 + local["estimated_time_hours"], 1),
        "provider": "fulfillment",
        "shipping_included": True,
    }


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def create_print_order(request: PrintServiceRequest) -> PrintServiceQuote:
    """Validate a print service request and generate a quote.

    Returns a quote with local and/or fulfillment options.
    Does NOT start printing — call ``confirm_print_order`` to begin.
    """
    errors = _validate_request(request)
    if errors:
        raise ValueError("; ".join(errors))

    order_id = _generate_order_id()

    # Estimate costs.
    local_option = _estimate_local_cost(request.material, quantity=request.quantity)
    fulfillment_option = _estimate_fulfillment_cost(request.material, quantity=request.quantity)

    # Determine recommendation.
    if request.prefer_local:
        recommended = "local"
        reasoning = "Local printing preferred — faster and cheaper."
        total_cost = local_option["cost_usd"]
        estimated_time = local_option["estimated_time_hours"]
    else:
        recommended = "fulfillment"
        reasoning = "Fulfillment selected — professional quality with shipping."
        total_cost = fulfillment_option["cost_usd"]
        estimated_time = fulfillment_option["estimated_time_hours"]

    # Check budget.
    if request.max_budget_usd is not None and total_cost > request.max_budget_usd:
        if recommended == "fulfillment" and local_option["cost_usd"] <= request.max_budget_usd:
            recommended = "local"
            reasoning = "Switched to local printing to stay within budget."
            total_cost = local_option["cost_usd"]
            estimated_time = local_option["estimated_time_hours"]
        else:
            reasoning += " Warning: both options exceed budget."

    # Compute printability score (simple heuristic).
    printability_score = 85  # default
    if request.model_path and os.path.isfile(request.model_path):
        ext = os.path.splitext(request.model_path)[1].lower()
        if ext in (".stl", ".3mf"):
            printability_score = 90
        elif ext in (".obj",):
            printability_score = 75
        elif ext in (".step", ".stp"):
            printability_score = 70
    elif request.prompt:
        printability_score = 60  # AI-generated models less reliable

    # Save order to DB.
    now = time.time()
    _save_order_to_db(
        order_id=order_id,
        status="received",
        request=request,
        material=request.material,
        cost_usd=total_cost,
        created_at=now,
        callback_url=request.callback_url,
    )

    return PrintServiceQuote(
        order_id=order_id,
        local_option=local_option,
        fulfillment_option=fulfillment_option,
        recommended=recommended,
        reasoning=reasoning,
        total_cost_usd=round(total_cost, 2),
        estimated_time_hours=round(estimated_time, 1),
        printability_score=printability_score,
    )


def confirm_print_order(
    order_id: str,
    *,
    option: str = "recommended",
) -> PrintServiceOrder:
    """Confirm and start processing a print order.

    Args:
        order_id: The order ID from ``create_print_order``.
        option: ``"local"``, ``"fulfillment"``, or ``"recommended"``.
    """
    order = get_order_status(order_id)
    if order.status not in ("received",):
        raise ValueError(f"Order {order_id} cannot be confirmed — current status is {order.status!r}")

    provider = option if option != "recommended" else "local"
    now = time.time()

    _update_order_status(
        order_id=order_id,
        status="validating",
        provider=provider,
        current_step="validating",
        steps_completed=["received"],
        updated_at=now,
    )

    return get_order_status(order_id)


def get_order_status(order_id: str) -> PrintServiceOrder:
    """Get the current status of a print service order."""
    from kiln.persistence import get_db

    db = get_db()
    row = db._conn.execute(
        "SELECT * FROM print_service_orders WHERE id = ?",
        (order_id,),
    ).fetchone()

    if row is None:
        raise ValueError(f"Order {order_id!r} not found")

    d = dict(row)
    try:
        steps = json.loads(d.get("steps_completed") or "[]")
    except (json.JSONDecodeError, TypeError):
        steps = []

    return PrintServiceOrder(
        order_id=d["id"],
        status=d["status"],
        model_path=d.get("model_path"),
        material=d.get("material", "pla"),
        provider=d.get("provider", "local"),
        printer_name=d.get("printer_name"),
        tracking_url=d.get("tracking_url"),
        cost_usd=d.get("cost_usd", 0.0),
        created_at=d["created_at"],
        updated_at=d["updated_at"],
        steps_completed=steps,
        current_step=d.get("current_step", "received"),
        error=d.get("error"),
    )


def cancel_order(order_id: str) -> dict[str, Any]:
    """Cancel a print service order.

    Only orders that have not started printing can be cancelled.
    """
    order = get_order_status(order_id)

    non_cancellable = {"printing", "shipping", "delivered", "cancelled", "failed"}
    if order.status in non_cancellable:
        return {
            "success": False,
            "error": f"Order {order_id} cannot be cancelled — status is {order.status!r}",
        }

    _update_order_status(
        order_id=order_id,
        status="cancelled",
        current_step="cancelled",
        updated_at=time.time(),
    )

    return {
        "success": True,
        "order_id": order_id,
        "message": f"Order {order_id} has been cancelled.",
    }


def list_orders(
    *,
    status: str | None = None,
    limit: int = 20,
) -> list[PrintServiceOrder]:
    """List print service orders."""
    from kiln.persistence import get_db

    db = get_db()

    if status:
        rows = db._conn.execute(
            "SELECT * FROM print_service_orders WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = db._conn.execute(
            "SELECT * FROM print_service_orders ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    orders: list[PrintServiceOrder] = []
    for row in rows:
        d = dict(row)
        try:
            steps = json.loads(d.get("steps_completed") or "[]")
        except (json.JSONDecodeError, TypeError):
            steps = []
        orders.append(
            PrintServiceOrder(
                order_id=d["id"],
                status=d["status"],
                model_path=d.get("model_path"),
                material=d.get("material", "pla"),
                provider=d.get("provider", "local"),
                printer_name=d.get("printer_name"),
                tracking_url=d.get("tracking_url"),
                cost_usd=d.get("cost_usd", 0.0),
                created_at=d["created_at"],
                updated_at=d["updated_at"],
                steps_completed=steps,
                current_step=d.get("current_step", "received"),
                error=d.get("error"),
            )
        )

    return orders


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _save_order_to_db(
    *,
    order_id: str,
    status: str,
    request: PrintServiceRequest,
    material: str,
    cost_usd: float,
    created_at: float,
    callback_url: str | None = None,
) -> None:
    """Insert a new order into the database."""
    from kiln.persistence import get_db

    db = get_db()
    with db._write_lock:
        db._conn.execute(
            """
            INSERT INTO print_service_orders
                (id, status, request, model_path, material, provider,
                 printer_name, tracking_url, cost_usd, created_at, updated_at,
                 steps_completed, current_step, error, callback_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                status,
                json.dumps(request.to_dict()),
                request.model_path,
                material,
                "local" if request.prefer_local else "fulfillment",
                request.printer_name,
                None,  # tracking_url
                cost_usd,
                created_at,
                created_at,  # updated_at
                json.dumps(["received"]),
                "received",
                None,  # error
                callback_url,
            ),
        )
        db._conn.commit()


def _update_order_status(
    *,
    order_id: str,
    status: str | None = None,
    provider: str | None = None,
    printer_name: str | None = None,
    tracking_url: str | None = None,
    current_step: str | None = None,
    steps_completed: list[str] | None = None,
    error: str | None = None,
    updated_at: float | None = None,
) -> None:
    """Update an existing order in the database."""
    from kiln.persistence import get_db

    db = get_db()
    updates: list[str] = []
    params: list[Any] = []

    if status is not None:
        updates.append("status = ?")
        params.append(status)
    if provider is not None:
        updates.append("provider = ?")
        params.append(provider)
    if printer_name is not None:
        updates.append("printer_name = ?")
        params.append(printer_name)
    if tracking_url is not None:
        updates.append("tracking_url = ?")
        params.append(tracking_url)
    if current_step is not None:
        updates.append("current_step = ?")
        params.append(current_step)
    if steps_completed is not None:
        updates.append("steps_completed = ?")
        params.append(json.dumps(steps_completed))
    if error is not None:
        updates.append("error = ?")
        params.append(error)
    if updated_at is not None:
        updates.append("updated_at = ?")
        params.append(updated_at)

    if not updates:
        return

    params.append(order_id)
    sql = f"UPDATE print_service_orders SET {', '.join(updates)} WHERE id = ?"

    with db._write_lock:
        db._conn.execute(sql, params)
        db._conn.commit()
