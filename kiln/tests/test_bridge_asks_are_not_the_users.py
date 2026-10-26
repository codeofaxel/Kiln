"""Every bridge's ask of Kiln's servers says the person did not make it.

A bridge (``kiln/_pro_*_bridge.py``) fetches something extra on its own --
the motion plan behind a home, the blade status behind a pre-flight, the
cost intelligence behind an estimate -- so a signed-out install refusing it
is not a person reaching for a feature.  ``kiln.daily_stats.record_account_wall``
counts exactly that reach, so every served call a bridge makes passes
``_asked_by_user=False`` and :func:`kiln.server._pro_api_call` skips the
count.  This pins the wiring for every bridge that asks the servers, a new
one included, and :class:`TestTheFlag` pins what the flag does.

A bridge that carries the person's OWN request -- it runs only because they
asked for exactly what it fetches -- says so with ``_asked_by_user=True`` and
is named in :data:`_PERSONS_OWN_ASK` with the reason.  Either way the flag is
written out: a bridge never leaves who asked to the default.
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

#: Bridges whose served call IS the person's ask, with why.
_PERSONS_OWN_ASK = {
    "_pro_material_rules_bridge": (
        "runs only when the person names the rules on check_material_environment; "
        "the per-requirement verdict is the feature they reached for"
    ),
    "_pro_fleet_fill_bridge": (
        "runs only when the person asks to plan copies of a part across their printers "
        "on Kiln Business; the plan is the feature they reached for"
    ),
    "_pro_fleet_materials_bridge": (
        "runs only when the person asks a material inventory door about several "
        "machines on Kiln Business; the cross-machine answer is what they reached for"
    ),
    "_pro_material_pick_bridge": (
        "runs only when the person asks what material a part should be, on a "
        "plan that includes Kiln Pro; the pick is the feature they reached for"
    ),
    "_pro_orientation_bridge": (
        "runs only when the person asks how to lay a part on the bed, on a plan "
        "that includes Kiln Pro; the ranking is the feature they reached for"
    ),
    "_pro_recovery_bridge": (
        "runs only when the person asks what to do about a failed print (a recovery "
        "plan, a fix plan, a failure analysis), on a plan that includes Kiln Pro; the "
        "plan is the feature they reached for"
    ),
    "_pro_load_bridge": (
        "runs only when the person asks estimate_structural_load for a part at "
        "Kiln Pro; the part's load figure is the feature they reached for"
    ),
    "_pro_ams_bridge": (
        "runs only when the person asks ams_filament_drying to START the dryer, and "
        "the start is refused without the check, so a signed-out refusal is the "
        "person meeting the sign-in wall"
    ),
}


#: Bridges whose one served call serves BOTH kinds of ask, and so forward the
#: flag the caller settled instead of stating one: the decoration bridge
#: carries a person's iterate / rollback / history (True, from the library's
#: tools) and the print-outcome record made on the way to something else
#: (False, from the learning tools).  The flag is the caller's parameter by
#: name, never a default, so the AST check below still sees who said it.
_CALLER_DECIDES = {
    "_pro_decoration_bridge": "asked_by_user",
}


def test_every_served_bridge_on_disk_is_seen():
    assert {path.stem for path in _SERVED} >= {
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
        if path.stem in _CALLER_DECIDES:
            assert isinstance(flag, ast.Name) and flag.id == _CALLER_DECIDES[path.stem], (
                f"{path.name}:{call.lineno} must forward the caller's {_CALLER_DECIDES[path.stem]!r}"
            )
            continue
        expected = path.stem in _PERSONS_OWN_ASK
        assert isinstance(flag, ast.Constant) and flag.value is expected, (
            f"{path.name}:{call.lineno} must pass _asked_by_user={expected}"
        )


def test_every_named_own_ask_is_a_served_bridge():
    assert set(_PERSONS_OWN_ASK) <= {path.stem for path in _SERVED}
    assert set(_CALLER_DECIDES) <= {path.stem for path in _SERVED}
    assert not set(_CALLER_DECIDES) & set(_PERSONS_OWN_ASK)


class TestTheFlag:
    """A signed-out install still gets the honest refusal, but an ask nobody
    made is not counted as a person reaching for the feature: the
    account-wall counter means a person asked
    (``kiln.daily_stats.record_account_wall``)."""

    def test_an_unasked_call_is_not_counted_as_an_account_wall(self, tmp_path, monkeypatch):
        import kiln.daily_stats as stats
        from kiln.server import _pro_api_call

        monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
        monkeypatch.delenv("KILN_API_URL", raising=False)
        monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
        counted: list[str] = []
        monkeypatch.setattr(stats, "record_account_wall", counted.append)

        quiet = _pro_api_call("cutter_wear_status", _asked_by_user=False, printer_id="default")
        assert quiet["code"] == "KILN_ACCOUNT_NOT_PAIRED"
        assert counted == []

        _pro_api_call("cutter_wear_status", printer_id="default")
        assert counted == ["cutter_wear_status"]
