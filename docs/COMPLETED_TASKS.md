# Kiln — Completed Tasks

Record of finished features and milestones, newest first.

## 2026-02-11

### Dependabot Config
Added `.github/dependabot.yml` with weekly update checks for pip dependencies (both `kiln/` and `octoprint-cli/`) and GitHub Actions versions. Open PR limit set to 5 per ecosystem.

### PyPI Publishing Pipeline
Upgraded `.github/workflows/publish.yml` to production-ready release workflow:
- **Test gate**: Full test suite (Python 3.10 + 3.12) must pass before any publish
- **Version validation**: Git tag is checked against `kiln/pyproject.toml` version — mismatches fail the build
- **Both packages enabled**: `kiln3d` and `kiln3d-octoprint` now both build and publish (octoprint-cli was previously commented out)
- **Trusted publishing**: OIDC `id-token` auth (no API tokens stored in secrets)
- **Version mismatch fix**: `octoprint-cli/__init__.py` was `1.0.0` vs `pyproject.toml` `0.1.0` — corrected to `0.1.0`

### README Polish Pass
- Added CI, PyPI version, Python version, and license badges to README header
- Updated test counts: `2,970+` total (was incorrectly showing `2650+` / `2413`)
- All feature references verified against current codebase

### Pre-Commit Hooks & Linting
- Created `.pre-commit-config.yaml` with Ruff (lint + format), trailing whitespace, EOF fixer, YAML check, large file guard, merge conflict check
- Added `[tool.ruff]` config to both `kiln/pyproject.toml` (target py310) and `octoprint-cli/pyproject.toml` (target py38)
- Ruff rules: E, F, W, I (imports), UP (pyupgrade), B (bugbear), SIM (simplify)
- Updated `CONTRIBUTING.md` with pre-commit setup instructions, monorepo guidance, and safety-critical code warnings

### Live OctoPrint Integration Smoke Test
- Created `kiln/tests/test_live_octoprint.py` with `@pytest.mark.live` marker
- Tests: connectivity, capabilities, job query, file list, upload + verify + delete round-trip, temperature sanity checks
- Skipped by default — activated via `pytest -m live` with `KILN_LIVE_OCTOPRINT_HOST` and `KILN_LIVE_OCTOPRINT_KEY` env vars
- Added `live` marker to `kiln/pyproject.toml` pytest config
- Includes instructions for running OctoPrint via Docker for CI

### Claimed PyPI Package Names
Reserved four PyPI package names as v0.0.1 placeholders:
- `kiln3d` — https://pypi.org/project/kiln3d/
- `kiln-print` — https://pypi.org/project/kiln-print/
- `kiln-mcp` — https://pypi.org/project/kiln-mcp/
- `kiln3d-octoprint` — https://pypi.org/project/kiln3d-octoprint/

### Model Safety Guardrails — Auto-Print Toggles
Added safety guardrails for AI-generated and marketplace-downloaded 3D models:

- `generate_and_print()` no longer auto-starts prints — uploads only, requires explicit `start_print` call
- `download_and_upload()` same — uploads only, explicit start required
- Two independent opt-in toggles via env vars: `KILN_AUTO_PRINT_MARKETPLACE` (moderate risk) and `KILN_AUTO_PRINT_GENERATED` (higher risk), both default OFF
- All generation/download tools return `experimental` or `verification_status` flags plus safety notices
- New `safety_settings` MCP tool shows current auto-print configuration and recommendations
- Setup wizard (`kiln setup`) now prompts users to configure auto-print preferences during onboarding
- MCP server system prompt updated to guide agents toward proven community models over generation
- Setup complete summary shows current toggle values and env var names for later changes

### Bambu A1/A1 Mini Compatibility
Fixed 3 bugs reported by Chris Miller during real-hardware testing on Bambu A1 mini:

- **Uppercase state parsing**: A1/A1 mini sends UPPERCASE `gcode_state` (e.g. "RUNNING" instead of "running"). Added `.lower()` normalization in `get_state()` and case-insensitive command matching in `_on_message()`.
- **Implicit FTPS on port 990**: A-series uses implicit TLS (wraps socket in TLS immediately), not explicit STARTTLS. Added `_ImplicitFTP_TLS` subclass with socket wrapping and TLS session reuse for data channels.
- **Print start confirmation**: `start_print()` now polls MQTT for `gcode_state` to confirm the printer actually started (up to 30s). Returns failure if printer enters error state or times out.
- Added 121 Bambu adapter tests including new test classes for uppercase states, print confirmation, and implicit FTPS.

