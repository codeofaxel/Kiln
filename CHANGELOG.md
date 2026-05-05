# Changelog

All notable changes to Kiln are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

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
