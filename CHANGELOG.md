# Changelog

All notable changes to Kiln are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Changed

- **Renamed the Prusa adapter to `prusalink`.**  The adapter talks to
  the local Prusa Link HTTP API (`/api/v1/status` with `X-Api-Key`
  auth), not Prusa Connect's cloud service, so the registry key,
  module name, and class now reflect that.  `kiln auth --type prusalink`
  replaces `--type prusaconnect`.  No back-compat alias was added —
  update any saved config that pins `type: prusaconnect` to
  `type: prusalink`.

## [1.1.4] - 2026-05-20

Native Windows support — install Kiln, connect your AI agent, and
generate designs on a stock Windows machine, with setup made easy.
The release also sharpens overhang detection for cantilevers and
islands, makes AMS Lite filament readings accurate, and puts safety
messages in plain language.

### Added

- **Native Windows support.**  The CLI, `kiln install-mcp`, and design
  generation run on a stock Windows install.  The install page
  (kiln3d.com/install) has a Windows walkthrough — Python setup through
  agent connection — with a copy-paste prompt that hands the whole
  setup to an AI agent.

### Changed

- **Plain-language safety and connection messages.**  Warnings describe
  what they mean for your print instead of leaning on internal terms.
- **Accurate AMS Lite filament readings.**  The Bambu A1 and A1 mini's
  AMS Lite can't measure remaining filament, so Kiln reports the level
  as unknown instead of a placeholder number.

### Fixed

- **Overhang detection holds for cantilevers and islands.**  Kiln
  clears supports only when an overhang is genuinely bridgeable; a
  one-sided cantilever or a floating island keeps "needs supports."
- **Clearer diagnostics.**  `kiln doctor` warnings name their check,
  and pointing Kiln at a missing model file reports "Mesh file not
  found" up front.

## [1.1.3] - 2026-05-19

Adhesion catches tall narrow detach risk regardless of material,
accounts for thermal stress from layer cooling, and admits when
it's uncertain.  Prints remember which goal they came from, so
monitoring, reprints, and version saves don't need to be told twice.

### Added

- **Adhesion model knows when geometry alone is risky.** A 2×2×250
  mm tower in any material now reads as "marginal" regardless of
  the force ratio — extreme aspect ratios (> 50) concentrate peel
  stress at the base in ways the static formula doesn't capture.
- **Aspect-ratio-aware peel force.** Peel grows nonlinearly with
  height-to-base ratio above ~10:1. Tall vases / pen holders stay
  secure; tall narrow towers in any material get flagged.
- **Thermal-stress contribution to peel** (free tier sees it for
  every material at a conservative default; Pro tier tunes it per
  material). ABS, ASA, Nylon, PP, and PEEK now generate larger
  effective peel stress on tall prints because they actually do —
  cooling layers cyclically pull at the base. PLA / PETG see
  proportionally less because they actually shrink less.
- **Adhesion verdicts admit when they're uncertain.** Every
  `AdhesionForceEstimate` now carries a `model_confidence` field:
  `"high"` for clear extremes, `"approximate"` for the messy middle
  where the static model is less reliable. Lets agents soften
  wording on uncertain verdicts.
- **Heads-up when the slicer will bridge instead of support.** For
  short-span undersides (tabletop tops, U-shapes, square bridges)
  the slicer usually crosses the gap with a bridge rather than
  building a support tower. Kiln now flags those so you can force
  supports if you want a smoother bottom surface.
- **Smart bridging.** Kiln no longer tells you to add supports
  when your printer can simply bridge across a small horizontal
  gap on its own. One clear verdict with the reason.
- **Kiln Pro tells you how many grams of filament your supports
  will use.** A number you can plan a spool around. Matches what
  your slicer will actually print — sticky filaments like PETG and
  TPU get bumped up because they take more to peel cleanly.
- **Overhang limits know your filament.** Bendy filaments like
  TPU get flagged at steeper angles; stiff ones like PLA get more
  leeway. (Free tier still uses the universal 45° rule of thumb;
  Kiln Pro tunes it per material.)
- **Support estimates always match the verdict.** If Kiln says
  you need supports, it now always tells you roughly how much
  filament those supports will take. No more "needs supports"
  paired with a 0-gram estimate.
- **Arched and pillared ceilings appear in the bridging report.**
  Curved doorway tops, colonnaded ceilings, rows of windows above
  pillars — each unsupported span shows up with its actual length,
  so bridge warnings fire.
