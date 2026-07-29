"""In-process consumption of MCP tool results — one shape, one helper.

Display-contract tools return ``[Image, payload_dict]`` so MCP clients
render the preview inline.  Code that calls such a tool IN-PROCESS
(``start_print`` running ``preflight_check``, fleet wrappers fanning a
single-printer tool out, decoration pipelines) wants the payload dict —
and reading ``.get(...)`` straight off the composite crashes exactly
when the callee succeeds and attaches its preview.  One site defended
itself inline (``decoration/apply.py``); every other site was a latent
crash.  This helper is the single door: unwrap before consuming.

``tests/test_display_contract_return_shapes.py`` (both repos) flags any
in-process assignment from a composite-capable tool in a function that
never references this helper.
"""

from __future__ import annotations

from typing import Any


def unwrap_tool_result(result: Any) -> Any:
    """Return the payload dict from a tool result of either shape.

    A display-contract composite is ``[content..., payload_dict]`` with
    the dict last; a plain tool result passes through untouched.  Never
    raises: an unexpected shape comes back as-is, so error handling
    stays with the caller.
    """
    if isinstance(result, (list, tuple)):
        for item in reversed(result):
            if isinstance(item, dict):
                return item
    return result