### Comprehensive Security Hardening
Full-project security audit and fix pass — 70+ vulnerabilities identified and fixed across 25+ files.

**Temperature Safety (P0 — hardware protection)**
- G-code validator now blocks negative temperatures and warns on cold extrusion risk (<150°C)
- Unrecognized G-code commands blocked instead of passed through with warning
- `set_temperature()` MCP tool validates bounds (0-300°C hotend, 0-130°C bed) before reaching adapters
- All 4 printer adapters (OctoPrint, Moonraker, Bambu, PrusaConnect) enforce temperature limits via shared `_validate_temp()` in base class
- CLI `temp` command validates temperature ranges before sending

**Path Traversal Fixes**
- `printer_snapshot()` restricts save_path to home/tmp directories
- Slicer `output_name` stripped to basename only — rejects directory traversal
- Bambu `start_print()` and `delete_file()` restrict paths to `/sdcard/` and `/cache/`
- CLI `snapshot --output` validates path boundaries

**Agent Security**
- Tool results sanitized before feeding to LLM in `agent_loop.py` — strips injection patterns, truncates to 50K chars
- System prompt includes explicit warning to ignore instructions in tool results
- `skip_preflight` parameter removed from `start_print()` — pre-flight checks are now mandatory

**REST API Hardening**
- Parameter pollution fixed: `**body` replaced with `inspect.signature()` filtering, rejects unknown params
- Rate limiting added (60 req/min per IP)
- CORS default changed from `["*"]` to `[]`
- Request body size limited to 1MB
- Error messages sanitized to prevent information leakage

**Payment Security**
- Circle USDC: destination address validated (Ethereum 0x format or Solana base58)
- Stripe: error messages sanitized — no raw exception details returned to clients
- Payment manager: billing charge failure handled gracefully with status tracking

**Infrastructure Fixes**
- MJPEG proxy: frame buffer capped at 10MB to prevent OOM
- Cloud sync: error messages sanitized to prevent credential leakage
- Scheduler: job status mutations wrapped in locks to prevent race conditions
- Materials tracker: spool warnings emitted outside lock to prevent deadlocks
- Webhook delivery queue bounded to 10K entries with overflow logging
- Event bus: duplicate subscription prevention
- Bed leveling: division-by-zero guard on empty mesh data
- Cost estimator: intermediate rounding removed to prevent accumulation errors

**Plugin & Subprocess Safety**
- Plugin loading gated by `KILN_ALLOWED_PLUGINS` allow-list
- OpenSCAD input validated: size limit (100KB), dangerous functions blocked (`import()`, `surface()`, `include`, `use`)
- OpenSCAD subprocess runs in isolated temp directory
- CLI config: removed credential type confusion fallback (access_code no longer falls back to API key)

**Defensive Measures**
- G-code batch limited to 100 commands per send
- File upload validates existence and size (max 500MB, rejects empty files)
- Pipeline G-code sample increased from 500 to 2000 lines
- Pipeline safety check failure now aborts (was silently continuing)

All 2891 tests passing (2652 kiln + 239 octoprint-cli).

### Multi-Model Support (OpenRouter / Any LLM)
- **tool_schema.py**: OpenAI function-calling schema converter — introspects FastMCP tool definitions and generates OpenAI-compatible JSON schemas with parameter descriptions from docstrings
- **tool_tiers.py**: Three-tier tool system — essential (15 tools for weak models), standard (43 for mid-range), full (101 for strong models) with auto-detection via `suggest_tier(model_name)`
- **agent_loop.py**: Generic agent loop for any OpenAI-compatible API — handles tool calling, multi-turn conversations, error recovery, and configurable max turns
- **openrouter.py**: OpenRouter-specific integration with curated 15-model catalog, auto-tier detection, convenience `run_openrouter()` function, and interactive REPL
- **rest_api.py**: FastAPI REST wrapper — exposes all MCP tools as `POST /api/tools/{name}` endpoints with discovery (`GET /api/tools`), agent loop endpoint (`POST /api/agent`), and optional Bearer auth
- New CLI commands: `kiln setup` (interactive wizard), `kiln rest` (REST API server), `kiln agent` (multi-model REPL)
- `rest` optional dependency group: `pip install kiln3d[rest]`

