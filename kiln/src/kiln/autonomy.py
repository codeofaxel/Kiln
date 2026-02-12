"""Autonomy tier system for Kiln.

Defines configurable autonomy levels that control how much freedom an AI
agent has when operating the printer.  Higher levels reduce the number of
operations that require explicit human confirmation.

Levels
------
- **Level 0 -- Confirm All** (default): Every ``confirm``-level tool
  requires explicit human approval before execution.
- **Level 1 -- Pre-screened**: Agent may execute ``confirm``-level tools
  autonomously IF the operation passes safety constraints (material match,
  time limit, temperature within profile).  User sets constraints.
- **Level 2 -- Full Trust**: Agent may execute any tool autonomously.
  Only ``emergency``-level tools still require confirmation.

Configuration is via env var (``KILN_AUTONOMY_LEVEL``) or the
``autonomy`` section in ``~/.kiln/config.yaml``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class AutonomyLevel(Enum):
    """Agent autonomy tier."""

    CONFIRM_ALL = 0
    PRE_SCREENED = 1
    FULL_TRUST = 2


@dataclass
class AutonomyConstraints:
    """Constraints for Level 1 (pre-screened) autonomy.

    When autonomy is at Level 1, the agent may skip confirmation for
    confirm-level tools ONLY if the operation satisfies these constraints.
    """

    max_print_time_seconds: Optional[int] = None  # None = no limit
    allowed_materials: Optional[List[str]] = None  # None = any material
    max_tool_temp: Optional[float] = None
    max_bed_temp: Optional[float] = None
    allowed_tools: Optional[List[str]] = None  # specific tool names allowed
    blocked_tools: Optional[List[str]] = None  # tools that ALWAYS require confirmation
    require_first_layer_check: bool = False  # If True, autonomous prints must monitor first layer
    monitoring_mode: Optional[str] = None  # "vision", "telemetry", "auto" (default). None = auto
    max_order_cost: Optional[float] = None  # Max cost (USD) per fulfillment order. None = no limit

    def to_dict(self) -> Dict[str, Any]:
        """Return non-None fields as a plain dict.

        Boolean fields are always included (even when ``False``) so that
        the caller can distinguish "not set" from "explicitly disabled".
        """
        result: Dict[str, Any] = {}
        for k, v in asdict(self).items():
            if v is not None:
                result[k] = v
            # Always include bool fields so False is distinguishable from None
        # require_first_layer_check is bool with a default, always include
        result["require_first_layer_check"] = self.require_first_layer_check
        if self.monitoring_mode is not None:
            result["monitoring_mode"] = self.monitoring_mode
        return result


@dataclass
class AutonomyConfig:
    """Full autonomy configuration."""

    level: AutonomyLevel = AutonomyLevel.CONFIRM_ALL
    constraints: AutonomyConstraints = field(default_factory=AutonomyConstraints)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a JSON-friendly dict."""
        return {
            "level": self.level.value,
            "level_name": self.level.name.lower(),
            "constraints": self.constraints.to_dict(),
        }


