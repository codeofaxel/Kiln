"""Tests for :mod:`kiln.incident_recorder`.

Every test uses ``tmp_path`` as the incidents root so the real
``~/.kiln/incidents/`` directory is never touched — critical since that
directory holds hand-curated evidence envelopes that must not be
polluted by test runs.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from kiln.incident_recorder import (
    INCIDENT_JSON_FILENAME,
    REPORT_FILENAME,
    STATUS_FILENAME,
    export_incident_for_sharing,
    list_incidents,
    record_incident,
)


# ---------------------------------------------------------------------------
# Fixtures


@pytest.fixture
def status() -> dict:
    """Minimal printer_status payload shaped like Bambu push_status output."""
    return {
        "printer_model": "bambu_a1",
        "ip": "10.0.1.42",
        "serial": "0309CA123456789AB",
        "tool_temp_c": 220.3,
        "bed_temp_c": 55.1,
        "print_error_flag": 0,
        "gcode_state": "printing",
    }


@pytest.fixture
def fake_artifacts(tmp_path: Path) -> dict:
    """Realistic stand-ins for an STL, a gcode, a 3MF, and a camera JPG."""
    stl = tmp_path / "disc.stl"
    stl.write_bytes(b"solid disc\nfacet normal 0 0 1\nendsolid disc\n")

    gcode = tmp_path / "disc.gcode"
    gcode.write_text(
        "; HEADER\n"
        "; printer: bambu_a1\n"
        "; bbox_min: -12.5, -12.5\n"
        "G28\n"
        "G1 X-12.5 Y-12.5 F3000\n"
        "; end header\n"
    )

    threemf = tmp_path / "disc.3mf"
    threemf.write_bytes(b"PK\x03\x04fake-3mf-bytes")

    jpg = tmp_path / "cam.jpg"
    jpg.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")

    return {
        "stl_path": str(stl),
        "gcode_path": str(gcode),
        "threemf_path": str(threemf),
        "camera_snapshot_path": str(jpg),
    }


# ---------------------------------------------------------------------------
# record_incident


def test_record_incident_creates_expected_files(
    tmp_path: Path, status: dict, fake_artifacts: dict
) -> None:
    root = tmp_path / "incidents"
    path = record_incident(
        "nozzle_crash_suspected",
        status,
        printer_id="default",
        bbox_info={"x_min": -12.5, "x_max": 12.5, "overlaps_bed_origin": True},
        user_description="Nozzle drove into purge tool",
        tool_call_trace=[
            {"tool": "compose_part_from_primitives", "args": {"r": 12.5}},
            {"tool": "slice_model", "args": {"printer_id": "bambu_a1"}},
            {"tool": "start_print", "args": {}},
        ],
        tags=["bambu_a1", "off_bed_geometry"],
        root_dir=root,
        **fake_artifacts,
    )

    incident_dir = Path(path)
    assert incident_dir.is_dir()
    assert incident_dir.parent == root

    # Core files.
    assert (incident_dir / REPORT_FILENAME).exists()
    assert (incident_dir / STATUS_FILENAME).exists()
    assert (incident_dir / INCIDENT_JSON_FILENAME).exists()

    # Evidence files copied (not symlinked).
    for name in ("model.stl", "job.gcode", "job.3mf", "camera.jpg"):
        copy = incident_dir / name
        assert copy.exists(), f"{name} not copied into incident dir"
        assert not copy.is_symlink(), f"{name} is a symlink — must be a copy"

    record = json.loads((incident_dir / INCIDENT_JSON_FILENAME).read_text())
    assert record["incident_type"] == "nozzle_crash_suspected"
    assert record["printer_id"] == "default"
    assert record["bbox_info"]["overlaps_bed_origin"] is True
    assert record["tags"] == ["bambu_a1", "off_bed_geometry"]
    assert "recorded_at_utc" in record
    assert record["incident_id"].endswith(record["incident_id"].split("_")[-1])

    # report.md references user description + bbox.
    report = (incident_dir / REPORT_FILENAME).read_text()
    assert "Nozzle drove into purge tool" in report
    assert "overlaps_bed_origin" in report


def test_record_incident_tolerates_missing_optional_fields(
    tmp_path: Path, status: dict
) -> None:
    """Only incident_type + printer_status are required; nothing else may crash."""
    path = record_incident(
        "user_cancel_pre_layer_5",
        status,
        root_dir=tmp_path,
    )
    incident_dir = Path(path)
    assert (incident_dir / INCIDENT_JSON_FILENAME).exists()

    record = json.loads((incident_dir / INCIDENT_JSON_FILENAME).read_text())
    assert record["incident_type"] == "user_cancel_pre_layer_5"
    assert "printer_id" not in record
    assert "bbox_info" not in record
    assert "tags" not in record
    assert "artifacts" not in record


def test_record_incident_missing_file_is_warned_not_crashed(
    tmp_path: Path, status: dict, caplog: pytest.LogCaptureFixture
) -> None:
    """Nonexistent artifact paths must log a warning, not raise."""
    missing = tmp_path / "does_not_exist.stl"
    with caplog.at_level("WARNING"):
        path = record_incident(
            "thermal_anomaly",
            status,
            stl_path=str(missing),
            root_dir=tmp_path,
        )

    incident_dir = Path(path)
    record = json.loads((incident_dir / INCIDENT_JSON_FILENAME).read_text())
    assert "artifacts" in record
    assert record["artifacts"]["stl_path"]["original_path"].endswith(
        "does_not_exist.stl"
    )
    assert "copy_error" in record["artifacts"]["stl_path"]
    assert any("missing" in rec.message for rec in caplog.records)


def test_record_incident_id_format(tmp_path: Path, status: dict) -> None:
    """incident_id must match YYYY-MM-DD_HH-MM-SS_<type>_<hash>."""
    import re as _re

    path = record_incident("hms_error", status, root_dir=tmp_path)
    name = Path(path).name
    assert _re.match(
        r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_hms_error_[0-9a-f]{8}$", name
    ), name


def test_record_incident_sanitizes_slashes_in_type(
    tmp_path: Path, status: dict
) -> None:
    """A stray ``/`` in incident_type must not escape the incidents root."""
    path = record_incident("bad/type\\here", status, root_dir=tmp_path)
    assert Path(path).parent == tmp_path
    # The slashes should have collapsed into underscores.
    assert "/" not in Path(path).name


# ---------------------------------------------------------------------------
# list_incidents


def test_list_incidents_empty_when_root_missing(tmp_path: Path) -> None:
    assert list_incidents(root_dir=tmp_path / "nothing") == []


def test_list_incidents_returns_newest_first(tmp_path: Path, status: dict) -> None:
    # Record three incidents spaced apart.  Without sleeping, the second-
    # resolution timestamps in the id may collide; the short hash keeps
    # directories unique but we want deterministic ordering.
    p1 = record_incident("first", status, root_dir=tmp_path)
    time.sleep(1.1)
    p2 = record_incident("second", status, root_dir=tmp_path)
    time.sleep(1.1)
    p3 = record_incident("third", status, root_dir=tmp_path)

    listing = list_incidents(limit=10, root_dir=tmp_path)
    assert len(listing) == 3

    types = [entry["incident_type"] for entry in listing]
    assert types == ["third", "second", "first"]

    # Paths match the directories we created.
    paths = [entry["path"] for entry in listing]
    assert paths == [p3, p2, p1]


def test_list_incidents_respects_limit(tmp_path: Path, status: dict) -> None:
    for i in range(5):
        record_incident(f"evt_{i}", status, root_dir=tmp_path)
        time.sleep(0.05)  # don't need full second; ids include a perf_counter hash
    assert len(list_incidents(limit=2, root_dir=tmp_path)) == 2


def test_list_incidents_survives_corrupt_entry(
    tmp_path: Path, status: dict
) -> None:
    """A directory with no incident.json must still appear in the listing."""
    record_incident("normal", status, root_dir=tmp_path)
    broken = tmp_path / "2020-01-01_00-00-00_corrupt_deadbeef"
    broken.mkdir()
    (broken / "incident.json").write_text("{not json")

    listing = list_incidents(root_dir=tmp_path)
    ids = [e["incident_id"] for e in listing]
    assert "2020-01-01_00-00-00_corrupt_deadbeef" in ids


# ---------------------------------------------------------------------------
# export_incident_for_sharing


def test_export_strips_user_paths_and_pii(
    tmp_path: Path, status: dict, fake_artifacts: dict
) -> None:
    path = record_incident(
        "nozzle_crash_suspected",
        status,
        printer_id="shop_02",
        user_description="Adam's disc crashed at /Users/adamarreola/project/x.stl",
        tool_call_trace=[{"tool": "start_print", "args": {"ip": "10.0.1.42"}}],
        tags=["bambu_a1"],
        root_dir=tmp_path,
        **fake_artifacts,
    )
    incident_id = Path(path).name

    shared = export_incident_for_sharing(
        incident_id, strip_user_paths=True, root_dir=tmp_path
    )

    # Fields that are inherently user-identifying are removed wholesale.
    assert "user_description" not in shared
    assert "tool_call_trace" not in shared

    # The raw record had an IP and a serial inside printer_status — those
    # should be scrubbed in the shared copy.
    assert shared["printer_status"]["printer_model"] == "bambu_a1"
    assert shared["printer_status"]["ip"] == "<IP>"
    assert shared["printer_status"]["serial"] == "<SERIAL>"

    # Artifact original_path (under /Users/...) is dropped entirely when
    # strip_user_paths=True; copied_as (a bare filename) is kept.
    for entry in shared["artifacts"].values():
        assert "original_path" not in entry
        assert "copied_as" in entry

    # No key whose value is a path should leak the home directory.
    serialized = json.dumps(shared)
    assert "/Users/adamarreola" not in serialized
    assert "10.0.1.42" not in serialized
    assert "0309CA123456789AB" not in serialized

    # gcode_sample is included (first few header lines) and scrubbed.
    assert "gcode_sample" in shared
    assert "bambu_a1" in shared["gcode_sample"]

    assert shared["anonymized"] is True


def test_export_without_strip_user_paths_still_scrubs_pii(
    tmp_path: Path, status: dict, fake_artifacts: dict
) -> None:
    """strip_user_paths=False keeps filesystem paths (scrubbed) but still
    removes IPs and serials from serialized values."""
    path = record_incident(
        "nozzle_crash_suspected",
        status,
        root_dir=tmp_path,
        **fake_artifacts,
    )
    shared = export_incident_for_sharing(
        Path(path).name, strip_user_paths=False, root_dir=tmp_path
    )
    # original_path is retained but scrubbed.
    for entry in shared["artifacts"].values():
        assert "original_path" in entry
        assert "/Users/adamarreola" not in entry["original_path"]
    # IP/serial still redacted.
    assert shared["printer_status"]["ip"] == "<IP>"
    assert shared["printer_status"]["serial"] == "<SERIAL>"


def test_export_raises_for_unknown_incident(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        export_incident_for_sharing("does_not_exist", root_dir=tmp_path)
