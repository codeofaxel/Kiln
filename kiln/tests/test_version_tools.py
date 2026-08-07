"""Tests for kiln.plugins.version_tools — design version control tools."""

from __future__ import annotations

import pytest

from kiln.plugins.version_tools import _design_dir, _ensure_design_dir, _parse_version_ref

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MockMcp:
    """Minimal MCP stub that captures registered tools by name."""

    def __init__(self) -> None:
        self._tools: dict = {}

    def tool(self):
        def decorator(fn):
            self._tools[fn.__name__] = fn
            return fn

        return decorator

    def __getitem__(self, name: str):
        return self._tools[name]


def _make_mcp_with_tools():
    """Instantiate the plugin and return a mock MCP with all tools registered."""
    from kiln.plugins.version_tools import _VersionToolsPlugin

    mcp = _MockMcp()
    _VersionToolsPlugin().register(mcp)
    return mcp


# ---------------------------------------------------------------------------
# TestParseVersionRef
# ---------------------------------------------------------------------------


class TestParseVersionRef:
    """_parse_version_ref: parses 'design_id:N' and plain integer refs."""

    def test_design_id_colon_version(self):
        result = _parse_version_ref("my-coaster:3")
        assert result == ("my-coaster", 3)

    def test_plain_int_with_default(self):
        result = _parse_version_ref("5", default_design_id="foo")
        assert result == ("foo", 5)

    def test_plain_int_no_default_raises(self):
        with pytest.raises(ValueError, match="design_id:N"):
            _parse_version_ref("5")

    def test_invalid_version_raises(self):
        with pytest.raises(ValueError, match="Invalid version number"):
            _parse_version_ref("foo:abc")

    def test_whitespace_stripped(self):
        result = _parse_version_ref(" foo : 3 ")
        assert result == ("foo", 3)


# ---------------------------------------------------------------------------
# TestDesignDir
# ---------------------------------------------------------------------------


class TestDesignDir:
    """_design_dir and _ensure_design_dir: path computation and creation."""

    def test_design_dir_path(self, monkeypatch, tmp_path):
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        result = _design_dir("my-coaster")
        # _design_dir uses os.path.expanduser on _DESIGNS_ROOT/design_id
        # since tmp_path has no ~, the path should end with my-coaster
        assert result.endswith("my-coaster")
        assert str(tmp_path) in result

    def test_ensure_creates_directory(self, monkeypatch, tmp_path):
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        path = _ensure_design_dir("new-design")
        import os

        assert os.path.isdir(path)
        assert path.endswith("new-design")


# ---------------------------------------------------------------------------
# TestSaveDesignVersion
# ---------------------------------------------------------------------------