def load_autonomy_config(*, config_path: Optional[Any] = None) -> AutonomyConfig:
    """Load autonomy configuration.

    Precedence: ``KILN_AUTONOMY_LEVEL`` env var > config file > default (0).
    """
    # Env var fast path
    env_level = os.environ.get("KILN_AUTONOMY_LEVEL")
    if env_level is not None:
        try:
            level_int = int(env_level)
            level = AutonomyLevel(level_int)
        except (ValueError, KeyError):
            logger.warning(
                "Invalid KILN_AUTONOMY_LEVEL=%r, defaulting to 0", env_level
            )
            level = AutonomyLevel.CONFIRM_ALL
        _flc_env = os.environ.get("KILN_MONITOR_REQUIRE_FIRST_LAYER", "").lower() in ("true", "1", "yes")
        _moc_env = os.environ.get("KILN_AUTONOMY_MAX_ORDER_COST")
        _max_order_cost: Optional[float] = None
        if _moc_env is not None:
            try:
                _max_order_cost = float(_moc_env)
            except ValueError:
                logger.warning("Invalid KILN_AUTONOMY_MAX_ORDER_COST=%r, ignoring", _moc_env)
        _mm_env = os.environ.get("KILN_MONITOR_MODE", "").lower().strip()
        _monitoring_mode: Optional[str] = _mm_env if _mm_env in ("vision", "telemetry", "auto") else None
        constraints = AutonomyConstraints(
            require_first_layer_check=_flc_env,
            max_order_cost=_max_order_cost,
            monitoring_mode=_monitoring_mode,
        )
        return AutonomyConfig(level=level, constraints=constraints)

    # Config file path
    try:
        from kiln.cli.config import _read_config_file, get_config_path

        path = config_path or get_config_path()
        raw = _read_config_file(path)
        autonomy_section = raw.get("autonomy", {})
        if not isinstance(autonomy_section, dict):
            return AutonomyConfig()

        level_val = autonomy_section.get("level", 0)
        try:
            level = AutonomyLevel(int(level_val))
        except (ValueError, KeyError):
            level = AutonomyLevel.CONFIRM_ALL

        constraints_raw = autonomy_section.get("constraints", {})
        if not isinstance(constraints_raw, dict):
            constraints_raw = {}

        _flc_raw = constraints_raw.get("require_first_layer_check", False)
        _flc = bool(_flc_raw) if not isinstance(_flc_raw, str) else _flc_raw.lower() in ("true", "1", "yes")

        _moc_raw = constraints_raw.get("max_order_cost")
        _max_order_cost_cfg: Optional[float] = None
        if _moc_raw is not None:
            try:
                _max_order_cost_cfg = float(_moc_raw)
            except (ValueError, TypeError):
                logger.warning("Invalid max_order_cost=%r in config, ignoring", _moc_raw)

        _mm_raw = constraints_raw.get("monitoring_mode")
        _mm_cfg: Optional[str] = None
        if isinstance(_mm_raw, str) and _mm_raw.lower().strip() in ("vision", "telemetry", "auto"):
            _mm_cfg = _mm_raw.lower().strip()

        constraints = AutonomyConstraints(
            max_print_time_seconds=constraints_raw.get("max_print_time_seconds"),
            allowed_materials=constraints_raw.get("allowed_materials"),
            max_tool_temp=constraints_raw.get("max_tool_temp"),
            max_bed_temp=constraints_raw.get("max_bed_temp"),
            allowed_tools=constraints_raw.get("allowed_tools"),
            blocked_tools=constraints_raw.get("blocked_tools"),
            require_first_layer_check=_flc,
            max_order_cost=_max_order_cost_cfg,
            monitoring_mode=_mm_cfg,
        )

        # Env var override for first-layer check
        _flc_env = os.environ.get("KILN_MONITOR_REQUIRE_FIRST_LAYER", "").lower()
        if _flc_env in ("true", "1", "yes"):
            constraints.require_first_layer_check = True
        elif _flc_env in ("false", "0", "no"):
            constraints.require_first_layer_check = False

        # Env var override for max order cost
        _moc_env_override = os.environ.get("KILN_AUTONOMY_MAX_ORDER_COST")
        if _moc_env_override is not None:
            try:
                constraints.max_order_cost = float(_moc_env_override)
            except ValueError:
                logger.warning("Invalid KILN_AUTONOMY_MAX_ORDER_COST=%r, ignoring", _moc_env_override)

        # Env var override for monitoring mode
        _mm_env_override = os.environ.get("KILN_MONITOR_MODE", "").lower().strip()
        if _mm_env_override in ("vision", "telemetry", "auto"):
            constraints.monitoring_mode = _mm_env_override

        return AutonomyConfig(level=level, constraints=constraints)
    except Exception:
        return AutonomyConfig()


