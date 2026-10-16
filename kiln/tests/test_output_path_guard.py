"""A caller-named output location cannot overwrite a file outside Kiln's lane.

Replays the reported shape through the real registered tools: an agent
calls ``repair_mesh`` with ``output_path`` naming a config file, and the
config file must survive.  Every door that runs a tool ends at the tool's
registered function, so the tests call it there, through the MCP
``call_tool`` door, and through the web-to-printer bridge's caller.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import struct
from pathlib import Path

import pytest

from kiln import output_paths


def _write_cube_stl(path: Path) -> None:
    """A closed 12-triangle cube, written as binary STL."""
    v = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0),
         (0, 0, 10), (10, 0, 10), (10, 10, 10), (0, 10, 10)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    with open(path, "wb") as fh:
        fh.write(b"cube".ljust(80, b"\0"))
        fh.write(struct.pack("<I", len(faces)))
        for a, b, c in faces:
            fh.write(struct.pack("<3f", 0, 0, 0))
            for i in (a, b, c):
                fh.write(struct.pack("<3f", *v[i]))
            fh.write(b"\0\0")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A throwaway home folder holding a config file worth protecting."""
    h = tmp_path / "home"
    (h / ".config").mkdir(parents=True)
    (h / "Documents").mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv(output_paths.OUTPUT_DIR_ENV, raising=False)
    conf = h / ".config" / "settings.conf"
    conf.write_text("IMPORTANT_USER_CONFIG=1\n")
    stl = h / "Documents" / "cube.stl"
    _write_cube_stl(stl)
    return h


def _registered(name: str):
    from kiln import server

    tool = server.mcp._tool_manager._tools.get(name)
    assert tool is not None, f"{name} is not registered"
    return tool


def _refused(result) -> bool:
    return (
        isinstance(result, dict)
        and result.get("success") is False
        and result.get("error", {}).get("code") == output_paths.REFUSAL_CODE
    )


class TestTheReportedOverwrite:
    def test_repair_mesh_cannot_overwrite_a_config_file(self, home):
        conf = home / ".config" / "settings.conf"
        result = _registered("repair_mesh").fn(
            file_path=str(home / "Documents" / "cube.stl"),
            output_path=str(conf),
        )
        assert _refused(result), result
        assert conf.read_text() == "IMPORTANT_USER_CONFIG=1\n"

    def test_a_symlink_named_like_a_mesh_is_judged_by_its_target(self, home):
        target = home / ".zshrc"
        target.write_text("export PATH=/usr/bin\n")
        link = home / "Documents" / "innocent.stl"
        link.symlink_to(target)
        result = _registered("mirror_mesh_model").fn(
            file_path=str(home / "Documents" / "cube.stl"),
            output_path=str(link),
        )
        assert _refused(result), result
        assert target.read_text() == "export PATH=/usr/bin\n"

    def test_a_non_mesh_file_in_a_normal_folder_is_refused(self, home):
        notes = home / "Documents" / "notes.txt"
        notes.write_text("keep me\n")
        result = _registered("repair_mesh").fn(
            file_path=str(home / "Documents" / "cube.stl"),
            output_path=str(notes),
        )
        assert _refused(result), result
        assert notes.read_text() == "keep me\n"

    def test_merge_stl_does_not_create_folders_inside_a_hidden_folder(self, home):
        out = home / ".ssh" / "new" / "merged.stl"
        cube = str(home / "Documents" / "cube.stl")
        result = _registered("merge_stl").fn(
            file_paths=json.dumps([cube, cube]), output_path=str(out)
        )
        assert _refused(result), result
        assert not (home / ".ssh").exists()


class TestOrdinaryUseStillWorks:
    def test_repair_writes_a_mesh_to_a_normal_folder(self, home):
        out = home / "Documents" / "repaired.stl"
        result = _registered("repair_mesh").fn(
            file_path=str(home / "Documents" / "cube.stl"),
            output_path=str(out),
        )
        assert not _refused(result), result
        assert out.exists() and out.stat().st_size > 84

    def test_an_empty_location_is_left_to_the_tool(self, home):
        assert output_paths.check_write_location("repair_mesh", "output_path", "") is None

    def test_kilns_own_folder_is_allowed(self, home):
        assert output_paths.check_write_location(
            "repair_mesh", "output_path", str(home / ".kiln" / "cache" / "x.stl")
        ) is None

    def test_the_backup_tool_may_write_a_database(self, home):
        dest = str(home / "Documents" / "kiln-backup.db")
        assert output_paths.check_write_location("backup_database", "output_path", dest) is None
        assert output_paths.check_write_location("repair_mesh", "output_path", dest)


