"""Every door that takes geometry, on files the programs people use exported.

Every fixture Kiln was tested with until October 2026 was made by Kiln:
millimetres, resting on the bed, one welded body, nothing touching.  The first
real exports to reach it end to end, from Blender and Autodesk Fusion, broke
it a dozen ways with every test green (a metres file read as 0.088 mm, 62
pieces counted as 62,780, a seam-split glTF called open, "won't print" beside
grade A, an unknown printer answered INTERNAL_ERROR and retryable, a composed
multicolour file Kiln's own slice refused).

This sweep closes the class rather than the instances.  It runs every tool in
the live registry whose signature takes a path to geometry (found by name, so
a new door joins the day it registers) on every file in ``tests/real_exports/``
and checks each reply against the truth measured in the program that made the
file (``tests/_real_exports.py`` holds the corpus, the doors and the checks):

A. what a reply claims about the file (pieces, sealed, open edges, crossing
   faces, size) agrees with the authoring tool, and a well-formed export is
   never a crash, a retryable dead end, or called broken;
C. one reply tells one story: its verdict, score, grade and sentences agree,
   by the grade ladder Kiln's own printability check uses;
D. the same object in two files that differ only in what a slicer ignores
   (the unit; where the part sits) gets the same answer: the same structural
   risk counts too, unless the file in metres asks the units question and
   counts nothing;
E. a caller's mistake (a missing file, a file that is not a model, a printer
   that is not there, an output folder that does not exist) is refused, never
   INTERNAL_ERROR and never marked retryable;
F. no sentence of any reply, refusals included, says HOW Kiln worked its
   answer out: an internal name, a module or source path, a rule id, a
   method's weight or factor beside its number (defect #16).  kiln-pro's
   output-leak gate runs the same scanner over the replies Kiln's servers
   send.

Guard B (a composed print file the compose door says slices is sliced, here,
by the slicer Kiln drives) is the last test in this file.

Runs as a plain ``pip install kiln3d`` would: every ``kiln_pro`` import fails
while a door runs.  Tools a plain install only forwards to Kiln's servers are
recorded with that reason; kiln-pro's own corpus test runs the engines behind
them on the same files.

A failure names the door, the file, both values and, where its shape matches
one, the 2026-10-23 defect it is, so a red run reads as a merge checklist.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("trimesh", reason="the corpus doors read meshes with trimesh")

from tests import _real_exports as rx  # noqa: E402

pytestmark = pytest.mark.slow  # the whole registry over the whole corpus: minutes serial

CORPUS = rx.load_corpus()


def _cad_kernel() -> bool:
    from kiln.step_import import _ocp_available

    return _ocp_available()


#: Doors whose effect reaches past this machine, so the sweep never calls
#: them.  Each says what it would reach.
NOT_CALLED = {
    "delete_file": "deletes a file from a printer's own storage",
    "publish_model": "publishes to model marketplaces over the network",
    "create_print_service_order": "places an order with a print service",
    "fulfillment_quote": "asks a print service for a price over the network",
    "get_fulfillment_quote_cached": "asks a print service for a price over the network",
    "generate_texture": "sends the mesh to an image-generation provider",
}
#: A plain install's stand-ins for kiln-pro tools: they forward the call to
#: Kiln's servers.  The engine behind each runs in kiln-pro's corpus test.
SERVED_MODULE = "kiln.pro_stubs"

#: Values for the non-geometry inputs a door requires, so it can be called
#: headless.  A door that grows a required input nobody listed fails
#: ``test_every_door_can_be_called`` until it has a row: that is how a new
#: door is never quietly left out.
DOOR_INPUTS: dict[str, dict[str, Any]] = {
    "add_assembly_part": {"assembly_json": "{}", "part_id": "part_1"},
    "analyze_generation_feedback": {"original_prompt": "a donut"},
    "apply_decoration": {"name": "corpus"},
    "audit_original_design": {"requirements": "holds a donut"},
    "cache_design_with_source": {"scad_source": "cube(1);"},
    "cache_model": {"source": "local"},
    "decorate_surface": {"content": "HI"},
    "extract_plate_object": {"object_name": "Donut"},
    "improve_generation_prompt": {"original_prompt": "a donut"},
    "multi_copy_print": {"copies": 2},
    "plan_multi_copy_split": {"copies": 2},
    "print_plate_object": {"object_name": "Donut"},
    "reprint_with_material": {"material_id": "PLA"},
    "route_print_job": {"material": "PLA"},
    "save_decoration": {"name": "corpus"},
    "save_design_version": {"design_id": "corpus", "scad_source": "cube(1);"},
    "validate_design_for_requirements": {"requirements": "holds a donut"},
    "wrap_gcode_as_3mf": {"gcode_path": "/nonexistent/corpus.gcode"},
}


@pytest.fixture(scope="module")
def registry():
    from kiln import server

    return server.mcp


def _doors() -> list[rx.Door]:
    from kiln import server

    return rx.discover_doors(server.mcp, module_prefix="kiln.")


DOORS = _doors()
CALLED = [d for d in DOORS if d.name not in NOT_CALLED and d.module != SERVED_MODULE]


def _interface() -> frozenset[str]:
    """The names a sentence may say (guard F): public Kiln's tools and their
    parameters, the paid tools a plain install names, and the settings the
    code reads.  Public tools only, so the answer does not depend on whether
    kiln-pro happens to be installed beside this checkout."""
    import kiln
    from kiln import server

    package = Path(kiln.__file__).parent
    return rx.interface_words(server.mcp, package, module_prefixes=("kiln.",),
                              manifests=(package / "pro_tool_manifest.json",))


INTERFACE = _interface()

#: What guard F (#16) finds that is recorded for a decision rather than fixed
#: in the change that found it: ``(door, word) -> (why, ISO date)``.  A row
#: past its date fails, and so does a row the sweep no longer finds.
METHOD_SAID_DECIDED: dict[tuple[str, str], tuple[str, str]] = {}


def _sweep_files() -> list[str]:
    return [rel for rel in rx.SWEEP_FILES if not rx.needs_cad_kernel(rel) or _cad_kernel()]


def _files_for(door: rx.Door, rel: str, work: Path) -> dict[str, Path]:
    """Each geometry parameter's file: a two-file door compares a real revision."""
    if len(door.geometry_params) > 1 and rel == rx.REVISION_PAIR[0]:
        pair = rx.REVISION_PAIR * len(door.geometry_params)
        return {p: rx.stage(r, work / f"in{i}") for i, (p, r) in enumerate(zip(door.geometry_params, pair, strict=False))}
    staged = rx.stage(rel, work / "in")
    return {p: staged for p in door.geometry_params}


