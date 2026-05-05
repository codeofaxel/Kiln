# v1.1.0 Design Templates Quality Pass — Handoff

**Branch:** `wip/design-templates-rebuild-2026-05-05` (offline; not pushed)
**Created:** 2026-05-05
**Goal:** Make all 17 remaining design patterns 10/10 quality + finish prepping v1.1.0 for tag + PyPI publish.

This file is a complete handoff for an agent picking up where the previous session left off. Read top-to-bottom; all relevant context, decisions, and specific edits are recorded here so you don't have to re-derive any of it.

---

## TL;DR — what's left to do

1. **Quality-pass design_patterns.json** — drop 3 patterns, merge 1, fix specific bugs, add structural fields, cite sources. Target: all remaining patterns 9-10/10. **NOT** invented numbers — honest values with confidence levels.
2. **Manifest resync** — `python3 -m kiln_pro.generate_manifest --copy-to /Users/adamarreola/Kiln/kiln/src/kiln/` to fix the 300 vs 301 mirror divergence flagged in audit.
3. **Naming decision** — "design patterns" → "design templates" in user-facing copy (judges' verdict was rename in marketing only, leave code alone). Full rename scope and cost are documented in §"Naming question" below; user wanted to consider full rename if worth it.
4. **v1.1.0 ship sequence** — version bumps (kiln-pro pyproject 1.0.0 → 1.1.0 + `__init__.py` 1.0.0 → 1.1.0; public kiln pyproject 1.0.0 → 1.1.0; tighten kiln-pro `requires = "kiln3d>=1.1.0"`), tag v1.1.0 both repos, PyPI publish (kiln3d via twine; kiln-pro via private GitHub-token path; **run wheel-leak gate** per CLAUDE.md SME-content rule before kiln-pro publish), release-notes blog post.
5. **CHANGELOG drafts** — drafted inline in user conversation (search for "v1.1.0 release notes drafts" in transcript). Need correction for: actual tool names (`recommend_hole`, `design_hole_for_product`, `explain_calibration_for_design`, `apply_calibration_recommendations`), drop "Cura" claim (parser doesn't support it), add product_intelligence_tools + product_faces_tools sections, use 25/20/44 named convention.

---

## State (already shipped to main, both repos)

### kiln-pro main = `dfa6e2e6` (pushed)
- `b6e8f3a1` audit ship-readiness fixes (security/page coming-soon → live link, spend-caps Loading-forever bug, license.py TODO date, kiln-pro stale-count refresh)
- `09e91018` ChatViewModel LLM prompts to ground truth + script historical-counts
- `546c73a` kiln-stats skill: named-vs-total counting principle + ChatViewModel 47→44 named fix
- `38be694c` Merge wizardly-rosalind audit follow-ups
- `f8f7b581` Merge vigorous-banach (fulfillment float-coerce)
- `5170f259` + `02162eb` Merge same-bed-runbook + revert (file removed; vestigial in history)
- `6ac32c6c` Merge origin/main: Pro commercial-use frontend + design archive CLI
- `3bb877a5` pyproject.toml: sync version to 1.0.0 to match `__init__.py`
- `b1b2f2c9` tolerance_stack: accept "scholz_mean_shift" alongside "scholz" + regression test
- `2c4a5326` product_faces_tools: gate all 6 MCP tools behind check_pro
- `f3c24218` chore: delete patent draft prompt (provisional filed)
- `dfa6e2e6` kiln-desktop: SettingsView CLI count 218 → 220

### public Kiln main = `1e09541` (pushed)
- `ed90267` count refresh 763 → 795 / 756 → 788 / 215 → 218 / 12979 → 14103 (14 docs)
- `1f5e9a8` tier-clarity edits with full tolerance-stacking depth (whitepaper §6.2 + litepaper bullet)
- `c35678c` count refresh confirmation push
- `a2f9e93` cherry-pick 1f5e9a8 onto current main
- `956795b` slice_and_print: add Pro+ calibration overlay
- `1e09541` count refresh 218 → 220 + 14103 → 14105

### Branches preserved (handoff context)
- `feature/newsletter-signup-wip` (`a3a4e6a`) — preserved NewsletterSubscribe.astro component for future newsletter feature
- `wip/design-templates-rebuild-2026-05-05` (this branch) — handoff for design-templates quality pass

### Branches deleted (audit cleanup)
14 branches confirmed-redundant + force-deleted with citations on 2026-05-05. Documented in transcript.

---

## The audit findings (summary)

The previous session ran two audits on design_patterns.json:

### Audit 1 — ship-blocking code bugs (FIXED before this branch was created)
1. ✅ `slice_and_print` was missing the calibration overlay (`_maybe_overlay_calibration` was only called from `slice_model` and `reslice_with_overrides`). Pro+ users silently bypassed their own calibration on the most-used tool. **Fixed in `956795b`.**
2. ✅ `tolerance_stack_analysis(method="scholz_mean_shift")` was dead-on-arrival — plugin advertised the literal but engine dispatched on `"scholz"` only. **Fixed in `b1b2f2c9` — engine now accepts both strings.**
3. ✅ 6 product_faces_tools were ungated — listed as Pro+ in manifest but had zero `check_pro` calls. **Fixed in `2c4a5326`.**
4. ✅ Version drift kiln-pro pyproject `0.2.1` vs `__init__.py` `1.0.0`. **Fixed in `3bb877a5`.**

### Audit 2 — design_patterns.json quality pass (NOT YET ADDRESSED — this branch's job)

**Per-pattern scorecard (full version in transcript):**

| Pattern | Current score | Notes |
|---|---:|---|
| `cantilever_bracket` | 9/10 | Quantitative load math; "scale linearly" claim is wrong (stress with L, deflection with L³) |
| `watertight_container` | 9/10 | Vase-mode + wall-shells + food-safety nuance; missing annealing/hydrostatic test pressure |
| `gear` | 9/10 | Module/pressure-angle/backlash correct; missing helical alternative + center-distance formula |
| `heat_set_insert_boss` | 9/10 | Every rule has thermodynamic justification; missing specific insert table + iron temp |
| `snap_fit_cantilever` | 8/10 | Solid; arm_length_to_deflection_ratio: 5.0 stated without source (Bayer/MIT recommend 7-10) |
| `press_fit` | 8/10 | Good interference range; doesn't distinguish radial-press (bearing) vs axial-press (dowel) |
| `living_hinge` | 8/10 | Strong material gate; "thousands of cycles" is hand-wave; need thickness-vs-cycle curve |
| `enclosure_box` | 8/10 | Most parameters; doesn't cross-reference siblings; no IP-rating guidance |
| `ball_joint_socket` | 8/10 | Print-in-place bridge layer rule is gold; no torque/holding-force spec |
| `dovetail_joint` | 8/10 | Taper range and clearance correct; no anti-walk-out lock feature |
| `battery_compartment` | 8/10 | Real cell dimensions; no spring-contact source; no 18650 thermal/vent (real fire risk) |
| `hinge_pin` | 8/10 | Odd-knuckle-count rule is gold; no torque/swing-load spec |
| `latch_clasp` | 7/10 | Force range shippable; agent_guidance shorter than peers; no draw-latch vs cam-latch |
| `pcb_enclosure_standoff` | 7/10 | M2/M2.5/M3 hole table; no PCB thickness assumption (1.6mm standard); no DIN-rail variant |
| `pegboard_hook` | 7/10 | Real load capacities; **`peg_diameter_mm: 4.0` contradicts `agent_guidance` "Standard pegboard holes are 6.35mm"** |
| `threaded_connection` | 7/10 | Correct redirect to inserts; **`min_pitch_mm: 1.5` excludes M3-M5 standard pitches (0.5-0.8mm)** |
| `phone_stand` | 6/10 | Generic; **`max_device_weight_g: 500` is below most tablets**; no Qi/MagSafe variant |

### Patterns to drop (3) — DECIDED
- `cable_management_clip` (6/10) — schema break: uses string formulas (`"cable_radius_plus_0.5"`, `"cable_diameter_times_0.8"`) that break programmatic consumption. Add to tasks.md as "rebuild later with numeric schema."
- `gopro_mount` (7/10) — single-vendor compat; trademark borderline per CLAUDE.md (the trademark + cross-repo discipline section); no source citation. Add to tasks.md as "build generic two-prong quick-release mount later."
- `wall_mount_bracket` (7/10) — overlaps with `cantilever_bracket` with no decision rule; merge unique fields (counterbore_depth_mm, counterbore_diameter_mm, screw_hole_spacing_mm, load_rating_per_screw_kg, min_material_around_hole_mm) INTO cantilever_bracket. Update cantilever_bracket display_name to "Cantilever / Wall Mount Bracket". Update use_cases to include wall_mount_bracket's: `shelf_mounting`, `tool_holders`, `light_fixtures`, `speaker_mounts`, `curtain_rod_brackets`, `picture_hangers`, `equipment_mounting`. Drop wall_mount_bracket entry.

### Structural gaps across ALL 20 (now 17 after drops)
1. **Zero source citations** anywhere in the file. `grep -c "source\|citation\|reference\|dossier"` returns 0. Single biggest credibility risk.
2. **No `failure_modes` field** anywhere — failure info buried inline in `agent_guidance` strings.
3. **No `confidence`/`tier`/`provenance` field** — can't distinguish "tested on 50 prints" from "consensus advice."
4. **No cross-references** between patterns (e.g., `enclosure_box` should link to `pcb_enclosure_standoff`, `heat_set_insert_boss`, `latch_clasp`, `snap_fit_cantilever`, `watertight_container`).
5. **`load_tables.json` exists as a sibling** at `/Users/adamarreola/Kiln/kiln/src/kiln/data/design_knowledge/load_tables.json` but is never referenced from any pattern.
6. **No printer-class gating** — Bambu 0.4mm and CR-10 0.8mm have different feasibility envelopes.

---

## The plan — phased

### Phase 1: Drop + merge (low risk, ~15 min)
1. Drop `cable_management_clip` from `design_patterns.json`. No code references it (verified — see "Files NOT to touch" below).
2. Drop `gopro_mount` from `design_patterns.json`. No code references it.
3. Merge `wall_mount_bracket` into `cantilever_bracket`:
   - Add design_rules: `counterbore_depth_mm: 2.5`, `counterbore_diameter_mm: 8.5`, `screw_hole_spacing_mm: 30`, `load_rating_per_screw_kg: 5`, `min_material_around_hole_mm: 4.0`
   - Add use_cases from wall_mount_bracket
   - Append wall_mount_bracket's agent_guidance bullets to cantilever_bracket's
   - Update display_name to "Cantilever / Wall Mount Bracket"
   - Drop wall_mount_bracket entry
4. Add tasks.md entries (in `/Users/adamarreola/Kiln-pro/tasks.md`, gitignored) for "rebuild cable_management_clip with numeric schema" and "build generic two-prong quick-release mount."

### Phase 2: Numerical fixes (medium risk, ~30 min)
1. **`phone_stand`**: `max_device_weight_g` 500 → 1500. Add `tablet_specific_min_base_depth_mm: 80`. Append agent_guidance bullet about Qi/MagSafe pocket variant. Cite source: empirical FDM consensus + Apple/Samsung phone+tablet weight specs.
2. **`threaded_connection`**: Split `min_pitch_mm: 1.5` into:
   - `printed_thread_min_pitch_mm: 1.5` (FDM-printable threads)
   - `fastener_thread_min_pitch_mm: 0.5` (threads cut by metal fastener; e.g., M3 = 0.5, M4 = 0.7)
   - Cite ISO 261:1998 metric thread standard.
3. **`pegboard_hook`**: `peg_diameter_mm: 4.0` → 6.0 (with 0.35mm clearance to standard 6.35mm pegboard hole). Update agent_guidance to remove the contradictory "Print pegs at 4.0mm" line. Add metric pegboard variant note (5mm holes; print pegs at 4.6mm).
4. **`cantilever_bracket`**: Replace agent_guidance line "4mm × 20mm cross-section supports ~5kg at 100mm length. Scale linearly for different loads." with: "For other dimensions, consult `load_tables.json` or use the bending formulas σ = 6PL/bh², deflection y = PL³/(3EI). Linear scaling is wrong for bending — stress scales with L, deflection with L³." Cite Shigley Ch. 4.

### Phase 3: Structural fields per pattern (medium risk, ~2-3 hours)

For each of the 17 remaining patterns, add four new top-level fields:

#### `failure_modes` array
Extract from existing `agent_guidance` strings into structured form:
```json
"failure_modes": [
  {
    "mode": "stress_concentration_at_base",
    "frequency": "very_common",
    "cause": "Sharp inside corner where the arm meets the body acts as a stress riser; cyclic flexing initiates a crack at the corner.",
    "prevention": "Add a 0.5mm-radius fillet at the arm base; use PETG/Nylon for repeated cycles."
  },
  {
    "mode": "layer_delamination_at_failure_plane",
    "frequency": "common_in_pla",
    "cause": "Layer lines along the arm length create a weak shear plane; PLA arms split along this plane after 2-3 cycles.",
    "prevention": "Print with arm in XY plane (orient so arm flexes parallel to bed). Avoid PLA for snap fits."
  }
]
```

#### `confidence` field — one of:
- `"empirical_kiln_tested"` — value validated under our testing program (currently NONE qualify; reserve for future)
- `"industry_consensus"` — value matches widely-published FDM/mechanical-engineering references and aligns with community consensus (most patterns)
- `"first_principles_estimate"` — derived from engineering formulas (Shigley, ASTM) and physically reasonable assumptions; not field-validated

**Be honest.** If a number was guessed, mark it `first_principles_estimate`. Do NOT label as `empirical_kiln_tested`.

#### `sources` array (per pattern)
Cite specific references this pattern's values came from. Examples:
- `"Bayer Snap-Fit Joint Design Manual (1995) — deflection ratios, stress-concentration mitigation"`
- `"Shigley & Mischke Mechanical Engineering Design 10th ed., Ch. 4 (beam bending)"`
- `"ISO 286-1:2010 — Geometrical Product Specifications"`
- `"McMaster-Carr Heat-Set Brass Insert Catalog (2024)"`
- `"Empirical FDM consensus (CNC Kitchen testing, MakersMuse video reviews)"`

#### `related_patterns` array (cross-refs)
Pattern names this one composes with. Example for `enclosure_box`:
```json
"related_patterns": ["pcb_enclosure_standoff", "heat_set_insert_boss", "latch_clasp", "snap_fit_cantilever", "watertight_container"]
```

### Phase 4: Per-pattern depth pass (research-heavy, optional for v1.1.0)

The audit identified specific gaps per pattern beyond the structural fixes. These are listed in the per-pattern scorecard above. Examples:

- `living_hinge`: add `cycle_count_curve` field (thickness vs cycle count). Sources: Polypropylene Living Hinge research (Polymer Engineering 2010).
- `ball_joint_socket`: add `holding_torque_n_m` field (PLA: 0.05-0.20 friction-fit; captured-ball variant for higher).
- `battery_compartment`: add `thermal_vent_required_for_18650: true` flag with vent_slot_width_mm spec. **18650 thermal runaway is a real fire risk — get this right.**
- `dovetail_joint`: add anti-walk-out lock feature description.
- `hinge_pin`: add `friction_torque_n_m` field; document removable vs captive variants.
- `pcb_enclosure_standoff`: add `pcb_thickness_assumption_mm: 1.6` (FR4 standard); note DIN-rail variant.
- `latch_clasp`: add cycle data per material; differentiate cam-latch vs draw-latch vs spring-latch.

**Recommendation**: skip Phase 4 for v1.1.0. Do Phases 1-3, ship 17 patterns with structural fields + honest confidence levels + corrected bugs. Tag Phase 4 as a v1.1.x or v1.2.0 effort.

### Phase 5: Update `_meta` block

Replace the existing thin `_meta` with a richer one:

```json
{
  "_meta": {
    "version": "2.0.0",
    "domain": "fdm",
    "description": "Functional design templates (formerly 'design_patterns') for FDM printing. Each template describes a common mechanical feature with material requirements, dimensional constraints, print orientation rules, structured failure modes, and source-cited engineering values.",
    "sources": [
      "Bayer 'Snap-Fit Joint Design' Engineering Polymers Design Guide (1995)",
      "Shigley & Mischke 'Mechanical Engineering Design' 10th ed. (Ch. 4 beams, Ch. 8 fasteners, Ch. 13 gears)",
      "ISO 286-1:2010 — Geometrical Product Specifications: ISO Code System for Tolerances",
      "ISO 261:1998 — ISO general purpose metric screw threads",
      "McMaster-Carr — Heat-Set Brass Threaded Insert specifications (catalog 2024)",
      "ASTM D790 — Standard Test Methods for Flexural Properties of Plastics",
      "Filament manufacturer technical datasheets (Bambu Lab, Prusament, Polymaker, eSun — 2024-2025)",
      "Empirical FDM community consensus (Hackaday, MakersMuse, CNC Kitchen video testing, r/3Dprinting)"
    ],
    "confidence_levels": {
      "industry_consensus": "Value matches widely-published references and community consensus.",
      "first_principles_estimate": "Derived from engineering formulas and reasonable assumptions; not field-validated.",
      "empirical_kiln_tested": "Validated under our testing program (reserved; not yet applied to any pattern)."
    },
    "audit_history": [
      "2026-04-09: Initial release (v1.0.0).",
      "2026-05-05: v2.0.0 — Added structured failure_modes, confidence, sources, related_patterns. Dropped 2 patterns (cable_management_clip schema-broken, gopro_mount trademark/single-vendor). Merged wall_mount_bracket into cantilever_bracket. Specific fixes: phone_stand max_device_weight_g, threaded_connection pitch split, pegboard_hook peg_diameter, cantilever_bracket scaling claim."
    ],
    "_split_note": "Safety-floor pattern data for AI-controlled FDM printing. Curated reasoning, decision trees, and SME narrative are available in Kiln Pro overlays. See https://kiln3d.com/pricing."
  },
  ...
}
```

---

## Naming question (judges' verdict + cost)

**Question:** rename "design patterns" → "design templates" everywhere?

### Why the user wants to rename
"Design patterns" carries software-engineering connotations (Gang of Four, OO patterns) that don't map to hardware. "Design templates" is more honest — these are starting points users customize, not patterns they recognize and apply.

### Judges' panel verdict
- Steve Jobs (89/100): rename in user-facing copy. "Design templates" tells the user immediately what the thing is.
- Jony Ive (92/100): rename in user-facing copy. Templates is honest; patterns carries OO baggage.
- antirez (76/100): **DO NOT rename code**. `design_patterns.json` is a file name. `find_design_patterns` is an MCP tool name (agent contract). Tests reference `design_patterns`. Renaming = days of churn for cosmetics.
- Marc Andreessen (94/100): rename in marketing only. "Refactoring an internal API for a marketing tweak is engineering self-indulgence."

**Verdict: marketing-only rename. Code stays.** Two-track: docs say "design templates"; code/JSON/MCP tools stay `design_patterns`.

### User's concern
"Two naming systems for same thing introduces room for confusion."

**Mitigation:** the AI translates between them at conversation time. Users don't typically see raw tool names; they see paraphrased AI responses. The "design templates" framing dominates user experience even if the API name stays `design_patterns`.

### If user wants the FULL rename (~1-2 hours, with risk)
Scope:
- Rename `design_patterns.json` → `design_templates.json` (1 file move)
- Update **26 code refs across 11 files**:
  - `kiln/src/kiln/design_intelligence.py` — `list_design_patterns()` function, `_load` helper, kind="design_patterns" parameter
  - `kiln/src/kiln/assembly.py` — `_load_design_patterns()`, `_JOINT_PATTERN_MAP`, multiple references
  - `kiln/src/kiln/skill_manifest.py` — agent-facing description strings
  - `kiln/src/kiln/tool_tiers.py` — tool tier registry
  - `kiln/src/kiln/plugins/design_tools.py` — MCP tool definitions: `find_design_patterns`, `list_design_patterns_catalog`, `list_design_patterns`
  - `kiln/src/kiln/server.py` — agent prompt strings
  - `kiln_pro/data_overlays.py` — overlay loader
- Update **3 tests files**: `test_design_tools.py`, `test_design_intelligence.py`, `test_material_data_sanity.py`
- Update **8 doc files**: README.md, LITEPAPER.md, WHITEPAPER.md, PROJECT_DOCS.md, llms.txt, FeatureGrid.astro, faq.astro, blog post
- **MCP tool renames are BREAKING** — `find_design_patterns` → `find_design_templates`. Any agent already wired to call the old names breaks. Mitigate with deprecation aliases on the old tool names that internally route to the new ones (~30 min extra).

### Recommended action
Marketing-only rename (~30 min, ~8 doc edits). Full rename = next session if user explicitly authorizes.

---

## Files to inspect

### Primary file (this branch's main work)
- `/Users/adamarreola/Kiln/kiln/src/kiln/data/design_knowledge/design_patterns.json` — 1056 lines, 21 entries (1 _meta + 20 patterns), 40KB

### Sibling files (worth knowing about)
- `/Users/adamarreola/Kiln/kiln/src/kiln/data/design_knowledge/load_tables.json` — currently unreferenced; integrate into cantilever_bracket and any pattern with load math
- `/Users/adamarreola/Kiln/kiln/src/kiln/data/design_knowledge/functional_requirements.json`
- `/Users/adamarreola/Kiln/kiln/src/kiln/data/design_knowledge/multi_material_pairing.json`
- `/Users/adamarreola/Kiln/kiln/src/kiln/data/design_knowledge/materials.json` — already has `_split_note` documenting the moat strip; design_patterns _meta should match that style

### Loader code (DON'T MODIFY)
- `/Users/adamarreola/Kiln/kiln/src/kiln/design_intelligence.py:418-1846` — `_load_design_patterns()` and `list_design_patterns()`. Loader handles the JSON dict structure. As long as JSON keys → pattern entries are preserved, loader works.
- `/Users/adamarreola/Kiln/kiln/src/kiln/assembly.py:427-591` — `_load_design_patterns()` (separate cache for assembly-side use) + `_JOINT_PATTERN_MAP` (joint_type shorthand → pattern key).

### MCP tool surface (DON'T MODIFY signatures unless user authorizes full rename)
- `/Users/adamarreola/Kiln/kiln/src/kiln/plugins/design_tools.py:580-615` — `list_design_patterns_catalog`, `find_design_patterns`. **These are agent contracts.** Renaming them is a breaking change.

### Tests to run after edits
```bash
cd /Users/adamarreola/Kiln-pro
python3 -m pytest /Users/adamarreola/Kiln/kiln/tests/test_design_intelligence.py -x -q
python3 -m pytest /Users/adamarreola/Kiln/kiln/tests/test_design_tools.py -x -q
python3 -m pytest /Users/adamarreola/Kiln/kiln/tests/test_material_data_sanity.py -x -q
python3 -m pytest /Users/adamarreola/Kiln/kiln/tests/test_assembly.py -x -q  # if it exists
```

If tests assert on specific patterns existing (e.g. wall_mount_bracket as a key), they'll break. Update test fixtures as needed. **Don't suppress test failures with skips** — fix the test or revert the change.

---

## Files NOT to touch / risks

### Risks of this branch
- **Don't push** until user authorizes. Branch is offline by design.
- **Don't bump versions** (kiln-pro pyproject, kiln pyproject, `__init__.py`) — those land in a separate v1.1.0 tag commit per user's stated process.
- **Don't tag** anything. The v1.1.0 tag waits until user explicit signoff.
- **Don't run PyPI publish.** That's the very last step.
- **Don't modify the kiln-desktop app** unless explicitly authorized. It's a SECRET WIP product per CLAUDE.md.
- **Don't leak internal-codename language** in commit messages or doc edits (see CLAUDE.md "Trademark + cross-repo discipline" — no "judges panel", "war room", "Steve says", "round 4", etc.).
- **Don't invent numbers**. If unsure, mark `confidence: "first_principles_estimate"` and cite the formula or reasoning. Fake citations are worse than no citations.
- **Don't push public Kiln main without running the public-Kiln pre-push hook** (audit_rls equivalent + doc-count check). The hook is automatic on `git push origin main`.
- **Don't push kiln-pro main without loading Supabase keys** from keychain: `. scripts/load_supabase_env.sh`. The pre-push gate audit_rls.py needs them.

### Prior-art preserved
- `wip/abandoned-baseline-experiment-2026-05-05` — was a stale 36-mod experiment from another session, since deleted (commit `b0eabb1` is unreachable now). User confirmed scrap.
- `feature/newsletter-signup-wip` — preserved NewsletterSubscribe.astro for future newsletter feature. Not on any merge path.

### Cross-repo discipline
- kiln-pro is private; public Kiln is open-source.
- KILN-031 is mentioned in kiln-pro source (CHANGELOG, calibration_pipeline_tools.py docstrings, etc.) but **NOT** in any committed public Kiln file. Provisional patent was filed 2026-05-05; can now safely describe the technique publicly without compromising novelty.
- `lessons_learned.md` in kiln-pro has incident records. Don't commit to public Kiln.

---

## v1.1.0 ship sequence (after this branch's work merges to main)

1. **Manifest resync**: `cd /Users/adamarreola/Kiln-pro && python3 -m kiln_pro.generate_manifest --copy-to /Users/adamarreola/Kiln/kiln/src/kiln/`. Commit both manifests if they changed.
2. **Version bumps** (in one commit each):
   - kiln-pro: `pyproject.toml` `1.0.0 → 1.1.0` AND `kiln_pro/__init__.py` `__version__ = "1.0.0" → "1.1.0"` AND tighten `requires = "kiln3d>=0.5.0" → "kiln3d>=1.1.0"`. Single commit.
   - public kiln: `kiln/pyproject.toml` `1.0.0 → 1.1.0`. Single commit.
3. **Push both repos.**
4. **Tag v1.1.0** in both repos: `git tag -a v1.1.0 -m "v1.1.0: Calibration Grounding + Engineering Toolkit"`. Then `git push origin v1.1.0` for each.
5. **Wheel-leak gate** before kiln-pro PyPI publish (per CLAUDE.md SME-content-on-disk rule):
   ```bash
   cd /Users/adamarreola/Kiln-pro
   python3 -m build --wheel
   python3 -c "
   import zipfile, glob
   wheel = glob.glob('dist/kiln_pro-*.whl')[0]
   with zipfile.ZipFile(wheel) as z:
       leaks = [n for n in z.namelist()
                if n.endswith(('.md', '.sql'))
                or 'generate_fasteners_doc' in n]
       assert not leaks, f'IP leaks in wheel: {leaks}'
   print('Clean')
   "
   ```
6. **PyPI publish**:
   - kiln3d: `cd /Users/adamarreola/Kiln/kiln && python3 -m build && twine upload dist/kiln3d-1.1.0*`
   - kiln-pro: private GitHub-token-gated path (see kiln-pro CLAUDE.md "Two-lock security")
7. **Auto-deploy Fly** if rest_api.py changed in this release window. Per memory: "Auto-deploy Fly after rest_api.py changes." (No rest_api.py changes in this v1.1.0 set per inspection, but verify.)
8. **Release notes blog post**: Astro file at `/Users/adamarreola/Kiln/docs/site/src/pages/blog/kiln-1-1-calibration-grounding.astro`. Drafts are inline in the user conversation transcript — search for "Kiln 1.1: Per-Printer Calibration Grounding". Apply count corrections (220 / 14,105 / 25 / 20 / 44) and tool-name corrections (`recommend_hole`, `design_hole_for_product`, `explain_calibration_for_design`, `apply_calibration_recommendations`, drop "Cura").

---

## Live counts (as of this handoff, 2026-05-05)

| Metric | Value |
|---|---|
| MCP capabilities | 795 |
| MCP tools | 788 |
| CLI commands | 220 |
| Tests collected (combined) | 14,105 |
| Named printer safety profiles | 44 (47 total keys − 3 non-named: `_meta`, `default`, `klipper_generic`) |
| Base materials | 25 (26 total keys − 1 `_meta`) |
| Brand-specific filament profiles | 52 |
| Design templates (after this branch's drops) | 17 (was 20; dropped cable_management_clip, gopro_mount, wall_mount_bracket) |
| Failure types (FailureType enum) | 10 |

**Convention** (decided 2026-05-05): use "named" counts everywhere, excluding `_meta`/metadata entries. Matches whitepaper's "44 named safety profiles" precedent.

---

## Specific user direction (preserved verbatim from transcript)

> "drop those 3, then. then fix the others to make them 10/10 completely great quality"

> "for 'My recommendation stands: 25 / 20 / 44. Honest, consistent with whitepaper, what users intuit when they ask "how many things does Kiln know about."' ok i agree then"

> "next up -- why not use design templates as the name everywhere? I'm unsure if 2 naming systems for the same thing introduces room for confusion, but it seems like it could"

> "make sure all your work thus far on this effort is committed to an offline feature branch the other agent can pick up on to continue your work"

---

## Suggested execution order for next agent

1. Read this handoff doc top to bottom.
2. Read the design_patterns.json file (1056 lines) to internalize current state.
3. Phase 1 (drop + merge) — safe mechanical edits.
4. Run tests after Phase 1 — confirm nothing broke.
5. Phase 2 (numerical fixes) — 4 specific edits.
6. Run tests again.
7. Phase 3 (structural fields) — 17 patterns × 4 new fields = 68 additions. Be honest with `confidence` levels. Cite real sources.
8. Run tests again.
9. **STOP and report to user.** Show the new file. Get sign-off before merging this branch to main.
10. After user signoff: merge `wip/design-templates-rebuild-2026-05-05` → main. Push.
11. Naming question (Phase 5 of plan): user will decide marketing-only-rename vs full-rename. If full rename, do it in a separate branch with deprecation aliases.
12. Manifest resync (one command).
13. v1.1.0 ship sequence.

---

## Open questions for the user (re-ask when picked up)

1. Confirm 17 patterns is the right count (after dropping 3 and merging 1 into another)?
2. Marketing-only rename "design templates" or full code rename? (Judges said marketing-only; user open to full if worth it.)
3. v1.1.0 vs 1.0.1 — judges said 1.1.0 (semver-honest); user can override.
4. Specific date for tag + PyPI publish?
5. Release-notes blog post — solo decision or judges-panel review again before posting?
6. Phase 4 (per-pattern depth pass) — do for v1.1.0 or defer to v1.1.x?

---

**End of handoff.** Sign off when this branch's work is done with a brief summary commit message, then hand back to user.
