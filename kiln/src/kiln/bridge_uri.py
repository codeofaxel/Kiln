"""Windows ``kiln://`` protocol-handler entry point — the thing a browser click
actually launches.

``python -m kiln.bridge_uri "kiln://bridge/enable"`` is what the registry
command runs. It exists as its own module, rather than routing through
``kiln.cli.main bridge handle-uri``, because importing the full CLI costs ~1.9s
before a single line of bridge code runs — and a click that takes 12 seconds
to show "Connected" reads as broken. This module imports nothing but the
standard library at the top; the bridge commands are pulled in inside
:func:`main`, and they in turn import only the bridge, never ``kiln.server``.

Nobody is watching a console for this launch (the handler runs under
``pythonw``), so an unknown or garbage URI exits 0 and says nothing: an error
dialog would be the only visible result, and the wrong one.  For the same
reason a failure inside the enable is written to ``~/.kiln/bridge.log`` -- the
one place ``kiln bridge status`` already points at -- and still exits 0.
"""
from __future__ import annotations

import logging
import os
import sys

_LOG_FILE = os.path.expanduser("~/.kiln/bridge.log")


def _log_failure(uri: str, exc: BaseException) -> None:
    """Append the traceback to the bridge log.  Its own handler rather than
    ``logging.basicConfig``: that is a no-op once the root logger has any
    handler, which it does under a test runner, and a failure that cannot be
    logged in a test cannot be tested."""
    try:
        os.makedirs(os.path.dirname(_LOG_FILE), exist_ok=True)
        handler = logging.FileHandler(_LOG_FILE, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        log = logging.getLogger(__name__)
        log.addHandler(handler)
        try:
            log.error("kiln:// handler failed for %r", uri, exc_info=exc)
        finally:
            log.removeHandler(handler)
            handler.close()
    except Exception:  # noqa: BLE001 -- there is nowhere left to say so
        pass


def main(uri: str) -> None:
    try:
        from kiln.cli.bridge_commands import _enable_bridge, parse_bridge_uri

        if parse_bridge_uri(uri) == "enable":
            _enable_bridge()
    except Exception as exc:  # noqa: BLE001 -- windowless: log it, never a dialog
        _log_failure(uri, exc)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