### Pre-Launch Infrastructure
- CONTRIBUTING.md, CODE_OF_CONDUCT.md, GitHub issue/PR templates
- Example configs: `kiln-config.yaml` (all 4 printer types), `claude-desktop-mcp.json`
- Fixed README/whitepaper image paths for PyPI rendering
- Version consistency pass (standardized to 0.1.0)

### Closed-Loop Vision Feedback
- 2 MCP tools: `monitor_print_vision` (snapshot + state + phase hints), `watch_print` (polling loop with periodic snapshot batches)
- Print phase detection: first_layers (< 10%), mid_print (10-90%), final_layers (> 90%) — each with curated failure hints
- `can_snapshot` capability flag on `PrinterCapabilities`, set `True` for OctoPrint and Moonraker adapters
- 2 new event types: `VISION_CHECK`, `VISION_ALERT`
- Works gracefully without webcam (metadata-only monitoring)
- 27 tests covering phase detection, snapshot paths, failure hints

### Cross-Printer Learning
- `print_outcomes` SQLite table with indexes on printer_name, file_hash, and outcome
- 7 new `KilnDB` methods: `save_print_outcome`, `get_print_outcome`, `list_print_outcomes`, `get_printer_learning_insights`, `get_file_outcomes`, `suggest_printer_for_outcome`, `_outcome_row_to_dict`
- 3 MCP tools: `record_print_outcome` (safety-validated), `get_printer_insights` (aggregated analytics), `suggest_printer_for_job` (ranked recommendations)
- **Safety guardrails**: Hard temperature limits (320C tool, 140C bed, 500mm/s speed), enum validation on outcomes/grades/failure modes, `SAFETY_VIOLATION` rejection for dangerous values, advisory-only disclaimers on all insight responses
- 30 tests covering DB CRUD, aggregation, edge cases

### Physical-World Platform Generalization
- `DeviceType` enum: `FDM_PRINTER`, `SLA_PRINTER`, `CNC_ROUTER`, `LASER_CUTTER`, `GENERIC`
- `DeviceAdapter = PrinterAdapter` alias for forward compatibility
- Extended `PrinterCapabilities`: `device_type` (default "fdm_printer"), `can_snapshot` (default False)
- Optional device methods: `set_spindle_speed()`, `set_laser_power()`, `get_tool_position()` — default implementations raise/return None
- All 4 existing adapters continue to work without modification
- 22 tests covering alias identity, enum values, capability defaults, backward compatibility

### Bundled Slicer Profiles Per Printer
- **`data/slicer_profiles.json`** — Curated PrusaSlicer/OrcaSlicer settings for 14 printer models: Ender 3, Ender 3 S1, K1, Prusa MK3S/MK4/Mini, Bambu X1C/P1S/A1, Voron 2.4, Elegoo Neptune 4, Sovol SV06, QIDI X-Plus 3
- Each profile: layer height, speeds, temps, retraction, fan, bed shape, G-code flavor — all optimized for the specific printer's kinematics and extruder type
- **`slicer_profiles.py`** — Loader with `resolve_slicer_profile()` that auto-generates temp `.ini` files for the slicer CLI, cached per printer+overrides
- 2 MCP tools: `list_slicer_profiles_tool`, `get_slicer_profile_tool`
- Agents auto-select the right profile by passing `printer_id` — no manual slicer config needed