- **Wall thickness probed directly from the mesh.** Stable across
  tessellation densities, single-body or multi-body.
- **Lattices and scaffolds: each bar measured separately.** A
  1.5 mm lattice bar reads at 1.5 mm regardless of joint geometry.
- **Helical-surface filter on thin-wall probes.** Threaded rods and
  springs report at their structural thickness.
- **Pro+ measures your rod's actual diameter.** Threaded rods,
  springs, and knurled grips come back with two concrete numbers
  — the rod's structural cross-section and the depth of its
  surface features — when you have Kiln Pro. Replaces the
  free-tier "this reading might be measuring surface texture"
  qualifier with hard numbers.
- **Cavity-width check** for engraved grooves, slot cuts, and
  debossed text — narrow cuts below the slicer's extrusion floor
  now show up with their actual widths.
- **Slicer support style on `analyze_printability`** —
  `slicer_style="grid"|"snug"|"organic"|"tree"`. Kiln Pro
  estimates support grams that style actually extrudes.
- **Saved goals are listable.** The CLI and any MCP client can ask
  Kiln for the list of saved goals, so agents and the web app can
  show an inbox view.
- **Free-tier design intelligence finds what you ask for.** Material
  recommendations, design template discovery, printer profile
  lookups, BOSL2-using SCAD generators, and Bambu A1 print prep now
  have their catalogs on disk.
- **Release gate** asserts data files ship in the wheel before any
  PyPI publish.

### Changed

- **Bridge length uses the longer XY axis of the bridge
  footprint** so warnings fire regardless of which direction the
  slicer chooses to bridge.
- **Adhesion docstring honesty** — the `risk_level` field is now
  documented as a best-effort approximation, especially in the
  middle range. For high-aspect-ratio prints in warp-prone
  materials, treat `secure` as `plausible` rather than `verified`.
- **Thin-wall reports return `0.0` when no thin walls are found.**
  Was `nozzle_diameter`. Read `thin_wall_count > 0` before treating
  `min_wall_thickness_mm` as a measurement.
- **`PrintabilityReport.triangle_count`** — the mesh's triangle
  count is now on the report so callers can tell coarse meshes
  apart from detailed ones.
- **Pro thresholds scale with your nozzle.** Kiln Pro's per-material
  wall and hole floors now match the nozzle you're actually
  printing with. A 0.6 mm nozzle gets 0.6 mm-appropriate thresholds
  instead of always using the 0.4 mm baseline.
- **Cavity check scales with part dimensions** so large interior
  cavities register.
- **New `PrintabilityReport` shape signals:** `dimensions_mm`,
  `connected_components`, `component_size_uniformity`. Kiln Pro
  reads these to recognize lattices and multi-part topologies.
- **Prints remember which goal they came from.** Monitoring a print,
  reprinting it, saving a new version, or iterating on a design all
  pick up the saved goal automatically.  Agents no longer need to
  restate the goal at every step.
- `get_design_brief` is renamed `analyze_design_requirements`. The
  old name is removed. This only affects callers that asked for the
  functional analysis directly; the user-facing entry point for new
  designs (`design_session`) is unchanged.

### Fixed

- Walls angled exactly 45° now get checked properly. A rounding
  quirk was letting them sneak past the overhang check.
- When you tell Kiln what filament you're using, design audits
  now use that filament's actual overhang limit instead of
  falling back to the generic 45° rule.
- Support estimates no longer come back as zero on T-shapes,
  mushrooms, tabletops, umbrellas, and other shapes where the
  overhang sits above a narrower base.
- Support percentage no longer reports above 100% on rare
  thin-disc-on-tall-post geometries.
- **Hole detection works on tilted parts.** No more straightening
  required. Parts coming out of slicer auto-orient or any other
  tilt source (typically 15–30° off) used to silently produce zero
  hole warnings; now the detector recovers each hole's axis from
  its own geometry, so 3°, 30°, 45° — any tilt — surfaces the
  same warnings a flat-laid part would.
- **Chamfered hole entries no longer hide the underlying hole.**
  Holes with countersunk or 45° chamfered mouths used to read as
  "non-circular feature" once the chamfer ate enough of the
  cluster's normal budget; the detector now tries every principal
  direction of the cluster as a candidate axis and keeps just the
  cylinder wall for the radius fit. Sparse-mesh cylinders (6-8
  segments — common in low-poly props) also stay together where
  the previous version shattered them on a floating-point boundary.
