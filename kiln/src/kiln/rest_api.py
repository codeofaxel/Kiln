"""Kiln REST API -- wraps all MCP tools as HTTP endpoints via FastAPI.

Provides a thin REST mapping layer over the existing MCP tool functions,
enabling any HTTP client (not just MCP clients) to control printers.

All tool endpoints follow a uniform pattern::

    POST /api/tools/{tool_name}
    Content-Type: application/json
    Authorization: Bearer <token>  (optional, depends on config)

    Body: {"param1": "value", "param2": 42}
    Response: {"success": true, "data": {...}}

Discovery endpoint::

    GET /api/tools
    Response: {"tools": [...], "count": 101, "tier": "full"}

Agent endpoint (runs the full agent loop via HTTP)::

    POST /api/agent
    Body: {"prompt": "...", "model": "...", "api_key": "..."}

FastAPI and uvicorn are optional dependencies.  Install them with::

    pip install kiln3d[rest]
"""

from __future__ import annotations

import asyncio
import inspect
import json as _json
import logging
import math
import os
import threading
import time as _time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.requests import Request  # module-level so PEP 563 deferred annotations resolve

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def _rest_auth_token_from_env() -> str | None:
    """Resolve REST bearer token from environment variables."""
    token = (os.environ.get("KILN_API_AUTH_TOKEN", "") or os.environ.get("KILN_AUTH_TOKEN", "")).strip()
    return token or None


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class RateLimiter:
    """Thread-safe in-memory sliding-window rate limiter.

    Tracks request timestamps per client key (API key or IP address)
    within a rolling time window.  Configurable via the ``KILN_RATE_LIMIT``
    environment variable (requests per minute; ``0`` to disable).

    Each :meth:`check` call returns ``(allowed, remaining, reset_epoch)``
    so callers can populate standard rate-limit response headers.
    """

    def __init__(self, max_requests: int = 60, window_seconds: float = 60.0):
        self._max = max_requests
        self._window = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    @property
    def limit(self) -> int:
        """Maximum requests allowed per window."""
        return self._max

    @property
    def window(self) -> float:
        """Window duration in seconds."""
        return self._window

    @property
    def enabled(self) -> bool:
        """Whether rate limiting is active (max > 0)."""
        return self._max > 0

    def check(self, key: str) -> tuple[bool, int, float]:
        """Check whether a request from *key* is allowed.

        Returns:
            A 3-tuple ``(allowed, remaining, reset_epoch)`` where:

            - *allowed*: ``True`` if the request should proceed.
            - *remaining*: How many requests remain in the current window.
            - *reset_epoch*: Unix timestamp when the window resets (for
              the ``X-RateLimit-Reset`` / ``Retry-After`` headers).
        """
        if not self.enabled:
            return True, self._max, _time.time() + self._window

        now_mono = _time.monotonic()
        now_wall = _time.time()
        cutoff = now_mono - self._window

        with self._lock:
            hits = self._hits[key]
            # Purge expired entries
            self._hits[key] = hits = [t for t in hits if t > cutoff]

            if len(hits) >= self._max:
                # Earliest hit determines when the window opens next
                oldest = hits[0] if hits else now_mono
                retry_after = oldest + self._window - now_mono
                reset_epoch = now_wall + retry_after
                return False, 0, reset_epoch

            hits.append(now_mono)
            remaining = max(0, self._max - len(hits))
            reset_epoch = now_wall + self._window
            return True, remaining, reset_epoch


def _build_rate_limiter() -> RateLimiter:
    """Create a :class:`RateLimiter` from the ``KILN_RATE_LIMIT`` env var.

    The env var is interpreted as *requests per minute*.  Defaults to
    ``60``.  Set to ``0`` to disable rate limiting entirely.
    """
    raw = os.environ.get("KILN_RATE_LIMIT", "60").strip()
    try:
        limit = int(raw)
    except ValueError:
        logger.warning("Invalid KILN_RATE_LIMIT value %r, defaulting to 60", raw)
        limit = 60
    if limit < 0:
        limit = 0
    return RateLimiter(max_requests=limit, window_seconds=60.0)