class TestFolderParameters:
    def test_an_output_folder_in_a_hidden_folder_is_refused(self, home):
        assert output_paths.check_write_location(
            "slice_model", "output_dir", str(home / ".ssh")
        )

    def test_an_output_folder_in_documents_is_allowed(self, home):
        assert output_paths.check_write_location(
            "slice_model", "output_dir", str(home / "Documents" / "slices")
        ) is None

    def test_a_folder_named_output_path_is_judged_as_a_folder(self, home):
        assert output_paths.check_write_location(
            "download_generated_model", "output_path", str(home / "Documents" / "models")
        ) is None


class TestConfinement:
    def test_kiln_output_dir_confines_every_named_location(self, home, tmp_path, monkeypatch):
        # pytest's tmp_path lives in the system temp folder, which is always
        # allowed; point that allowance somewhere else so the test judges
        # the confinement and not the temp exemption.
        other_temp = tmp_path / "elsewhere-temp"
        other_temp.mkdir()
        monkeypatch.setattr(output_paths, "_temp_root", lambda: other_temp.resolve())
        allowed = home / "Documents" / "kiln-out"
        monkeypatch.setenv(output_paths.OUTPUT_DIR_ENV, str(allowed))
        assert output_paths.check_write_location(
            "repair_mesh", "output_path", str(allowed / "a.stl")
        ) is None
        assert output_paths.check_write_location(
            "repair_mesh", "output_path", str(home / "Documents" / "a.stl")
        )


class TestEveryDoor:
    def test_every_registered_tool_with_a_write_location_is_guarded(self):
        from kiln import server

        guarded = []
        for name, tool in server.mcp._tool_manager._tools.items():
            params = inspect.signature(tool.fn).parameters
            if any(output_paths.write_param_kind(name, p) for p in params):
                assert getattr(tool.fn, "__kiln_output_path_guard__", False), (
                    f"{name} takes a write location but is not guarded"
                )
                guarded.append(name)
        # The reported tools are among them, and the set is not trivially small.
        for name in ("repair_mesh", "merge_stl", "mirror_mesh_model",
                     "splice_mesh_at_z", "hollow_mesh_model", "slice_model"):
            assert name in guarded
        assert len(guarded) >= 40

    def test_the_mcp_call_tool_door_refuses(self, home):
        from kiln import server

        conf = home / ".config" / "settings.conf"
        result = asyncio.run(server.mcp._tool_manager._tools["repair_mesh"].run(
            {"file_path": str(home / "Documents" / "cube.stl"), "output_path": str(conf)},
            context=None,
        ))
        assert _refused(result), result
        assert conf.read_text() == "IMPORTANT_USER_CONFIG=1\n"

    def test_the_bridge_door_refuses(self, home, monkeypatch):
        from kiln import bridge_client, server

        monkeypatch.setattr(server, "ensure_runtime_config", lambda: None)
        call = bridge_client._default_tool_caller()
        conf = home / ".config" / "settings.conf"
        result = call("repair_mesh", {
            "file_path": str(home / "Documents" / "cube.stl"),
            "output_path": str(conf),
        })
        assert _refused(result), result
        assert conf.read_text() == "IMPORTANT_USER_CONFIG=1\n"

    def test_a_tool_registered_later_is_guarded_too(self):
        from kiln import server

        def late_tool_for_guard_test(output_path: str = "") -> dict:
            return {"success": True}

        server.mcp.tool()(late_tool_for_guard_test)
        try:
            tool = server.mcp._tool_manager._tools["late_tool_for_guard_test"]
            assert _refused(tool.fn(output_path=os.path.expanduser("~/.bashrc")))
        finally:
            server.mcp._tool_manager._tools.pop("late_tool_for_guard_test", None)


class TestDownloadNames:
    """A download's saved name is part of where it writes.

    The bytes of a marketplace file are whatever its uploader chose, so a
    name that lands them in a shell startup file or a login item would run
    them.  The folder alone was checked; the name beside it was not.
    """

    def _download(self, **kwargs):
        return _registered("download_model").fn(file_id=1, **kwargs)

    def test_a_hidden_file_name_is_refused(self, home):
        result = self._download(dest_dir=str(home), file_name=".zshrc")
        assert _refused(result), result
        assert not (home / ".zshrc").exists()

    def test_a_login_item_name_is_refused(self, home):
        result = self._download(dest_dir=str(home / "Documents"), file_name="com.evil.plist")
        assert _refused(result), result

    def test_a_path_in_the_name_is_refused(self, home):
        result = self._download(dest_dir=str(home / "Documents"), file_name="../.ssh/x.stl")
        assert _refused(result), result

    def test_the_login_items_folder_is_refused(self, home):
        agents = home / "Library" / "LaunchAgents"
        result = self._download(dest_dir=str(agents), file_name="part.stl")
        assert _refused(result), result

    def test_an_ordinary_name_passes_the_guard(self, home):
        assert output_paths.refusal_for_call("download_model", {
            "dest_dir": str(home / "Documents"), "file_name": "bracket.zip",
        }) is None