### Printer Profile Intelligence (Firmware Quirks DB)
- **`data/printer_intelligence.json`** — Full operational knowledge base for 13 printer models: firmware type, extruder/hotend info, enclosure status, ABL capability
- **Material compatibility matrix** per printer: PLA/PETG/ABS/TPU/PA-CF/PC with exact hotend/bed/fan temps and material-specific tips
- **Firmware quirks** — printer-specific gotchas (e.g. "PTFE tube degrades above 240°C", "Nextruder requires 0.4mm retraction — don't increase")
- **Calibration guidance** — step-by-step procedures (first_steps, flow_rate_test, retraction_test, esteps)
- **Known failure modes** — symptom → cause → fix database for common issues
- **`printer_intelligence.py`** — Loader with `get_material_settings()`, `diagnose_issue()` (fuzzy symptom search)
- 3 MCP tools: `get_printer_intelligence`, `get_material_recommendation`, `troubleshoot_printer`

### Pre-Validated Print Pipelines
- **`pipelines.py`** — Named command sequences that chain multiple MCP operations:
  - **`quick_print`** — resolve profile → slice → G-code safety validation → upload → preflight → start print (6 steps with error handling at each stage)
  - **`calibrate`** — connect → home axes → auto bed level → return calibration guidance from intelligence DB
  - **`benchmark`** — resolve profile → slice → upload → report printer stats from history
- Each pipeline returns `PipelineResult` with per-step timing, success/failure, and diagnostic data
- Pipeline registry with `list_pipelines()` for discoverability
- 4 MCP tools: `list_print_pipelines`, `run_quick_print`, `run_calibrate`, `run_benchmark`

### Agent Memory & Print History Logging
- **Print history table** (`print_history`) in SQLite persistence — tracks every completed/failed job with printer_name, duration, material_type, file_hash, slicer_profile, notes, agent_id, and JSON metadata
- **Agent memory table** (`agent_memory`) — persistent key-value store scoped by agent_id and namespace (global, fleet, per-printer). Survives across sessions
- **Auto-logging event subscriber** — `_log_print_completion` hooked to `JOB_COMPLETED` and `JOB_FAILED` events, writes history records automatically
- 6 new MCP tools: `print_history`, `printer_stats`, `annotate_print`, `save_agent_note`, `get_agent_context`, `delete_agent_note`
- All DB methods use `_write_lock` for thread safety, JSON serialization for complex data, `time.time()` timestamps

### Bundled Safety Profiles Database
- **`data/safety_profiles.json`** — Curated per-printer safety database with 26 printer models: Creality (Ender 3/5, CR-10, K1), Prusa (Mini, MK3S, MK4, XL), Bambu Lab (X1C, P1S, P1P, A1, A1 Mini), Voron (0, 2.4), Rat Rig, Elegoo, Sovol, FlashForge, QIDI, AnkerMake, Artillery
- Each profile: max hotend/bed/chamber temps, max feedrate, volumetric flow, build volume, safety notes (e.g. PTFE hotend warnings)
- **`safety_profiles.py`** — Loader with `get_profile()` (fuzzy matching + fallback to default), `list_profiles()`, `get_all_profiles()`, `profile_to_dict()`
- **`validate_gcode_for_printer()`** — New function in `gcode.py` that validates commands against a specific printer's limits instead of generic defaults
- 3 new MCP tools: `list_safety_profiles`, `get_safety_profile`, `validate_gcode_safe`
- Error messages include printer display name for clarity (e.g. "exceeds Creality Ender 3 max hotend temperature (260°C)")

### Webcam Streaming / Live View
- `MJPEGProxy` class in `streaming.py` — full MJPEG stream proxy with start/stop lifecycle
- `webcam_stream` MCP tool for agents to start/stop/check stream status
- `kiln stream` CLI command with `--port` and `--stop` options
- 20 tests in `test_streaming.py`
- Previously listed in TASKS.md as medium priority; already shipped

## 2026-02-10

### OTA Firmware Updates
- `FirmwareComponent`, `FirmwareStatus`, `FirmwareUpdateResult` dataclasses in `base.py`
- `can_update_firmware` capability flag on `PrinterCapabilities`
- **Moonraker adapter** — `get_firmware_status()` via `/machine/update/status`, `update_firmware()` via `/machine/update/upgrade`, `rollback_firmware()` via `/machine/update/rollback`
- **OctoPrint adapter** — `get_firmware_status()` via Software Update plugin check API, `update_firmware()` via plugin update API with auto-discovery of updatable targets
- Safety: both adapters refuse updates while printing
- 3 new MCP tools: `firmware_status`, `update_firmware`, `rollback_firmware` (auth-gated)
- 3 new CLI commands: `kiln firmware status`, `kiln firmware update`, `kiln firmware rollback`
- 55 new tests across 4 test files (adapter, MCP tools, CLI)
- Total test count: 2,078