class TestSaveDesignVersion:
    """save_design_version MCP tool: create, increment, pro fallback."""

    def test_first_version_creates_recipe(self, monkeypatch, tmp_path):
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        mcp = _make_mcp_with_tools()
        result = mcp["save_design_version"](
            design_id="test-coaster",
            scad_source="cube([80, 80, 3]);",
            prompt="flat coaster",
        )
        assert result["ok"] is True
        ver = result["version"]
        assert ver["version"] == 1
        assert ver["design_id"] == "test-coaster"
        assert ver["diff_from_prev"] is None
        assert ver["parent_version"] is None

    def test_second_version_increments(self, monkeypatch, tmp_path):
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        mcp = _make_mcp_with_tools()
        mcp["save_design_version"](
            design_id="test-coaster",
            scad_source="cube([80, 80, 3]);",
        )
        result = mcp["save_design_version"](
            design_id="test-coaster",
            scad_source="cube([90, 90, 3]);",
            notes="bigger coaster",
        )
        assert result["ok"] is True
        ver = result["version"]
        assert ver["version"] == 2
        assert ver["parent_version"] is not None
        # diff should capture the source change
        assert ver["diff_from_prev"] is not None
        assert "80" in ver["diff_from_prev"] or "90" in ver["diff_from_prev"]

    def test_pro_enrichment_skipped_without_kiln_pro(self, monkeypatch, tmp_path):
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        mcp = _make_mcp_with_tools()
        # Should not raise even though kiln_pro is absent; result is still ok
        result = mcp["save_design_version"](
            design_id="pro-test",
            scad_source="sphere(r=10);",
            stl_path="/nonexistent/output.stl",
            provenance={"tools_used": ["openscad"]},
        )
        assert result["ok"] is True
        # No crash from missing kiln_pro
        assert "version" in result

    def test_save_works_when_kiln_pro_hook_unimportable(self, monkeypatch, tmp_path):
        """FREE-TIER REGRESSION: with the kiln-pro bridge unimportable
        (the exact shape of a free install where kiln-pro is absent),
        ``save_design_version`` must still succeed recipe-only — no crash,
        no branch block, identical behaviour to before the hook existed.

        We force the ImportError deterministically even though kiln-pro
        is installed in this dev env, so the regression is proven, not
        merely assumed-absent.
        """
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))

        import builtins

        real_import = builtins.__import__

        def _blocked_import(name, *args, **kwargs):
            if name == "kiln_pro.bridge" or name.startswith("kiln_pro.bridge"):
                raise ImportError("simulated free tier: kiln_pro absent")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _blocked_import)

        mcp = _make_mcp_with_tools()
        result = mcp["save_design_version"](
            design_id="free-tier-design",
            scad_source="cube([10, 10, 10]);",
            prompt="a cube",
            notes="free save",
        )

        # Recipe path still works exactly as before.
        assert result["ok"] is True
        assert result["version"]["version"] == 1
        assert result["version"]["design_id"] == "free-tier-design"
        # No Pro-only branch block leaked into the free response.
        assert "branch" not in result["version"]
        # The recipe sidecar was written to disk (the free-tier
        # source of truth), proving the save genuinely completed.
        recipe_path = result["version"]["path"]
        assert recipe_path and recipe_path.endswith(".json")

    def test_brief_id_persists_on_first_version(self, monkeypatch, tmp_path):
        """A4: caller-supplied brief_id lands on the recipe + in the response."""
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        mcp = _make_mcp_with_tools()
        result = mcp["save_design_version"](
            design_id="goal-coaster",
            scad_source="cube([80, 80, 3]);",
            brief_id="abc123",
            intent_hash="ih-deadbeef",
        )
        assert result["ok"] is True
        ver = result["version"]
        assert ver["brief_id"] == "abc123"
        assert ver["intent_hash"] == "ih-deadbeef"

    def test_brief_id_inherited_from_parent_when_omitted(self, monkeypatch, tmp_path):
        """A4: an iteration of a brief-attached design inherits the brief."""
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        mcp = _make_mcp_with_tools()
        # First version attaches a brief
        mcp["save_design_version"](
            design_id="iter-coaster",
            scad_source="cube([80, 80, 3]);",
            brief_id="parent-brief-id",
            intent_hash="parent-intent",
        )
        # Second version omits brief_id — must inherit from parent
        result = mcp["save_design_version"](
            design_id="iter-coaster",
            scad_source="cube([90, 90, 3]);",
        )
        assert result["ok"] is True
        assert result["version"]["brief_id"] == "parent-brief-id"
        assert result["version"]["intent_hash"] == "parent-intent"

    def test_explicit_brief_id_overrides_inheritance(self, monkeypatch, tmp_path):
        """A4: caller-supplied brief_id overrides the parent's brief on iterate."""
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        mcp = _make_mcp_with_tools()
        mcp["save_design_version"](
            design_id="reassign-coaster",
            scad_source="cube([80, 80, 3]);",
            brief_id="original-brief",
        )
        result = mcp["save_design_version"](
            design_id="reassign-coaster",
            scad_source="cube([90, 90, 3]);",
            brief_id="new-brief",
        )
        assert result["version"]["brief_id"] == "new-brief"

    def test_no_brief_means_no_brief(self, monkeypatch, tmp_path):
        """Default (no brief): recipe field stays None, doesn't fabricate."""
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        mcp = _make_mcp_with_tools()
        result = mcp["save_design_version"](
            design_id="no-goal-coaster",
            scad_source="cube([50, 50, 3]);",
        )
        assert result["version"]["brief_id"] is None
        assert result["version"]["intent_hash"] is None