- **Hex pockets are distinguishable from drilled holes.** Each
  detected hole carries a new `facet_segments` field: smoothly
  tessellated round bores report 20-48 facets, hex nut traps
  report 6, octagonal pockets 8. Callers (printability
  recommendations, kiln-pro overlays) can use the count to suppress
  hole-diameter warnings on intentional polygonal pockets without
  the detector having to guess intent from the mesh.
- **Sub-floor pocket recommendations now match what you can
  actually do about them.** A 0.5 mm round bore below the FDM
  hole-printing floor gets the existing "enlarge in CAD or drill
  after printing" recommendation; a 0.5 mm hex pocket gets a
  different one that explicitly says drilling won't help and points
  at the inscribed-dimension issue. Polygonal pockets used to get
  the round-bore advice, which was misleading.

### Internal

- New CI guard asserts the `kiln-pro` package is NOT installed in
  the public Kiln CI environment — prevents future tier-coupling
  leak regressions where public tests silently depend on the Pro
  overlay.

### Compatibility

- Released as `kiln3d` 1.1.3 — pair with `kiln-pro` 1.1.3 for the
  Pro+ experience.

## [1.1.2] - 2026-05-16

Hole-aware printability, automatic previews on the tools that change a
mesh, audits that pick up where inspection left off, and recovery
events on the bus so agents can wait on a recovered print instead of
polling.

### Added

- **Printability analysis detects holes.** Each hole's position, size,
  depth, and orientation is reported; undersized holes get flagged.
- `detect_holes(file_path)` is exposed standalone in
  `kiln.generation.validation`.
- `analyze_printability` gets two new arguments: `printer_id` (forwards
  to Pro+ per-printer tuning when available) and
  `include_hole_detection` (default True; perf opt-out).
- **Automatic inspection previews** on `preflight_check`,
  `start_print_recovery`, and `complete_print_recovery`.
- **Tools that change a mesh show you what they did.** Saving or
  rolling back a design version, applying a decoration, composing
  parts, rotating a model, importing a STEP file, and ~20 other mesh
  operations attach a small preview to their response.
- **Audits skip work when the inspection already happened.** If an
  earlier tool already inspected the mesh, the audit reuses those
  findings instead of re-running them.
- **Audits confirm the design matches what you asked for.** When the
  design started from a saved design-goal questionnaire (Kiln Pro),
  the audit ends with a plain-English summary or names the goals that
  weren't met.
- **Audits surface concrete remediation candidates.** On Kiln Pro, the
  audit lists the specific fixes available for each warning. Pass
  `apply_remedies=True` to let the overlay dispatch them; the default
  surfaces the options without mutating.
- **Recovery is on the event bus.** Three new event types —
  `RECOVERY_NEEDED`, `RECOVERY_STARTED`, `RECOVERY_COMPLETED` — let
  agents wait on recovery instead of polling. `watch_print_status`
  wakes on `recovery.completed` by default.

### Changed

- **Every Kiln event names who triggered it.** Each event records its
  origin — system, agent, or user — and carries that across threads.

### Fixed

- Hole detection handles rotated or simplified meshes.
- `reslice_with_overrides` and `slice_and_print` now include the
  `calibration_used` block on the response when a calibrated profile
  applies, matching `recommend_settings`'s shape.

## [1.1.1] - 2026-05-13

Vision event-name split, MCP-client config self-heal, an event-aware
`watch_print_status` subscribe mode, and a few quality fixes.

### Added

- `watch_print_status` `block_until_event` subscribe mode — agents can
  await a specific event instead of polling.
- `kiln health`: detects MCP-client config drift and self-heals stale
  `kiln` command paths in Claude Desktop / Claude Code / Codex configs.
- `kiln install-mcp`: richer installer promoted from kiln-pro.
- `kiln serve`: orphan-host watchdog exits cleanly when the MCP host
  goes away.

### Changed

- Vision events: `VISION_CHECK` split into `VISION_FRAME_CAPTURED` and
  `VISION_AGENT_INSPECTION`.  Subscribers should migrate; the original
  name is removed.
- The split is by **actor**, not payload size: `VISION_FRAME_CAPTURED`
  marks a system auto-capture (background watcher / first-layer
  monitor) and carries the snapshot bytes plus raw signals;
  `VISION_AGENT_INSPECTION` marks an explicit agent
  `monitor_print_vision` call and carries thin metadata.  Subscribers
  can now distinguish routine monitoring from deliberate inspection.