### Additional Fulfillment Providers (Shapeways + Sculpteo)
- `ShapewaysProvider` — Full implementation with OAuth2 client-credentials auth, model upload (base64), per-material pricing, order placement, status tracking, and cancellation
- `SculpteoProvider` — Full implementation with Bearer token auth, file upload, UUID-based pricing, order placement via store API, status tracking, and cancellation
- `FulfillmentProviderRegistry` — Pluggable registry with auto-detection from env vars (`KILN_FULFILLMENT_PROVIDER`, or auto-detect from `KILN_CRAFTCLOUD_API_KEY` / `KILN_SHAPEWAYS_CLIENT_ID` / `KILN_SCULPTEO_API_KEY`)
- Updated `server.py` and `cli/main.py` to use registry instead of hardcoded Craftcloud
- 87 new tests: 45 Shapeways (including OAuth2 token lifecycle), 33 Sculpteo, 9 registry
- Total fulfillment providers: 3 (Craftcloud, Shapeways, Sculpteo)
- Total test count: 1,898+

### Text-to-Model Generation
- New `kiln/src/kiln/generation/` module with `GenerationProvider` ABC and shared dataclasses
- **Meshy adapter** (`MeshyProvider`) — cloud text-to-3D via Meshy API (preview mode, async job model)
- **OpenSCAD adapter** (`OpenSCADProvider`) — local parametric generation, agent writes .scad code, Kiln compiles to STL
- **Mesh validation pipeline** (`validate_mesh()`) — binary/ASCII STL and OBJ parsing, manifold check, bounding box, dimension limits, polygon count limits. Zero external dependencies (pure `struct` parsing).
- 7 new MCP tools: `generate_model`, `generation_status`, `download_generated_model`, `await_generation`, `generate_and_print`, `validate_generated_mesh`
- 3 new CLI commands: `kiln generate`, `kiln generate-status`, `kiln generate-download`
- `generate_and_print` — full pipeline tool: text → generate → validate → slice → upload → print
- `await_generation` — polling tool for async cloud providers (like `await_print_completion`)
- Comprehensive test suite: unit tests for adapters (mock HTTP), validation pipeline, MCP tools, and CLI commands

### Post-Print Quality Validation
- `validate_print_quality` MCP tool — assesses print quality after completion
- Captures webcam snapshot (if available) and returns base64 or saves to file
- Analyses job events: retry count, progress consistency, timing anomalies
- Quality grading: PASS / WARNING / REVIEW based on detected issues
- Structured recommendations for agent follow-up
- Works with or without a job_id (auto-finds most recent completed job)

### CLI Test Coverage for Advanced Features
- 72 new CLI tests in `test_cli_advanced.py`
- Covers all 30+ untested commands: `snapshot`, `wait`, `history`, `cost`, `compare-cost`, `slice`
- Material subcommands: `set`, `show`, `spools`, `add-spool`
- Level, stream, sync (`status`/`now`/`configure`), plugins (`list`/`info`)
- Order subcommands: `materials`, `quote`, `place`, `status`, `cancel`
- Billing subcommands: `setup`, `status`, `history`
- Parametrized `--help` tests for all 30 command/subcommand combinations
- Total test count: 1,811+

### End-to-End Integration Test
- 13 integration tests in `test_integration.py`
- Full pipeline: discover → auth → preflight → upload → print → wait → history
- Slice → upload → print in one shot via `--print-after`
- Error propagation: preflight failure, upload failure, adapter error, printer offline, no webcam
- Tests compose real CLI commands with mock printer backend via `CliRunner`

### Bambu Webcam Support
- `get_snapshot()` on BambuAdapter — tries HTTPS/HTTP snapshot endpoint on the printer
- `get_stream_url()` on BambuAdapter — returns `rtsps://<host>:322/streaming/live/1` (Bambu LAN RTSP stream)
- Falls back gracefully to `None` if camera not accessible

