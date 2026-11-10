"""A hinge's served answer keeps its upgrade line on the way to the stage.

Kiln's servers answer a moving joint's sweep on every plan; below the plan
that holds the rest of the answer, their reply carries an ``upgrade`` block
beside the verdict (a line, the plan, and a link on Kiln's own site; or, for
someone whose plan has ended, ``owned`` and how to get back in).  The stage
reads ``upgrade`` off the ``kiln.joints.v1`` block and shows the line once.

These tests stand in for the servers at the one served door
(``kiln.server._pro_api_call``) with kiln-pro removed from the import system,
compose through the real tool door, and read the block the stage would get.
The block is data from another machine: only its known keys travel, a link
off Kiln's own site drops it, and nothing about it changes the verdict.
"""

from __future__ import annotations

import builtins
import copy
import json
from pathlib import Path

import pytest

from kiln import joint_stage
from tests._served_sweep import canned_sweep
from tests.test_joint_stage import _compose, _hinge_motion, tools  # noqa: F401 -- the fixture

_FREE = {
    "line": "Kiln Pro judges this hinge for the filaments you print it in.",
    "url": "https://kiln3d.com/pricing?utm_source=agent",
    "tier": "pro",
}
_OWNED = {"line": "Your Kiln Pro sign-in has ended: sign back in.", "tier": "pro", "owned": True}


@pytest.fixture()
def servers(monkeypatch):
    """Kiln's servers as a plain install reaches them: the test's answer at
    the served door, and no kiln-pro on this computer."""
    real_import = builtins.__import__

    def no_kiln_pro(name, *args, **kwargs):
        if name.startswith("kiln_pro"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_kiln_pro)
    state: dict = {"upgrade": None}

    def call(tool, **kwargs):
        answer = {
            "schema": "joint_sweep_answer/1",
            "joints": [{"index": i, "sweep": canned_sweep("hinge_0_170"), "why_not": None}
                       for i in kwargs.get("sweep") or []],
        }
        if state["upgrade"] is not None:
            answer["upgrade"] = copy.deepcopy(state["upgrade"])
        return answer

    import kiln.server

    monkeypatch.setattr(kiln.server, "_pro_api_call", call)
    return state


def _block(tools, folder: Path, servers, upgrade) -> dict:  # noqa: F811
    servers["upgrade"] = upgrade
    folder.mkdir(parents=True, exist_ok=True)
    _reply, composed = _compose(tools, folder, _hinge_motion())
    block = joint_stage.stage_block(str(composed))
    assert block is not None and block["joints"], "the hinge reached the stage"
    return block


@pytest.mark.parametrize("upgrade", [_FREE, _OWNED, {**_OWNED, "url": "https://kiln3d.com/account/billing"}])
def test_a_served_upgrade_reaches_the_stage_block(tools, tmp_path, servers, upgrade):  # noqa: F811
    block = _block(tools, tmp_path, servers, upgrade)

    assert block["upgrade"] == upgrade


def test_the_upgrade_travels_beside_a_served_file_too(tools, tmp_path, servers):  # noqa: F811
    servers["upgrade"] = _FREE
    _reply, composed = _compose(tools, tmp_path, _hinge_motion())

    sent = json.loads(joint_stage.sidecar_for_mesh(str(composed)))

    assert sent["upgrade"] == _FREE


def test_the_upgrade_never_changes_the_verdict(tools, tmp_path, servers):  # noqa: F811
    with_line = _block(tools, tmp_path / "a", servers, _FREE)
    without = _block(tools, tmp_path / "b", servers, None)

    assert with_line["joints"] == without["joints"]


def test_a_caller_on_the_plan_gets_no_block(tools, tmp_path, servers):  # noqa: F811
    # At Kiln Pro and above the servers send no upgrade; none is made up here.
    block = _block(tools, tmp_path, servers, None)

    assert "upgrade" not in block


@pytest.mark.parametrize("upgrade", [
    {**_FREE, "url": "https://evil.example/pricing"},
    {**_FREE, "url": "http://kiln3d.com/pricing"},
    {**_FREE, "url": "https://kiln3d.com.evil.example/pricing"},
    {**_FREE, "url": "https://kiln3d.com/pricing\nX-Injected: 1"},
    {**_FREE, "line": ""},
    {**_FREE, "line": "x" * 5000},
    {**_FREE, "line": 7},
    {**_FREE, "tier": ["pro"]},
    {**_OWNED, "owned": "yes"},
    {"url": "https://kiln3d.com/pricing", "tier": "pro"},
    "Upgrade to Kiln Pro",
    ["line"],
])
def test_a_malformed_or_foreign_upgrade_is_dropped_and_the_answer_stands(tools, tmp_path, servers, upgrade):  # noqa: F811
    block = _block(tools, tmp_path, servers, upgrade)

    assert "upgrade" not in block
    assert block["joints"][0]["sweep"]["summary"]  # the verdict still reached the stage


def test_only_the_known_keys_travel(tools, tmp_path, servers):  # noqa: F811
    block = _block(tools, tmp_path, servers, {**_FREE, "script": "<script>", "price": 99})

    assert block["upgrade"] == _FREE


def test_a_block_read_from_another_machine_drops_a_bad_upgrade_and_keeps_the_joints(tools, tmp_path, servers):  # noqa: F811
    block = _block(tools, tmp_path, servers, _FREE)
    block["upgrade"] = {**_FREE, "url": "https://evil.example/"}

    checked = joint_stage.checked_block(block)

    assert checked is not None and checked["joints"] == block["joints"]
    assert "upgrade" not in checked