# ---------------------------------------------------------------------------
# TestListDesignVersions
# ---------------------------------------------------------------------------


class TestListDesignVersions:
    """list_design_versions MCP tool: history listing, newest first."""

    def test_list_returns_versions_newest_first(self, monkeypatch, tmp_path):
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        mcp = _make_mcp_with_tools()
        for i in range(1, 4):
            mcp["save_design_version"](
                design_id="coaster",
                scad_source=f"cube([{i * 10}, {i * 10}, 3]);",
            )
        result = mcp["list_design_versions"](design_id="coaster")
        assert result["ok"] is True
        assert result["count"] == 3
        versions = result["versions"]
        # Newest first means version numbers descend
        version_nums = [v["version"] for v in versions]
        assert version_nums == sorted(version_nums, reverse=True)

    def test_list_empty_design(self, monkeypatch, tmp_path):
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        mcp = _make_mcp_with_tools()
        result = mcp["list_design_versions"](design_id="ghost-design")
        assert result["ok"] is True
        assert result["count"] == 0
        assert result["versions"] == []


# ---------------------------------------------------------------------------
# TestDiffDesignVersions
# ---------------------------------------------------------------------------


class TestDiffDesignVersions:
    """diff_design_versions MCP tool: unified diff between two versions."""

    def test_diff_shows_scad_changes(self, monkeypatch, tmp_path):
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        mcp = _make_mcp_with_tools()
        mcp["save_design_version"](
            design_id="diff-coaster",
            scad_source="cube([80, 80, 3]);",
        )
        mcp["save_design_version"](
            design_id="diff-coaster",
            scad_source="cylinder(r=40, h=3);",
        )
        result = mcp["diff_design_versions"](
            version_id_a="diff-coaster:1",
            version_id_b="diff-coaster:2",
        )
        assert result["ok"] is True
        diff = result["diff"]
        assert "cube" in diff or "cylinder" in diff


# ---------------------------------------------------------------------------
# TestRollbackDesignVersion
# ---------------------------------------------------------------------------


class TestRollbackDesignVersion:
    """rollback_design_version MCP tool: creates new version from old source."""

    def test_rollback_creates_new_version_with_old_source(self, monkeypatch, tmp_path):
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        mcp = _make_mcp_with_tools()
        # v1 with source A
        mcp["save_design_version"](
            design_id="rb-coaster",
            scad_source="cube([80, 80, 3]); // v1",
        )
        # v2 with source B
        mcp["save_design_version"](
            design_id="rb-coaster",
            scad_source="sphere(r=40); // v2",
        )
        # rollback to v1
        result = mcp["rollback_design_version"](
            design_id="rb-coaster",
            to_version_id="1",
        )
        assert result["ok"] is True
        ver = result["version"]
        # A new version should be created (v3)
        assert ver["version"] == 3
        assert ver["restored_from_version"] == 1
        # Verify the rolled-back recipe has v1's source
        get_result = mcp["get_design_version"](version_id="rb-coaster:3")
        assert get_result["ok"] is True
        assert "v1" in (get_result["version"]["source_scad"] or "")


# ---------------------------------------------------------------------------
# TestGetDesignVersion
# ---------------------------------------------------------------------------


class TestGetDesignVersion:
    """get_design_version MCP tool: retrieve by version_id."""

    def test_get_existing_version(self, monkeypatch, tmp_path):
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        mcp = _make_mcp_with_tools()
        mcp["save_design_version"](
            design_id="fetch-coaster",
            scad_source="cube([80, 80, 3]);",
            prompt="a flat coaster",
            notes="initial",
        )
        result = mcp["get_design_version"](version_id="fetch-coaster:1")
        assert result["ok"] is True
        ver = result["version"]
        assert ver["version"] == 1
        assert ver["source_scad"] == "cube([80, 80, 3]);"
        assert ver["prompt"] == "a flat coaster"
        assert ver["notes"] == "initial"

    def test_get_nonexistent_version_returns_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        mcp = _make_mcp_with_tools()
        result = mcp["get_design_version"](version_id="foo:999")
        assert result["ok"] is False
        assert "error" in result