### Resumable Downloads
- `resumable_download()` shared helper in `marketplaces/base.py`
- Uses HTTP `Range` headers to resume interrupted downloads from `.part` temp files
- Automatic retry with up to 3 attempts on failure
- Handles servers that don't support Range (restarts cleanly)
- Handles 416 Range Not Satisfiable (file already complete)
- Thingiverse and MyMiniFactory adapters now both use `resumable_download()`
- Atomic rename from `.part` → final file on completion

### Print Failure Analysis Tool
- `analyze_print_failure(job_id)` MCP tool
- Examines job record, related events (retries, errors, progress), and timing
- Produces structured diagnosis: symptoms, likely causes, recommendations
- Detects patterns: quick failures (setup issues), late failures (adhesion/cooling), retry exhaustion
- Correlates progress % at failure to suggest specific fixes (first-layer, supports, etc.)

### Bambu "cancelling" State + OctoPrint Bed Mesh
- Added `"cancelling"` → `PrinterStatus.BUSY` to Bambu adapter state map (was unmapped → UNKNOWN)
- Added `get_bed_mesh()` to OctoPrint adapter via Bed Level Visualizer plugin API (`/api/plugin/bedlevelvisualizer`)
- Returns `None` gracefully if plugin not installed

### Await Print Completion MCP Tool
- `await_print_completion(job_id, timeout, poll_interval)` MCP tool
- Supports both job-based tracking (via queue/scheduler) and direct printer monitoring
- Returns `outcome` field: completed, failed, cancelled, or timeout
- Includes progress log with completion % snapshots at each poll interval
- Configurable timeout (default 2h) and poll interval (default 15s)
- Lets agents fire-and-forget a print and pick up the result later

### Cost Comparison: Local vs. Fulfillment
- `compare_print_options` MCP tool — side-by-side local vs. outsourced cost comparison
- Runs local cost estimate (filament + electricity) and Craftcloud fulfillment quote in one call
- Returns unified comparison with `cheaper` recommendation, cost delta, time estimates
- `kiln compare-cost` CLI command with human-readable and JSON output
- Falls back gracefully if either source is unavailable

### Auto-Retry with Exponential Backoff
- Added `retry_backoff_base` parameter to `JobScheduler` (default 30s)
- Retry delays: 30s → 60s → 120s (exponential backoff on failure)
- `_retry_not_before` dict tracks per-job backoff timestamps
- Dispatch phase skips jobs still in backoff window
- Backoff state cleaned up on completion, permanent failure, or successful retry
- JOB_SUBMITTED event now includes `retry_delay_seconds` field

### Launch Readiness Fixes (Gap Analysis)
- **Bambu env var bug fix**: `access_code` in `config.py` now reads from `KILN_PRINTER_ACCESS_CODE` (falls back to `KILN_PRINTER_API_KEY` for backward compat). Previously both `api_key` and `access_code` read from the same env var, breaking Bambu auth via env config.
- **Automatic preflight in `start_print()`**: The MCP `start_print()` tool now runs `preflight_check()` automatically before starting a print. Returns `PREFLIGHT_FAILED` with full check details if the printer isn't ready. Agents no longer need to remember to call preflight first. Opt-out via `skip_preflight=True`.
- **`can_download` on `ModelSummary`**: Search results now include a `can_download` field so agents know upfront which marketplace results can be downloaded programmatically vs. require manual browser download (Cults3D).
- **TASKS.md backlog expansion**: Added 9 new tasks from gap analysis covering CLI test coverage, integration tests, Bambu webcam, await-completion tool, failure analysis, cost comparison, text-to-model generation, auto-retry, and post-print quality validation.
- **CLAUDE.md rule**: Added mandatory completed-task tracking — shipped features must always be moved from TASKS.md to COMPLETED_TASKS.md.

### Print Cost Estimation
- `kiln.cost_estimator` module with G-code extrusion analysis
- `MaterialProfile` and `CostEstimate` dataclasses
- 7 built-in material profiles (PLA, PETG, ABS, TPU, ASA, Nylon, PC)
- Parses absolute/relative E-axis extrusion, M82/M83 mode switching, G92 resets
- Slicer time comment extraction (PrusaSlicer, Cura, OrcaSlicer formats)
- `kiln cost` CLI command, `estimate_cost` and `list_materials` MCP tools
- 50 tests

