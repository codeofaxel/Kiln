"""Tests for octoprint_cli.config."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from octoprint_cli.config import (
    DEFAULTS,
    _normalize_host,
    get_default_config_path,
    init_config,
    load_config,
    validate_config,
)


# ===================================================================
# _normalize_host
# ===================================================================


class TestNormalizeHost:
    """Tests for URL normalization."""

    def test_adds_http_scheme(self) -> None:
        assert _normalize_host("octopi.local") == "http://octopi.local"

    def test_strips_trailing_slash(self) -> None:
        assert _normalize_host("http://octopi.local/") == "http://octopi.local"

    def test_strips_multiple_trailing_slashes(self) -> None:
        assert _normalize_host("http://octopi.local///") == "http://octopi.local"

    def test_preserves_https(self) -> None:
        assert _normalize_host("https://octopi.local") == "https://octopi.local"

    def test_preserves_http(self) -> None:
        assert _normalize_host("http://octopi.local") == "http://octopi.local"

    def test_strips_whitespace(self) -> None:
        assert _normalize_host("  http://octopi.local  ") == "http://octopi.local"

    def test_empty_string(self) -> None:
        assert _normalize_host("") == ""

    def test_only_hostname(self) -> None:
        assert _normalize_host("192.168.1.100") == "http://192.168.1.100"

    def test_hostname_with_port(self) -> None:
        assert _normalize_host("octopi.local:5000") == "http://octopi.local:5000"

    def test_full_url_with_port(self) -> None:
        assert _normalize_host("http://octopi.local:5000/") == "http://octopi.local:5000"

    def test_case_insensitive_scheme_detection(self) -> None:
        assert _normalize_host("HTTP://octopi.local") == "HTTP://octopi.local"
        assert _normalize_host("Https://octopi.local") == "Https://octopi.local"


# ===================================================================
# load_config - config file tier
# ===================================================================


class TestLoadConfigFile:
    """Test load_config reading from YAML config files."""

    def test_reads_all_fields_from_file(
        self, sample_config_file: Path, env_clean: None
    ) -> None:
        config = load_config(config_path=str(sample_config_file))
        assert config["host"] == "http://myprinter.local"
        assert config["api_key"] == "FILEAPIKEY789"
        assert config["timeout"] == 15
        assert config["retries"] == 2

    def test_defaults_when_no_file_exists(
        self, tmp_path: Path, env_clean: None
    ) -> None:
        config = load_config(config_path=str(tmp_path / "nonexistent.yaml"))
        assert config["host"] == _normalize_host(str(DEFAULTS["host"]))
        assert config["api_key"] == DEFAULTS["api_key"]
        assert config["timeout"] == DEFAULTS["timeout"]
        assert config["retries"] == DEFAULTS["retries"]

    def test_partial_config_file(
        self, tmp_path: Path, env_clean: None
    ) -> None:
        """A config file with only host should merge with defaults for the rest."""
        p = tmp_path / "partial.yaml"
        with p.open("w") as fh:
            yaml.safe_dump({"host": "http://partial.local"}, fh)
        config = load_config(config_path=str(p))
        assert config["host"] == "http://partial.local"
        assert config["api_key"] == DEFAULTS["api_key"]
        assert config["timeout"] == DEFAULTS["timeout"]
        assert config["retries"] == DEFAULTS["retries"]

    def test_invalid_yaml_returns_defaults(
        self, tmp_path: Path, env_clean: None
    ) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text("{{{{invalid yaml")
        config = load_config(config_path=str(p))
        assert config["host"] == _normalize_host(str(DEFAULTS["host"]))

    def test_yaml_with_non_dict_returns_defaults(
        self, tmp_path: Path, env_clean: None
    ) -> None:
        """A YAML file that parses to a list should be treated as empty."""
        p = tmp_path / "list.yaml"
        p.write_text("- item1\n- item2\n")
        config = load_config(config_path=str(p))
        assert config["host"] == _normalize_host(str(DEFAULTS["host"]))


# ===================================================================
# load_config - env var tier
# ===================================================================


class TestLoadConfigEnvVars:
    """Test that environment variables override file values."""

    def test_env_host_overrides_file(
        self,
        sample_config_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OCTOPRINT_HOST", "http://envhost.local")
        monkeypatch.delenv("OCTOPRINT_API_KEY", raising=False)
        config = load_config(config_path=str(sample_config_file))
        assert config["host"] == "http://envhost.local"
        # api_key should still come from file
        assert config["api_key"] == "FILEAPIKEY789"

    def test_env_api_key_overrides_file(
        self,
        sample_config_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("OCTOPRINT_HOST", raising=False)
        monkeypatch.setenv("OCTOPRINT_API_KEY", "ENVKEY999")
        config = load_config(config_path=str(sample_config_file))
        assert config["host"] == "http://myprinter.local"
        assert config["api_key"] == "ENVKEY999"

    def test_both_env_vars_override_file(
        self,
        sample_config_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OCTOPRINT_HOST", "http://envhost.local")
        monkeypatch.setenv("OCTOPRINT_API_KEY", "ENVKEY999")
        config = load_config(config_path=str(sample_config_file))
        assert config["host"] == "http://envhost.local"
        assert config["api_key"] == "ENVKEY999"

    def test_empty_env_var_does_not_override(
        self,
        sample_config_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An empty env var should not override the file value."""
        monkeypatch.setenv("OCTOPRINT_HOST", "")
        monkeypatch.delenv("OCTOPRINT_API_KEY", raising=False)
        config = load_config(config_path=str(sample_config_file))
        # Empty string is falsy so the file value should be used
        assert config["host"] == "http://myprinter.local"


