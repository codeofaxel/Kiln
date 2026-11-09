"""Kiln's servers' joint sweeps, answered inside the test run.

Public Kiln asks Kiln's servers to pose an assembly's moving joints through
their ranges (:mod:`kiln._pro_assembly_bridge`); the sweep itself is Kiln
Pro's.  The tests here exercise what public Kiln does with the answer, so the
answer is stubbed in: :func:`served_sweeps` replaces the bridge's request with
a table, and :func:`canned_sweep` reads answers recorded from Kiln's servers
for the stage tests' own hinge.
"""

from __future__ import annotations

import contextlib
import json
import os

_SWEEP_ANSWERS = os.path.join(os.path.dirname(__file__), "fixtures", "joint_sweep", "hinge_answers.json")


@contextlib.contextmanager
def served_sweeps(answers: dict, pieces: dict | None = None):
    """Kiln's servers' joint sweeps answered here, never over a network:
    *answers* maps ``(part_a, part_b, range_max)`` to the sweep they give (a
    ``MotionSweep`` as served, e.g. one of :func:`canned_sweep`'s), and a
    joint missing from it is a joint the servers gave no sweep for.
    *pieces* is their answer to which parts print as one piece when a compose
    asks (``None``: they gave none)."""
    from kiln import _pro_assembly_bridge as bridge

    def ask(parts_by_id, interfaces):
        out = {}
        for i, iface in enumerate(interfaces):
            motion = getattr(iface, "motion", None)
            if motion is None:
                continue
            key = (iface.part_a_id, iface.part_b_id, float(motion.range_max))
            if key in answers:
                out[i] = (answers[key], None)
        return out, None

    def ask_both(parts_by_id, interfaces, *, sweeps=True, pieces=False):
        joints, miss = ask(parts_by_id, interfaces) if sweeps else ({}, None)
        return joints, miss, (answered if pieces else None)

    answered = pieces
    real, real_both = bridge._sweep_all, bridge._assembly_answers
    bridge._sweep_all, bridge._assembly_answers = ask, ask_both
    try:
        yield answers
    finally:
        bridge._sweep_all, bridge._assembly_answers = real, real_both


def canned_sweep(name: str) -> dict:
    """A sweep as Kiln's servers gave it for the stage tests' hinge (frame
    and leaf placed at 30, 20, 5; ``hinge_0_170`` binds where the leaf meets
    the plate, ``hinge_0_120`` clears its range)."""
    with open(_SWEEP_ANSWERS, encoding="utf-8") as fh:
        return json.load(fh)[name]