### Multi-Material Tracking
- `kiln.materials` module with spool inventory and per-printer material tracking
- `LoadedMaterial`, `Spool`, `MaterialWarning` dataclasses
- `MaterialTracker` class: set/get material, check mismatch, deduct usage
- Spool CRUD operations with low/empty warnings via event bus
- `printer_materials` and `spools` DB tables
- `kiln material` CLI command group, 6 MCP tools
- 68 tests

### Bed Leveling Triggers
- `kiln.bed_leveling` module with configurable auto-leveling policies
- `LevelingPolicy` and `LevelingStatus` dataclasses
- `BedLevelManager`: subscribes to job completion events, evaluates triggers
- Policies: max prints between levels, max hours, auto-before-first-print
- Mesh variance calculation from probed data
- `leveling_history` DB table, `get_bed_mesh()` adapter method on Moonraker
- `kiln level` CLI command, 3 MCP tools
- 33 tests

### Webcam Streaming (MJPEG Proxy)
- `kiln.streaming` module with MJPEG proxy server
- `MJPEGProxy` class: reads upstream MJPEG stream, re-serves to local clients
- `StreamInfo` dataclass with client count, frames served, uptime tracking
- `get_stream_url()` adapter method on OctoPrint and Moonraker
- `kiln stream` CLI command, `webcam_stream` MCP tool
- 20 tests

### Cloud Sync
- `kiln.cloud_sync` module for syncing printer configs, jobs, events to cloud
- `SyncConfig` and `SyncStatus` dataclasses
- `CloudSyncManager`: background daemon thread, HMAC-SHA256 signed payloads
- Push unsynced jobs/events/printers, cursor-based incremental sync
- `sync_log` DB table with sync tracking
- `kiln sync` CLI command group, 3 MCP tools
- 30 tests

### Plugin System
- `kiln.plugins` module with entry-point-based plugin discovery
- `KilnPlugin` ABC: lifecycle hooks, MCP tools, event handlers, CLI commands
- `PluginManager`: discover, activate/deactivate, pre/post-print hooks
- `PluginHook` enum, `PluginInfo` and `PluginContext` dataclasses
- Plugin isolation: exceptions in hooks don't crash the system
- `kiln plugins` CLI command group, 2 MCP tools
- 35 tests

### Fulfillment Service Integration (Craftcloud)
- `kiln.fulfillment` module with `FulfillmentProvider` ABC and `CraftcloudProvider` implementation
- `FulfillmentProvider` abstract base: `list_materials()`, `get_quote()`, `place_order()`, `get_order_status()`, `cancel_order()`
- Craftcloud adapter: upload → quote → order workflow via REST API with Bearer token auth
- Dataclasses: `Material`, `Quote`, `QuoteRequest`, `OrderRequest`, `OrderResult`, `ShippingOption`
- `OrderStatus` enum with 9 states (pending, confirmed, in_production, shipped, delivered, cancelled, failed, refunded, unknown)
- `kiln order` CLI command group: `materials`, `quote`, `place`, `status`, `cancel`
- 5 MCP tools: `fulfillment_materials`, `fulfillment_quote`, `fulfillment_order`, `fulfillment_order_status`, `fulfillment_cancel`
- Rich CLI output formatters for quotes, orders, and material listings
- 32 tests

### Prusa Connect Adapter
- 4th printer backend via Prusa Link local REST API
- Supports Prusa MK4, XL, Mini+ with `X-Api-Key` authentication
- Maps all 9 Prusa Link states to `PrinterStatus` enum
- Read-only adapter: status, files, upload, print control (no temp set or raw G-code — Prusa Link limitation)
- `can_set_temp=False`, `can_send_gcode=False` in capabilities
- Wired into CLI config, discovery (HTTP probe on port 80), and MCP server
- 28 tests