_rate_limiter = _build_rate_limiter()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class RestApiConfig:
    """Configuration for the REST API server."""

    host: str = field(default_factory=lambda: os.environ.get("KILN_REST_HOST", "127.0.0.1"))
    port: int = 8420
    auth_token: str | None = field(default_factory=_rest_auth_token_from_env)
    cors_origins: list[str] = field(default_factory=list)
    tool_tier: str = "full"  # Which tools to expose


# ---------------------------------------------------------------------------
# Tool introspection helpers
# ---------------------------------------------------------------------------


def _get_mcp_instance():
    """Lazily import and return the FastMCP server instance from kiln.server.

    Uses a deferred import so the REST module can be loaded without
    triggering the heavyweight server module at import time.
    """
    from kiln.server import mcp  # noqa: F811

    return mcp


def _list_tool_schemas(mcp_instance, *, tier: str = "full") -> list[dict[str, Any]]:
    """Extract tool metadata from the FastMCP tool manager.

    Returns a list of dicts with name, description, parameters, and
    endpoint path for each registered tool, filtered by *tier*.
    """
    # Build the set of tool names allowed for this tier.
    # The "full" tier exposes all tools — no filtering needed.
    allowed: set[str] | None = None
    if tier != "full":
        try:
            from kiln.tool_tiers import TIERS

            tier_list = TIERS.get(tier)
            if tier_list is not None:
                allowed = set(tier_list)
        except ImportError:
            pass  # If tool_tiers is unavailable, expose all tools.

    tools = mcp_instance._tool_manager.list_tools()
    schemas = []
    for t in tools:
        if allowed is not None and t.name not in allowed:
            continue
        schemas.append(
            {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.parameters or {},
                "method": "POST",
                "endpoint": f"/api/tools/{t.name}",
            }
        )
    return schemas


def _allowed_tool_names(tier: str) -> set[str] | None:
    """Return the set of tool names allowed for *tier*, or None if all."""
    if tier == "full":
        return None  # Full tier exposes everything.
    try:
        from kiln.tool_tiers import TIERS

        tier_list = TIERS.get(tier)
        if tier_list is not None:
            return set(tier_list)
    except ImportError:
        pass
    return None


def _get_tool_function(mcp_instance, tool_name: str):
    """Look up a tool's underlying Python function by name.

    Returns the callable, or None if the tool does not exist.
    """
    tool = mcp_instance._tool_manager.get_tool(tool_name)
    if tool is None:
        return None
    return tool.fn


# ---------------------------------------------------------------------------
# FastAPI application factory
# ---------------------------------------------------------------------------