- Pro-tool manifest registers `get_design_pull_request` so agents
  discover it without a kiln-pro install.

### Fixed

- `print_health_monitor`: silent stall-event publish — stall warnings
  were observable internally but never reached subscribers.
- Bambu adapter: `can_snapshot=True` when the printer has a working
  camera URL, regardless of ffmpeg availability.
- `kiln health`: tolerates invalid TOML escapes in third-party MCP
  client configs.

## [1.1.0] - 2026-05-06

Design templates rebuild + automatic Pro+ calibration on every slice
tool + safer pre-print validation gate + a self-service tier diagnostic.

### Added

- **`check_my_tier` MCP tool** — answers "what plan am I on, and why" for
  agents handling user questions about tier / subscription / paywall
  confusion. Walks the live tier-resolution chain (env var → license
  file → OAuth session → cached entitlement → free fallback) and
  returns a plain-English summary the agent can paste straight to the
  user. Free-tier safe — no license needed to call.
- **First-time calibration overlay notice** — the first time Kiln
  applies your slicer calibration to a slice (Pro tier), the response
  carries a `first_time_notice` block that the agent can surface to
  the user ("Kiln just used your OrcaSlicer profile…"). After that,
  silent overlay with the audit trail in every slice response.
  Removable marker at `~/.kiln/calibration_overlay_first_use.seen` if
  you want to re-trigger the notice.
- **Pro+ continuous calibration learning** — when Kiln Pro is
  installed, per-printer offsets refine from every observation (not
  just one-time profile imports).  Freshness tracking surfaces when
  your printer drifts; full provenance answers "why is this dimension
  what it is?".
- **`tablet_stand` template** — dedicated for tablets / iPads, with
  wider base depth, taller back panel, and slot sized for keyboard
  cases. Phone stands stay focused on phones.
- **Pre-print validation gate runs by default** — `slice_and_print`,
  `run_quick_print`, and `run_reslice_and_print` now validate the
  mesh against printer + material constraints before slicing.
- **Per-machine calibration overlay applied uniformly** — Pro+
  calibration now applies to `slice_and_print` (previously only
  `slice_model` and `reslice_with_overrides`).
- **`kiln events tail` / `kiln events summary`** — read-only CLI
  access to the local event log.
- **Craftcloud order safety gates** — fulfillment orders now require
  an explicit preview-confirmation token AND a shipping-address
  confirmation token before placing.  Two new endpoints
  (`POST /api/fulfillment/preview-confirm`,
  `POST /api/fulfillment/shipping-confirm`) issue the tokens after
  the user has actually seen the rendered preview and reviewed the
  shipping address.  Prevents "agent placed an order I didn't
  expect," surprise shipping mismatches, and stale-quote charges.
- **Local shipping profiles store** — named shipping addresses now
  persist locally at `~/.kiln/shipping_profiles.json` (override via
  `KILN_SHIPPING_PROFILES_PATH`) and can be referenced by name when
  issuing a shipping-confirm token.  No more re-entering the same
  address every time you order a part for the same workshop or
  customer.
- **Food-safety material overlays** — material-recommendation,
  slice, and food-contact tool paths now consult a structured
  food-safety catalog.  PETG / PETG-HF surface as food-safe; PLA
  returns a `conditional` verdict with constraints (water-only,
  hand-wash, replace 6-12 months); ABS, TPU, ASA, and CF blends
  hard-refuse with `MATERIAL_NOT_FOOD_SAFE`.  Affects
  `generate_pet_bowl`, material recommendations, and slice metadata.
- **Anonymous unique-device counting** — heartbeat telemetry
  (already on in v1.0) now includes a one-way-hashed device
  fingerprint so we can count unique installs without identifying
  anyone.  No reversal possible; no PII collected.
- **Free-tier `recommend_settings` calibration overlay** — free
  users now get per-printer calibration data folded into setting
  recommendations when a calibrated profile is present (Pro+ writes,
  free reads).  Smaller-than-Pro continuous-learning, but real.

### Changed

- **3 design-template MCP tools renamed** — `find_design_templates`,
  `list_design_templates_catalog`, `get_design_template_info`. The
  prior `_design_patterns_` names are removed in this release (see
  Removed below).
