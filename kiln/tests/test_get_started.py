"""Tests for the get_started() onboarding MCP tool.

Covers:
- Return structure contains all expected keys
- safety_tools list includes safety_status
- session_recovery section exists with correct fields
- tip references safety_status
"""

from __future__ import annotations

from unittest.mock import patch

from kiln.server import get_started

# get_started() adds a serve_process_pileup key only when leftover serve
# processes cross the warning threshold on the running machine; pin the
# healthy report so the key-contract assertion is machine-independent.
_HEALTHY_SIBLINGS = {"count": 1, "pids": [111], "oldest_age": "05:44", "warning": None}


class TestGetStarted:
    """Tests for the get_started() MCP tool."""

    def test_returns_success(self):
        result = get_started()
        assert result["success"] is True

    def test_has_required_keys(self):
        """Every documented section is present.  Extra ones are welcome.

        Presence, not exact equality, because the two failure modes are
        not symmetric.  A section REMOVED is a real regression: agents
        lose a documented surface and have no other way to rediscover
        it.  A section ADDED is almost always somebody teaching agents
        something new.  A subset check catches the first; equality
        caught the first AND failed every instance of the second.

        Which is how it actually behaved: adding ``session_maintenance``
        turned all four CI legs red for a correct change.  A tripwire
        that fires on every legitimate edit gets synced mechanically,
        without being read, and an assertion nobody reads has stopped
        defending anything.  Noticing NEW sections is review's job, not
        a red build's.
        """
        with patch(
            "kiln.serve_siblings.check_serve_siblings",
            return_value=_HEALTHY_SIBLINGS,
        ):
            result = get_started()
        expected_keys = {
            "success",
            "update",
            "account",
            "overview",
            "tool_discovery",
            "quick_start",
            "core_workflows",
            "when_kiln_gets_it_wrong",
            "inline_3d_stage",
            "creating_models",
            "safety_tools",
            "session_recovery",
            "session_maintenance",
            "tip",
            "openscad",
        }
        missing = expected_keys - set(result.keys())
        assert not missing, (
            f"get_started() no longer documents {sorted(missing)} — an agent "
            "that needed those sections has no other way to find them."
        )

    def test_openscad_key_present(self):
        """openscad section is always present with at least an 'installed' or 'version' field."""
        result = get_started()
        openscad = result["openscad"]
        assert isinstance(openscad, dict)
        # Must have at least one diagnostic key — not an empty dict.
        assert len(openscad) > 0

    def test_teaches_the_inline_3d_stage(self):
        """The stage is invisible in tool schemas (it rides _meta and the
        result hook), so the mandated first call is where an agent learns it
        exists at all.  A real session searched the tool surface for an
        'interactive viewer', found only PNG tools, and told the user Kiln
        has no 3D stage — onboarding must make that conclusion impossible."""
        result = get_started()
        stage = result["inline_3d_stage"]
        blob = " ".join(stage.values())
        assert "interactive" in blob and "3D stage" in blob
        # The door for "show me this mesh file", by name.
        assert "import_external_mesh" in stage["show_me_this_file"]
        # Nothing in a docstring said heavy meshes are handled — the same
        # session rejected the stage on mesh size when the payload path
        # decimates automatically.
        assert "decimated" in stage["show_me_this_file"]
        # And it must not be findable only by luck: the searchable marker
        # the stamped tool descriptions carry is named here.
        assert "INLINE 3D STAGE" in stage["how_to_find_it"]

    def test_show_a_mesh_file_workflow_names_the_stage_door(self):
        wf = get_started()["core_workflows"]["show_a_mesh_file_in_3d"]
        assert "import_external_mesh" in wf

    def test_safety_tools_includes_safety_status(self):
        result = get_started()
        safety_tools = result["safety_tools"]
        status_entries = [t for t in safety_tools if t.startswith("safety_status")]
        assert len(status_entries) == 1
        assert "comprehensive safety dashboard" in status_entries[0]

    def test_safety_tools_still_includes_safety_settings(self):
        result = get_started()
        safety_tools = result["safety_tools"]
        settings_entries = [t for t in safety_tools if t.startswith("safety_settings")]
        assert len(settings_entries) == 1

    def test_safety_tools_order(self):
        """safety_status should appear before safety_settings."""
        result = get_started()
        tools = result["safety_tools"]
        status_idx = next(i for i, t in enumerate(tools) if "safety_status" in t)
        settings_idx = next(i for i, t in enumerate(tools) if "safety_settings" in t)
        assert status_idx < settings_idx

    def test_session_recovery_structure(self):
        result = get_started()
        sr = result["session_recovery"]
        assert "description" in sr
        assert sr["tool"] == "get_agent_context"
        assert "usage" in sr
        assert "get_agent_context" in sr["usage"]

    def test_tip_mentions_safety_status(self):
        result = get_started()
        assert "safety_status" in result["tip"]

    def test_tip_mentions_safety_settings(self):
        result = get_started()
        assert "safety_settings" in result["tip"]