class TestMarketplaceSuppliedNames:
    """The name a marketplace reports for a file is outside input too."""

    BASE = "https://api.thingiverse.com"

    def _serve(self, name: str) -> None:
        import responses

        responses.add(
            responses.GET, f"{self.BASE}/files/456",
            json={"id": 456, "name": name, "download_url": f"{self.BASE}/files/456/download"},
        )
        responses.add(
            responses.GET, f"{self.BASE}/files/456/download",
            body=b"echo pwned", content_type="application/octet-stream",
        )

    def test_a_hidden_script_name_from_the_marketplace_is_not_saved(self, tmp_path):
        import responses

        from kiln.thingiverse import ThingiverseClient, ThingiverseError

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET, f"{self.BASE}/files/456",
                json={"id": 456, "name": ".zshrc", "download_url": f"{self.BASE}/files/456/download"},
            )
            with pytest.raises(ThingiverseError):
                ThingiverseClient(token="t").download_file(456, str(tmp_path))
        assert list(tmp_path.iterdir()) == []

    def test_a_leading_dot_is_dropped_from_a_model_name(self, tmp_path):
        import responses

        from kiln.thingiverse import ThingiverseClient

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET, f"{self.BASE}/files/456",
                json={"id": 456, "name": ".benchy.stl", "download_url": f"{self.BASE}/files/456/download"},
            )
            rsps.add(
                responses.GET, f"{self.BASE}/files/456/download",
                body=b"solid x", content_type="application/octet-stream",
            )
            path = ThingiverseClient(token="t").download_file(456, str(tmp_path))
        assert Path(path).name == "benchy.stl"


class TestKilnsOwnState:
    def test_an_export_cannot_replace_kilns_token_file(self, home):
        assert output_paths.check_write_location(
            "export_audit_trail", "output_path", str(home / ".kiln" / "auth_tokens.json")
        )

    def test_a_backup_cannot_replace_kilns_live_database(self, home):
        assert output_paths.check_write_location(
            "backup_database", "output_path", str(home / ".kiln" / "kiln.db")
        )

    def test_an_export_never_replaces_an_existing_file(self, home):
        existing = home / "Documents" / "package.json"
        existing.write_text("{}")
        assert output_paths.check_write_location("export_audit_trail", "output_path", str(existing))
        assert output_paths.check_write_location(
            "export_audit_trail", "output_path", str(home / "Documents" / "audit.json")
        ) is None


class TestSpellingAndLinks:
    """macOS and Windows ignore letter case; a link at a name is followed."""

    def test_a_differently_cased_login_items_folder_is_refused(self, home):
        assert output_paths.check_write_location(
            "slice_model", "output_dir", str(home / "library" / "LaunchAgents")
        )

    def test_a_differently_cased_system_folder_is_refused(self, home):
        assert output_paths.check_write_location("slice_model", "output_dir", "/USR/local/kiln")

    def test_kilns_state_rule_holds_under_another_spelling(self, home):
        assert output_paths.check_write_location(
            "backup_database", "output_path", str(home / ".KILN" / "kiln.db")
        )

    def test_a_file_name_that_is_a_link_is_judged_by_its_target(self, home):
        target = home / ".zshrc"
        target.write_text("export PATH=/usr/bin\n")
        (home / "Documents" / "innocent.stl").symlink_to(target)
        result = _registered("download_model").fn(
            file_id=1, dest_dir=str(home / "Documents"), file_name="innocent.stl",
        )
        assert _refused(result), result
        assert target.read_text() == "export PATH=/usr/bin\n"

    def test_a_download_is_not_saved_over_a_link(self, tmp_path):
        import responses

        from kiln.thingiverse import ThingiverseClient, ThingiverseError

        target = tmp_path / "victim.txt"
        target.write_text("keep me")
        (tmp_path / "dl").mkdir()
        (tmp_path / "dl" / "benchy.stl").symlink_to(target)
        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses.GET, "https://api.thingiverse.com/files/456",
                json={"id": 456, "name": "benchy.stl",
                      "download_url": "https://api.thingiverse.com/files/456/download"},
            )
            rsps.add(
                responses.GET, "https://api.thingiverse.com/files/456/download",
                body=b"echo pwned", content_type="application/octet-stream",
            )
            with pytest.raises(ThingiverseError):
                ThingiverseClient(token="t").download_file(456, str(tmp_path / "dl"))
        assert target.read_text() == "keep me"
