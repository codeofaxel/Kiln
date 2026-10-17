"""Tests for kiln.cli.config — configuration management."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kiln.cli.config import (
    _normalize_host,
    get_config_path,
    list_printers,
    load_printer_config,
    remove_printer,
    save_printer,
    set_active_printer,
    validate_printer_config,
)

# ---------------------------------------------------------------------------
# _normalize_host
# ---------------------------------------------------------------------------


class TestNormalizeHost:
    def test_adds_scheme(self):
        assert _normalize_host("octopi.local") == "http://octopi.local"

    def test_preserves_https(self):
        assert _normalize_host("https://printer.example.com") == "https://printer.example.com"

    def test_strips_trailing_slash(self):
        assert _normalize_host("http://host/") == "http://host"

    def test_strips_whitespace(self):
        assert _normalize_host("  http://host  ") == "http://host"

    def test_empty_string(self):
        assert _normalize_host("") == ""

    # Bambu-specific: raw IP, no http:// prefix
    def test_bambu_no_scheme_added(self):
        assert _normalize_host("192.168.1.100", "bambu") == "192.168.1.100"

    def test_bambu_strips_accidental_http(self):
        assert _normalize_host("http://192.168.1.100", "bambu") == "192.168.1.100"

    def test_bambu_strips_accidental_https(self):
        assert _normalize_host("https://192.168.1.100", "bambu") == "192.168.1.100"

    def test_bambu_strips_whitespace(self):
        assert _normalize_host("  192.168.1.100  ", "bambu") == "192.168.1.100"

    def test_bambu_strips_trailing_slash(self):
        assert _normalize_host("192.168.1.100/", "bambu") == "192.168.1.100"


# ---------------------------------------------------------------------------
# get_config_path
# ---------------------------------------------------------------------------


class TestGetConfigPath:
    def test_returns_home_based_path(self):
        p = get_config_path()
        assert p == Path.home() / ".kiln" / "config.yaml"


# ---------------------------------------------------------------------------
# save_printer / load_printer_config
# ---------------------------------------------------------------------------


class TestSaveAndLoad:
    def test_save_and_load_octoprint(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        save_printer(
            "ender3",
            "octoprint",
            "http://octopi.local",
            api_key="abc123",
            config_path=cfg_path,
        )
        cfg = load_printer_config("ender3", config_path=cfg_path)
        assert cfg["type"] == "octoprint"
        assert cfg["host"] == "http://octopi.local"
        assert cfg["api_key"] == "abc123"

    def test_save_and_load_moonraker(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        save_printer(
            "voron",
            "moonraker",
            "voron.local:7125",
            config_path=cfg_path,
        )
        cfg = load_printer_config("voron", config_path=cfg_path)
        assert cfg["type"] == "moonraker"
        assert cfg["host"] == "http://voron.local:7125"

    def test_save_and_load_creality_with_model(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        save_printer(
            "k1-max",
            "creality",
            "192.168.1.55",
            printer_model="creality_k1_max",
            config_path=cfg_path,
        )
        cfg = load_printer_config("k1-max", config_path=cfg_path)
        assert cfg["type"] == "creality"
        assert cfg["host"] == "http://192.168.1.55"
        assert cfg["printer_model"] == "creality_k1_max"

    def test_save_and_load_bambu(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        save_printer(
            "x1c",
            "bambu",
            "192.168.1.100",
            access_code="12345678",
            serial="01P00A000000001",
            config_path=cfg_path,
        )
        cfg = load_printer_config("x1c", config_path=cfg_path)
        assert cfg["type"] == "bambu"
        assert cfg["host"] == "192.168.1.100"  # Raw IP, no http:// prefix
        assert cfg["access_code"] == "12345678"
        assert cfg["serial"] == "01P00A000000001"

    def test_bambu_env_var_no_http_prefix(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KILN_PRINTER_HOST", "192.168.1.100")
        monkeypatch.setenv("KILN_PRINTER_TYPE", "bambu")
        monkeypatch.setenv("KILN_PRINTER_API_KEY", "12345678")
        monkeypatch.setenv("KILN_PRINTER_SERIAL", "01P00A000000001")
        cfg = load_printer_config(config_path=tmp_path / "config.yaml")
        assert cfg["host"] == "192.168.1.100"  # No http:// prefix
        assert cfg["type"] == "bambu"

    def test_sets_active_by_default(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        save_printer("a", "octoprint", "http://a", api_key="k", config_path=cfg_path)
        raw = yaml.safe_load(cfg_path.read_text())
        assert raw["active_printer"] == "a"

    def test_auto_select_single_printer(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        save_printer("only", "moonraker", "http://only", config_path=cfg_path)
        # Load without specifying name — should auto-select the only one
        cfg = load_printer_config(config_path=cfg_path)
        assert cfg["type"] == "moonraker"

    def test_multiple_printers_requires_active(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        save_printer("a", "moonraker", "http://a", config_path=cfg_path)
        save_printer("b", "moonraker", "http://b", set_active=False, config_path=cfg_path)
        # Remove active_printer key
        raw = yaml.safe_load(cfg_path.read_text())
        raw.pop("active_printer", None)
        cfg_path.write_text(yaml.safe_dump(raw))
        with pytest.raises(ValueError, match="Multiple printers"):
            load_printer_config(config_path=cfg_path)

    def test_printer_not_found(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        save_printer("a", "moonraker", "http://a", config_path=cfg_path)
        with pytest.raises(ValueError, match="not found"):
            load_printer_config("nonexistent", config_path=cfg_path)

    def test_no_printers_configured(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("{}")
        with pytest.raises(ValueError, match="No printers configured"):
            load_printer_config(config_path=cfg_path)

    def test_env_var_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KILN_PRINTER_HOST", "http://env-host")
        monkeypatch.setenv("KILN_PRINTER_TYPE", "moonraker")
        cfg = load_printer_config(config_path=tmp_path / "config.yaml")
        assert cfg["host"] == "http://env-host"
        assert cfg["type"] == "moonraker"

    def test_env_var_includes_printer_model(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KILN_PRINTER_HOST", "192.168.1.55")
        monkeypatch.setenv("KILN_PRINTER_TYPE", "creality")
        monkeypatch.setenv("KILN_PRINTER_MODEL", "creality_k1_max")
        cfg = load_printer_config(config_path=tmp_path / "config.yaml")
        assert cfg["host"] == "http://192.168.1.55"
        assert cfg["type"] == "creality"
        assert cfg["printer_model"] == "creality_k1_max"

    def test_default_settings_applied(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        save_printer("p", "moonraker", "http://p", config_path=cfg_path)
        cfg = load_printer_config("p", config_path=cfg_path)
        assert cfg["timeout"] == 30
        assert cfg["retries"] == 3


# ---------------------------------------------------------------------------
# save_printer under a name already saved
# ---------------------------------------------------------------------------

_A1_SERIAL = "01P00A000000001"


def _saved_a1(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    save_printer(
        "a1", "bambu", "192.168.1.6",
        access_code="x", serial=_A1_SERIAL, printer_model="bambu_a1",
        camera_stream_url="rtsps://192.168.1.50/live",
        config_path=cfg, probe=False,
    )
    return cfg


def _entry(cfg: Path, name: str) -> dict:
    return yaml.safe_load(cfg.read_text())["printers"][name]


class TestSaveUnderASavedName:
    """Same machine (serial, or an unchanged address) updates and keeps
    what was stated before; a different machine is refused unwritten;
    an address-only printer that moved is replaced with a notice."""

    def test_a_new_name_is_added_without_a_notice(self, tmp_path):
        result = save_printer("p", "bambu", "10.0.0.2", access_code="x", serial="S1",
                              config_path=tmp_path / "config.yaml", probe=False)
        assert result.outcome == "added"
        assert result.notice is None
        assert result.path == tmp_path / "config.yaml"

    def test_a_different_serial_is_refused_and_nothing_is_written(self, tmp_path):
        cfg = _saved_a1(tmp_path)
        before = cfg.read_text()
        with pytest.raises(ValueError, match="serial ending 0001, at 192.168.1.6"):
            save_printer("a1", "bambu", "192.168.1.7", access_code="y", serial="01P00A000000002",
                         config_path=cfg, probe=False)
        assert cfg.read_text() == before

    def test_the_refusal_names_a_free_name(self, tmp_path):
        cfg = _saved_a1(tmp_path)
        save_printer("a1-2", "bambu", "192.168.1.8", access_code="z", serial="S3", config_path=cfg, probe=False)
        with pytest.raises(ValueError) as info:
            save_printer("a1", "bambu", "192.168.1.7", access_code="y", serial="S2", config_path=cfg, probe=False)
        assert info.value.suggested_name == "a1-3"
        assert "'a1-3'" in str(info.value)

    def test_the_same_serial_updates_and_keeps_model_and_camera(self, tmp_path):
        cfg = _saved_a1(tmp_path)
        result = save_printer("a1", "bambu", "192.168.1.7", access_code="y", serial=_A1_SERIAL.lower(),
                              config_path=cfg, probe=False)
        entry = _entry(cfg, "a1")
        assert entry["host"] == "192.168.1.7"
        assert entry["access_code"] == "y"
        assert entry["printer_model"] == "bambu_a1"
        assert entry["camera_stream_url"] == "rtsps://192.168.1.50/live"
        assert result.outcome == "updated"
        assert "from 192.168.1.6 to 192.168.1.7" in result.notice
        assert "printer_model, camera_stream_url" in result.notice

    def test_a_different_printer_type_is_refused(self, tmp_path):
        cfg = _saved_a1(tmp_path)
        before = cfg.read_text()
        with pytest.raises(ValueError, match="different kind of printer"):
            save_printer("a1", "moonraker", "http://192.168.1.6", config_path=cfg, probe=False)
        assert cfg.read_text() == before

    def test_a_different_elegoo_mainboard_is_refused(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        save_printer("sat", "elegoo", "10.0.0.3", serial="MAINBOARD-AAAA", config_path=cfg, probe=False)
        with pytest.raises(ValueError, match="serial ending AAAA"):
            save_printer("sat", "elegoo", "10.0.0.3", serial="MAINBOARD-BBBB", config_path=cfg, probe=False)
        assert _entry(cfg, "sat")["mainboard_id"] == "MAINBOARD-AAAA"

    def test_an_address_only_printer_that_moved_is_replaced_with_a_notice(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        save_printer("ender", "octoprint", "http://10.0.0.5", api_key="k", printer_model="ender3",
                     config_path=cfg, probe=False)
        result = save_printer("ender", "octoprint", "http://10.0.0.9", api_key="k2", config_path=cfg, probe=False)
        entry = _entry(cfg, "ender")
        assert entry["host"] == "http://10.0.0.9"
        assert entry["api_key"] == "k2"
        assert "printer_model" not in entry
        assert result.outcome == "replaced"
        assert "(was at http://10.0.0.5)" in result.notice
        assert "api_key, printer_model" in result.notice

    def test_an_address_only_printer_at_the_same_address_is_updated(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        save_printer("ender", "octoprint", "http://10.0.0.5", api_key="k", printer_model="ender3",
                     config_path=cfg, probe=False)
        result = save_printer("ender", "octoprint", "http://10.0.0.5", api_key="k2", config_path=cfg, probe=False)
        assert result.outcome == "updated"
        assert result.notice.startswith("Updated existing printer 'ender'.")
        assert _entry(cfg, "ender")["printer_model"] == "ender3"

    def test_a_replaced_entry_drops_the_old_credentials_and_camera(self, tmp_path):
        # Everything saved under the name describes the machine at the old
        # address: its key, its model's limits, its camera (whose URL may
        # embed a password).  When the address changed and nothing proves
        # this is that machine, none of it applies to the new address; the
        # notice says what has to be stated again.
        cfg = tmp_path / "config.yaml"
        save_printer("ender", "octoprint", "http://10.0.0.5", api_key="k", printer_model="ender3",
                     camera_stream_url="rtsp://user:secret@10.0.0.50/live",
                     config_path=cfg, probe=False)
        result = save_printer("ender", "octoprint", "http://10.0.0.9", config_path=cfg, probe=False)
        entry = _entry(cfg, "ender")
        assert entry == {"type": "octoprint", "host": "http://10.0.0.9"}
        assert result.outcome == "replaced"
        assert result.dropped == ("api_key", "printer_model", "camera_stream_url")
        assert "api_key, printer_model, camera_stream_url" in result.notice
        assert "secret" not in result.notice

    def test_a_serial_printer_saved_without_its_serial_at_a_new_address_is_refused(self, tmp_path):
        # The saved entry names one machine by serial; a save that names
        # none, at another address, cannot be shown to be that machine, so
        # neither the serial nor the access code is carried to the new host.
        cfg = _saved_a1(tmp_path)
        before = cfg.read_text()
        with pytest.raises(ValueError, match="serial ending 0001") as info:
            save_printer("a1", "bambu", "192.168.1.7", access_code="y", config_path=cfg, probe=False)
        assert info.value.suggested_name == "a1-2"
        assert cfg.read_text() == before

    def test_a_serial_printer_saved_without_its_serial_at_the_same_address_is_updated(self, tmp_path):
        cfg = _saved_a1(tmp_path)
        result = save_printer("a1", "bambu", "192.168.1.6", access_code="y", config_path=cfg, probe=False)
        entry = _entry(cfg, "a1")
        assert result.outcome == "updated"
        assert entry["serial"] == _A1_SERIAL
        assert entry["access_code"] == "y"

    def test_a_new_camera_replaces_both_saved_camera_urls(self, tmp_path):
        cfg = _saved_a1(tmp_path)
        save_printer("a1", "bambu", "192.168.1.6", access_code="x", serial=_A1_SERIAL,
                     camera_snapshot_url="http://192.168.1.60/snap.jpg", config_path=cfg, probe=False)
        entry = _entry(cfg, "a1")
        assert entry["camera_snapshot_url"] == "http://192.168.1.60/snap.jpg"
        assert "camera_stream_url" not in entry
        assert entry["printer_model"] == "bambu_a1"


# ---------------------------------------------------------------------------
# set_active_printer
# ---------------------------------------------------------------------------


class TestSaveWithAReadMachineId:
    """A machine id Kiln read off the printer is stored for the types that
    state none, and judges a save under the name the way a stated serial
    does: the same id at a new address updates and keeps the credential,
    a different id is refused, and an id that could not be read this time
    is replaced as an id-less printer is -- never refused, since the
    person never typed it."""

    @staticmethod
    def _identity(value="CZPX4720X004XC34242", source="prusalink_info.serial"):
        from kiln.printers.base import MachineIdentity

        return MachineIdentity(value=value, source=source, kind="printer_serial")

    def _saved_mk4(self, tmp_path, **extra):
        cfg = tmp_path / "config.yaml"
        save_printer("mk4", "prusalink", "http://10.0.0.5", api_key="k", printer_model="prusa_mk4",
                     camera_stream_url="rtsp://user:secret@10.0.0.50/live",
                     machine_identity=self._identity(), config_path=cfg, probe=False, **extra)
        return cfg

    def test_the_read_id_is_stored_with_its_source(self, tmp_path):
        cfg = self._saved_mk4(tmp_path)
        entry = _entry(cfg, "mk4")
        assert entry["machine_id"] == "CZPX4720X004XC34242"
        assert entry["machine_id_source"] == "prusalink_info.serial"

    def test_a_bambu_save_never_stores_a_read_id(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        save_printer("a1", "bambu", "10.0.0.2", access_code="x", serial="S1",
                     machine_identity=self._identity(), config_path=cfg, probe=False)
        assert "machine_id" not in _entry(cfg, "a1")

    def test_a_printer_that_moved_is_updated_when_it_reports_the_saved_id(self, tmp_path):
        cfg = self._saved_mk4(tmp_path)
        result = save_printer("mk4", "prusalink", "http://10.0.0.9",
                              machine_identity=self._identity(), config_path=cfg, probe=False)
        entry = _entry(cfg, "mk4")
        assert result.outcome == "updated"
        assert "from http://10.0.0.5 to http://10.0.0.9" in result.notice
        assert entry["host"] == "http://10.0.0.9"
        assert entry["api_key"] == "k"
        assert entry["camera_stream_url"] == "rtsp://user:secret@10.0.0.50/live"
        assert entry["machine_id"] == "CZPX4720X004XC34242"
        assert "machine_id" not in result.notice

    def test_the_id_is_compared_without_regard_to_case(self, tmp_path):
        cfg = self._saved_mk4(tmp_path)
        result = save_printer("mk4", "prusalink", "http://10.0.0.9",
                              machine_identity=self._identity("czpx4720x004xc34242"),
                              config_path=cfg, probe=False)
        assert result.outcome == "updated"

    def test_a_different_id_at_a_new_address_is_refused_unwritten(self, tmp_path):
        cfg = self._saved_mk4(tmp_path)
        before = cfg.read_text()
        with pytest.raises(ValueError, match="machine id ending 4242") as info:
            save_printer("mk4", "prusalink", "http://10.0.0.9", api_key="k2",
                         machine_identity=self._identity("CZPX4720X004XC99999"),
                         config_path=cfg, probe=False)
        assert info.value.suggested_name == "mk4-2"
        assert cfg.read_text() == before

    def test_a_different_id_at_the_same_address_is_refused_too(self, tmp_path):
        cfg = self._saved_mk4(tmp_path)
        with pytest.raises(ValueError, match="A different machine answers as 'mk4'"):
            save_printer("mk4", "prusalink", "http://10.0.0.5",
                         machine_identity=self._identity("CZPX4720X004XC99999"),
                         config_path=cfg, probe=False)
        assert _entry(cfg, "mk4")["machine_id"] == "CZPX4720X004XC34242"

    def test_a_moved_printer_whose_id_could_not_be_read_is_replaced_not_refused(self, tmp_path):
        cfg = self._saved_mk4(tmp_path)
        result = save_printer("mk4", "prusalink", "http://10.0.0.9", config_path=cfg, probe=False)
        entry = _entry(cfg, "mk4")
        assert result.outcome == "replaced"
        assert "api_key" not in entry
        assert "machine_id" not in entry
        assert "machine_id_source" not in entry
        assert "printer_model" not in entry
        assert result.dropped == ("api_key", "printer_model", "camera_stream_url")
        assert "machine_id" not in result.notice

    def test_an_id_from_another_source_says_nothing_either_way(self, tmp_path):
        # A backend that starts reading a different field must not call
        # every printer saved before it a different machine.
        cfg = self._saved_mk4(tmp_path)
        result = save_printer("mk4", "prusalink", "http://10.0.0.9",
                              machine_identity=self._identity("OTHER", source="prusalink_info.hostname"),
                              config_path=cfg, probe=False)
        assert result.outcome == "replaced"
        assert _entry(cfg, "mk4")["machine_id"] == "OTHER"

    def test_a_save_at_the_same_address_keeps_the_saved_id_when_none_was_read(self, tmp_path):
        cfg = self._saved_mk4(tmp_path)
        result = save_printer("mk4", "prusalink", "http://10.0.0.5", api_key="k2",
                              config_path=cfg, probe=False)
        assert result.outcome == "updated"
        assert _entry(cfg, "mk4")["machine_id"] == "CZPX4720X004XC34242"
        assert "machine_id" not in result.notice

    def test_the_result_carries_the_id_the_entry_holds(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        result = save_printer("mk4", "prusalink", "http://10.0.0.5", api_key="k",
                              machine_identity=self._identity(), config_path=cfg, probe=False)
        assert result.machine_identity == self._identity()
        plain = save_printer("ender", "octoprint", "http://10.0.0.6", api_key="k",
                             config_path=cfg, probe=False)
        assert plain.machine_identity is None

    def test_a_reader_is_asked_only_when_the_address_answers(self, tmp_path, monkeypatch):
        import kiln.cli.config as mod

        cfg = tmp_path / "config.yaml"
        asked: list[str] = []

        def reader():
            asked.append("read")
            return self._identity()

        monkeypatch.setattr(mod, "_check_printer_url", lambda url, *, printer_type, probe: (url, [], False))
        save_printer("mk4", "prusalink", "http://10.0.0.5", api_key="k",
                     read_machine_identity=reader, config_path=cfg, probe=True)
        assert asked == []
        assert "machine_id" not in _entry(cfg, "mk4")

        monkeypatch.setattr(mod, "_check_printer_url", lambda url, *, printer_type, probe: (url, [], True))
        result = save_printer("mk4", "prusalink", "http://10.0.0.5", api_key="k",
                              read_machine_identity=reader, config_path=cfg, probe=True)
        assert asked == ["read"]
        assert _entry(cfg, "mk4")["machine_id"] == "CZPX4720X004XC34242"
        assert result.machine_identity == self._identity()

    def test_a_reader_that_fails_leaves_the_save_without_an_id(self, tmp_path, monkeypatch):
        import kiln.cli.config as mod

        cfg = tmp_path / "config.yaml"
        monkeypatch.setattr(mod, "_check_printer_url", lambda url, *, printer_type, probe: (url, [], True))

        def reader():
            raise RuntimeError("printer hung up")

        result = save_printer("mk4", "prusalink", "http://10.0.0.5", api_key="k",
                              read_machine_identity=reader, config_path=cfg, probe=True)
        assert result.outcome == "added"
        assert "machine_id" not in _entry(cfg, "mk4")


class TestSetActive:
    def test_set_active(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        save_printer("a", "moonraker", "http://a", config_path=cfg_path)
        save_printer("b", "moonraker", "http://b", config_path=cfg_path)
        set_active_printer("a", config_path=cfg_path)
        raw = yaml.safe_load(cfg_path.read_text())
        assert raw["active_printer"] == "a"

    def test_set_active_not_found(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        save_printer("a", "moonraker", "http://a", config_path=cfg_path)
        with pytest.raises(ValueError, match="not found"):
            set_active_printer("nope", config_path=cfg_path)


# ---------------------------------------------------------------------------
# list_printers
# ---------------------------------------------------------------------------


class TestListPrinters:
    def test_list_empty(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("{}")
        result = list_printers(config_path=cfg_path)
        assert result == []

    def test_list_multiple(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        save_printer("a", "octoprint", "http://a", api_key="k", config_path=cfg_path)
        save_printer("b", "moonraker", "http://b", config_path=cfg_path)
        result = list_printers(config_path=cfg_path)
        assert len(result) == 2
        names = {p["name"] for p in result}
        assert names == {"a", "b"}
        # b should be active (last saved with set_active=True)
        active = [p for p in result if p["active"]]
        assert len(active) == 1
        assert active[0]["name"] == "b"


# ---------------------------------------------------------------------------
# remove_printer
# ---------------------------------------------------------------------------


class TestRemovePrinter:
    def test_remove(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        save_printer("a", "moonraker", "http://a", config_path=cfg_path)
        save_printer("b", "moonraker", "http://b", config_path=cfg_path)
        remove_printer("b", config_path=cfg_path)
        result = list_printers(config_path=cfg_path)
        assert len(result) == 1
        assert result[0]["name"] == "a"

    def test_remove_active_selects_next(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        save_printer("a", "moonraker", "http://a", config_path=cfg_path)
        save_printer("b", "moonraker", "http://b", config_path=cfg_path)
        # b is active
        remove_printer("b", config_path=cfg_path)
        raw = yaml.safe_load(cfg_path.read_text())
        assert raw["active_printer"] == "a"

    def test_remove_not_found(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("{}")
        with pytest.raises(ValueError, match="not found"):
            remove_printer("nope", config_path=cfg_path)


# ---------------------------------------------------------------------------
# validate_printer_config
# ---------------------------------------------------------------------------


class TestValidate:
    def test_valid_octoprint(self):
        ok, err = validate_printer_config({"type": "octoprint", "host": "http://h", "api_key": "k"})
        assert ok is True
        assert err is None

    def test_octoprint_missing_api_key(self):
        ok, err = validate_printer_config({"type": "octoprint", "host": "http://h"})
        assert ok is False
        assert "api_key" in err

    def test_valid_moonraker(self):
        ok, err = validate_printer_config({"type": "moonraker", "host": "http://h"})
        assert ok is True

    def test_valid_creality_without_api_key(self):
        ok, err = validate_printer_config({"type": "creality", "host": "http://h"})
        assert ok is True
        assert err is None

    def test_valid_bambu(self):
        ok, err = validate_printer_config({
            "type": "bambu", "host": "http://h",
            "access_code": "abc", "serial": "123",
        })
        assert ok is True

    def test_bambu_missing_access_code(self):
        ok, err = validate_printer_config({"type": "bambu", "host": "http://h", "serial": "123"})
        assert ok is False
        assert "access_code" in err

    def test_bambu_missing_serial(self):
        ok, err = validate_printer_config({"type": "bambu", "host": "http://h", "access_code": "abc"})
        assert ok is False
        assert "serial" in err

    def test_unknown_type(self):
        ok, err = validate_printer_config({"type": "unknown", "host": "http://h"})
        assert ok is False

    def test_missing_host(self):
        ok, err = validate_printer_config({"type": "moonraker", "host": ""})
        assert ok is False
        assert "host" in err
