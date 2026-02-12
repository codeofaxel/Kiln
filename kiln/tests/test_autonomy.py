"""Tests for kiln.autonomy — autonomy tier system.

Coverage areas:
- AutonomyLevel enum values and names
- AutonomyConstraints dataclass defaults and serialisation
- AutonomyConfig dataclass defaults and serialisation
- load_autonomy_config from env var, config file, and defaults
- check_autonomy at all levels with all safety classifications
- Level 1 constraint checking (materials, time, temps, tool lists)
- save_autonomy_config round-trip
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest
import yaml

from kiln.autonomy import (
    AutonomyConfig,
    AutonomyConstraints,
    AutonomyLevel,
    check_autonomy,
    load_autonomy_config,
    save_autonomy_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, data: Dict[str, Any]) -> Path:
    """Write a YAML config file and return the path."""
    config_path = tmp_path / "config.yaml"
    with config_path.open("w") as fh:
        yaml.safe_dump(data, fh)
    return config_path


# ---------------------------------------------------------------------------
# TestAutonomyLevel
# ---------------------------------------------------------------------------


class TestAutonomyLevel:
    """Enum values and string representations."""

    def test_confirm_all_value(self):
        assert AutonomyLevel.CONFIRM_ALL.value == 0

    def test_pre_screened_value(self):
        assert AutonomyLevel.PRE_SCREENED.value == 1

    def test_full_trust_value(self):
        assert AutonomyLevel.FULL_TRUST.value == 2

    def test_construct_from_int(self):
        assert AutonomyLevel(0) is AutonomyLevel.CONFIRM_ALL
        assert AutonomyLevel(1) is AutonomyLevel.PRE_SCREENED
        assert AutonomyLevel(2) is AutonomyLevel.FULL_TRUST

    def test_invalid_int_raises(self):
        with pytest.raises(ValueError):
            AutonomyLevel(99)

    def test_name_strings(self):
        assert AutonomyLevel.CONFIRM_ALL.name == "CONFIRM_ALL"
        assert AutonomyLevel.PRE_SCREENED.name == "PRE_SCREENED"
        assert AutonomyLevel.FULL_TRUST.name == "FULL_TRUST"


# ---------------------------------------------------------------------------
# TestConstraints
# ---------------------------------------------------------------------------


class TestConstraints:
    """AutonomyConstraints dataclass defaults and serialisation."""

    def test_defaults_all_none(self):
        c = AutonomyConstraints()
        assert c.max_print_time_seconds is None
        assert c.allowed_materials is None
        assert c.max_tool_temp is None
        assert c.max_bed_temp is None
        assert c.allowed_tools is None
        assert c.blocked_tools is None
        assert c.max_order_cost is None

    def test_to_dict_empty_when_defaults(self):
        c = AutonomyConstraints()
        # Bool fields are always included (even when False)
        assert c.to_dict() == {"require_first_layer_check": False}

    def test_to_dict_excludes_none(self):
        c = AutonomyConstraints(max_print_time_seconds=3600, max_tool_temp=250.0)
        d = c.to_dict()
        assert d == {"max_print_time_seconds": 3600, "max_tool_temp": 250.0, "require_first_layer_check": False}
        assert "allowed_materials" not in d
        assert "blocked_tools" not in d
        assert "max_order_cost" not in d

    def test_to_dict_includes_max_order_cost(self):
        c = AutonomyConstraints(max_order_cost=50.0)
        d = c.to_dict()
        assert d["max_order_cost"] == 50.0

    def test_to_dict_includes_lists(self):
        c = AutonomyConstraints(
            allowed_materials=["PLA", "PETG"],
            blocked_tools=["emergency_stop"],
        )
        d = c.to_dict()
        assert d["allowed_materials"] == ["PLA", "PETG"]
        assert d["blocked_tools"] == ["emergency_stop"]

    def test_equality(self):
        a = AutonomyConstraints(max_tool_temp=210.0)
        b = AutonomyConstraints(max_tool_temp=210.0)
        assert a == b

    def test_inequality(self):
        a = AutonomyConstraints()
        b = AutonomyConstraints(max_tool_temp=210.0)
        assert a != b


# ---------------------------------------------------------------------------
# TestAutonomyConfig
# ---------------------------------------------------------------------------


class TestAutonomyConfig:
    """AutonomyConfig dataclass and serialisation."""

    def test_default_level_is_confirm_all(self):
        cfg = AutonomyConfig()
        assert cfg.level == AutonomyLevel.CONFIRM_ALL

    def test_to_dict_default(self):
        cfg = AutonomyConfig()
        d = cfg.to_dict()
        assert d["level"] == 0
        assert d["level_name"] == "confirm_all"
        assert d["constraints"] == {"require_first_layer_check": False}

    def test_to_dict_with_level_and_constraints(self):
        cfg = AutonomyConfig(
            level=AutonomyLevel.PRE_SCREENED,
            constraints=AutonomyConstraints(
                max_print_time_seconds=7200,
                allowed_materials=["PLA"],
            ),
        )
        d = cfg.to_dict()
        assert d["level"] == 1
        assert d["level_name"] == "pre_screened"
        assert d["constraints"]["max_print_time_seconds"] == 7200
        assert d["constraints"]["allowed_materials"] == ["PLA"]


# ---------------------------------------------------------------------------
# TestLoadConfig
# ---------------------------------------------------------------------------


class TestLoadConfig:
    """Loading autonomy config from env var, config file, and defaults."""

    def test_default_when_no_env_no_file(self, tmp_path):
        config_path = tmp_path / "nonexistent.yaml"
        cfg = load_autonomy_config(config_path=config_path)
        assert cfg.level == AutonomyLevel.CONFIRM_ALL
        assert cfg.constraints == AutonomyConstraints()

    def test_env_var_level_0(self, tmp_path):
        with patch.dict(os.environ, {"KILN_AUTONOMY_LEVEL": "0"}):
            cfg = load_autonomy_config(config_path=tmp_path / "x.yaml")
        assert cfg.level == AutonomyLevel.CONFIRM_ALL

    def test_env_var_level_1(self, tmp_path):
        with patch.dict(os.environ, {"KILN_AUTONOMY_LEVEL": "1"}):
            cfg = load_autonomy_config(config_path=tmp_path / "x.yaml")
        assert cfg.level == AutonomyLevel.PRE_SCREENED

    def test_env_var_level_2(self, tmp_path):
        with patch.dict(os.environ, {"KILN_AUTONOMY_LEVEL": "2"}):
            cfg = load_autonomy_config(config_path=tmp_path / "x.yaml")
        assert cfg.level == AutonomyLevel.FULL_TRUST

    def test_env_var_invalid_defaults_to_zero(self, tmp_path):
        with patch.dict(os.environ, {"KILN_AUTONOMY_LEVEL": "banana"}):
            cfg = load_autonomy_config(config_path=tmp_path / "x.yaml")
        assert cfg.level == AutonomyLevel.CONFIRM_ALL

    def test_env_var_out_of_range_defaults_to_zero(self, tmp_path):
        with patch.dict(os.environ, {"KILN_AUTONOMY_LEVEL": "99"}):
            cfg = load_autonomy_config(config_path=tmp_path / "x.yaml")
        assert cfg.level == AutonomyLevel.CONFIRM_ALL

    def test_env_var_takes_precedence_over_file(self, tmp_path):
        config_path = _write_config(tmp_path, {"autonomy": {"level": 2}})
        with patch.dict(os.environ, {"KILN_AUTONOMY_LEVEL": "0"}):
            cfg = load_autonomy_config(config_path=config_path)
        assert cfg.level == AutonomyLevel.CONFIRM_ALL

    def test_config_file_level(self, tmp_path):
        config_path = _write_config(tmp_path, {"autonomy": {"level": 2}})
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KILN_AUTONOMY_LEVEL", None)
            cfg = load_autonomy_config(config_path=config_path)
        assert cfg.level == AutonomyLevel.FULL_TRUST

    def test_config_file_with_constraints(self, tmp_path):
        config_path = _write_config(tmp_path, {
            "autonomy": {
                "level": 1,
                "constraints": {
                    "max_print_time_seconds": 3600,
                    "allowed_materials": ["PLA", "PETG"],
                    "max_tool_temp": 230.0,
                    "max_bed_temp": 70.0,
                },
            },
        })
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KILN_AUTONOMY_LEVEL", None)
            cfg = load_autonomy_config(config_path=config_path)
        assert cfg.level == AutonomyLevel.PRE_SCREENED
        assert cfg.constraints.max_print_time_seconds == 3600
        assert cfg.constraints.allowed_materials == ["PLA", "PETG"]
        assert cfg.constraints.max_tool_temp == 230.0
        assert cfg.constraints.max_bed_temp == 70.0

    def test_config_file_invalid_level_defaults_to_zero(self, tmp_path):
        config_path = _write_config(tmp_path, {"autonomy": {"level": "bad"}})
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KILN_AUTONOMY_LEVEL", None)
            cfg = load_autonomy_config(config_path=config_path)
        assert cfg.level == AutonomyLevel.CONFIRM_ALL

    def test_config_file_autonomy_not_dict(self, tmp_path):
        config_path = _write_config(tmp_path, {"autonomy": "invalid"})
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KILN_AUTONOMY_LEVEL", None)
            cfg = load_autonomy_config(config_path=config_path)
        assert cfg.level == AutonomyLevel.CONFIRM_ALL

    def test_config_file_constraints_not_dict(self, tmp_path):
        config_path = _write_config(tmp_path, {
            "autonomy": {"level": 1, "constraints": "bad"},
        })
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KILN_AUTONOMY_LEVEL", None)
            cfg = load_autonomy_config(config_path=config_path)
        assert cfg.level == AutonomyLevel.PRE_SCREENED
        assert cfg.constraints == AutonomyConstraints()

    def test_config_file_missing_autonomy_section(self, tmp_path):
        config_path = _write_config(tmp_path, {"printers": {}})
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KILN_AUTONOMY_LEVEL", None)
            cfg = load_autonomy_config(config_path=config_path)
        assert cfg.level == AutonomyLevel.CONFIRM_ALL


# ---------------------------------------------------------------------------
# TestCheckAutonomy
# ---------------------------------------------------------------------------


class TestCheckAutonomy:
    """check_autonomy at all levels and safety classifications."""

    # --- safe/guarded always allowed regardless of level ---

    def test_safe_tool_always_allowed(self):
        for level in AutonomyLevel:
            cfg = AutonomyConfig(level=level)
            result = check_autonomy("printer_status", "safe", config=cfg)
            assert result["allowed"] is True

    def test_guarded_tool_always_allowed(self):
        for level in AutonomyLevel:
            cfg = AutonomyConfig(level=level)
            result = check_autonomy("upload_file", "guarded", config=cfg)
            assert result["allowed"] is True

    # --- emergency always denied regardless of level ---

    def test_emergency_tool_always_denied(self):
        for level in AutonomyLevel:
            cfg = AutonomyConfig(level=level)
            result = check_autonomy("emergency_stop", "emergency", config=cfg)
            assert result["allowed"] is False
            assert "emergency" in result["reason"].lower()

    # --- Level 0: confirm tools denied ---

    def test_level0_confirm_tool_denied(self):
        cfg = AutonomyConfig(level=AutonomyLevel.CONFIRM_ALL)
        result = check_autonomy("start_print", "confirm", config=cfg)
        assert result["allowed"] is False
        assert result["level"] == 0

    # --- Level 2: confirm tools allowed ---

    def test_level2_confirm_tool_allowed(self):
        cfg = AutonomyConfig(level=AutonomyLevel.FULL_TRUST)
        result = check_autonomy("start_print", "confirm", config=cfg)
        assert result["allowed"] is True
        assert result["level"] == 2

    def test_level2_blocked_tool_denied(self):
        cfg = AutonomyConfig(
            level=AutonomyLevel.FULL_TRUST,
            constraints=AutonomyConstraints(blocked_tools=["cancel_print"]),
        )
        result = check_autonomy("cancel_print", "confirm", config=cfg)
        assert result["allowed"] is False
        assert "blocked" in result["reason"].lower()

    def test_level2_non_blocked_tool_allowed(self):
        cfg = AutonomyConfig(
            level=AutonomyLevel.FULL_TRUST,
            constraints=AutonomyConstraints(blocked_tools=["cancel_print"]),
        )
        result = check_autonomy("start_print", "confirm", config=cfg)
        assert result["allowed"] is True

    # --- Level 1: constraint checking ---

    def test_level1_no_constraints_allows(self):
        cfg = AutonomyConfig(level=AutonomyLevel.PRE_SCREENED)
        result = check_autonomy("start_print", "confirm", config=cfg)
        assert result["allowed"] is True
        assert result["constraints_met"] is True

    def test_level1_blocked_tool_denied(self):
        cfg = AutonomyConfig(
            level=AutonomyLevel.PRE_SCREENED,
            constraints=AutonomyConstraints(blocked_tools=["send_gcode"]),
        )
        result = check_autonomy("send_gcode", "confirm", config=cfg)
        assert result["allowed"] is False

    def test_level1_allowed_tools_whitelist_pass(self):
        cfg = AutonomyConfig(
            level=AutonomyLevel.PRE_SCREENED,
            constraints=AutonomyConstraints(allowed_tools=["start_print", "cancel_print"]),
        )
        result = check_autonomy("start_print", "confirm", config=cfg)
        assert result["allowed"] is True

    def test_level1_allowed_tools_whitelist_deny(self):
        cfg = AutonomyConfig(
            level=AutonomyLevel.PRE_SCREENED,
            constraints=AutonomyConstraints(allowed_tools=["start_print"]),
        )
        result = check_autonomy("cancel_print", "confirm", config=cfg)
        assert result["allowed"] is False
        assert "whitelist" in result["reason"].lower()

    # --- result fields ---

    def test_result_has_required_fields(self):
        cfg = AutonomyConfig()
        result = check_autonomy("start_print", "confirm", config=cfg)
        assert "allowed" in result
        assert "reason" in result
        assert "level" in result
        assert "constraints_met" in result


# ---------------------------------------------------------------------------
# TestConstraintChecking
# ---------------------------------------------------------------------------


class TestConstraintChecking:
    """Level 1 constraint checking: materials, time, temps."""

    def _cfg(self, **kwargs) -> AutonomyConfig:
        return AutonomyConfig(
            level=AutonomyLevel.PRE_SCREENED,
            constraints=AutonomyConstraints(**kwargs),
        )

    # --- Material ---

    def test_material_allowed(self):
        cfg = self._cfg(allowed_materials=["PLA", "PETG"])
        result = check_autonomy(
            "start_print", "confirm",
            operation_context={"material": "PLA"},
            config=cfg,
        )
        assert result["allowed"] is True

    def test_material_case_insensitive(self):
        cfg = self._cfg(allowed_materials=["pla"])
        result = check_autonomy(
            "start_print", "confirm",
            operation_context={"material": "PLA"},
            config=cfg,
        )
        assert result["allowed"] is True

    def test_material_denied(self):
        cfg = self._cfg(allowed_materials=["PLA"])
        result = check_autonomy(
            "start_print", "confirm",
            operation_context={"material": "ABS"},
            config=cfg,
        )
        assert result["allowed"] is False
        assert "ABS" in result["reason"]

    def test_material_no_context_passes(self):
        """If no material in context, constraint is not checked."""
        cfg = self._cfg(allowed_materials=["PLA"])
        result = check_autonomy("start_print", "confirm", config=cfg)
        assert result["allowed"] is True

    def test_material_empty_string_passes(self):
        cfg = self._cfg(allowed_materials=["PLA"])
        result = check_autonomy(
            "start_print", "confirm",
            operation_context={"material": ""},
            config=cfg,
        )
        assert result["allowed"] is True

    # --- Print time ---

    def test_time_within_limit(self):
        cfg = self._cfg(max_print_time_seconds=7200)
        result = check_autonomy(
            "start_print", "confirm",
            operation_context={"estimated_time_seconds": 3600},
            config=cfg,
        )
        assert result["allowed"] is True

    def test_time_exceeds_limit(self):
        cfg = self._cfg(max_print_time_seconds=3600)
        result = check_autonomy(
            "start_print", "confirm",
            operation_context={"estimated_time_seconds": 7200},
            config=cfg,
        )
        assert result["allowed"] is False
        assert "7200" in result["reason"]

    def test_time_no_context_passes(self):
        cfg = self._cfg(max_print_time_seconds=3600)
        result = check_autonomy("start_print", "confirm", config=cfg)
        assert result["allowed"] is True

    def test_time_exact_limit_passes(self):
        cfg = self._cfg(max_print_time_seconds=3600)
        result = check_autonomy(
            "start_print", "confirm",
            operation_context={"estimated_time_seconds": 3600},
            config=cfg,
        )
        assert result["allowed"] is True

    # --- Tool temperature ---

    def test_tool_temp_within_limit(self):
        cfg = self._cfg(max_tool_temp=250.0)
        result = check_autonomy(
            "start_print", "confirm",
            operation_context={"tool_temp": 210.0},
            config=cfg,
        )
        assert result["allowed"] is True

    def test_tool_temp_exceeds_limit(self):
        cfg = self._cfg(max_tool_temp=250.0)
        result = check_autonomy(
            "start_print", "confirm",
            operation_context={"tool_temp": 260.0},
            config=cfg,
        )
        assert result["allowed"] is False
        assert "260" in result["reason"]

    def test_tool_temp_no_context_passes(self):
        cfg = self._cfg(max_tool_temp=250.0)
        result = check_autonomy("start_print", "confirm", config=cfg)
        assert result["allowed"] is True

    # --- Bed temperature ---

    def test_bed_temp_within_limit(self):
        cfg = self._cfg(max_bed_temp=80.0)
        result = check_autonomy(
            "start_print", "confirm",
            operation_context={"bed_temp": 60.0},
            config=cfg,
        )
        assert result["allowed"] is True

    def test_bed_temp_exceeds_limit(self):
        cfg = self._cfg(max_bed_temp=80.0)
        result = check_autonomy(
            "start_print", "confirm",
            operation_context={"bed_temp": 100.0},
            config=cfg,
        )
        assert result["allowed"] is False
        assert "100" in result["reason"]

    # --- Multiple constraints ---

    def test_all_constraints_pass(self):
        cfg = self._cfg(
            max_print_time_seconds=7200,
            allowed_materials=["PLA", "PETG"],
            max_tool_temp=250.0,
            max_bed_temp=80.0,
        )
        result = check_autonomy(
            "start_print", "confirm",
            operation_context={
                "material": "PLA",
                "estimated_time_seconds": 3600,
                "tool_temp": 210.0,
                "bed_temp": 60.0,
            },
            config=cfg,
        )
        assert result["allowed"] is True

    def test_first_failing_constraint_stops(self):
        """If material fails, we get a material error even if temp would also fail."""
        cfg = self._cfg(
            allowed_materials=["PLA"],
            max_tool_temp=200.0,
        )
        result = check_autonomy(
            "start_print", "confirm",
            operation_context={"material": "ABS", "tool_temp": 260.0},
            config=cfg,
        )
        assert result["allowed"] is False
        assert "ABS" in result["reason"]

    def test_blocked_takes_priority_over_allowed(self):
        cfg = self._cfg(
            allowed_tools=["start_print"],
            blocked_tools=["start_print"],
        )
        result = check_autonomy("start_print", "confirm", config=cfg)
        assert result["allowed"] is False
        assert "blocked" in result["reason"].lower()

    # --- Order cost (spending cap) ---

    def test_order_cost_within_limit(self):
        cfg = self._cfg(max_order_cost=100.0)
        result = check_autonomy(
            "fulfillment_place_order", "confirm",
            operation_context={"cost": 50.0},
            config=cfg,
        )
        assert result["allowed"] is True

    def test_order_cost_exceeds_limit(self):
        cfg = self._cfg(max_order_cost=100.0)
        result = check_autonomy(
            "fulfillment_place_order", "confirm",
            operation_context={"cost": 150.0},
            config=cfg,
        )
        assert result["allowed"] is False
        assert "spending cap" in result["reason"].lower()

    def test_order_cost_exact_limit_passes(self):
        cfg = self._cfg(max_order_cost=100.0)
        result = check_autonomy(
            "place_order", "confirm",
            operation_context={"cost": 100.0},
            config=cfg,
        )
        assert result["allowed"] is True

    def test_order_cost_no_context_passes(self):
        cfg = self._cfg(max_order_cost=100.0)
        result = check_autonomy("fulfillment_place_order", "confirm", config=cfg)
        assert result["allowed"] is True

    def test_order_cost_non_order_tool_ignored(self):
        """Spending cap only applies to order-related tools."""
        cfg = self._cfg(max_order_cost=10.0)
        result = check_autonomy(
            "start_print", "confirm",
            operation_context={"cost": 999.0},
            config=cfg,
        )
        assert result["allowed"] is True

    def test_order_cost_fulfillment_order_tool(self):
        cfg = self._cfg(max_order_cost=25.0)
        result = check_autonomy(
            "fulfillment_order", "confirm",
            operation_context={"cost": 30.0},
            config=cfg,
        )
        assert result["allowed"] is False

    def test_order_cost_no_limit_set(self):
        cfg = self._cfg()  # max_order_cost=None (default)
        result = check_autonomy(
            "fulfillment_place_order", "confirm",
            operation_context={"cost": 99999.0},
            config=cfg,
        )
        assert result["allowed"] is True


# ---------------------------------------------------------------------------
# TestLoadMaxOrderCost
# ---------------------------------------------------------------------------


class TestLoadMaxOrderCost:
    """Loading max_order_cost from env var and config file."""

    def test_env_var_sets_max_order_cost(self, tmp_path):
        with patch.dict(os.environ, {
            "KILN_AUTONOMY_LEVEL": "1",
            "KILN_AUTONOMY_MAX_ORDER_COST": "75.50",
        }):
            cfg = load_autonomy_config(config_path=tmp_path / "x.yaml")
        assert cfg.constraints.max_order_cost == 75.50

    def test_env_var_invalid_max_order_cost_ignored(self, tmp_path):
        with patch.dict(os.environ, {
            "KILN_AUTONOMY_LEVEL": "1",
            "KILN_AUTONOMY_MAX_ORDER_COST": "not_a_number",
        }):
            cfg = load_autonomy_config(config_path=tmp_path / "x.yaml")
        assert cfg.constraints.max_order_cost is None

    def test_config_file_sets_max_order_cost(self, tmp_path):
        config_path = _write_config(tmp_path, {
            "autonomy": {
                "level": 1,
                "constraints": {"max_order_cost": 200.0},
            },
        })
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KILN_AUTONOMY_LEVEL", None)
            os.environ.pop("KILN_AUTONOMY_MAX_ORDER_COST", None)
            cfg = load_autonomy_config(config_path=config_path)
        assert cfg.constraints.max_order_cost == 200.0

    def test_env_var_overrides_config_file_max_order_cost(self, tmp_path):
        config_path = _write_config(tmp_path, {
            "autonomy": {
                "level": 1,
                "constraints": {"max_order_cost": 200.0},
            },
        })
        with patch.dict(os.environ, {
            "KILN_AUTONOMY_MAX_ORDER_COST": "50.0",
        }, clear=False):
            os.environ.pop("KILN_AUTONOMY_LEVEL", None)
            cfg = load_autonomy_config(config_path=config_path)
        assert cfg.constraints.max_order_cost == 50.0

    def test_no_max_order_cost_defaults_to_none(self, tmp_path):
        config_path = _write_config(tmp_path, {"autonomy": {"level": 1}})
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KILN_AUTONOMY_LEVEL", None)
            os.environ.pop("KILN_AUTONOMY_MAX_ORDER_COST", None)
            cfg = load_autonomy_config(config_path=config_path)
        assert cfg.constraints.max_order_cost is None


# ---------------------------------------------------------------------------
# TestSaveConfig
# ---------------------------------------------------------------------------


class TestSaveConfig:
    """save_autonomy_config round-trip."""

    def test_save_and_load_round_trip(self, tmp_path):
        config_path = _write_config(tmp_path, {})
        original = AutonomyConfig(
            level=AutonomyLevel.PRE_SCREENED,
            constraints=AutonomyConstraints(
                max_print_time_seconds=7200,
                allowed_materials=["PLA", "PETG"],
                max_tool_temp=230.0,
            ),
        )

        save_autonomy_config(original, config_path=config_path)

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KILN_AUTONOMY_LEVEL", None)
            loaded = load_autonomy_config(config_path=config_path)

        assert loaded.level == AutonomyLevel.PRE_SCREENED
        assert loaded.constraints.max_print_time_seconds == 7200
        assert loaded.constraints.allowed_materials == ["PLA", "PETG"]
        assert loaded.constraints.max_tool_temp == 230.0

    def test_save_preserves_other_config(self, tmp_path):
        config_path = _write_config(tmp_path, {
            "printers": {"myprinter": {"type": "octoprint", "host": "http://localhost"}},
            "active_printer": "myprinter",
        })

        cfg = AutonomyConfig(level=AutonomyLevel.FULL_TRUST)
        save_autonomy_config(cfg, config_path=config_path)

        with config_path.open() as fh:
            raw = yaml.safe_load(fh)

        assert raw["printers"]["myprinter"]["type"] == "octoprint"
        assert raw["active_printer"] == "myprinter"
        assert raw["autonomy"]["level"] == 2

    def test_save_default_config_no_constraints(self, tmp_path):
        config_path = _write_config(tmp_path, {})
        cfg = AutonomyConfig()  # all defaults
        save_autonomy_config(cfg, config_path=config_path)

        with config_path.open() as fh:
            raw = yaml.safe_load(fh)

        assert raw["autonomy"]["level"] == 0
        assert "constraints" not in raw["autonomy"]

    def test_save_creates_file_if_missing(self, tmp_path):
        config_path = tmp_path / "subdir" / "config.yaml"
        cfg = AutonomyConfig(level=AutonomyLevel.FULL_TRUST)
        save_autonomy_config(cfg, config_path=config_path)

        assert config_path.exists()
        with config_path.open() as fh:
            raw = yaml.safe_load(fh)
        assert raw["autonomy"]["level"] == 2
