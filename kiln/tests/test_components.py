"""Tests for kiln.components -- component catalog, matching, library paths."""

from __future__ import annotations

import pytest

from kiln.components import (
    Component,
    ComponentMatch,
    _reset_catalog,
    get_available_libraries,
    get_component,
    get_library_path,
    list_components,
    match_components,
)


@pytest.fixture(autouse=True)
def reset():
    _reset_catalog()
    yield
    _reset_catalog()


# ---------------------------------------------------------------------------
# TestComponentCatalogLoading
# ---------------------------------------------------------------------------


class TestComponentCatalogLoading:
    """Verify the component catalog loads and parses correctly."""

    def test_catalog_loads_successfully(self) -> None:
        from kiln.components import _get_catalog

        catalog = _get_catalog()
        assert isinstance(catalog, dict)
        assert len(catalog) > 0

    def test_catalog_has_expected_components(self) -> None:
        from kiln.components import _get_catalog

        catalog = _get_catalog()
        assert "spur_gear" in catalog
        assert "threaded_rod" in catalog
        assert "knuckle_hinge" in catalog

    def test_component_has_required_fields(self) -> None:
        comp = get_component("spur_gear")
        assert comp is not None
        assert comp.component_id == "spur_gear"
        assert isinstance(comp.display_name, str)
        assert isinstance(comp.library, str)
        assert isinstance(comp.import_line, str)
        assert isinstance(comp.example_call, str)
        assert isinstance(comp.key_params, dict)
        assert isinstance(comp.agent_guidance, str)
        assert isinstance(comp.printability_notes, str)
        assert isinstance(comp.category, str)
        assert isinstance(comp.user_intents, list)

    def test_meta_key_is_skipped(self) -> None:
        from kiln.components import _get_catalog

        catalog = _get_catalog()
        assert "_meta" not in catalog

    def test_catalog_singleton_returns_same_instance(self) -> None:
        from kiln.components import _get_catalog

        first = _get_catalog()
        second = _get_catalog()
        assert first is second


# ---------------------------------------------------------------------------
# TestGetComponent
# ---------------------------------------------------------------------------


class TestGetComponent:
    """get_component looks up by ID."""

    def test_get_existing_component(self) -> None:
        comp = get_component("spur_gear")
        assert comp is not None
        assert isinstance(comp, Component)

    def test_get_nonexistent_component(self) -> None:
        assert get_component("nonexistent") is None

    def test_component_has_correct_library(self) -> None:
        comp = get_component("spur_gear")
        assert comp is not None
        assert comp.library == "BOSL2"


# ---------------------------------------------------------------------------
# TestListComponents
# ---------------------------------------------------------------------------


class TestListComponents:
    """list_components returns filtered, sorted results."""

    def test_list_all_components(self) -> None:
        comps = list_components()
        assert len(comps) > 0

    def test_list_by_category_mechanical(self) -> None:
        comps = list_components(category="mechanical")
        assert len(comps) > 0
        assert all(c.category == "mechanical" for c in comps)

    def test_list_by_category_fasteners(self) -> None:
        comps = list_components(category="fasteners")
        assert len(comps) > 0
        assert all(c.category == "fasteners" for c in comps)

    def test_list_by_nonexistent_category(self) -> None:
        comps = list_components(category="nonexistent_category_xyz")
        assert comps == []

    def test_list_sorted_by_display_name(self) -> None:
        comps = list_components()
        names = [c.display_name for c in comps]
        assert names == sorted(names)


# ---------------------------------------------------------------------------
# TestMatchComponents
# ---------------------------------------------------------------------------


class TestMatchComponents:
    """match_components finds components by natural-language description."""

    def test_match_gear(self) -> None:
        results = match_components("I want a gear")
        ids = [m.component.component_id for m in results]
        assert "spur_gear" in ids

    def test_match_screw(self) -> None:
        results = match_components("need a screw hole")
        ids = [m.component.component_id for m in results]
        assert "screw_hole" in ids

    def test_match_hinge(self) -> None:
        results = match_components("box with a hinge")
        ids = [m.component.component_id for m in results]
        assert "knuckle_hinge" in ids

    def test_match_bearing(self) -> None:
        results = match_components("linear bearing housing")
        ids = [m.component.component_id for m in results]
        assert "linear_bearing" in ids

    def test_match_multiple(self) -> None:
        results = match_components("gear with screw mounting")
        ids = [m.component.component_id for m in results]
        # Should match at least one gear and one screw-related component
        has_gear = any("gear" in cid for cid in ids)
        has_screw = any("screw" in cid for cid in ids)
        assert has_gear
        assert has_screw

    def test_match_no_results(self) -> None:
        results = match_components("a simple cube")
        assert results == []

    def test_match_case_insensitive(self) -> None:
        results = match_components("GEAR")
        ids = [m.component.component_id for m in results]
        assert "spur_gear" in ids

    def test_match_plural(self) -> None:
        results = match_components("gears")
        ids = [m.component.component_id for m in results]
        # "gears" should match components with "gear" intent
        has_gear = any("gear" in cid for cid in ids)
        assert has_gear

    def test_matches_sorted_by_score(self) -> None:
        results = match_components("spur gear with threads")
        if len(results) >= 2:
            scores = [m.score for m in results]
            assert scores == sorted(scores, reverse=True)

    def test_match_returns_matched_intents(self) -> None:
        results = match_components("I need a hinge")
        hinge_matches = [m for m in results if m.component.component_id == "knuckle_hinge"]
        assert len(hinge_matches) > 0
        assert len(hinge_matches[0].matched_intents) > 0


# ---------------------------------------------------------------------------
# TestGetLibraryPath
# ---------------------------------------------------------------------------


class TestGetLibraryPath:
    """get_library_path resolves bundled library directories."""

    def test_bosl2_path_exists(self) -> None:
        path = get_library_path("BOSL2")
        assert "BOSL2" in path

    def test_unknown_library_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown library"):
            get_library_path("unknown")


# ---------------------------------------------------------------------------
# TestGetAvailableLibraries
# ---------------------------------------------------------------------------


class TestGetAvailableLibraries:
    """get_available_libraries returns metadata for bundled libraries."""

    def test_returns_list(self) -> None:
        libs = get_available_libraries()
        assert isinstance(libs, list)
        assert len(libs) > 0

    def test_libraries_have_expected_keys(self) -> None:
        libs = get_available_libraries()
        for lib in libs:
            assert "name" in lib
            assert "license" in lib
            assert "path" in lib
            assert "available" in lib


# ---------------------------------------------------------------------------
# TestComponentToDict
# ---------------------------------------------------------------------------


class TestComponentToDict:
    """Component and ComponentMatch serialisation."""

    def test_component_to_dict(self) -> None:
        comp = get_component("spur_gear")
        assert comp is not None
        d = comp.to_dict()
        assert isinstance(d, dict)
        assert d["component_id"] == "spur_gear"
        assert "display_name" in d
        assert "library" in d
        assert "import_line" in d
        assert "example_call" in d
        assert "key_params" in d
        assert "agent_guidance" in d
        assert "printability_notes" in d
        assert "category" in d
        assert "user_intents" in d

    def test_component_match_to_dict(self) -> None:
        comp = get_component("spur_gear")
        assert comp is not None
        match = ComponentMatch(
            component=comp,
            score=2.0,
            matched_intents=["gear", "cog"],
        )
        d = match.to_dict()
        assert isinstance(d, dict)
        assert "component" in d
        assert d["score"] == 2.0
        assert d["matched_intents"] == ["gear", "cog"]
        assert d["component"]["component_id"] == "spur_gear"