- **Design template count: 17 → 18** — dropped 3 (cable management
  clip schema break, GoPro mount single-vendor, wall mount bracket
  folded into cantilever bracket), then split phone stand into
  `phone_stand` + `tablet_stand`.
- **`design_patterns.json` → `design_templates.json`** — file name
  matches the user-facing concept.  Old Python imports
  (`DesignPattern`, `pattern_id`, `get_design_pattern`, etc.) are
  removed in this release (see Removed below).
- **WHITEPAPER + LITEPAPER** — refreshed to match current capability
  + count.
- **Site nav** — Install hoisted to the top of the Product menu;
  install copy refreshed.

### Fixed

- **Free-tier safety-floor fallback** — when the engineering overlay
  isn't loaded, design intelligence returns conservative safety-floor
  recommendations instead of an error.
- **PTFE temperature clamp now actually fires** — the safety guard
  capping PTFE-lined hotends at 240°C had a silent failure path
  (`PrinterIntel` dataclass treated as a dict) that meant Ender 3,
  Ender 5, and any other PTFE-lined hotend was getting the profile's
  raw 260°C ceiling instead of the PTFE-safe 240°C.  Now clamps
  correctly; set `KILN_OVERRIDE_PTFE_LIMIT=1` to disable for
  user-installed all-metal conversions.
- **Decoration engine fixes** — face detection, deboss math,
  decoration hierarchy, and fit calculations all hardened in
  this release.  Affects users who hit edge-case decoration
  bugs in v1.0.
- **Documentation counts** refreshed across README, white paper,
  litepaper, FAQ, and the marketing site.

### Removed

- MCP tools `find_design_patterns`, `list_design_patterns_catalog`,
  `get_design_pattern_info` — replaced by the `_design_templates_`
  variants in this release.  No back-compat aliases.
- Python imports `DesignPattern`, `pattern_id`, `kb.patterns`,
  `get_design_pattern`, `list_design_patterns`,
  `find_patterns_for_use_case` — all renamed to `DesignTemplate`,
  `template_id`, `kb.templates`, `get_design_template`,
  `list_design_templates`, `find_templates_for_use_case`.

## [1.0.0] - 2026-04-29

All changes vs v0.5.0:

### Added

- **Stable Public Package** — Kiln now ships as `kiln3d 1.0.0` on PyPI with production/stable package metadata.
- **MCP Registry Release** — Kiln now publishes 1.0.0 metadata for MCP-compatible clients.
- **Expanded MCP Surface** — Kiln now exposes 763 total MCP capabilities for local agent-operated manufacturing workflows.
- **Expanded CLI Surface** — The `kiln` CLI now exposes 215 commands across setup, design, slicing, printing, monitoring, recovery, and release workflows.
- **MCP Installer Command** — Added `kiln install-mcp` for easier setup in MCP clients.
- **Account Pairing Flow** — Added local sign-in, pairing, and account-status commands for connecting a machine to a Kiln account.
- **Tool Discovery Metadata** — Updated capability metadata so agents can find the right tool faster.
- **Git for 3D Foundations** — Added design versioning, ancestry, aliases, rollback, mesh-only imports, and release-signing foundations.
- **STEP Import** — Added STEP file import support with multi-backend handling.
- **Design Recipe System** — Added recipe/provenance tracking so generated and modified objects can be edited later.
- **Surface Decoration Tools** — Added workflows for text, image, SVG, photo, and reusable surface decorations.
- **Photo Emboss Pipeline** — Added image-to-surface and emboss/deboss tooling for relief-style printed surfaces.
- **Surface Intelligence** — Added face detection and placement helpers for decorating printable models.
- **Multicolor 3MF Composer** — Added 3MF composition for multicolor and multi-material print outputs.
- **Colored 3MF Preview** — Added per-face color rendering so multicolor files can be previewed before printing.
- **Universal Model Visualizer** — Added adaptive model preview rendering for flat, tall, colored, and Bambu-wrapped 3MF files.
- **Mesh Tooling Expansion** — Added repair, split, merge, mirror, hollow, simplify, compare, orient, and printability tools.
- **Pre-Generation Estimator** — Added time, filament, and cost estimates before a model is generated.
- **Post-Generation Validation** — Added validation gates for AI-generated meshes before print.
- **OpenSCAD Verification** — Added static checks for common parametric model mistakes before printing.
- **Generation Feedback Loop** — Failed print outcomes can feed back into future generation constraints.
- **Material Intelligence Expansion** — Expanded material data, brand filament profiles, compatibility rules, troubleshooting, and post-processing guidance.
- **Filament Resolver** — Added smarter brand/material matching across slicing, recommendations, and print planning.
- **Multi-Material System Support** — Added support metadata for multi-material printer workflows.
- **Creality Moonraker Support** — Added Creality local Moonraker adapter support where local Moonraker is reachable.
- **Bambu AMS Auto-Routing** — Added AMS-aware material routing and safer tray handling.
- **Multi-Color Copies** — Added workflows for printing multiple copies in different AMS colors.
- **MakerWorld Adapter** — Added MakerWorld marketplace support alongside existing marketplace integrations.
- **Assembly Manual Metadata** — Added public-side metadata for assembly manual workflows.
- **Print Health Monitor** — Reworked monitoring around a canonical health monitor with predictive risk and failure signals.
- **Print Watchdog** — Added watchdog support for safer monitoring and recovery decisions.
- **Incident Recorder** — Added structured incident recording for safety-critical printer events.
- **Failure Vocabulary** — Added unified failure type and severity normalization.
- **Printer Model Resolver** — Added explicit printer model resolution instead of unsafe guessing.
- **Safety Gap Warnings** — Added warnings when Kiln cannot fully verify printer-specific safety limits.
- **G-Code Interception Rules** — Added default interception rules for safer printer command handling.
- **Preview Confirmation Gate** — Added confirmation tokens so print starts can be tied to a verified preview.

