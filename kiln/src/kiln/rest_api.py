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
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class RestApiConfig:
    """Configuration for the REST API server."""

    host: str = "0.0.0.0"
    port: int = 8420
    auth_token: str | None = None  # If set, require Bearer token
    cors_origins: list[str] = field(default_factory=lambda: ["*"])
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


def _list_tool_schemas(mcp_instance, *, tier: str = "full") -> List[Dict[str, Any]]:
    """Extract tool metadata from the FastMCP tool manager.

    Returns a list of dicts with name, description, parameters, and
    endpoint path for each registered tool.
    """
    tools = mcp_instance._tool_manager.list_tools()
    schemas = []
    for t in tools:
        schemas.append({
            "name": t.name,
            "description": t.description or "",
            "parameters": t.parameters or {},
            "method": "POST",
            "endpoint": f"/api/tools/{t.name}",
        })
    return schemas


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


def create_app(config: RestApiConfig | None = None) -> "FastAPI":
    """Create and configure the FastAPI application.

    All tool functions are imported lazily from ``kiln.server`` and wrapped
    as POST endpoints under ``/api/tools/{tool_name}``.

    Also provides:

    - ``GET /api/tools`` -- list all available tools with schemas
    - ``GET /api/health`` -- server health check
    - ``POST /api/agent`` -- run agent loop (requires OpenRouter/OpenAI key)
    """
    try:
        from fastapi import FastAPI, HTTPException, Request, Depends
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import JSONResponse
    except ImportError:
        raise ImportError(
            "FastAPI is required for the REST API. "
            "Install it with: pip install kiln3d[rest]"
        )

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

    # ----- Auth dependency ------------------------------------------------

    async def verify_auth(request: Request):
        """Verify Bearer token if auth is configured."""
        if not config.auth_token:
            return
        auth_header = request.headers.get("Authorization", "")
        if auth_header != f"Bearer {config.auth_token}":
            raise HTTPException(
                status_code=401,
                detail="Invalid or missing auth token",
            )

    # ----- Health check ---------------------------------------------------

    @app.get("/api/health")
    async def health():
        """Server health check."""
        return {"status": "ok", "version": "0.1.0"}

    # ----- Tool discovery -------------------------------------------------

    @app.get("/api/tools")
    async def list_tools(_=Depends(verify_auth)):
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
        _=Depends(verify_auth),
    ):
        """Execute an MCP tool by name with JSON parameters."""
        mcp_instance = _get_mcp_instance()
        func = _get_tool_function(mcp_instance, tool_name)
        if func is None:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown tool: {tool_name}",
            )

        # Parse body (may be empty for no-param tools)
        try:
            body = await request.json()
        except Exception:
            body = {}

        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400,
                detail="Request body must be a JSON object",
            )

        # Execute the tool function
        try:
            result = func(**body)
            # Handle async tool functions
            if inspect.isawaitable(result):
                result = await result
            return result
        except TypeError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid parameters: {exc}",
            )
        except Exception as exc:
            logger.exception("Error executing tool %s", tool_name)
            return {
                "success": False,
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": str(exc),
                },
            }

    # ----- Agent loop endpoint --------------------------------------------

    @app.post("/api/agent")
    async def run_agent(request: Request, _=Depends(verify_auth)):
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
            )

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
            from kiln.agent_loop import run_agent_loop, AgentConfig
        except ImportError:
            raise HTTPException(
                status_code=501,
                detail=(
                    "Agent loop module not available. "
                    "Ensure kiln.agent_loop is installed."
                ),
            )

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
            result = await loop.run_in_executor(
                None, lambda: run_agent_loop(prompt, agent_config)
            )
            return result.to_dict()
        except Exception as exc:
            logger.exception("Agent loop error")
            return {
                "success": False,
                "error": {
                    "code": "AGENT_ERROR",
                    "message": str(exc),
                },
            }

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
            "Uvicorn is required to run the REST server. "
            "Install with: pip install kiln3d[rest]"
        )

    if config is None:
        config = RestApiConfig()

    app = create_app(config)
    logger.info(
        "Starting Kiln REST API on %s:%d (tier: %s)",
        config.host,
        config.port,
        config.tool_tier,
    )
    uvicorn.run(app, host=config.host, port=config.port)