### Multi-Marketplace Search
- `MarketplaceAdapter` ABC with `search()`, `get_details()`, `get_files()`, `download_file()`
- Concrete adapters: Thingiverse (REST), MyMiniFactory (REST v2), Cults3D (GraphQL, metadata-only)
- `MarketplaceRegistry` with `search_all()` fan-out, round-robin interleaving, per-adapter fault isolation
- `search_all_models`, `marketplace_info`, `download_and_upload` MCP tools
- `download_and_upload` combines marketplace download + printer upload in one step
- Cults3D adapter signals `supports_download = False` (API limitation)

### Slicer Integration
- `kiln.slicer` module wrapping PrusaSlicer / OrcaSlicer CLI
- Auto-detects slicer on PATH, macOS app bundles, and `KILN_SLICER_PATH` env var
- `kiln slice` CLI command with `--print-after` to slice-upload-print in one step
- `slice_model`, `find_slicer_tool`, `slice_and_print` MCP tools
- Supports STL, 3MF, STEP, OBJ, AMF input formats
- 17 tests

### Webcam Snapshot Support
- `get_snapshot()` optional method on `PrinterAdapter` base class
- OctoPrint: fetches from `/webcam/?action=snapshot`
- Moonraker: discovers webcam via `/server/webcams/list`, fetches snapshot URL
- `kiln snapshot` CLI command (save to file or base64 JSON)
- `printer_snapshot` MCP tool with optional save_path
- 5 tests

### kiln wait Command
- `kiln wait` blocks until the current print finishes
- Polls printer status at configurable interval (`--interval`)
- `--timeout` for maximum wait time
- Exits 0 on success (IDLE), 1 on error/offline
- Shows inline progress bar in human mode

### kiln history Command
- `kiln history` shows past prints from SQLite database
- Filters by status (`--status completed|failed|cancelled`)
- Rich table output with file, status, printer, duration, date
- `format_history()` output formatter

### Material/Filament Tracking in Preflight
- `kiln preflight --material PLA|PETG|ABS|TPU|ASA|Nylon|PC`
- Validates tool and bed target temperatures against material ranges
- Warns when temperatures are outside expected range for the material
- 4 tests

### Batch Printing
- `kiln print *.gcode` accepts multiple files via glob expansion
- `--queue` flag submits files to the job queue for sequential printing
- Without `--queue`, prints first file and lists remaining
- 4 tests

### CLI Flow Gaps Closed
- `kiln preflight` — CLI access to pre-print safety checks
- `kiln print` auto-uploads local files before starting
- BambuAdapter None guard when paho-mqtt not installed

### End-to-End Print Flow Diagram
- Created `docs/PRINT_FLOW.md` with full Mermaid diagram
- Covers: idea → find design → slice → setup → preflight → upload → print → monitor → done

### Kiln CLI
- Full Click-based CLI with 16 subcommands
- `kiln discover` (mDNS + HTTP probe), `kiln auth`, `kiln status`, `kiln files`, `kiln upload`, `kiln print`, `kiln cancel`, `kiln pause`, `kiln resume`, `kiln temp`, `kiln gcode`, `kiln printers`, `kiln use`, `kiln remove`, `kiln preflight`, `kiln serve`
- `--json` flag on every command for agent consumption
- Config management via `~/.kiln/config.yaml`
- Rich terminal output with plain-text fallback

### README Update
- Updated to reflect Moonraker stable, Bambu stable, Thingiverse, CLI, full MCP tool list

### Thingiverse Integration
- `ThingiverseClient` with search, browse, download
- 6 MCP tools: `search_models`, `model_details`, `model_files`, `download_model`, `browse_models`, `list_model_categories`
- 50+ tests

### Bambu Lab Adapter
- Full PrinterAdapter implementation over LAN MQTT
- Supports X1C, P1S, A1

### Moonraker Promoted to Stable
- Full coverage of all PrinterAdapter methods
- Tested against Klipper-based printers

### Core Infrastructure (by other instance)
- Job scheduler with background dispatch
- Priority queue with status tracking
- Event bus with pub/sub
- SQLite persistence for jobs and events
- Webhook delivery with HMAC signing
- API key authentication with scopes
- Billing/fee tracking for network jobs
- G-code safety validator
- Printer registry for fleet management
- MCP resources (kiln://status, kiln://printers, etc.)
- MCP prompt templates (print_workflow, fleet_workflow, troubleshooting)