def _run(registry, door: rx.Door, files: dict[str, Path], work: Path,
         extra: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """One headless call as a plain install: the arguments it was handed, and its reply."""
    args = rx.build_args(door, files, work, {**DOOR_INPUTS.get(door.name, {}), **(extra or {})})
    with rx.without_kiln_pro():
        return args, rx.call(registry, door.name, args)


def _method_said(door: str, case: str, reply: dict[str, Any], args: dict[str, Any],
                 seen: set[str]) -> list[str]:
    """Guard F for one reply, less what ``METHOD_SAID_DECIDED`` records (each
    word it finds is added to *seen*, for the stale check)."""
    out = []
    for where, kind, said, word in rx.method_said(reply, interface=INTERFACE, callers_words=args):
        if (door, word) in METHOD_SAID_DECIDED:
            seen.add(word)
            continue
        out.append(f"{door}({case}): {where} says \"{said}\" ({kind.replace('_', ' ')}) {rx._defect(16)}")
    return out


def _decided_rows_hold(door: str, seen: set[str]) -> list[str]:
    """Each decided row for *door* is still found, and inside its date."""
    import datetime

    out = []
    for (name, word), (why, by) in sorted(METHOD_SAID_DECIDED.items()):
        if name != door:
            continue
        if word not in seen:
            out.append(f"{door}: METHOD_SAID_DECIDED row for {word!r} is STALE -- the sweep no longer finds it")
        elif datetime.date.today() > datetime.date.fromisoformat(by):
            out.append(f"{door}: METHOD_SAID_DECIDED row for {word!r} is PAST DUE ({by}): {why}")
    return out


def test_the_sweep_found_the_doors():
    """A rule that finds nothing proves nothing: the doors a real export meets
    first must be in the sweep, and the reasons cover every door left out."""
    names = {d.name for d in CALLED}
    for core in ("analyze_printability", "diagnose_mesh", "validate_and_prepare_mesh",
                 "slice_model", "repair_mesh", "import_step_file"):
        assert core in names, f"{core} is no longer found by the geometry-parameter rule"
    left_out = {d.name for d in DOORS} - names
    unexplained = {n for n in left_out if n not in NOT_CALLED
                   and next(d for d in DOORS if d.name == n).module != SERVED_MODULE}
    assert not unexplained, unexplained


def test_every_door_can_be_called():
    """Every required input of every called door has a value to call it with."""
    missing = {
        d.name: [p for p in d.required if p not in d.geometry_params and p not in DOOR_INPUTS.get(d.name, {})]
        for d in CALLED
    }
    missing = {k: v for k, v in missing.items() if v}
    assert not missing, (
        f"doors with required inputs the corpus sweep cannot fill: {missing}; "
        "add a row to DOOR_INPUTS (or to NOT_CALLED, with what the door would reach)"
    )


def test_the_corpus_truth_is_sane():
    """The truth files describe what the files hold (pieces, closed, sizes)."""
    assert CORPUS["donut_m.stl"]["piece_count"] == CORPUS["donut_mm.stl"]["piece_count"] == 8
    assert CORPUS["donut_m.stl"]["unit"] == "m" and CORPUS["donut_mm.stl"]["unit"] == "mm"
    assert CORPUS["knob_below_bed_mm.stl"]["rests_on_bed"] is False
    assert CORPUS["knob_on_bed_mm.stl"]["rests_on_bed"] is True
    assert CORPUS["assembly_3_bodies.step"]["piece_count"] == 3
    assert all(t["all_closed"] for t in CORPUS.values())


@pytest.mark.parametrize("door", CALLED, ids=lambda d: d.name)
def test_door_on_every_real_export(registry, door, tmp_path):
    """Guards A, C and D for one door, over the whole corpus."""
    ladder = rx.Ladder.from_kiln()
    replies: dict[str, dict[str, Any]] = {}
    findings: list[str] = []
    seen: set[str] = set()
    for rel in _sweep_files():
        work = tmp_path / Path(rel).stem
        args, reply = _run(registry, door, _files_for(door, rel, work), work)
        replies[rel] = reply
        findings += rx.check_crash(door.name, rel, CORPUS[rel], reply)
        findings += rx.check_truth(door.name, rel, CORPUS[rel], reply)
        findings += rx.check_story(door.name, rel, reply, ladder)
        findings += _method_said(door.name, rel, reply, args, seen)
    for rel_a, rel_b, kind in rx.SAME_OBJECT_PAIRS:
        if rel_a in replies and rel_b in replies:
            findings += rx.check_pair(door.name, kind, rel_a, replies[rel_a], rel_b, replies[rel_b])
    findings += _decided_rows_hold(door.name, seen)
    assert not findings, f"{door.name}: {len(findings)} finding(s) on real exports\n  " + "\n  ".join(findings)


@pytest.mark.parametrize("door", CALLED, ids=lambda d: d.name)
def test_a_callers_mistake_is_refused_not_retried(registry, door, tmp_path):
    """Guard E: a missing file, a file that is not a model, a printer that is
    not there, an output folder that does not exist -- each a refusal, and no
    refusal says how Kiln works inside (guard F)."""
    findings: list[str] = []
    for case, spec in rx.caller_mistakes(door, tmp_path / "cases").items():
        work = tmp_path / re.sub(r"\W+", "_", case)
        args, reply = _run(registry, door, spec["files"], work, spec["extra"])
        findings += rx.check_refusal(door.name, case, reply)
        findings += _method_said(door.name, case, reply, args, set())
    assert not findings, f"{door.name}: {len(findings)} caller mistake(s) not refused\n  " + "\n  ".join(findings)


# ---------------------------------------------------------------------------
# Guard B: a composed print file is sliced by the slicer Kiln drives
# ---------------------------------------------------------------------------


def _have_slicer() -> bool:
    try:
        from kiln.slicer import find_slicer

        find_slicer()
        return True
    except Exception:  # noqa: BLE001 -- no slicer on this machine
        return False


@pytest.mark.parametrize("set_name", sorted(rx.MULTICOLOUR_SETS))
def test_a_composed_multicolour_file_slices_in_the_slicer_kiln_drives(registry, set_name, tmp_path):
    """Compose the touching pieces of a multicolour export into one 3MF and
    slice it with Kiln's own slice door for the same printer, as a plain
    install.  The message counts colours, and the file slices."""
    if not _have_slicer():
        pytest.skip("no slicer on this machine: a composed file cannot be sliced here")
    findings = rx.check_compose_and_slice(registry, set_name, CORPUS, tmp_path, posture=rx.without_kiln_pro)
    assert not findings, f"{set_name}: {len(findings)} finding(s)\n  " + "\n  ".join(findings)


def test_guard_f_finds_a_name_planted_in_a_real_doors_sentence(registry, tmp_path):
    """Guard F on a real door's real reply: the sentence it says passes, and
    the same sentence with an internal name, a module path or a method's
    number planted in it fails.  A guard that cannot fail on a real reply
    proves nothing about the sweep."""
    door = next(d for d in CALLED if d.name == "analyze_non_manifold_edges")
    args, reply = _run(registry, door, _files_for(door, "knob_on_bed_mm.stl", tmp_path), tmp_path)
    said = reply.get("message")
    assert isinstance(said, str) and said, reply
    assert rx.method_said(reply, interface=INTERFACE, callers_words=args) == []
    for planted, kind in ((" Held by _HELD_STRETCH_THIN.", "internal_name"),
                          (" Graded in kiln.printability._score_to_grade.", "internal_name"),
                          (" The edges carry assigned_weight.", "internal_name"),
                          (" Rule edge_count_v2 applied.", "internal_name"),
                          (" (weight 0.80, delta +0.10mm)", "method_number"),
                          (" (readiness=62, min=75)", "method_number")):
        found = rx.method_said({**reply, "message": said + planted}, interface=INTERFACE, callers_words=args)
        assert any(k == kind for _w, k, _s, _word in found), (planted, found)


# ---------------------------------------------------------------------------
# The checks can fail, and can pass: each defect's shape, and its fixed shape
# ---------------------------------------------------------------------------


class TestTheChecksThemselves:
    """A guard that cannot fail is theatre, and one that cannot pass is noise.
    Each check is handed the reply shape a 2026-10-23 defect produced and the
    shape its fix produces, with no registry and no slicer."""

    DONUT_M = CORPUS["donut_m.stl"]
    GLB = CORPUS["donut.glb"]

    def test_won_t_print_beside_a_passing_grade_is_one_story_too_many(self):
        ladder = rx.Ladder.from_kiln()
        broken = {"verdict": "wont_print", "report": {"printable": False, "score": 98, "grade": "A"},
                  "message": "Won't print as it is: it hangs below the bed.  Printability score 98/100 (grade A)."}
        fixed = {"verdict": "wont_print", "report": {"printable": False, "score": 40, "grade": "F"},
                 "message": "Won't print as it is: it hangs below the bed."}
        assert any("defect #1" in f for f in rx.check_story("door", "f.stl", broken, ladder))
        assert rx.check_story("door", "f.stl", fixed, ladder) == []

    def test_a_score_in_the_sentence_is_caught_and_the_verdict_alone_is_not(self):
        ladder = rx.Ladder.from_kiln()
        report = {"printable": False, "score": 50, "grade": "F"}
        for said in ("Prints with supports (overhangs=12.0%).  Printability score 50/100 (grade F).",
                     "Estimated 1h 3m, 20g PLA. Printability: F (50/100).",
                     "Not ready (printability 50/100). 1 issue: thin walls"):
            reply = {"verdict": "prints_with_supports", "report": report, "message": said}
            assert any("defect #14" in f for f in rx.check_story("door", "f.stl", reply, ladder)), said
        plain = {"verdict": "prints_with_supports", "report": report,
                 "message": "Prints with supports (overhangs=12.0%)."}
        assert rx.check_story("door", "f.stl", plain, ladder) == []

    def test_a_structural_score_in_a_sentence_is_caught_and_the_risks_alone_are_not(self):
        ladder = rx.Ladder.from_kiln()
        plan = {"overall_structural_score": 45, "structural_grade": "F", "critical_count": 1, "warning_count": 3}
        # What the structural doors said until 2026-10-23, in each place a person reads it.
        for said in ({"summary": "Found 4 structural risk(s) (1 critical, 3 warning). Score: 45/100 (F)."},
                     {"summary": "Applied 1 reinforcement(s): add_base. Score improved 45 → 100 (F → A)."},
                     {"summary": "Tested 9 variants of Shelf Bracket. Best score: 100/100 (A)."},
                     {"special_notes": ["Structural grade F — using high infill and perimeters."]},
                     {"next_steps": "STL ready at x.stl. Structural grade: F. Recommended: 4 perimeters."},
                     {"gates": [{"name": "structural_integrity",
                                 "message": "Structural score 45/100 (F). 1 critical risk(s) found."}]}):
            found = rx.check_story("door", "f.stl", {**plan, **said}, ladder)
            assert any("same class as defect #14" in f for f in found), said
        plain = {**plan,
                 "summary": "1 critical structural risk and 3 warnings: a tipping risk (critical), 3 sharp "
                            "inside corners. First, widen the base or add a brim. 4 reinforcements recommended.",
                 "special_notes": ["With 1 critical structural risk and 3 warnings, using high infill and "
                                   "perimeters to compensate for geometry weaknesses."],
                 # The orientation gate said "improves score by 12.0%" until 2026-10-23, a score too.
                 "gates": [{"name": "orientation_efficiency",
                            "message": "Turn it 90° about Y before slicing: it stands lower (80.0 → 10.0 mm)."},
                           {"name": "design_requirements", "message": "1/2 checks passed."}]}
        assert rx.check_story("door", "f.stl", plain, ladder) == []

    def test_any_other_score_in_a_sentence_is_caught_and_a_measurement_is_not(self):
        ladder = rx.Ladder.from_kiln()
        # What the orientation, material and printer-health doors said until 2026-10-23,
        # at the depth each sits, and the other shapes a score takes in a sentence.
        for said in ({"message": "Best orientation: rotate X=0.0deg Y=90.0deg (score 100.0).  "
                                 "Original score: 62.0.  Improvement: 61.3%."},
                     {"gates": [{"name": "orientation_efficiency",
                                 "message": "Best orientation improves score by 12.3%."}]},
                     {"recommendation": {"reasoning": "PETG scores highest for 'strong' intent "
                                                      "(primary attribute: strength). Score: 62.6/100."}},
                     {"recommendation": {"reasoning": "POOR FIT ON HAND: TPU suits 'flexible' materially "
                                                      "better (68.5/100 vs 48.0/100) but you don't have it."}},
                     {"trend_report": {"alerts": [{"message": "Printer health score is 0%. "
                                                              "Recommend maintenance inspection."}]}},
                     {"summary": "Ready to print, grade B."},
                     {"next_steps": ["The new pose is 41% better."]}):
            found = rx.check_story("door", "f.stl", said, ladder)
            assert any("same class as defect #14" in f for f in found), said
            assert rx.scores_said(said), said
        plain = {"message": "Turn it 90° about Y before slicing: it needs less support (~12,000 → ~500 mm3), "
                            "less of it overhangs (19.0% → 4.2% of its surface) and it stands lower "
                            "(80.0 → 10.0 mm).",
                 "summary": "Estimated ~70 min with an 8 mm brim; 3 parts; 1/2 checks passed.",
                 "recommendation": {"reasoning": "PETG is the best fit for a strong part: against PLA+, it is "
                                                 "stronger. PLA+, the next best, costs less (~$20/kg against "
                                                 "~$22/kg)."},
                 "trend_report": {"alerts": [{"message": "This printer needs attention: only 40% of its 12 "
                                                         "prints in the last 30 days succeeded."}]},
                 "warnings": ["Use a food-grade PETG.", "The part scores under the minimum this check was "
                              "asked to hold (min_printability_score=60)."],
                 "upgrade_hint": "Upgrade to Kiln Pro: https://kiln3d.com/pricing"}
        assert rx.check_story("door", "f.stl", plain, ladder) == []
        assert rx.scores_said(plain) == []

    def test_pieces_that_touch_the_part_are_never_called_floating_scrap(self):
        donut = CORPUS["donut_mm.stl"]
        for broken in ({"report": {"has_floating_fragments": True}}, {"fusion": {"floating_bodies": 7}},
                       {"has_floating_parts": True}):
            assert any("defect #13" in f for f in rx.check_truth("door", "donut_mm.stl", donut, broken)), broken
        fixed = {"report": {"has_floating_fragments": False}, "fusion": {"floating_bodies": 0}}
        assert rx.check_truth("door", "donut_mm.stl", donut, fixed) == []

    def test_touching_pieces_each_closed_are_a_closed_file(self):
        donut = CORPUS["donut_mm.stl"]
        broken = {"passed": False, "result": {"is_manifold": False}}
        assert rx.check_truth("door", "donut_mm.stl", donut, broken)
        assert rx.check_truth("door", "donut_mm.stl", donut, {"result": {"is_manifold": True}}) == []

    def test_a_grade_the_ladder_would_not_give_is_caught(self):
        ladder = rx.Ladder.from_kiln()
        reply = {"report": {"score": 95, "grade": "C", "printable": True}}
        assert rx.check_story("door", "f.stl", reply, ladder)

    def test_a_metres_file_read_as_millimetres_must_say_so(self):
        silent = {"dimensions_mm": {"x": 0.088, "y": 0.088, "z": 0.025}}
        said = {**silent, "warnings": ["This looks like a file in metres: it is 1000x too small."]}
        converted = {"dimensions_mm": {"x": 88.0, "y": 88.0, "z": 24.6}}
        assert any("defect #2" in f for f in rx.check_truth("door", "donut_m.stl", self.DONUT_M, silent))
        assert rx.check_truth("door", "donut_m.stl", self.DONUT_M, said) == []
        assert rx.check_truth("door", "donut_m.stl", self.DONUT_M, converted) == []

    def test_pieces_and_seams_are_checked_against_the_authoring_tool(self):
        broken = {"report": {"component_count": 62780, "is_watertight": False, "hole_count": 21}}
        fixed = {"report": {"component_count": 8, "is_watertight": True, "hole_count": 0}}
        found = rx.check_truth("door", "donut.glb", self.GLB, broken)
        assert len(found) == 3 and all("defect #7" in f for f in found), found
        assert rx.check_truth("door", "donut.glb", self.GLB, fixed) == []

    def test_a_claim_about_something_else_is_not_a_claim_about_the_file(self):
        reply = {"repaired": {"component_count": 1}, "placement": {"score_if_placed": 90},
                 "zones": [{"watertight": False}]}
        assert rx.check_truth("door", "donut.glb", self.GLB, reply) == []

    def test_the_same_object_in_two_units_gets_the_same_counts(self):
        a, b = {"component_count": 62780}, {"component_count": 550}
        assert any("defect #3" in f for f in rx.check_pair("door", "unit", "m", a, "mm", b))
        assert rx.check_pair("door", "unit", "m", {"component_count": 8}, "mm", {"component_count": 8}) == []

    def test_structural_risks_on_a_metres_file_are_its_units_question_not_a_count(self):
        ladder = rx.Ladder.from_kiln()
        question = ("This model measures 0.088mm at its largest, which is not a printable size, and more "
                    "than one unit would explain it (meters → 88mm, inches → 2.2352mm).")
        risk = {"risk_type": "sharp_corner", "severity": "critical"}
        mm = {"risk_count": 21, "critical_count": 1, "warning_count": 20, "risks": [risk] * 21}
        # What the structural doors said about Blender's metres donut until
        # 2026-10-23, beside the units question the dispatcher added.
        for broken in ({"risk_count": 1, "critical_count": 1, "warning_count": 0, "risks": [risk],
                        "units_question": question},
                       {"recommendation_count": 1, "reinforcements": [{"reinforcement_type": "fillet"}],
                        "units_question": question},
                       {"gates": [{"name": "structural_integrity", "details": {
                           "risks": [risk], "overall_structural_score": 75, "critical_count": 1,
                           "warning_count": 0}}], "printability": {"units_question": question}},
                       {"summary": "Skipped 1: fillet. The part is as it was. 1 critical structural risk: a "
                                   "cluster of sharp edges (critical).", "units_question": question}):
            assert any("defect #15" in f for f in rx.check_story("door", "donut_m.stl", broken, ladder)), broken
        assert any("defect #15" in f for f in rx.check_pair(
            "door", "unit", "donut_m.stl", {**mm, "critical_count": 1, "warning_count": 0}, "donut_mm.stl", mm))
        # The fixed shape: the question, and no count; the millimetre file keeps its counts.
        fixed = {"risk_count": None, "critical_count": None, "warning_count": None, "risks": [],
                 "units_question": question}
        assert rx.check_story("door", "donut_m.stl", fixed, ladder) == []
        assert rx.check_pair("door", "unit", "donut_m.stl", fixed, "donut_mm.stl", mm) == []
        # No count for the metres file and no question either is a silence, not an answer.
        silent = {k: v for k, v in fixed.items() if k != "units_question"}
        assert any("defect #15" in f for f in rx.check_pair("door", "unit", "donut_m.stl", silent,
                                                            "donut_mm.stl", mm))

    def test_a_sentence_never_says_how_the_answer_was_worked_out(self):
        """Guard F (#16): each shape of a method said in a sentence, from the
        kiln-pro sentences fixed on 2026-10-23 and the names code has, is
        caught; each name the interface owns is not."""
        interface = frozenset({"repair_mesh", "radius_mm", "KILN_SKIP_PREVIEW_GATE", "viewer_url"})
        for said, kind, word in (
                ("Nozzle wear at 70% of its lifetime. Lifetime-wear contribution: 0.123.", "method_number",
                 "contribution"),
                ("the retry was held back: Print-ready (readiness=62, min=75)", "method_number", "readiness="),
                ("Predicted success 92%; +2.1 warp pts vs source branch", "method_number", "points"),
                ("Predicted success 92%; 2.1-pt warp reduction vs source branch", "method_number", "points"),
                ("the second version is 12 points closer to the reference", "method_number", "points"),
                ("1. Caliper measurement (weight 0.80, delta +0.10mm)", "method_number", "weight"),
                ("Penalty 5 for a full plate; threshold 0.5.", "method_number", "penalty"),
                ("Stress multiplier: 1.5 for ABS.", "method_number", "multiplier"),
                ("Held by _HELD_STRETCH_THIN.", "internal_name", "_HELD_STRETCH_THIN"),
                ("Ranked by WALL_STRETCH_LIMIT.", "internal_name", "WALL_STRETCH_LIMIT"),
                ("See kiln.job_router for the ranking.", "internal_name", "kiln.job_router"),
                ("Raised in printability.py on line 40.", "internal_name", "printability.py"),
                ("Read from kiln/src/kiln/data/materials.json.", "internal_name", "kiln/src/kiln/data/materials.json"),
                ("Offset set by caliper_direct_v1.", "internal_name", "caliper_direct_v1"),
                ("The part's assigned_weight is low.", "internal_name", "assigned_weight")):
            found = rx.method_said({"message": said}, interface=interface)
            assert (kind, word) in {(k, w) for _p, k, _s, w in found}, (said, found)
        # The person's numbers, a count, a tier, the interface and the caller's
        # own words: never a hit.
        plain = {"message": "Estimated 1h 3m, 24 g of PLA; an 8 mm brim; weight 24 g; ~70 min at 215 °C.",
                 "summary": "Prints with supports (overhangs=25.5%, bridges=3.8mm), 3 layers at z=4.0mm (N=8).",
                 "warnings": ["Its lowest point is 6.00 mm below the build plate.",
                              "A safety factor of 2.4 under that load; flow multiplier 0.95.",
                              "Overhangs past the 45° threshold need support; use 12 pt text.",
                              "Upgrade to Kiln Pro: https://kiln3d.com/pricing?from=report&min=75"],
                 "next_steps": ["Use repair_mesh with radius_mm=2-4, or set KILN_SKIP_PREVIEW_GATE=1 for CI.",
                                "Open viewer_url; the code is NO_SUCH_PRINTER.",
                                "Saved donut_mm_repaired.stl beside ~/.kiln/kiln.db and /tmp/kiln_x/out_3.stl.",
                                "edges.left_sharp says why; my_printer_2 is the printer you named."],
                 "error": {"code": "NO_SUCH_PRINTER"}, "edges": {"left_sharp": []}}
        assert rx.method_said(plain, interface=interface, callers_words={"printer_id": "my_printer_2"}) == []

    def test_where_a_part_sits_does_not_change_its_verdict(self):
        below = {"verdict": "wont_print", "report": {"score": 41, "grade": "F", "printable": False}}
        on_bed = {"verdict": "ready", "report": {"score": 92, "grade": "A", "printable": True}}
        assert any("defect #5" in f for f in rx.check_pair("door", "placement", "below", below, "on", on_bed))
        assert rx.check_pair("door", "placement", "below", on_bed, "on", on_bed) == []

    def test_a_callers_mistake_must_not_read_as_a_fault_to_retry(self):
        internal = {"success": False, "error": {"code": "INTERNAL_ERROR", "message": "Unknown printer model 'default'",
                                                "retryable": True}}
        generic = {"success": False, "error": {"code": "ERROR", "message": "Mirror failed: no such file",
                                               "retryable": True}}
        refused = {"success": False, "error": {"code": "INVALID_PRINTER", "message": "No printer called 'default'",
                                               "retryable": False}}
        busy = {"success": False, "error": {"code": "RATE_LIMITED", "message": "wait 2 s", "retryable": True}}
        assert any("defect #6" in f for f in rx.check_refusal("door", "printer_id='default'", internal))
        assert rx.check_refusal("door", "missing_file", generic)
        assert rx.check_refusal("door", "printer_id='default'", refused) == []
        assert rx.check_refusal("door", "missing_file", busy) == []

    def test_calling_a_well_formed_export_broken_is_caught_but_the_cad_refusal_is_not(self):
        lie = {"success": False, "error": {"code": "INVALID_ARGS", "retryable": False,
                                           "message": "Not a valid binary STL: the header declares 1869181810 triangles"}}
        honest = {"success": False, "error": {"code": "INVALID_ARGS", "retryable": False,
                                              "message": "This is a STEP (CAD) file, not a mesh -- nothing is wrong with it."}}
        assert rx.check_crash("door", "donut.glb", self.GLB, lie)
        assert rx.check_crash("door", "enclosure_v1.step", CORPUS["enclosure_v1.step"], honest) == []
