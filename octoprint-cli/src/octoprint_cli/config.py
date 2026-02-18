from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

DEFAULTS: dict[str, object] = {
    "host": "http://octopi.local",
    "api_key": "",
    "timeout": 30,
    "retries": 3,
}


def get_default_config_path() -> Path:
    """Return the default path to the CLI config file (~/.octoprint-cli/config.yaml)."""
    return Path.home() / ".octoprint-cli" / "config.yaml"


def _normalize_host(host: str) -> str:
    """Normalize a host URL by ensuring a scheme and stripping trailing slashes."""
    host = host.strip()
    if host and not re.match(r"^https?://", host, re.IGNORECASE):
        host = "http://" + host
    return host.rstrip("/")


def _load_config_file(config_path: Path) -> dict[str, object]:
    """Read and parse a YAML config file, returning an empty dict on any failure."""
    if not config_path.is_file():
        return {}
    try:
        with config_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if isinstance(data, dict):
            return data
        return {}
    except (yaml.YAMLError, OSError):
        return {}


def load_config(
    host: str | None = None,
    api_key: str | None = None,
    config_path: str | None = None,
) -> dict[str, object]:
    """Resolve configuration using a three-tier precedence hierarchy.

    Priority (highest first):
        1. Explicit parameters (*host*, *api_key*) passed directly (e.g. from CLI flags).
        2. Environment variables ``OCTOPRINT_HOST`` and ``OCTOPRINT_API_KEY``.
        3. Values read from the YAML config file at *config_path* (or the default location).

    Returns a dict with keys ``host``, ``api_key``, ``timeout``, and ``retries``.
    """
    # --- Layer 3: start with built-in defaults ---
    config: dict[str, object] = dict(DEFAULTS)

    # --- Layer 3 (cont.): merge config file on top of defaults ---
    path = Path(config_path) if config_path else get_default_config_path()
    file_values = _load_config_file(path)
    for key in ("host", "api_key", "timeout", "retries"):
        if key in file_values and file_values[key] is not None:
            config[key] = file_values[key]

    # --- Layer 2: environment variables override file values ---
    env_host = os.environ.get("OCTOPRINT_HOST")
    if env_host:
        config["host"] = env_host

    env_api_key = os.environ.get("OCTOPRINT_API_KEY")
    if env_api_key:
        config["api_key"] = env_api_key

    # --- Layer 1: explicit parameters take top priority ---
    if host is not None:
        config["host"] = host

    if api_key is not None:
        config["api_key"] = api_key

    # --- Post-processing ---
    config["host"] = _normalize_host(str(config["host"]))
    config["timeout"] = int(config["timeout"])  # type: ignore[arg-type]
    config["retries"] = int(config["retries"])  # type: ignore[arg-type]

    return config


def init_config(host: str, api_key: str) -> Path:
    """Create the config directory and write an initial config file.

    Returns the :class:`~pathlib.Path` to the newly created config file.
    """
    config_path = get_default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "host": _normalize_host(host),
        "api_key": api_key,
        "timeout": DEFAULTS["timeout"],
        "retries": DEFAULTS["retries"],
    }

    with config_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False)

    return config_path


def validate_config(config: dict[str, object]) -> tuple[bool, str | None]:
    """Validate a resolved configuration dict.

    Returns ``(True, None)`` when the config is valid, or
    ``(False, error_message)`` describing the first problem found.
    """
    host = config.get("host", "")
    if not isinstance(host, str) or not host:
        return False, "host is required"

    if not re.match(r"^https?://", host, re.IGNORECASE):
        return False, "host must start with http:// or https://"

    # Very light URL sanity check: scheme + at least one character of hostname.
    if not re.match(r"^https?://[^\s/]+", host, re.IGNORECASE):
        return False, "host does not appear to be a valid URL"

    api_key = config.get("api_key", "")
    if not isinstance(api_key, str) or not api_key.strip():
        return False, "api_key is required and must be non-empty"

    return True, None
