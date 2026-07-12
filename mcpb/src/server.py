"""Kiln MCP server — MCPB entry point.

This file is intentionally tiny. The manifest's ``uv`` server type resolves
and installs ``kiln3d`` (declared in ../pyproject.toml) into an isolated
environment on first run, using platform-native wheels — this sidesteps the
compiled-dependency (pydantic, numpy, cryptography) portability problem that
a hand-vendored bundle would hit. Once ``kiln3d`` is installed, this script
just hands off to the real entry point.
"""

from kiln.server import main

if __name__ == "__main__":
    main()
