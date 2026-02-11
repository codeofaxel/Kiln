# Kiln — Completed Tasks

Record of finished features and milestones, newest first.

## 2026-02-10

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