### Printer & Slicing Improvements

- **Bambu 3MF Reliability** — Improved Bambu 3MF wrapping, thumbnails, object printing, and startup handling.
- **Bambu MQTT Compatibility** — Fixed Bambu connection behavior with newer MQTT client versions.
- **Bambu A-Series Handling** — Improved A-series state parsing, file transfer behavior, and print-start polling.
- **AMS Ambiguity Handling** — Kiln now fails safer when AMS tray state is ambiguous.
- **Build Volume Resolution** — Printer build volumes now resolve consistently across tool paths.
- **Slicer Profiles Expansion** — Expanded bundled slicer profile metadata and printer compatibility data.
- **3MF Extraction** — Added extraction support for models embedded inside `.3mf` and `.gcode.3mf` files.
- **Prusa/Bambu Time Estimates** — Improved print time estimation from slicer output.
- **SCAD Color Preview Fixes** — OpenSCAD previews now preserve expected colors.
- **Adaptive Preview Angles** — Model previews choose better angles for different part shapes.

### Fixed

- **Config Validation** — `printer_model` validation now preserves case-sensitive model IDs.
- **Registry Recovery** — Printer registry access is more reliable across CLI, MCP, and plugin paths.
- **Monitor Import Fixes** — Fixed missing health-check imports and retired stale monitor paths.
- **Tool Name Cleanup** — Removed stale suffixes and repaired naming collisions.
- **Auth Command Cleanup** — Clarified sign-in, pairing, linking, and identity behavior.
- **Preview Token Binding** — Preview confirmation now binds more reliably to the intended file.
- **Path Traversal Fixes** — Hardened marketplace downloads, snapshots, and file destinations.
- **Shell Injection Fix** — Hardened preview notification command handling.
- **Bambu Credential Masking** — Bambu access codes are masked in sensitive output paths.
- **Public Install Cleanup** — Removed install paths that could fail for normal public package users.

### Security & Safety

- **License Change** — Kiln moved to AGPL-3.0-or-later with commercial licensing available.
- **Release Signing Workflow** — Added signed-release workflow foundations.
- **SBOM Workflow** — Added software bill of materials generation.
- **SLSA Attestation Workflow** — Added provenance/attestation workflow for release artifacts.
- **PyPI Publishing Hardening** — Fixed release workflow issues for trusted publishing.
- **Printer Crash Prevention** — Added stronger safety checks around upload, startup, and command execution.
- **Tier Boundary Clarity** — Free, Pro, Business, and Enterprise capabilities are clearer across CLI, MCP metadata, and account-aware workflows.

## [0.4.2] - 2026-03-18