# ===================================================================
# load_config - CLI flags tier (highest priority)
# ===================================================================


class TestLoadConfigCLIFlags:
    """Test that explicit parameters (CLI flags) override env and file."""

    def test_host_flag_overrides_all(
        self,
        sample_config_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OCTOPRINT_HOST", "http://envhost.local")
        monkeypatch.delenv("OCTOPRINT_API_KEY", raising=False)
        config = load_config(
            host="http://flaghost.local",
            config_path=str(sample_config_file),
        )
        assert config["host"] == "http://flaghost.local"

    def test_api_key_flag_overrides_all(
        self,
        sample_config_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OCTOPRINT_API_KEY", "ENVKEY999")
        monkeypatch.delenv("OCTOPRINT_HOST", raising=False)
        config = load_config(
            api_key="FLAGKEY111",
            config_path=str(sample_config_file),
        )
        assert config["api_key"] == "FLAGKEY111"

    def test_both_flags_override_all(
        self,
        sample_config_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OCTOPRINT_HOST", "http://envhost.local")
        monkeypatch.setenv("OCTOPRINT_API_KEY", "ENVKEY999")
        config = load_config(
            host="http://flaghost.local",
            api_key="FLAGKEY111",
            config_path=str(sample_config_file),
        )
        assert config["host"] == "http://flaghost.local"
        assert config["api_key"] == "FLAGKEY111"


# ===================================================================
# load_config - precedence integration
# ===================================================================


class TestLoadConfigPrecedence:
    """Integration tests confirming the full precedence chain."""

    def test_precedence_flags_over_env_over_file(
        self,
        sample_config_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OCTOPRINT_HOST", "http://envhost.local")
        monkeypatch.setenv("OCTOPRINT_API_KEY", "ENVKEY999")

        # Only override host via flag; api_key should come from env
        config = load_config(
            host="http://flaghost.local",
            config_path=str(sample_config_file),
        )
        assert config["host"] == "http://flaghost.local"
        assert config["api_key"] == "ENVKEY999"
        # timeout and retries should come from file
        assert config["timeout"] == 15
        assert config["retries"] == 2

    def test_host_normalized_from_all_tiers(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Host gets normalized regardless of which tier provides it."""
        monkeypatch.delenv("OCTOPRINT_HOST", raising=False)
        monkeypatch.delenv("OCTOPRINT_API_KEY", raising=False)
        # Write a config file with a host missing the scheme
        p = tmp_path / "cfg.yaml"
        with p.open("w") as fh:
            yaml.safe_dump({"host": "myprinter.local/", "api_key": "KEY"}, fh)
        config = load_config(config_path=str(p))
        assert config["host"] == "http://myprinter.local"


# ===================================================================
# validate_config
# ===================================================================


class TestValidateConfig:
    """Tests for validate_config()."""

    def test_valid_config(self) -> None:
        ok, msg = validate_config({
            "host": "http://octopi.local",
            "api_key": "ABCDEF123",
        })
        assert ok is True
        assert msg is None

    def test_missing_host(self) -> None:
        ok, msg = validate_config({"api_key": "ABCDEF123"})
        assert ok is False
        assert "host" in msg.lower()

    def test_empty_host(self) -> None:
        ok, msg = validate_config({"host": "", "api_key": "ABCDEF123"})
        assert ok is False
        assert "host" in msg.lower()

    def test_host_without_scheme(self) -> None:
        ok, msg = validate_config({
            "host": "octopi.local",
            "api_key": "KEY",
        })
        assert ok is False
        assert "http" in msg.lower()

    def test_host_with_only_scheme(self) -> None:
        ok, msg = validate_config({
            "host": "http://",
            "api_key": "KEY",
        })
        assert ok is False
        assert "valid url" in msg.lower()

    def test_missing_api_key(self) -> None:
        ok, msg = validate_config({"host": "http://octopi.local"})
        assert ok is False
        assert "api_key" in msg.lower()

    def test_empty_api_key(self) -> None:
        ok, msg = validate_config({
            "host": "http://octopi.local",
            "api_key": "",
        })
        assert ok is False
        assert "api_key" in msg.lower()

    def test_whitespace_only_api_key(self) -> None:
        ok, msg = validate_config({
            "host": "http://octopi.local",
            "api_key": "   ",
        })
        assert ok is False
        assert "api_key" in msg.lower()

    def test_non_string_host(self) -> None:
        ok, msg = validate_config({
            "host": 12345,
            "api_key": "KEY",
        })
        assert ok is False

    def test_non_string_api_key(self) -> None:
        ok, msg = validate_config({
            "host": "http://octopi.local",
            "api_key": 12345,
        })
        assert ok is False

    def test_https_host_valid(self) -> None:
        ok, msg = validate_config({
            "host": "https://octopi.local",
            "api_key": "KEY",
        })
        assert ok is True
        assert msg is None

    def test_host_with_port_valid(self) -> None:
        ok, msg = validate_config({
            "host": "http://192.168.1.100:5000",
            "api_key": "KEY",
        })
        assert ok is True
        assert msg is None


# ===================================================================
# init_config
# ===================================================================


class TestInitConfig:
    """Tests for init_config()."""

    def test_creates_config_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """init_config should create the config directory and file."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        result_path = init_config("octopi.local", "MY_API_KEY")
        assert result_path.exists()
        assert result_path.is_file()

        with result_path.open() as fh:
            data = yaml.safe_load(fh)
        assert data["host"] == "http://octopi.local"
        assert data["api_key"] == "MY_API_KEY"
        assert data["timeout"] == DEFAULTS["timeout"]
        assert data["retries"] == DEFAULTS["retries"]

    def test_normalizes_host(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        result_path = init_config("myprinter.local:5000/", "KEY")
        with result_path.open() as fh:
            data = yaml.safe_load(fh)
        assert data["host"] == "http://myprinter.local:5000"

    def test_creates_parent_directories(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fake_home = tmp_path / "deep" / "nested" / "home"
        fake_home.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        result_path = init_config("octopi.local", "KEY")
        assert result_path.parent.is_dir()


# ===================================================================
# get_default_config_path
# ===================================================================


class TestGetDefaultConfigPath:
    """Tests for get_default_config_path()."""

    def test_returns_path_object(self) -> None:
        path = get_default_config_path()
        assert isinstance(path, Path)

    def test_ends_with_config_yaml(self) -> None:
        path = get_default_config_path()
        assert path.name == "config.yaml"
        assert path.parent.name == ".octoprint-cli"
