"""Kiln MCP server — MCPB entry point.

This file is intentionally tiny. The manifest's ``uv`` server type resolves
and installs ``kiln3d`` (declared in ../pyproject.toml) into an isolated
environment on first run, using platform-native wheels — this sidesteps the
compiled-dependency (pydantic, numpy, cryptography) portability problem that
a hand-vendored bundle would hit. Once ``kiln3d`` is installed, this script
just hands off to the real entry point.

``kiln.server.main`` guards everything it does, but it cannot guard its own
import — and this is the door where a half-resolved install is most likely,
because the install happens on first run and this line is the first thing to
touch it.  A failure here looks identical to the user: Claude Desktop says
the server failed to start and nothing says why.  So the import gets the
same breadcrumb and the same recovery server as every other door.
"""

import sys

if __name__ == "__main__":
    try:
        from kiln.server import main
    except Exception as exc:  # noqa: BLE001
        try:
            from kiln import startup_failure
        except Exception:  # noqa: BLE001
            # ``kiln`` itself is not importable, so there is nothing left
            # to explain it with.  Let the traceback stand — the MCPB
            # runtime surfaces install failures on its own.
            raise exc from None
        diagnosis = startup_failure.handle(exc, phase="importing the server")
        breadcrumb = startup_failure.breadcrumb_path()
        startup_failure.serve_safe_mode(
            diagnosis, breadcrumb if breadcrumb.is_file() else None
        )
        sys.exit(1)

    main()