# ---------------------------------------------------------------------------
# TestSearchDesignVersions
# ---------------------------------------------------------------------------


class TestSearchDesignVersions:
    """search_design_versions MCP tool: substring search across all designs."""

    def test_search_finds_by_prompt(self, monkeypatch, tmp_path):
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        mcp = _make_mcp_with_tools()
        mcp["save_design_version"](
            design_id="ash-coaster",
            scad_source="cube([80, 80, 3]);",
            prompt="ash cat portrait coaster",
        )
        mcp["save_design_version"](
            design_id="generic-coaster",
            scad_source="cube([80, 80, 3]);",
            prompt="plain simple coaster",
        )
        result = mcp["search_design_versions"](query="ash")
        assert result["ok"] is True
        assert result["count"] >= 1
        found_ids = [v["design_id"] for v in result["versions"]]
        assert "ash-coaster" in found_ids
        assert "generic-coaster" not in found_ids

    def test_search_no_results(self, monkeypatch, tmp_path):
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        mcp = _make_mcp_with_tools()
        mcp["save_design_version"](
            design_id="some-coaster",
            scad_source="cube([80, 80, 3]);",
            prompt="simple flat coaster",
        )
        result = mcp["search_design_versions"](query="zzzzz")
        assert result["ok"] is True
        assert result["count"] == 0
        assert result["versions"] == []


# ---------------------------------------------------------------------------
# design_id is caller-supplied, and ~/.kiln/designs is per-machine
# ---------------------------------------------------------------------------
#
# Two defects, found 2026-08-03, both in the same two lines.
#
# ``_design_dir`` built its path by f-string, so a design_id of "../../pwned"
# resolved OUTSIDE the library and ``_ensure_design_dir`` then os.makedirs()'d
# it — verified creating a real directory outside ~/.kiln before the fix.
#
# And nothing asked whether this machine is allowed to answer for a design
# library at all.  The hosted server runs ONE ~/.kiln for every customer with
# no persistent volume, so a save there collides with a stranger's and is
# discarded on the next deploy, while ``search_design_versions`` — which walks
# every directory under the root — hands one customer another's design names,
# prompts and notes.
#
# Both checks live on the shared resolvers rather than on tool arguments,
# because two of the six doors never receive a design_id directly: they parse
# it out of a "design_id:N" reference.


class TestDesignIdCannotEscapeTheRoot:
    """A caller-supplied name must not select a directory outside the root."""

    @pytest.mark.parametrize(
        "hostile",
        ["../../pwned", "../escaped", "..", ".", "a/b", "/etc", "", "   "],
    )
    def test_traversal_is_refused(self, monkeypatch, tmp_path, hostile):
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        with pytest.raises(ValueError):
            _design_dir(hostile)

    @pytest.mark.parametrize(
        "hostile", ["../../pwned", "..", "a/b", "/etc"]
    )
    def test_ensure_creates_nothing_outside_the_root(
        self, monkeypatch, tmp_path, hostile
    ):
        """The sharp end: this call is the one that os.makedirs()."""
        import os

        root = tmp_path / "designs"
        root.mkdir()
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(root))
        before = set(os.listdir(tmp_path))
        with pytest.raises(ValueError):
            _ensure_design_dir(hostile)
        assert set(os.listdir(tmp_path)) == before, (
            "a refused design_id still created something next to the root"
        )
        assert not list(root.iterdir()), "a refused design_id created a directory"

    def test_ordinary_ids_are_untouched(self, monkeypatch, tmp_path):
        """The check must cost a real user nothing.

        These are the shapes actually on disk in a real library — plain
        slugs with hyphens, digits and underscores.
        """
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        for good in ("kiln-coaster", "my-mug-v3", "monitor-stand-450", "ok1", "a_b"):
            assert _design_dir(good).endswith(good)
            assert _ensure_design_dir(good).endswith(good)


