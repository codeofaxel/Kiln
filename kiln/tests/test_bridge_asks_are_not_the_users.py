"""Every bridge's ask of Kiln's servers says the person did not make it.

A bridge (``kiln/_pro_*_bridge.py``) fetches something extra on its own --
the motion plan behind a home, the blade status behind a pre-flight, the
cost intelligence behind an estimate -- so a signed-out install refusing it
is not a person reaching for a feature.  ``kiln.daily_stats.record_account_wall``
counts exactly that reach, so every served call a bridge makes passes
``_asked_by_user=False`` and :func:`kiln.server._pro_api_call` skips the
count.  This pins the wiring for every bridge that asks the servers, a new
one included; ``test_closest_filament_column.py::TestNobodyAskedForTheColumn``
pins what the flag does.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_PACKAGE = Path(__file__).resolve().parent.parent / "src" / "kiln"
_BRIDGES = sorted(_PACKAGE.glob("_pro_*_bridge.py"))


def _served_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) == "_pro_api_call"
    ]


#: The bridges that ask Kiln's servers.  The others (the fault and guide
#: bridges) answer only from kiln-pro on this computer and send nothing.
_SERVED = sorted(path for path in _BRIDGES if _served_calls(path))


def test_every_served_bridge_on_disk_is_seen():
    assert {path.stem for path in _SERVED} >= {
        "_pro_colour_bridge",
        "_pro_cost_bridge",
        "_pro_cutter_bridge",
        "_pro_motion_bridge",
        "_pro_nozzle_bridge",
        "_pro_placement_bridge",
    }, [path.stem for path in _SERVED]


@pytest.mark.parametrize("path", _SERVED, ids=lambda p: p.stem)
def test_a_bridge_never_counts_its_ask_as_a_persons(path):
    for call in _served_calls(path):
        flags = [kw.value for kw in call.keywords if kw.arg == "_asked_by_user"]
        assert len(flags) == 1, f"{path.name}:{call.lineno} does not say who asked"
        flag = flags[0]
        assert isinstance(flag, ast.Constant) and flag.value is False, (
            f"{path.name}:{call.lineno} must pass _asked_by_user=False"
        )