def create_app(config: RestApiConfig | None = None) -> FastAPI:
    """Create and configure the FastAPI application.

    All tool functions are imported lazily from ``kiln.server`` and wrapped
    as POST endpoints under ``/api/tools/{tool_name}``.

    .. note:: Loads ``.env`` from the working directory and ``~/.kiln/.env``
       so that Stripe/Circle API keys are available without manual export.

    Also provides:

    - ``GET /api/tools`` -- list all available tools with schemas
    - ``GET /api/health`` -- server health check
    - ``POST /api/agent`` -- run agent loop (requires OpenRouter/OpenAI key)
    """
    # Load .env file if present.
    try:
        from pathlib import Path as _P

        from dotenv import load_dotenv

        load_dotenv()
        load_dotenv(_P.home() / ".kiln" / ".env")
    except ImportError:
        pass

    try:
        from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import JSONResponse
    except ImportError:
        raise ImportError("FastAPI is required for the REST API. Install it with: pip install kiln3d[rest]") from None

    if config is None:
        config = RestApiConfig()

    app = FastAPI(
        title="Kiln REST API",
        description="REST API for AI-agent-driven 3D printer control",
        version="0.1.0",
    )

    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ----- Rate-limit middleware ------------------------------------------

    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import Response as StarletteResponse

    class _RateLimitMiddleware(BaseHTTPMiddleware):
        """Middleware that enforces rate limits and injects headers."""

        async def dispatch(self, request: Request, call_next):
            # Skip rate limiting for health checks
            if request.url.path == "/api/health":
                return await call_next(request)

            # Determine client identity: auth token > IP
            client_key = self._client_key(request)
            allowed, remaining, reset_epoch = _rate_limiter.check(client_key)

            if not allowed and _rate_limiter.enabled:
                retry_after = max(1, int(math.ceil(reset_epoch - _time.time())))
                return JSONResponse(
                    {
                        "success": False,
                        "error": f"Rate limit exceeded ({_rate_limiter.limit} requests/min). Retry after {retry_after}s.",
                    },
                    status_code=429,
                    headers={
                        "Retry-After": str(retry_after),
                        "X-RateLimit-Limit": str(_rate_limiter.limit),
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": str(int(reset_epoch)),
                    },
                )

            response: StarletteResponse = await call_next(request)

            # Attach rate-limit headers to all responses
            if _rate_limiter.enabled:
                response.headers["X-RateLimit-Limit"] = str(_rate_limiter.limit)
                response.headers["X-RateLimit-Remaining"] = str(remaining)
                response.headers["X-RateLimit-Reset"] = str(int(reset_epoch))

            return response

        @staticmethod
        def _client_key(request: Request) -> str:
            """Derive a rate-limit key from the request.

            Uses the Bearer token (if present) so that authenticated
            clients get their own bucket.  Falls back to client IP.
            """
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer ") and len(auth) > 7:
                return f"token:{auth[7:]}"
            if request.client:
                return f"ip:{request.client.host}"
            return "ip:unknown"

    app.add_middleware(_RateLimitMiddleware)

    # ----- Auth dependency ------------------------------------------------

    async def verify_auth(request: Request):
        """Verify Bearer token if auth is configured."""
        if not config.auth_token:
            return
        import hmac as _hmac

        auth_header = request.headers.get("Authorization", "")
        expected = f"Bearer {config.auth_token}"
        if not _hmac.compare_digest(auth_header, expected):
            raise HTTPException(
                status_code=401,
                detail="Invalid or missing auth token",
            )

    _auth_dep = Depends(verify_auth)

    async def verify_license(request: Request) -> dict:
        """Extract and validate license key from Authorization header."""
        from kiln.fulfillment.proxy_server import get_orchestrator

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="License key required. Run 'kiln register' to get a free key.")
        key = auth[7:].strip()
        if not key:
            raise HTTPException(status_code=401, detail="Empty license key")

        orch = get_orchestrator()
        result = orch.validate_license(key)
        if not result.get("valid", False):
            raise HTTPException(status_code=401, detail=result.get("error", "Invalid license key"))
        return result

    _license_dep = Depends(verify_license)
    _file_upload = File(...)

    # ----- Health check ---------------------------------------------------

    @app.get("/api/health")
    async def health():
        """Server health check."""
        return {"status": "ok", "version": "0.1.0"}

    # ----- Tool discovery -------------------------------------------------

    @app.get("/api/tools")
    async def list_tools(_=_auth_dep):
        """List all available MCP tools with their schemas."""
        mcp_instance = _get_mcp_instance()
        schemas = _list_tool_schemas(mcp_instance, tier=config.tool_tier)
        return {
            "tools": schemas,
            "count": len(schemas),
            "tier": config.tool_tier,
        }

    # ----- Dynamic tool execution -----------------------------------------

    @app.post("/api/tools/{tool_name}")
    async def execute_tool(
        tool_name: str,
        request: Request,
        _=_auth_dep,
    ):
        """Execute an MCP tool by name with JSON parameters."""
        # Tier gate: reject tools outside the configured tier.
        allowed = _allowed_tool_names(config.tool_tier)
        if allowed is not None and tool_name not in allowed:
            raise HTTPException(
                status_code=403,
                detail=f"Tool '{tool_name}' is not available in the '{config.tool_tier}' tier.",
            )

        mcp_instance = _get_mcp_instance()
        func = _get_tool_function(mcp_instance, tool_name)
        if func is None:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown tool: {tool_name}",
            )

        # Parse body with size limit (may be empty for no-param tools)
        raw = await request.body()
        if len(raw) > 1_048_576:  # 1 MB
            actual_kb = len(raw) / 1024
            return JSONResponse(
                {"success": False, "error": f"Request body too large ({actual_kb:.1f}KB, max 1024.0KB)."},
                status_code=413,
            )

        if raw:
            try:
                body = _json.loads(raw)
            except _json.JSONDecodeError as exc:
                return JSONResponse(
                    {"success": False, "error": f"Invalid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})."},
                    status_code=400,
                )
            except Exception as exc:
                return JSONResponse(
                    {"success": False, "error": f"Invalid JSON: {exc}"},
                    status_code=400,
                )
        else:
            body = {}

        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400,
                detail="Request body must be a JSON object",
            )

        # Filter body to only include parameters the tool actually accepts
        sig = inspect.signature(func)
        valid_params = set(sig.parameters.keys())
        filtered = {k: v for k, v in body.items() if k in valid_params}
        unknown = set(body.keys()) - valid_params
        if unknown:
            return JSONResponse(
                {"success": False, "error": f"Unknown parameters: {', '.join(sorted(unknown))}"},
                status_code=400,
            )

        # Execute the tool function
        try:
            result = func(**filtered)
            # Handle async tool functions
            if inspect.isawaitable(result):
                result = await result
            return result
        except TypeError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid parameters: {exc}",
            ) from exc
        except Exception as exc:
            logger.exception("Tool execution failed: %s", tool_name)
            return JSONResponse(
                {"success": False, "error": f"Tool '{tool_name}' execution failed: {type(exc).__name__}"},
                status_code=500,
            )

    # ----- Agent loop endpoint --------------------------------------------

    @app.post("/api/agent")
    async def run_agent(request: Request, _=_auth_dep):
        """Run the agent loop with a prompt and return the result.

        Requires an API key for the LLM provider (OpenRouter, OpenAI, etc.)
        in the request body.
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(
                status_code=400,
                detail="Request body must be valid JSON",
            ) from None

        prompt = body.get("prompt")
        if not prompt:
            raise HTTPException(
                status_code=400,
                detail="'prompt' is required",
            )

        api_key = body.get("api_key")
        if not api_key:
            raise HTTPException(
                status_code=400,
                detail="'api_key' is required for agent loop",
            )

        try:
            from kiln.agent_loop import AgentConfig, run_agent_loop
        except ImportError:
            raise HTTPException(
                status_code=501,
                detail=("Agent loop module not available. Ensure kiln.agent_loop is installed."),
            ) from None

        agent_config = AgentConfig(
            api_key=api_key,
            model=body.get("model", "openai/gpt-4o"),
            tool_tier=body.get("tool_tier", config.tool_tier),
            max_turns=body.get("max_turns", 20),
            base_url=body.get("base_url", "https://openrouter.ai/api/v1"),
        )

        try:
            # Run in thread pool to avoid blocking the event loop
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, lambda: run_agent_loop(prompt, agent_config))
            return result.to_dict()
        except Exception as exc:
            logger.exception("Agent loop error")
            return JSONResponse(
                {"success": False, "error": f"Agent execution failed: {type(exc).__name__}: {exc}"},
                status_code=500,
            )

    # ----- Stripe webhook ------------------------------------------------

    @app.post("/api/webhooks/stripe")
    async def stripe_webhook(request: Request):
        """Handle Stripe webhook events (e.g. setup_intent.succeeded)."""
        raw_body = await request.body()
        sig_header = request.headers.get("Stripe-Signature", "")
        webhook_secret = os.environ.get("KILN_STRIPE_WEBHOOK_SECRET", "")

        if not webhook_secret:
            logger.warning("Stripe webhook received but KILN_STRIPE_WEBHOOK_SECRET not set")
            return JSONResponse(
                {
                    "success": False,
                    "error": "Webhook secret not configured. Set KILN_STRIPE_WEBHOOK_SECRET environment variable.",
                },
                status_code=400,
            )

        try:
            import stripe as _stripe_mod  # type: ignore[import-untyped]
        except ImportError:
            return JSONResponse(
                {"success": False, "error": "stripe package not installed. Install with: pip install kiln3d[payments]"},
                status_code=500,
            )

        try:
            event = _stripe_mod.Webhook.construct_event(
                raw_body,
                sig_header,
                webhook_secret,
            )
        except ValueError as exc:
            return JSONResponse({"success": False, "error": f"Invalid payload: {exc}"}, status_code=400)
        except _stripe_mod.error.SignatureVerificationError:
            return JSONResponse(
                {
                    "success": False,
                    "error": "Invalid signature. Verify KILN_STRIPE_WEBHOOK_SECRET matches your Stripe Dashboard webhook signing secret.",
                },
                status_code=400,
            )

        if event["type"] == "setup_intent.succeeded":
            si = event["data"]["object"]
            pm_id = si.get("payment_method")
            customer_id = si.get("customer")
            if pm_id:
                try:
                    from kiln.cli.config import save_billing_config

                    save_data = {"stripe_payment_method_id": pm_id}
                    if customer_id:
                        save_data["stripe_customer_id"] = customer_id
                    save_billing_config(save_data)
                    logger.info(
                        "Stripe webhook: persisted payment_method %s",
                        pm_id,
                    )
                except Exception:
                    logger.exception("Failed to persist payment method from webhook")

        elif event["type"] == "checkout.session.completed":
            session_obj = event["data"]["object"]
            if session_obj.get("payment_status") != "paid":
                # Async payment not yet complete — wait for async webhook
                return {"received": True}

            customer_email = session_obj.get("customer_details", {}).get("email", "")
            session_metadata = session_obj.get("metadata", {})
            tier_str = session_metadata.get("tier", "pro")

            from kiln.licensing import LicenseTier, generate_license_key

            tier_map = {"business": LicenseTier.BUSINESS, "enterprise": LicenseTier.ENTERPRISE}
            tier = tier_map.get(tier_str, LicenseTier.PRO)

            try:
                license_key = generate_license_key(tier=tier, email=customer_email)
            except ValueError as exc:
                logger.error("License key generation failed: %s", exc)
                return JSONResponse(
                    {"success": False, "error": f"License signing key not configured: {exc}"},
                    status_code=500,
                )

            # Store only a hash of the key on the Stripe session (never the full key).
            import hashlib as _hashlib

            license_key_hash = _hashlib.sha256(license_key.encode("utf-8")).hexdigest()[:12]
            try:
                _stripe_mod.api_key = os.environ.get("KILN_STRIPE_SECRET_KEY", "")
                _stripe_mod.checkout.Session.modify(
                    session_obj["id"],
                    metadata={**session_metadata, "license_key_hash": license_key_hash},
                )
                logger.info(
                    "License key generated for session %s (%s, %s)",
                    session_obj["id"],
                    customer_email,
                    tier_str,
                )
            except Exception:
                logger.exception(
                    "Failed to store license key on Stripe session %s",
                    session_obj["id"],
                )

        return {"received": True}

    # ----- Fulfillment proxy endpoints ---------------------------------------

    @app.get("/api/fulfillment/materials")
    async def fulfillment_materials(
        provider: str = "craftcloud",
        license_info: dict = _license_dep,
    ):
        from kiln.fulfillment.proxy_server import get_orchestrator

        try:
            orch = get_orchestrator()
            materials = orch.handle_materials(provider)
            return JSONResponse({"success": True, "materials": materials, "provider": provider})
        except Exception:
            logger.exception("Proxy materials error")
            return JSONResponse({"success": False, "error": "Internal server error"}, status_code=500)

    @app.post("/api/fulfillment/quote")
    async def fulfillment_quote(
        file: UploadFile = _file_upload,
        material_id: str = Form(...),
        quantity: int = Form(1),
        shipping_country: str = Form("US"),
        provider: str = Form("craftcloud"),
        license_info: dict = _license_dep,
    ):
        import tempfile

        from kiln.fulfillment.base import QuoteRequest
        from kiln.fulfillment.proxy_server import get_orchestrator

        # Validate file extension
        _ALLOWED_EXTENSIONS = {".stl", ".obj", ".3mf", ".step", ".stp", ".iges", ".igs"}
        suffix = Path(file.filename).suffix.lower() if file.filename else ".stl"
        if suffix not in _ALLOWED_EXTENSIONS:
            return JSONResponse(
                {"success": False, "error": f"Unsupported file type: {suffix}. Allowed: {', '.join(sorted(_ALLOWED_EXTENSIONS))}"},
                status_code=400,
            )

        # Read with size limit (100 MB)
        _MAX_FILE_SIZE = 100 * 1024 * 1024
        content = await file.read()
        if len(content) > _MAX_FILE_SIZE:
            return JSONResponse(
                {"success": False, "error": f"File too large ({len(content) / (1024 * 1024):.1f} MB). Maximum is 100 MB."},
                status_code=413,
            )

        # Save uploaded file to temp location
        tmp_path = None
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        try:
            orch = get_orchestrator()
            request = QuoteRequest(
                file_path=tmp_path,
                material_id=material_id,
                quantity=quantity,
                shipping_country=shipping_country,
            )
            result = orch.handle_quote(
                provider,
                tmp_path,
                request,
                user_email=license_info.get("email", ""),
            )
            return JSONResponse({"success": True, **result})
        except Exception as exc:
            # Import FulfillmentError lazily to handle it
            try:
                from kiln.fulfillment.base import FulfillmentError

                if isinstance(exc, FulfillmentError):
                    return JSONResponse({"success": False, "error": str(exc), "code": exc.code}, status_code=400)
            except ImportError:
                pass
            logger.exception("Proxy quote error")
            return JSONResponse({"success": False, "error": "Internal server error"}, status_code=500)
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)

    @app.post("/api/fulfillment/order")
    async def fulfillment_order(
        request: Request,
        license_info: dict = _license_dep,
    ):
        from kiln.fulfillment.base import OrderRequest
        from kiln.fulfillment.proxy_server import get_orchestrator
        from kiln.licensing import LicenseTier

        body = await request.json()
        provider = body.get("provider", "craftcloud")

        try:
            orch = get_orchestrator()
            order_req = OrderRequest(
                quote_id=body["quote_id"],
                shipping_option_id=body.get("shipping_option_id", ""),
                shipping_address=body.get("shipping_address", {}),
                notes=body.get("notes", ""),
            )
            # Convert tier string back to enum
            tier_str = license_info.get("tier", "free")
            try:
                user_tier = LicenseTier(tier_str)
            except ValueError:
                user_tier = LicenseTier.FREE
            quote_token = body.get("quote_token", "")
            if not quote_token:
                return JSONResponse(
                    {"success": False, "error": "quote_token is required. Get one from POST /api/fulfillment/quote.", "code": "MISSING_QUOTE_TOKEN"},
                    status_code=400,
                )
            result = orch.handle_order(
                provider,
                order_req,
                user_email=license_info.get("email", ""),
                user_tier=user_tier,
                quote_token=quote_token,
            )
            return JSONResponse({"success": True, **result})
        except Exception as exc:
            # Import error types lazily
            try:
                from kiln.fulfillment.base import FulfillmentError
                from kiln.payments import PaymentError

                if isinstance(exc, FulfillmentError):
                    status = 402 if exc.code == "FREE_TIER_LIMIT" else 400
                    return JSONResponse({"success": False, "error": str(exc), "code": exc.code}, status_code=status)
                if isinstance(exc, PaymentError):
                    return JSONResponse(
                        {"success": False, "error": str(exc), "code": getattr(exc, "code", "PAYMENT_ERROR")},
                        status_code=402,
                    )
            except ImportError:
                pass
            logger.exception("Proxy order error")
            return JSONResponse({"success": False, "error": "Internal server error"}, status_code=500)

    @app.get("/api/fulfillment/order/{order_id}/status")
    async def fulfillment_order_status(
        order_id: str,
        provider: str = "craftcloud",
        license_info: dict = _license_dep,
    ):
        from kiln.fulfillment.proxy_server import get_orchestrator

        try:
            orch = get_orchestrator()
            result = orch.handle_status(provider, order_id)
            return JSONResponse({"success": True, "order": result})
        except Exception as exc:
            try:
                from kiln.fulfillment.base import FulfillmentError

                if isinstance(exc, FulfillmentError):
                    return JSONResponse({"success": False, "error": str(exc), "code": exc.code}, status_code=400)
            except ImportError:
                pass
            logger.exception("Proxy status error")
            return JSONResponse({"success": False, "error": "Internal server error"}, status_code=500)

    @app.post("/api/fulfillment/order/{order_id}/cancel")
    async def fulfillment_cancel(
        order_id: str,
        request: Request,
        license_info: dict = _license_dep,
    ):
        from kiln.fulfillment.proxy_server import get_orchestrator
        from kiln.licensing import LicenseTier

        body = await request.json() if request.headers.get("content-type") == "application/json" else {}
        provider = body.get("provider", "craftcloud")
        try:
            orch = get_orchestrator()
            tier_str = license_info.get("tier", "free")
            try:
                user_tier = LicenseTier(tier_str)
            except ValueError:
                user_tier = LicenseTier.FREE
            result = orch.handle_cancel(provider, order_id, user_tier=user_tier)
            return JSONResponse({"success": True, "order": result})
        except Exception as exc:
            try:
                from kiln.fulfillment.base import FulfillmentError

                if isinstance(exc, FulfillmentError):
                    return JSONResponse({"success": False, "error": str(exc), "code": exc.code}, status_code=400)
            except ImportError:
                pass
            logger.exception("Proxy cancel error")
            return JSONResponse({"success": False, "error": "Internal server error"}, status_code=500)

    # ----- License endpoints -------------------------------------------------

    @app.post("/api/license/validate")
    async def license_validate(request: Request):
        from kiln.fulfillment.proxy_server import get_orchestrator

        body = await request.json()
        key = body.get("license_key", "")
        if not key:
            return JSONResponse({"valid": False, "error": "No license key provided"}, status_code=400)

        orch = get_orchestrator()
        result = orch.validate_license(key)
        # Add usage stats
        if result.get("valid") and result.get("email"):
            ledger = orch._ledger
            result["usage"] = {
                "orders_this_month": ledger.network_jobs_this_month_for_user(result["email"]),
                "orders_limit": ledger._policy.free_tier_jobs,
            }
        # Serialize tier enum for JSON
        if "tier" in result:
            result["tier"] = result["tier"].value if hasattr(result["tier"], "value") else str(result["tier"])
        if "info" in result:
            result.pop("info")  # LicenseInfo not JSON-serializable
        return JSONResponse(result)

    @app.post("/api/license/register")
    async def license_register(request: Request):
        from kiln.fulfillment.proxy_server import get_orchestrator

        body = await request.json()
        email = body.get("email", "").strip()
        if not email or "@" not in email:
            return JSONResponse({"success": False, "error": "Valid email required"}, status_code=400)

        try:
            orch = get_orchestrator()
            result = orch.register_user(email)
            return JSONResponse({"success": True, **result})
        except Exception:
            logger.exception("License registration error")
            return JSONResponse({"success": False, "error": "Registration failed. Please try again."}, status_code=500)

    return app


# ---------------------------------------------------------------------------
# Server runner
# ---------------------------------------------------------------------------


def run_rest_server(config: RestApiConfig | None = None) -> None:
    """Start the REST API server (blocking).

    Creates the FastAPI application and runs it with uvicorn.
    """
    try:
        import uvicorn
    except ImportError:
        raise ImportError(
            "Uvicorn is required to run the REST server. Install with: pip install kiln3d[rest]"
        ) from None

    if config is None:
        config = RestApiConfig()

    app = create_app(config)

    # Refuse to bind to non-localhost without authentication
    _LOCALHOST_ADDRESSES = {"127.0.0.1", "localhost", "::1"}
    if config.host not in _LOCALHOST_ADDRESSES and not config.auth_token:
        raise RuntimeError(
            f"REST API cannot bind to {config.host} without authentication. "
            "Set KILN_API_AUTH_TOKEN=<token> (or pass --auth-token), "
            "or bind to localhost by setting KILN_REST_HOST=127.0.0.1"
        )

    logger.info(
        "Starting Kiln REST API on %s:%d (tier: %s)",
        config.host,
        config.port,
        config.tool_tier,
    )
    uvicorn.run(app, host=config.host, port=config.port)
