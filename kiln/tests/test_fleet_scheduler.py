"""Tests for kiln.fleet_scheduler — capability matching, time estimation, load balancing.

Covers:
- PrinterCapabilities / JobRequirements / PrinterScore dataclasses and to_dict()
- FleetSchedulingStrategy enum values
- estimate_print_time() heuristics (materials, layer heights, edge cases)
- filter_by_capabilities() filtering by material, build volume, nozzle size
- select_best_printer() scoring and ranking under all strategies
- get_fleet_capabilities() integration with registry and queue mocks
"""

from __future__ import annotations

from unittest.mock import MagicMock

from kiln.fleet_scheduler import (
    FleetSchedulingStrategy,
    JobRequirements,
    PrinterCapabilities,
    PrinterScore,
    _score_printer,
    estimate_print_time,
    filter_by_capabilities,
    get_fleet_capabilities,
    select_best_printer,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cap(
    printer_id: str = "printer-1",
    *,
    materials: list[str] | None = None,
    max_build_volume: tuple[float, float, float] = (220.0, 220.0, 250.0),
    nozzle_sizes: list[float] | None = None,
    is_available: bool = True,
    current_load: float = 0.0,
    estimated_queue_wait_minutes: int = 0,
    success_rate: float = 0.9,
) -> PrinterCapabilities:
    return PrinterCapabilities(
        printer_id=printer_id,
        materials=materials or ["PLA", "PETG"],
        max_build_volume=max_build_volume,
        nozzle_sizes=nozzle_sizes or [0.4],
        is_available=is_available,
        current_load=current_load,
        estimated_queue_wait_minutes=estimated_queue_wait_minutes,
        success_rate=success_rate,
    )


# ---------------------------------------------------------------------------
# Dataclass tests
# ---------------------------------------------------------------------------


class TestPrinterCapabilities:
    """PrinterCapabilities dataclass construction and serialisation."""

    def test_basic_construction(self):
        cap = _make_cap()
        assert cap.printer_id == "printer-1"
        assert cap.materials == ["PLA", "PETG"]
        assert cap.is_available is True

    def test_to_dict_returns_serialisable(self):
        cap = _make_cap(max_build_volume=(300.0, 300.0, 400.0))
        d = cap.to_dict()
        assert d["printer_id"] == "printer-1"
        assert d["max_build_volume"] == [300.0, 300.0, 400.0]
        assert isinstance(d["max_build_volume"], list)

    def test_to_dict_round_trips_all_fields(self):
        cap = _make_cap(success_rate=0.75, current_load=0.5)
        d = cap.to_dict()
        assert d["success_rate"] == 0.75
        assert d["current_load"] == 0.5
        assert d["estimated_queue_wait_minutes"] == 0


class TestJobRequirements:
    """JobRequirements dataclass construction and serialisation."""

    def test_all_none_by_default(self):
        req = JobRequirements()
        assert req.material is None
        assert req.min_build_volume is None
        assert req.nozzle_size is None

    def test_to_dict_with_volume(self):
        req = JobRequirements(min_build_volume=(200.0, 200.0, 200.0))
        d = req.to_dict()
        assert d["min_build_volume"] == [200.0, 200.0, 200.0]

    def test_to_dict_without_volume(self):
        req = JobRequirements(material="PLA")
        d = req.to_dict()
        assert d["min_build_volume"] is None
        assert d["material"] == "PLA"


class TestPrinterScore:
    """PrinterScore dataclass construction and serialisation."""

    def test_to_dict(self):
        score = PrinterScore(
            printer_id="p1",
            total_score=0.85,
            success_component=0.36,
            load_component=0.3,
            wait_component=0.19,
        )
        d = score.to_dict()
        assert d["printer_id"] == "p1"
        assert d["total_score"] == 0.85


# ---------------------------------------------------------------------------
# Enum tests
# ---------------------------------------------------------------------------


class TestFleetSchedulingStrategy:
    """FleetSchedulingStrategy enum values and string serialisation."""

    def test_all_values(self):
        assert FleetSchedulingStrategy.ROUND_ROBIN.value == "round_robin"
        assert FleetSchedulingStrategy.LEAST_LOADED.value == "least_loaded"
        assert FleetSchedulingStrategy.CAPABILITY_MATCHED.value == "capability_matched"

    def test_from_value(self):
        assert FleetSchedulingStrategy("round_robin") is FleetSchedulingStrategy.ROUND_ROBIN


# ---------------------------------------------------------------------------
# estimate_print_time tests
# ---------------------------------------------------------------------------


class TestEstimatePrintTime:
    """estimate_print_time() heuristic time estimation."""

    def test_zero_size_returns_one(self):
        assert estimate_print_time(0) == 1

    def test_negative_size_returns_one(self):
        assert estimate_print_time(-100) == 1

    def test_one_mb_pla_default_layer(self):
        # 1 MB * 45 min/MB * 1.0 (PLA) * 1.0 (0.2mm) = 45 min
        result = estimate_print_time(1024 * 1024)
        assert result == 45

    def test_material_multiplier_tpu(self):
        # 1 MB * 45 * 1.6 (TPU) = 72 min
        result = estimate_print_time(1024 * 1024, material="TPU")
        assert result == 72

    def test_material_case_insensitive(self):
        result_lower = estimate_print_time(1024 * 1024, material="pla")
        result_upper = estimate_print_time(1024 * 1024, material="PLA")
        assert result_lower == result_upper

    def test_unknown_material_defaults_to_pla_speed(self):
        result = estimate_print_time(1024 * 1024, material="MYSTERY_FILAMENT")
        assert result == estimate_print_time(1024 * 1024, material="PLA")

    def test_thinner_layers_take_longer(self):
        result_02 = estimate_print_time(1024 * 1024, layer_height_mm=0.2)
        result_01 = estimate_print_time(1024 * 1024, layer_height_mm=0.1)
        assert result_01 == result_02 * 2

    def test_thicker_layers_take_less(self):
        result_02 = estimate_print_time(1024 * 1024, layer_height_mm=0.2)
        result_03 = estimate_print_time(1024 * 1024, layer_height_mm=0.3)
        assert result_03 < result_02

    def test_very_small_layer_height_clamped(self):
        # Should not divide by zero or overflow
        result = estimate_print_time(1024 * 1024, layer_height_mm=0.0)
        assert result >= 1

    def test_large_file(self):
        # 100 MB should be proportionally longer
        result_1 = estimate_print_time(1024 * 1024)
        result_100 = estimate_print_time(100 * 1024 * 1024)
        assert result_100 == result_1 * 100


# ---------------------------------------------------------------------------
# filter_by_capabilities tests
# ---------------------------------------------------------------------------


class TestFilterByCapabilities:
    """filter_by_capabilities() filtering logic."""

    def test_empty_capabilities_returns_empty(self):
        result = filter_by_capabilities([], JobRequirements(material="PLA"))
        assert result == []

    def test_no_requirements_returns_all_available(self):
        caps = [_make_cap("p1"), _make_cap("p2")]
        result = filter_by_capabilities(caps, JobRequirements())
        assert len(result) == 2

    def test_unavailable_printers_filtered_out(self):
        caps = [
            _make_cap("p1", is_available=True),
            _make_cap("p2", is_available=False),
        ]
        result = filter_by_capabilities(caps, JobRequirements())
        assert len(result) == 1
        assert result[0].printer_id == "p1"

    def test_material_filter(self):
        caps = [
            _make_cap("p1", materials=["PLA", "PETG"]),
            _make_cap("p2", materials=["ABS", "ASA"]),
        ]
        result = filter_by_capabilities(caps, JobRequirements(material="PLA"))
        assert len(result) == 1
        assert result[0].printer_id == "p1"

    def test_material_filter_case_insensitive(self):
        caps = [_make_cap("p1", materials=["pla"])]
        result = filter_by_capabilities(caps, JobRequirements(material="PLA"))
        assert len(result) == 1

    def test_build_volume_filter(self):
        caps = [
            _make_cap("small", max_build_volume=(150.0, 150.0, 150.0)),
            _make_cap("large", max_build_volume=(300.0, 300.0, 400.0)),
        ]
        result = filter_by_capabilities(caps, JobRequirements(min_build_volume=(200.0, 200.0, 200.0)))
        assert len(result) == 1
        assert result[0].printer_id == "large"

    def test_build_volume_exact_match_passes(self):
        caps = [_make_cap("exact", max_build_volume=(200.0, 200.0, 200.0))]
        result = filter_by_capabilities(caps, JobRequirements(min_build_volume=(200.0, 200.0, 200.0)))
        assert len(result) == 1

    def test_build_volume_one_axis_too_small(self):
        caps = [_make_cap("narrow", max_build_volume=(300.0, 150.0, 300.0))]
        result = filter_by_capabilities(caps, JobRequirements(min_build_volume=(200.0, 200.0, 200.0)))
        assert len(result) == 0

    def test_nozzle_size_filter(self):
        caps = [
            _make_cap("p1", nozzle_sizes=[0.4]),
            _make_cap("p2", nozzle_sizes=[0.4, 0.6, 0.8]),
        ]
        result = filter_by_capabilities(caps, JobRequirements(nozzle_size=0.6))
        assert len(result) == 1
        assert result[0].printer_id == "p2"

    def test_combined_filters(self):
        caps = [
            _make_cap("p1", materials=["PLA"], max_build_volume=(300.0, 300.0, 300.0), nozzle_sizes=[0.4]),
            _make_cap("p2", materials=["PLA"], max_build_volume=(150.0, 150.0, 150.0), nozzle_sizes=[0.4]),
            _make_cap("p3", materials=["ABS"], max_build_volume=(300.0, 300.0, 300.0), nozzle_sizes=[0.4]),
        ]
        result = filter_by_capabilities(
            caps,
            JobRequirements(material="PLA", min_build_volume=(200.0, 200.0, 200.0)),
        )
        assert len(result) == 1
        assert result[0].printer_id == "p1"


# ---------------------------------------------------------------------------
# _score_printer tests
# ---------------------------------------------------------------------------


class TestScorePrinter:
    """_score_printer() composite scoring."""

    def test_perfect_printer(self):
        cap = _make_cap(success_rate=1.0, current_load=0.0, estimated_queue_wait_minutes=1)
        score = _score_printer(cap)
        # 1.0*0.4 + 1.0*0.3 + (1/1)*0.3 = 1.0
        assert score.total_score == 1.0

    def test_fully_loaded_printer(self):
        cap = _make_cap(success_rate=1.0, current_load=1.0, estimated_queue_wait_minutes=1)
        score = _score_printer(cap)
        # 1.0*0.4 + 0.0*0.3 + (1/1)*0.3 = 0.7
        assert score.total_score == 0.7

    def test_zero_success_rate(self):
        cap = _make_cap(success_rate=0.0, current_load=0.0, estimated_queue_wait_minutes=1)
        score = _score_printer(cap)
        # 0.0*0.4 + 1.0*0.3 + (1/1)*0.3 = 0.6
        assert score.total_score == 0.6

    def test_zero_wait_minutes_treated_as_one(self):
        cap = _make_cap(estimated_queue_wait_minutes=0)
        score = _score_printer(cap)
        # wait component: (1/1)*0.3 = 0.3
        assert score.wait_component == 0.3

    def test_long_wait_reduces_score(self):
        short = _score_printer(_make_cap(estimated_queue_wait_minutes=1))
        long = _score_printer(_make_cap(estimated_queue_wait_minutes=60))
        assert short.total_score > long.total_score


# ---------------------------------------------------------------------------
# select_best_printer tests
# ---------------------------------------------------------------------------


class TestSelectBestPrinter:
    """select_best_printer() ranking under different strategies."""

    def test_capability_matched_default_strategy(self):
        caps = [
            _make_cap("slow", success_rate=0.5, current_load=0.8),
            _make_cap("fast", success_rate=0.95, current_load=0.1),
        ]
        ranked = select_best_printer(caps)
        assert ranked[0].printer_id == "fast"
        assert ranked[1].printer_id == "slow"

    def test_capability_matched_filters_by_material(self):
        caps = [
            _make_cap("pla-only", materials=["PLA"]),
            _make_cap("abs-only", materials=["ABS"]),
        ]
        ranked = select_best_printer(caps, material="ABS")
        assert len(ranked) == 1
        assert ranked[0].printer_id == "abs-only"

    def test_capability_matched_no_match_returns_empty(self):
        caps = [_make_cap("pla-only", materials=["PLA"])]
        ranked = select_best_printer(caps, material="NYLON")
        assert ranked == []

    def test_least_loaded_ignores_capabilities(self):
        caps = [
            _make_cap("loaded", materials=["ABS"], current_load=0.9),
            _make_cap("empty", materials=["ABS"], current_load=0.1),
        ]
        ranked = select_best_printer(
            caps,
            material="PLA",  # should be ignored for LEAST_LOADED
            strategy=FleetSchedulingStrategy.LEAST_LOADED,
        )
        assert len(ranked) == 2
        assert ranked[0].printer_id == "empty"

    def test_round_robin_returns_all_available(self):
        caps = [
            _make_cap("p1", is_available=True),
            _make_cap("p2", is_available=False),
            _make_cap("p3", is_available=True),
        ]
        ranked = select_best_printer(caps, strategy=FleetSchedulingStrategy.ROUND_ROBIN)
        assert len(ranked) == 2
        ids = {s.printer_id for s in ranked}
        assert ids == {"p1", "p3"}

    def test_empty_fleet(self):
        ranked = select_best_printer([], material="PLA")
        assert ranked == []

    def test_build_volume_and_nozzle_combined(self):
        caps = [
            _make_cap("tiny", max_build_volume=(100.0, 100.0, 100.0), nozzle_sizes=[0.4]),
            _make_cap("big", max_build_volume=(350.0, 350.0, 400.0), nozzle_sizes=[0.4, 0.6]),
        ]
        ranked = select_best_printer(
            caps,
            min_build_volume=(200.0, 200.0, 200.0),
            nozzle_size=0.6,
        )
        assert len(ranked) == 1
        assert ranked[0].printer_id == "big"


# ---------------------------------------------------------------------------
# get_fleet_capabilities tests
# ---------------------------------------------------------------------------


class TestGetFleetCapabilities:
    """get_fleet_capabilities() integration with registry/queue mocks."""

    def _mock_registry(self, fleet_status: list[dict]) -> MagicMock:
        registry = MagicMock()
        registry.get_fleet_status.return_value = fleet_status
        return registry

    def _mock_queue(self, queued_jobs: list | None = None) -> MagicMock:
        queue = MagicMock()
        queue.list_jobs.return_value = queued_jobs or []
        return queue

    def test_empty_fleet(self):
        registry = self._mock_registry([])
        queue = self._mock_queue()
        caps = get_fleet_capabilities(registry, queue)
        assert caps == []

    def test_single_idle_printer(self):
        registry = self._mock_registry(
            [
                {
                    "name": "voron",
                    "backend": "moonraker",
                    "connected": True,
                    "state": "idle",
                    "tool_temp_actual": None,
                    "tool_temp_target": None,
                    "bed_temp_actual": None,
                    "bed_temp_target": None,
                }
            ]
        )
        queue = self._mock_queue()
        caps = get_fleet_capabilities(registry, queue)
        assert len(caps) == 1
        assert caps[0].printer_id == "voron"
        assert caps[0].is_available is True
        assert caps[0].current_load == 0.0

    def test_printing_printer_not_available(self):
        registry = self._mock_registry(
            [
                {
                    "name": "ender",
                    "backend": "octoprint",
                    "connected": True,
                    "state": "printing",
                    "tool_temp_actual": 200.0,
                    "tool_temp_target": 200.0,
                    "bed_temp_actual": 60.0,
                    "bed_temp_target": 60.0,
                }
            ]
        )
        queue = self._mock_queue()
        caps = get_fleet_capabilities(registry, queue)
        assert len(caps) == 1
        assert caps[0].is_available is False

    def test_queue_depth_affects_load(self):
        registry = self._mock_registry(
            [
                {
                    "name": "voron",
                    "backend": "moonraker",
                    "connected": True,
                    "state": "idle",
                    "tool_temp_actual": None,
                    "tool_temp_target": None,
                    "bed_temp_actual": None,
                    "bed_temp_target": None,
                }
            ]
        )
        # 5 targeted jobs for this printer
        jobs = []
        for _i in range(5):
            job = MagicMock()
            job.printer_name = "voron"
            jobs.append(job)
        queue = self._mock_queue(jobs)
        caps = get_fleet_capabilities(registry, queue)
        assert caps[0].current_load == 0.5  # 5/10

    def test_untargeted_jobs_distributed_fairly(self):
        registry = self._mock_registry(
            [
                {
                    "name": "p1",
                    "backend": "mock",
                    "connected": True,
                    "state": "idle",
                    "tool_temp_actual": None,
                    "tool_temp_target": None,
                    "bed_temp_actual": None,
                    "bed_temp_target": None,
                },
                {
                    "name": "p2",
                    "backend": "mock",
                    "connected": True,
                    "state": "idle",
                    "tool_temp_actual": None,
                    "tool_temp_target": None,
                    "bed_temp_actual": None,
                    "bed_temp_target": None,
                },
            ]
        )
        # 4 untargeted jobs across 2 printers = 2 each
        jobs = []
        for _ in range(4):
            job = MagicMock()
            job.printer_name = None
            jobs.append(job)
        queue = self._mock_queue(jobs)
        caps = get_fleet_capabilities(registry, queue)
        # Each gets ceil(4/2) = 2 jobs → load = 2/10 = 0.2
        assert caps[0].current_load == 0.2
        assert caps[1].current_load == 0.2

    def test_custom_printer_metadata(self):
        registry = self._mock_registry(
            [
                {
                    "name": "voron",
                    "backend": "moonraker",
                    "connected": True,
                    "state": "idle",
                    "tool_temp_actual": None,
                    "tool_temp_target": None,
                    "bed_temp_actual": None,
                    "bed_temp_target": None,
                }
            ]
        )
        queue = self._mock_queue()
        meta = {
            "voron": {
                "materials": ["PLA", "PETG", "ABS", "ASA", "NYLON"],
                "max_build_volume": (350.0, 350.0, 400.0),
                "nozzle_sizes": [0.4, 0.6],
            }
        }
        caps = get_fleet_capabilities(registry, queue, printer_metadata=meta)
        assert caps[0].materials == ["PLA", "PETG", "ABS", "ASA", "NYLON"]
        assert caps[0].max_build_volume == (350.0, 350.0, 400.0)
        assert caps[0].nozzle_sizes == [0.4, 0.6]

    def test_persistence_provides_success_rate(self):
        registry = self._mock_registry(
            [
                {
                    "name": "voron",
                    "backend": "moonraker",
                    "connected": True,
                    "state": "idle",
                    "tool_temp_actual": None,
                    "tool_temp_target": None,
                    "bed_temp_actual": None,
                    "bed_temp_target": None,
                }
            ]
        )
        queue = self._mock_queue()
        persistence = MagicMock()
        persistence.suggest_printer_for_outcome.return_value = [
            {"printer_name": "voron", "success_rate": 0.95},
        ]
        caps = get_fleet_capabilities(registry, queue, persistence=persistence)
        assert caps[0].success_rate == 0.95

    def test_persistence_failure_uses_default(self):
        registry = self._mock_registry(
            [
                {
                    "name": "voron",
                    "backend": "moonraker",
                    "connected": True,
                    "state": "idle",
                    "tool_temp_actual": None,
                    "tool_temp_target": None,
                    "bed_temp_actual": None,
                    "bed_temp_target": None,
                }
            ]
        )
        queue = self._mock_queue()
        persistence = MagicMock()
        persistence.suggest_printer_for_outcome.side_effect = RuntimeError("db error")
        caps = get_fleet_capabilities(registry, queue, persistence=persistence)
        assert caps[0].success_rate == 0.8  # conservative default

    def test_load_capped_at_one(self):
        registry = self._mock_registry(
            [
                {
                    "name": "busy",
                    "backend": "mock",
                    "connected": True,
                    "state": "idle",
                    "tool_temp_actual": None,
                    "tool_temp_target": None,
                    "bed_temp_actual": None,
                    "bed_temp_target": None,
                }
            ]
        )
        # 15 targeted jobs → 15/10 capped to 1.0
        jobs = []
        for _ in range(15):
            job = MagicMock()
            job.printer_name = "busy"
            jobs.append(job)
        queue = self._mock_queue(jobs)
        caps = get_fleet_capabilities(registry, queue)
        assert caps[0].current_load == 1.0
