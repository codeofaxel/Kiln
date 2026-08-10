"""A model's print history belongs to the SHAPE, not to one export of it.

``get_model_history`` / ``get_success_rate`` identified a model by file
hash alone.  A file hash names bytes: re-export an unchanged part from CAD
and it changes (exporters stamp a header and may re-weld vertices), so the
part's own history read as empty — a model nobody had ever printed.

These tests pin the fix and its limits:

- a re-export finds the history the original recorded;
- a genuinely different design does NOT, even when it shares the older v1
  signature (the over-merge the v2 signature exists to catch);
- rows recorded before signatures were stored still join through the file
  hash, so nothing already on disk is orphaned;
- the answer says which identity it used, so a thin answer is visibly thin.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kiln.persistence import KilnDB
from kiln.print_dna import (
    ModelFingerprint,
    design_match_sql,
    get_model_history,
    get_success_rate,
    record_print_dna,
)


@pytest.fixture()
def db(tmp_path: Path) -> KilnDB:
    instance = KilnDB(db_path=str(tmp_path / "test.db"))
    yield instance
    instance.close()


@pytest.fixture(autouse=True)
def _patch_db(db: KilnDB) -> None:
    with patch("kiln.persistence.get_db", return_value=db):
        yield


def _fp(**overrides: Any) -> ModelFingerprint:
    defaults = {
        "file_hash": "hash-original",
        "triangle_count": 100,
        "vertex_count": 50,
        "bounding_box": {},
        "surface_area_mm2": 500.0,
        "volume_mm3": 250.0,
        "overhang_ratio": 0.1,
        "complexity_score": 0.3,
        "geometric_signature": "v1sig",
        "geometric_signature_v2": "v2:aaaa",
    }
    defaults.update(overrides)
    return ModelFingerprint(**defaults)


def _record(fp: ModelFingerprint, outcome: str = "success") -> None:
    record_print_dna(fp, "bambu_a1", "PLA", {"layer_height": 0.2}, outcome)


class TestHistoryFollowsTheShape:
    def test_reexport_finds_the_original_history(self) -> None:
        """The bug this fixes: same part, exported again, new bytes."""
        _record(_fp(file_hash="hash-original"))
        _record(_fp(file_hash="hash-original"), outcome="failed")

        # The re-export: different file hash, identical geometry.
        history = get_model_history(
            "hash-reexported",
            geometric_signature="v1sig",
            geometric_signature_v2="v2:aaaa",
        )
        assert len(history) == 2

        rate = get_success_rate(
            "hash-reexported",
            geometric_signature="v1sig",
            geometric_signature_v2="v2:aaaa",
        )
        assert rate["total_prints"] == 2
        assert rate["success_rate"] == 0.5
        assert rate["identified_by"] == "shape"

    def test_file_hash_alone_still_misses_a_reexport(self) -> None:
        """The honest limit: without the shape, the old answer is the answer.
        This is why the tool asks for model_path."""
        _record(_fp(file_hash="hash-original"))
        assert get_model_history("hash-reexported") == []
        rate = get_success_rate("hash-reexported")
        assert rate["total_prints"] == 0
        assert rate["identified_by"] == "file"

    def test_a_different_design_is_not_this_design(self) -> None:
        """Sharing the older v1 signature must not merge two designs — the
        over-merge v2 exists to catch."""
        _record(_fp(file_hash="sibling", geometric_signature="v1sig",
                    geometric_signature_v2="v2:bbbb"))
        history = get_model_history(
            "mine", geometric_signature="v1sig", geometric_signature_v2="v2:aaaa"
        )
        assert history == []

    def test_pre_signature_rows_still_join_by_file(self) -> None:
        """A row recorded before signatures were stored is reachable through
        the file hash — the fix must not orphan existing history."""
        _record(_fp(file_hash="hash-original", geometric_signature="",
                    geometric_signature_v2=""))
        history = get_model_history(
            "hash-original",
            geometric_signature="v1sig",
            geometric_signature_v2="v2:aaaa",
        )
        assert len(history) == 1

    def test_legacy_and_reexport_rows_combine(self) -> None:
        """Both halves of a real history: one row from before v2, one after."""
        _record(_fp(file_hash="hash-original", geometric_signature="v1sig",
                    geometric_signature_v2=""))
        _record(_fp(file_hash="hash-v2era"))
        rate = get_success_rate(
            "hash-original",
            geometric_signature="v1sig",
            geometric_signature_v2="v2:aaaa",
        )
        assert rate["total_prints"] == 2

    def test_no_identity_matches_nothing_not_everything(self) -> None:
        """An identity-less query must not return the whole table."""
        _record(_fp())
        sql, params, identified_by = design_match_sql("", "", "")
        assert sql == "1 = 0" and params == [] and identified_by == "none"
        assert get_model_history("") == []

    def test_v1_only_is_reported_as_less_precise(self) -> None:
        _, _, identified_by = design_match_sql("h", "v1sig", "")
        assert identified_by == "shape_v1_only"


class TestToolSurface:
    """The tool is what an agent actually calls."""

    @staticmethod
    def _tool():
        from kiln.plugins.intelligence_tools import _IntelligenceToolsPlugin

        captured: dict[str, Any] = {}

        class FakeMCP:
            def tool(self, *a: Any, **k: Any):
                def deco(fn):
                    captured[fn.__name__] = fn
                    return fn
                return deco

        _IntelligenceToolsPlugin().register(FakeMCP())
        return captured

    def test_model_path_identifies_the_design(self, tmp_path: Path) -> None:
        """The headline: hand the tool the file and it finds the history the
        original export recorded, without the caller knowing about hashes."""
        tools = self._tool()
        history_tool = tools["get_model_print_history"]

        _record(_fp(file_hash="hash-original"))

        stl = tmp_path / "part.stl"
        stl.write_bytes(b"not really an stl")

        with patch("kiln.print_dna.fingerprint_model", return_value=_fp(
            file_hash="hash-reexported"
        )):
            result = history_tool(model_path=str(stl))

        assert result["success"] is True
        assert result["total_prints"] == 1
        assert result["identified_by"] == "shape"
        assert "note" not in result

    def test_hash_only_answer_says_it_may_be_incomplete(self) -> None:
        tools = self._tool()
        _record(_fp(file_hash="hash-original"))
        result = tools["get_model_print_history"](file_hash="hash-original")
        assert result["identified_by"] == "file"
        assert "note" in result

    def test_no_identity_is_a_validation_error(self) -> None:
        tools = self._tool()
        result = tools["get_model_print_history"]()
        assert result.get("success") is not True

    def test_unparseable_model_path_falls_back_to_supplied_identity(
        self, tmp_path: Path
    ) -> None:
        """A file we cannot fingerprint must not lose the caller's own
        identity — it degrades to the hash answer, never to an error."""
        tools = self._tool()
        _record(_fp(file_hash="hash-original"))
        bad = tmp_path / "bad.stl"
        bad.write_bytes(b"\x00\x01")
        result = tools["get_model_print_history"](
            file_hash="hash-original", model_path=str(bad)
        )
        assert result["success"] is True
        assert result["total_prints"] == 1


class TestCADFileAtTheHistoryDoor:
    """A CAD file gets told what to do, not sent in a circle.

    ``model_path`` turned this tool into a door a customer can hand a file
    to, and CAD is the format engineering customers send.  A STEP cannot be
    fingerprinted (the signatures count triangles, which a tessellation
    produces rather than the part having them), so it must refuse — but the
    refusal used to read "pass model_path" to a caller who had just passed
    it, and pointed at ``fingerprint_model``, which dies on the same file.
    """

    @staticmethod
    def _step(tmp_path: Path, name: str = "bracket.step") -> Path:
        path = tmp_path / name
        path.write_text(
            "ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
        )
        return path

    def test_engine_refuses_cad_and_names_the_conversion(self, tmp_path: Path) -> None:
        from kiln.print_dna import fingerprint_model

        with pytest.raises(ValueError, match="import_step_file"):
            fingerprint_model(str(self._step(tmp_path)))

    @pytest.mark.parametrize("name", ["bracket.step", "bracket.stp"])
    def test_history_door_names_the_conversion(self, tmp_path: Path, name: str) -> None:
        tools = TestToolSurface._tool()
        step = self._step(tmp_path, name)

        result = tools["get_model_print_history"](model_path=str(step))

        assert result["success"] is False
        assert result["error"]["code"] == "UNREADABLE_INPUT"
        message = result["error"]["message"]
        assert "import_step_file" in message
        # The old runaround: telling a caller to supply what they supplied.
        assert "pass model_path" not in message

    def test_fingerprint_door_names_the_conversion(self, tmp_path: Path) -> None:
        tools = TestToolSurface._tool()

        result = tools["fingerprint_model"](file_path=str(self._step(tmp_path)))

        assert result["success"] is False
        assert "import_step_file" in result["error"]["message"]

    def test_the_named_remedy_is_a_real_tool(self) -> None:
        """The bug was naming a dead end.  Whatever we point at must exist."""
        captured: dict[str, Any] = {}

        class FakeMCP:
            def tool(self, *_a: Any, **kw: Any):
                def deco(fn):
                    captured[kw.get("name", fn.__name__)] = fn
                    return fn

                return deco

        from kiln.plugins.step_tools import plugin

        plugin.register(FakeMCP())
        assert "import_step_file" in captured

    def test_cad_does_not_cost_the_caller_their_own_identity(
        self, tmp_path: Path
    ) -> None:
        """The fall-through is deliberate: a file we cannot read must not
        erase a hash the caller already supplied."""
        tools = TestToolSurface._tool()
        _record(_fp(file_hash="hash-original"))

        result = tools["get_model_print_history"](
            file_hash="hash-original", model_path=str(self._step(tmp_path))
        )

        assert result["success"] is True
        assert result["total_prints"] == 1