def check_autonomy(
    tool_name: str,
    safety_level: str,
    *,
    operation_context: Optional[Dict[str, Any]] = None,
    config: Optional[AutonomyConfig] = None,
) -> Dict[str, Any]:
    """Check whether the agent may proceed without human confirmation.

    :param tool_name: The MCP tool being invoked (e.g. ``"start_print"``).
    :param safety_level: The tool's safety classification
        (``"safe"``, ``"guarded"``, ``"confirm"``, ``"emergency"``).
    :param operation_context: Optional context about the specific operation,
        e.g. ``{"material": "PLA", "estimated_time_seconds": 3600,
        "tool_temp": 210, "bed_temp": 60}``.
    :param config: Pre-loaded config (for testing).  Loads from disk if *None*.
    :returns: Dict with ``allowed`` (bool), ``reason`` (str), ``level`` (int),
        and ``constraints_met`` (bool).
    """
    if config is None:
        config = load_autonomy_config()

    ctx = operation_context or {}

    # Safe and guarded tools always allowed
    if safety_level in ("safe", "guarded"):
        return {
            "allowed": True,
            "reason": "Tool is safe/guarded -- no confirmation needed",
            "level": config.level.value,
            "constraints_met": True,
        }

    # Emergency tools always require confirmation (even at Level 2)
    if safety_level == "emergency":
        return {
            "allowed": False,
            "reason": "Emergency tools always require human confirmation",
            "level": config.level.value,
            "constraints_met": False,
        }

    # Level 0: everything else requires confirmation
    if config.level == AutonomyLevel.CONFIRM_ALL:
        return {
            "allowed": False,
            "reason": "Autonomy level 0 -- all confirm-level tools require human approval",
            "level": 0,
            "constraints_met": False,
        }

    # Level 2: full trust, confirm tools allowed
    if config.level == AutonomyLevel.FULL_TRUST:
        # Still respect blocked_tools
        if config.constraints.blocked_tools and tool_name in config.constraints.blocked_tools:
            return {
                "allowed": False,
                "reason": f"Tool {tool_name!r} is explicitly blocked",
                "level": 2,
                "constraints_met": False,
            }
        result_l2: Dict[str, Any] = {
            "allowed": True,
            "reason": "Autonomy level 2 -- full trust granted",
            "level": 2,
            "constraints_met": True,
        }
        if config.constraints.require_first_layer_check and tool_name in ("start_print", "quick_print"):
            result_l2["require_first_layer_check"] = True
        return result_l2

    # Level 1: pre-screened -- check constraints
    c = config.constraints

    # Check blocked tools
    if c.blocked_tools and tool_name in c.blocked_tools:
        return {
            "allowed": False,
            "reason": f"Tool {tool_name!r} is explicitly blocked",
            "level": 1,
            "constraints_met": False,
        }

    # Check allowed tools whitelist
    if c.allowed_tools is not None and tool_name not in c.allowed_tools:
        return {
            "allowed": False,
            "reason": f"Tool {tool_name!r} not in allowed_tools whitelist",
            "level": 1,
            "constraints_met": False,
        }

    # Check material constraint
    if c.allowed_materials is not None:
        op_material = ctx.get("material", "").upper()
        allowed_upper = [m.upper() for m in c.allowed_materials]
        if op_material and op_material not in allowed_upper:
            return {
                "allowed": False,
                "reason": (
                    f"Material {op_material!r} not in allowed list: "
                    f"{c.allowed_materials}"
                ),
                "level": 1,
                "constraints_met": False,
            }

    # Check print time constraint
    if c.max_print_time_seconds is not None:
        op_time = ctx.get("estimated_time_seconds")
        if op_time is not None and op_time > c.max_print_time_seconds:
            return {
                "allowed": False,
                "reason": (
                    f"Estimated time {op_time}s exceeds limit "
                    f"{c.max_print_time_seconds}s"
                ),
                "level": 1,
                "constraints_met": False,
            }

    # Check temperature constraints
    if c.max_tool_temp is not None:
        op_tool_temp = ctx.get("tool_temp")
        if op_tool_temp is not None and op_tool_temp > c.max_tool_temp:
            return {
                "allowed": False,
                "reason": (
                    f"Tool temp {op_tool_temp}\u00b0C exceeds limit "
                    f"{c.max_tool_temp}\u00b0C"
                ),
                "level": 1,
                "constraints_met": False,
            }

    if c.max_bed_temp is not None:
        op_bed_temp = ctx.get("bed_temp")
        if op_bed_temp is not None and op_bed_temp > c.max_bed_temp:
            return {
                "allowed": False,
                "reason": (
                    f"Bed temp {op_bed_temp}\u00b0C exceeds limit "
                    f"{c.max_bed_temp}\u00b0C"
                ),
                "level": 1,
                "constraints_met": False,
            }

    # Check order cost constraint (spending cap)
    _ORDER_TOOLS = ("fulfillment_place_order", "place_order", "fulfillment_order")
    if c.max_order_cost is not None and tool_name in _ORDER_TOOLS:
        op_cost = ctx.get("cost")
        if op_cost is not None and op_cost > c.max_order_cost:
            return {
                "allowed": False,
                "reason": (
                    f"Order cost ${op_cost:.2f} exceeds spending cap "
                    f"${c.max_order_cost:.2f}"
                ),
                "level": 1,
                "constraints_met": False,
            }

    result: Dict[str, Any] = {
        "allowed": True,
        "reason": "All Level 1 constraints satisfied",
        "level": 1,
        "constraints_met": True,
    }
    if c.require_first_layer_check and tool_name in ("start_print", "quick_print"):
        result["require_first_layer_check"] = True
    return result


def save_autonomy_config(
    config: AutonomyConfig,
    *,
    config_path: Optional[Any] = None,
) -> None:
    """Save autonomy configuration to the config file."""
    from kiln.cli.config import _read_config_file, _write_config_file, get_config_path

    path = config_path or get_config_path()
    raw = _read_config_file(path)

    autonomy_section: Dict[str, Any] = {
        "level": config.level.value,
    }

    if config.constraints != AutonomyConstraints():
        constraints_dict = config.constraints.to_dict()
        if constraints_dict:
            autonomy_section["constraints"] = constraints_dict

    raw["autonomy"] = autonomy_section
    _write_config_file(path, raw)
