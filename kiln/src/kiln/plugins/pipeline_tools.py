"""Pipeline execution control tools plugin.

Extracts pipeline management MCP tools from server.py into a focused plugin
module.  Provides tools for listing pipelines and controlling pipeline
execution (status, pause, resume, abort, retry).

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


class _PipelineToolsPlugin:
    """Pipeline listing and execution control tools.

    Tools:
        - list_print_pipelines
        - pipeline_status
        - pipeline_pause
        - pipeline_resume
        - pipeline_abort
        - pipeline_retry_step
    """

    @property
    def name(self) -> str:
        return "pipeline_tools"

    @property
    def description(self) -> str:
        return "Pipeline listing and execution control tools"

    def register(self, mcp: Any) -> None:
        """Register pipeline tools with the MCP server."""

        import kiln.server as _srv

        # ------------------------------------------------------------------
        # list_print_pipelines
        # ------------------------------------------------------------------

        @mcp.tool()
        def list_print_pipelines() -> dict:
            """List all available pre-validated print pipelines.

            Pipelines are named command sequences that chain multiple operations
            into reliable one-shot workflows (e.g. quick_print, calibrate, benchmark).
            """
            if err := _srv._check_auth("pipeline"):
                return err
            return {"success": True, "pipelines": _srv._list_pipelines()}

        # ------------------------------------------------------------------
        # pipeline_status
        # ------------------------------------------------------------------

        @mcp.tool()
        def pipeline_status(execution_id: str) -> dict:
            """Get the current state of a pipeline execution.

            Returns the execution state (running/paused/completed/failed/aborted),
            completed steps, and the name of the next step to run.

            Args:
                execution_id: The pipeline execution ID returned when starting a pipeline.
            """
            if err := _srv._check_auth("pipeline"):
                return err
            ex = _srv._get_execution(execution_id)
            if ex is None:
                return _srv._error_dict(
                    f"No pipeline execution found with id '{execution_id}'",
                    code="NOT_FOUND",
                )
            return {"success": True, **ex.status_dict()}

        # ------------------------------------------------------------------
        # pipeline_pause
        # ------------------------------------------------------------------

        @mcp.tool()
        def pipeline_pause(execution_id: str) -> dict:
            """Pause a running pipeline at the next step boundary.

            The pipeline will finish the current step and then pause before
            starting the next one.  Use ``pipeline_resume`` to continue.

            Args:
                execution_id: The pipeline execution ID.
            """
            if err := _srv._check_auth("pipeline"):
                return err
            ex = _srv._get_execution(execution_id)
            if ex is None:
                return _srv._error_dict(
                    f"No pipeline execution found with id '{execution_id}'",
                    code="NOT_FOUND",
                )
            if ex.state != _srv._PipelineState.RUNNING:
                return _srv._error_dict(
                    f"Cannot pause: pipeline state is {ex.state.value}",
                    code="INVALID_STATE",
                )
            ex.pause()
            return {"success": True, "message": "Pause requested. Pipeline will pause before the next step."}

        # ------------------------------------------------------------------
        # pipeline_resume
        # ------------------------------------------------------------------

        @mcp.tool()
        def pipeline_resume(execution_id: str) -> dict:
            """Resume a paused pipeline from where it stopped.

            Continues executing from the next unfinished step.

            Args:
                execution_id: The pipeline execution ID.
            """
            if err := _srv._check_auth("pipeline"):
                return err
            ex = _srv._get_execution(execution_id)
            if ex is None:
                return _srv._error_dict(
                    f"No pipeline execution found with id '{execution_id}'",
                    code="NOT_FOUND",
                )
            if ex.state != _srv._PipelineState.PAUSED:
                return _srv._error_dict(
                    f"Cannot resume: pipeline state is {ex.state.value}",
                    code="INVALID_STATE",
                )
            result = ex.resume()
            return {"success": result.success, **result.to_dict()}

        # ------------------------------------------------------------------
        # pipeline_abort
        # ------------------------------------------------------------------

        @mcp.tool()
        def pipeline_abort(execution_id: str) -> dict:
            """Abort a running or paused pipeline.

            Immediately marks the pipeline as aborted. Any completed steps
            are preserved in the result.

            Args:
                execution_id: The pipeline execution ID.
            """
            if err := _srv._check_auth("pipeline"):
                return err
            ex = _srv._get_execution(execution_id)
            if ex is None:
                return _srv._error_dict(
                    f"No pipeline execution found with id '{execution_id}'",
                    code="NOT_FOUND",
                )
            if ex.state in (_srv._PipelineState.COMPLETED, _srv._PipelineState.ABORTED):
                return _srv._error_dict(
                    f"Cannot abort: pipeline state is {ex.state.value}",
                    code="INVALID_STATE",
                )
            result = ex.abort()
            return {"success": False, **result.to_dict()}

        # ------------------------------------------------------------------
        # pipeline_retry_step
        # ------------------------------------------------------------------

        @mcp.tool()
        def pipeline_retry_step(execution_id: str, step_index: int) -> dict:
            """Retry a specific failed step in a pipeline, then continue from there.

            Re-runs the step at the given index and, if it succeeds, continues
            executing the remaining steps.

            Args:
                execution_id: The pipeline execution ID.
                step_index: Zero-based index of the step to retry.
            """
            if err := _srv._check_auth("pipeline"):
                return err
            ex = _srv._get_execution(execution_id)
            if ex is None:
                return _srv._error_dict(
                    f"No pipeline execution found with id '{execution_id}'",
                    code="NOT_FOUND",
                )
            if ex.state not in (_srv._PipelineState.FAILED, _srv._PipelineState.PAUSED):
                return _srv._error_dict(
                    f"Cannot retry: pipeline state is {ex.state.value} (must be failed or paused)",
                    code="INVALID_STATE",
                )
            result = ex.retry_step(step_index)
            return {"success": result.success, **result.to_dict()}

        _logger.debug("Registered pipeline tools")


plugin = _PipelineToolsPlugin()