class TestDesignLibraryIsPerMachine:
    """The hosted server has no per-account library, so it must not pretend."""

    def test_every_resolver_refuses_on_hosted(self, monkeypatch, tmp_path):
        from kiln.plugins.version_tools import _designs_root

        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        for call in (
            lambda: _designs_root(),
            lambda: _design_dir("my-mug-v3"),
            lambda: _ensure_design_dir("my-mug-v3"),
        ):
            with pytest.raises(ValueError) as excinfo:
                call()
            assert "local Kiln install" in str(excinfo.value), (
                "the refusal must name where the tool DOES work, or it "
                "reads as Kiln being broken"
            )

    def test_local_install_is_unaffected(self, monkeypatch, tmp_path):
        """The operator IS the caller locally; this must cost them nothing."""
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
        assert _ensure_design_dir("my-mug-v3").endswith("my-mug-v3")

    def test_the_refusal_is_typed_and_word_for_word(self, monkeypatch, tmp_path):
        """The refusal carries a TYPE, and the sentence itself is pinned.

        The type is what lets each tool catch the refusal explicitly before
        its generic handler — without it, "the refusal reaches the caller"
        depends on every broad handler happening to return ``str(exc)``
        verbatim, which nothing enforced.  It stays a ``ValueError``
        subclass so the tools' existing envelope handling is unchanged.

        The sentence is asserted byte-for-byte because it is user-facing
        copy that names where the tool DOES work; a reword that drops that
        half turns a helpful refusal into "Kiln is broken."
        """
        from kiln.errors import HostedUnavailableError
        from kiln.plugins.version_tools import _designs_root

        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        with pytest.raises(HostedUnavailableError) as excinfo:
            _designs_root()
        assert isinstance(excinfo.value, ValueError)
        assert str(excinfo.value) == (
            "Your design library is not available on the hosted Kiln API: "
            "it lives on the machine that made the designs, and this server "
            "keeps no per-account copy of it. Run this from your local Kiln "
            "install or the CLI, where your files are."
        )

    def test_search_does_not_read_a_shared_library(self, monkeypatch, tmp_path):
        """The read side, which is worse than the write side.

        ``search_design_versions`` walks every directory under the root, so
        on a shared box it is the door that returns other customers' design
        names, prompts and notes.  It resolves the root itself, which is why
        it needed the shared resolver rather than a second expanduser.
        """
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        (tmp_path / "someone-elses-design").mkdir()
        mcp = _make_mcp_with_tools()

        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        result = mcp["search_design_versions"](query="a")
        assert result["ok"] is False, (
            "search returned a result set from a library shared with every "
            "other tenant"
        )
        assert "local Kiln install" in result["error"]

    def test_tools_return_an_envelope_not_a_stack_trace(self, monkeypatch, tmp_path):
        """The refusal is a ValueError so the tools' own handlers shape it.

        Every tool here already funnels exceptions into {"ok": False,
        "error": ...}, which is why the fix needed no per-tool branch — but
        that only holds while it stays true, so it is asserted rather than
        assumed.
        """
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        mcp = _make_mcp_with_tools()

        for name, kwargs in (
            ("save_design_version", {"design_id": "m", "scad_source": "cube(1);"}),
            ("list_design_versions", {"design_id": "m"}),
            ("rollback_design_version", {"design_id": "m", "to_version_id": "m:1"}),
            ("get_design_version", {"version_id": "m:1"}),
            ("search_design_versions", {"query": "a"}),
        ):
            result = mcp[name](**kwargs)
            assert isinstance(result, dict), f"{name} did not return a dict"
            assert result.get("ok") is False, f"{name} reported success"
            assert result.get("error"), f"{name} gave no reason"

    def test_a_traversal_id_also_comes_back_as_an_envelope(
        self, monkeypatch, tmp_path
    ):
        """Same contract for the other new refusal, on a local install."""
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
        mcp = _make_mcp_with_tools()
        result = mcp["save_design_version"](
            design_id="../../pwned", scad_source="cube(1);"
        )
        assert result.get("ok") is False
        assert "simple name" in result.get("error", "")