### Added
- SDCP V3 HTTP push upload for Elegoo Centauri Carbon with V2 fallback — thanks [@bobbyhiddn](https://github.com/bobbyhiddn)
- Multi-color multi-copy printing (each copy a different AMS color)
- Parametric design template library (10 OpenSCAD modules: gears, threads, containers, lattice, etc.)
- Three-layer generation system (parametric library, compile-fix loop, visual verification)
- MCP session greeting — agents see full capability map on connect without discovery
- did-you-mean CLI suggestions for mistyped commands
- 3MF cost estimation from Bambu/OrcaSlicer slice metadata
- Improved upon Image-to-3D support via Gemini Deep Think provider
- Fulfillment materials command and smart material resolver

### Fixed
- Heatup false-positive temp drift alerts during cold starts (reached-target gating)
- Elegoo CLI adapter missing from `_make_adapter()` (silently fell through to Moonraker)
- Elegoo WebSocket deadlock (`Lock` to `RLock`)
- Elegoo SDCP V3 `CurrentStatus` returned as list instead of int
- `mcp.instructions` read-only property crash on FastMCP 1.9+
- Phantom MCP tool references in agent guidance
- External thread compile time 2min to 17sec
- Stale skill manifest test

### Dependencies
- certifi 2026.1.4 to 2026.2.25
- rich 14.3.2 to 14.3.3
- pydantic-settings 2.12.0 to 2.13.1
- GitHub Actions: upload-artifact v4 to v7, download-artifact v4 to v8

### Added
- 25 mesh analysis and transformation MCP tools (pure Python, no external mesh libraries):
  - Geometry analysis: `analyze_mesh_geometry`, `mesh_quality_scorecard`, `analyze_non_manifold_edges`, `diagnose_mesh`
  - Repair: `repair_mesh`, `repair_mesh_advanced` (hole closing), `remove_mesh_floating_regions`
  - Transformations: `mirror_mesh_model`, `hollow_mesh_model`, `center_model_on_bed`, `scale_mesh_to_fit`, `optimize_print_orientation`, `simplify_mesh_model`
  - Composition: `compose_models`, `merge_mesh_files`, `split_mesh_by_component`
  - Estimation: `estimate_material_cost`, `estimate_support_material`, `estimate_mesh_print_time`, `predict_print_failure`
  - Comparison: `compare_mesh_versions` (with Hausdorff distance)
  - Readiness: `check_print_readiness` (single-call gate with optional auto-repair)
  - Export/Import: `export_model_3mf`, `extract_model_from_3mf` (3MF/gcode.3mf → STL)
- Parametric design templates with `list_design_templates`, `generate_from_template`, `generate_template_variations`
- Design advisor tool for recommending generation approach (template, OpenSCAD, or AI)
- Closed-loop design iteration: `iterate_design` (generate → validate → improve → regenerate)
- Print cost estimation in `monitor_print` and `preflight_check` (9 materials, USD estimates)
- Cross-printer learning feedback loop with auto-outcome recording from scheduler
- `recommend_settings` MCP tool for history-based print setting recommendations
- Outcome-aware preflight warnings (low success rate advisories)
- `get_started` MCP onboarding tool for AI agents
- `safety_status` MCP dashboard tool
- `confirm_action` two-step confirmation gate for destructive operations
- `safety_audit` MCP tool for querying the safety audit log
- Smart printer routing based on historical success rates
- Tier-aware agent error messages with suggested alternative tools
- Agent onboarding improvements (`get_started` tool, session recovery hints)
- Fly.io deployment support (`deploy.sh`, `Dockerfile.api`, GitHub Actions workflow)
- Circle setup script (`scripts/circle_setup.py`) for one-time entity secret and wallet provisioning
- Health check endpoint (`/api/health`) on REST API
- Donation info endpoint on REST API

### Changed
- `generate_and_print` and `download_and_upload` no longer auto-start prints (upload only, explicit start required)
- Auto-print toggles for marketplace and generated models (env var opt-in, default OFF)
- Docs: clarified product boundary to reflect existing intent (orchestration layer, partner integrations; no strategy change)
- Partner/provider naming is now canonical for remote integration surfaces (`partner` CLI group and provider-oriented MCP tools)
- Legacy `network_*` MCP tools and `kiln network ...` CLI remain as compatibility aliases (deprecated in `v0.2.0`, removal target `v0.4.0`)
- Fulfillment responses now include explicit provider ownership metadata (`provider_name`, `provider_order_id`, `provider_terms_url`, `support_owner=provider`, `merchant_of_record=provider`)
- Billing language standardized to "orchestration fee" in user-facing docs/CLI copy
- SKILL.md reorganized: quick start moved to top, fulfillment section added, JSON response examples
- Enriched `kiln status --json` with `printer_name` and `printer_type` fields
- Improved config validation errors with actionable quick-fix suggestions
- Bambu MQTT timeout error now includes troubleshooting checklist
- Rewrote Circle payment provider for W3S Programmable Wallets API (replaced deprecated Transfers API)
- Circle payments now use RSA-OAEP entity secret encryption for secure wallet operations

### Fixed
- CI failures: OpenSCAD macOS fallback test on Linux, flaky uptime test tolerance
- Bambu A1/A1 Mini uppercase state parsing
- Bambu A-series implicit FTPS on port 990
- Print start confirmation polling for Bambu printers
- YAML parse errors now surfaced instead of silently returning empty config

### Dependencies
- Added `cryptography>=41.0` to payments optional dependencies

### Security
- Safety audit log records all guarded/confirm/emergency tool executions
- Emergency cooldown escalation (circuit breaker) for repeated blocked actions
- Unified temperature limit resolution via safety profiles (single source of truth)
- Pause/resume rate limiting to prevent mechanical wear
- Dry-run mode for `send_gcode`
- G-code auto-detect printer profile from slicer comments

## [0.1.0] - 2026-02-10

### Added
- OctoPrint REST adapter (full printer control)
- Moonraker REST adapter for Klipper-based printers
- Bambu Lab MQTT adapter for X1C, P1S, A1 over LAN
- Prusa Link REST adapter for MK4, XL, Mini+
- MCP server with 79+ tools for AI agent printer control
- CLI with 47+ commands and `--json` output on every command
- Fleet management: multi-printer registry, fleet status
- Priority job queue with background dispatch and auto-retry with exponential backoff
- Mandatory preflight checks before print jobs
- G-code safety validation with per-printer limits (28 printer safety profiles)
- Bundled slicer profiles for 14 printer models (PrusaSlicer/OrcaSlicer)
- Printer intelligence database (firmware quirks, material compatibility, failure modes)
- Pre-validated pipelines: quick_print, calibrate, benchmark
- Slicer integration (PrusaSlicer, OrcaSlicer) with auto-detection
- Model marketplace adapters: MyMiniFactory, Cults3D, Thingiverse (deprecated — acquired by MyMiniFactory, Feb 2026)
- Fulfillment service adapters: Craftcloud
- Text-to-model generation via Meshy AI (cloud) and OpenSCAD (local)
- Mesh validation pipeline (STL/OBJ parsing, manifold check, dimension limits)
- Print cost estimation from G-code analysis
- Material and spool tracking with mismatch warnings
- Bed leveling trigger system with configurable policies
- OTA firmware updates for OctoPrint and Moonraker
- Webcam snapshot capture and MJPEG stream proxy
- Print history and agent memory persistence (SQLite)
- Cross-printer learning database with outcome tracking
- Closed-loop vision monitoring (snapshot + print phase hints)
- Print failure analysis and post-print quality validation
- Await print completion polling tool
- Local vs. fulfillment cost comparison tool
- Webhooks with HMAC-SHA256 signing
- Event bus with pub/sub
- Cloud sync for printer configs and job history
- Plugin system with entry-point discovery
- API key authentication with scope-based access
- Billing/fee tracking for fulfillment orders (5% platform fee, first 3 free/month)
- Multi-model agent support via OpenRouter (any OpenAI-compatible LLM)
- REST API wrapper (FastAPI) exposing all MCP tools as HTTP endpoints
- Tool tiers: essential (15), standard (43), full (101+)
- Network printer discovery via mDNS and HTTP probing
- Device type generalization (FDM, SLA, CNC, Laser forward-compatible)
- Resumable marketplace downloads with HTTP Range headers
- One-line install script for Linux/macOS

### Security
- Temperature range enforcement per printer and material
- Path traversal prevention on snapshots, slicer output, and Bambu file operations
- Agent tool result sanitization (injection pattern stripping, truncation)
- REST API hardening: parameter filtering, rate limiting, CORS lockdown, body size limits
- Payment address validation (Ethereum, Solana)
- Plugin loading gated by allow-list
- OpenSCAD input validation (size limit, dangerous function blocking)
- File upload validation (existence, size, empty file rejection)
- G-code batch size limits (100 commands max)
