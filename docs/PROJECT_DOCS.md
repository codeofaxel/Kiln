<p align="center">
  <img src="assets/kiln-horizontal-dark.svg" alt="Kiln" width="400">
</p>

# Kiln Documentation

## About Kiln

### Overview

Kiln is agentic infrastructure for physical fabrication. It provides a unified interface for AI agents to control 3D printers, outsource to manufacturing services, and route jobs across distributed printer networks — all through the Model Context Protocol (MCP) or a conventional CLI.

**Three ways to print:**

- **🖨️ Your printers.** Control OctoPrint, Moonraker, Bambu Lab, or Prusa Link machines on your LAN — or remotely via Bambu Cloud.
- **🏭 Fulfillment centers.** *(API access required — in partner onboarding with Craftcloud and Sculpteo.)* Outsource to Craftcloud (150+ services) or Sculpteo (75+ materials). No printer required — or use alongside local printers for overflow and specialty materials.
- **🌐 Distributed network.** *(Coming soon.)* Route jobs to decentralized peer-to-peer printer networks, or register your own printer to earn revenue.

All three modes use the same MCP tools and CLI commands.

**Key properties:**

- **Local-first.** Local printer communication stays on your network. No cloud relay, no accounts, no telemetry.
- **Adapter-based.** One interface covers OctoPrint, Moonraker, Bambu Lab, and Prusa Link. New backends plug in without changing upstream consumers.
- **Safety-enforced.** Pre-flight checks, G-code validation, and temperature limits are protocol-level — not optional.
- **Agent-native.** Every operation returns structured JSON. Every error includes machine-readable status codes. `--json` on every CLI command.

### Supported Printers

| Backend | Protocol | Printers | Status |
|---|---|---|---|
| OctoPrint | HTTP REST | Any OctoPrint-connected printer | Stable |
| Moonraker | HTTP REST | Klipper-based (Voron, RatRig, etc.) | Stable |
| Bambu Lab | MQTT/LAN | X1C, P1S, A1 | Stable |
| Prusa Link | HTTP REST | MK4, XL, Mini+ | Stable |
| Elegoo | WebSocket/SDCP | Centauri Carbon, Saturn, Mars series. Neptune 4/OrangeStorm Giga use Moonraker. | Stable |

### Key Concepts

**PrinterAdapter** — Abstract base class defining the contract for all printer backends. Implements: status, files, upload, print, cancel, pause, resume, temperature, G-code, snapshot.

**PrinterStatus** — Normalized enum: `IDLE`, `PRINTING`, `PAUSED`, `ERROR`, `OFFLINE`. Every backend maps its native state model to this enum.

**MCP Tools** — Typed functions exposed to agents via the Model Context Protocol. Each tool has a defined input schema and returns structured JSON.

**MarketplaceAdapter** — Abstract base class for 3D model repositories. Implements: search, details, files, download. Concrete adapters for MyMiniFactory, Cults3D, and Thingiverse (deprecated — acquired by MyMiniFactory, Feb 2026).

**MarketplaceRegistry** — Manages connected marketplace adapters. Provides `search_all()` for parallel fan-out search across all sources with round-robin result interleaving.

**GenerationProvider** — Abstract base class for text-to-3D model generation backends. Implements: generate, get_job_status, download_result. Concrete providers for Meshy (cloud AI) and OpenSCAD (local parametric).

**Mesh Validation** — Pipeline that checks generated STL/OBJ files for 3D-printing readiness: geometry parsing, manifold checks, dimension limits, polygon count validation. Uses pure Python (no external mesh libraries).

**Job Queue** — Priority queue backed by SQLite. Jobs are dispatched to idle printers by a background scheduler with history-based smart routing (best-performing printer for the job's file/material is preferred).

**DeviceType** — Enum classifying physical devices. Currently covers FDM printers with room for future device types.

**DeviceAdapter** — Alias for `PrinterAdapter`. Alternative import name.

**Cross-Printer Learning** — Agent-curated outcome database (`print_outcomes` table) that records success/failure/quality per printer and file. Safety-validated: rejects physically dangerous parameter values.

**PaymentProvider** — Abstract base class for payment processing backends. Implements: create_payment, get_payment_status, refund_payment, authorize_payment, capture_payment, cancel_payment. Concrete providers for Stripe and Circle (USDC).

**PaymentManager** — Orchestrates payment collection across providers. Routes to the correct rail (Stripe/Circle/crypto), enforces spend limits, persists transactions, and emits payment events.

**HeaterWatchdog** — Background daemon that monitors heater state and auto-cools idle heaters after a configurable timeout (default 30 min). Prevents heaters from being left on when no print is active.

**LicenseManager** — Offline-first license tier management. Resolves tier from key prefix (`kiln_pro_`, `kiln_biz_`) with cached remote validation fallback. Never blocks printer operations.

---

## Getting Started

### Installation

```bash
# From PyPI
pip install kiln3d

# From source (development)
pip install -e ./kiln
```

Requirements: Python 3.10+, pip.

### Discover Printers

```bash
kiln discover
```

Scans your local LAN (Ethernet or Wi-Fi) using mDNS and HTTP probing.
If discovery does not find your printer, add it directly by IP with `kiln auth`.

### Add a Printer

```bash
# OctoPrint
kiln auth --name ender3 --host http://octopi.local --type octoprint --api-key YOUR_KEY

# Moonraker
kiln auth --name voron --host http://voron.local:7125 --type moonraker

# Prusa Link
kiln auth --name prusa-mini --host http://192.168.1.44 --type prusaconnect --api-key YOUR_KEY

# Bambu Lab
kiln auth --name x1c --host 192.168.1.100 --type bambu --access-code 12345678 --serial 01P00A000000001
```

### Ethernet-Only Setup (No Wi-Fi)

Kiln works the same over Ethernet and Wi-Fi because it talks to printer APIs over LAN IP.

```bash
# Verify endpoint from your host:
curl http://192.168.1.44/api/v1/status                  # Prusa Link
curl http://192.168.1.50:7125/server/info               # Moonraker
curl http://192.168.1.60/api/version                    # OctoPrint

# Register directly by IP (no discovery required):
kiln auth --name printer --host http://192.168.1.44 --type prusaconnect --api-key YOUR_KEY
```

### First Print

```bash
kiln status                    # Verify printer is online
kiln preflight --material PLA  # Run safety checks
kiln upload benchy.gcode       # Upload file to printer
kiln print benchy.gcode        # Start printing
kiln wait                      # Block until complete
```

### Slice and Print (STL to Object)

```bash
kiln slice benchy.stl --print-after
```

Auto-detects PrusaSlicer or OrcaSlicer, slices to G-code, uploads, and starts printing.

---

## CLI Reference

### Global Options

| Flag | Description |
|---|---|
| `--printer NAME` | Target a specific printer (overrides active) |
| `--json` | Output structured JSON (for agents/scripts) |
| `--help` | Show command help |

### Commands

#### `kiln discover`
Scan the local LAN for printers via mDNS and HTTP probing.
If no printers are found, use `kiln auth --host <ip>` to register directly.

#### `kiln auth`
Save printer credentials to `~/.kiln/config.yaml`.

| Flag | Required | Description |
|---|---|---|
| `--name` | Yes | Friendly name for this printer |
| `--host` | Yes | Printer URL (e.g., `http://octopi.local`) |
| `--type` | Yes | Backend: `octoprint`, `moonraker`, `bambu`, `prusaconnect` |
| `--api-key` | OctoPrint, Prusa Link | OctoPrint or Prusa Link API key |
| `--access-code` | Bambu | Bambu Lab access code |
| `--serial` | Bambu | Bambu Lab serial number |

#### `kiln status`
Get printer state, temperatures, and active job progress.

#### `kiln files`
List G-code files available on the printer.

#### `kiln upload <file>`
Upload a local G-code file to the printer.

#### `kiln print <files>... [--queue]`
Start printing. Accepts multiple files via glob expansion. With `--queue`, files are submitted to the job queue for sequential printing.

#### `kiln cancel`
Cancel the active print job.

#### `kiln pause` / `kiln resume`
Pause or resume the active print.

#### `kiln temp [--tool N] [--bed N]`
Get current temperatures, or set targets. Without flags, returns current temps.

#### `kiln gcode <commands>...`
Send raw G-code commands to the printer. Commands are validated for safety before sending.

#### `kiln preflight [--material MAT]`
Run pre-print safety checks. Optional `--material` validates temperatures against expected ranges for: PLA, PETG, ABS, TPU, ASA, Nylon, PC.

#### `kiln slice <file> [--print-after] [--profile PATH] [--output-dir DIR]`
Slice an STL/3MF/STEP/OBJ/AMF file to G-code. `--print-after` chains upload and print.

#### `kiln snapshot [--save PATH]`
Capture a webcam snapshot. `--save` writes to file; otherwise returns base64 in JSON mode.

#### `kiln wait [--timeout N] [--interval N]`
Block until the current print finishes. Returns exit code 0 on success, 1 on error/timeout.

#### `kiln history [--status S] [--limit N]`
Query past print records from the local database. Filter by `completed`, `failed`, `cancelled`.

#### `kiln printers`
List all saved printers with type and active status.

#### `kiln use <name>`
Switch the active printer.

#### `kiln remove <name>`
Remove a saved printer from config.

#### `kiln serve`
Start the MCP server (for agent integration).

#### `kiln cost <file> [--material MAT] [--electricity-rate N] [--printer-wattage N]`
Estimate the cost of printing a G-code file. Analyzes extrusion, calculates filament weight and cost, and optionally includes electricity cost.

#### `kiln material set|show|spools|add-spool`
Material tracking commands. `set` records loaded material on a printer. `show` displays current material. `spools` lists spool inventory. `add-spool` registers a new spool.

#### `kiln level [--status] [--trigger] [--set-prints N] [--set-hours N] [--enable/--disable]`
Bed leveling management. Check status, manually trigger leveling, or configure auto-leveling policy.

#### `kiln stream [--port 8081] [--stop]`
Start or stop the MJPEG webcam streaming proxy. Proxies the upstream printer webcam stream to a local HTTP endpoint.

#### `kiln sync status|now|configure`
Cloud sync management. `status` shows sync state. `now` triggers immediate sync. `configure` sets cloud URL and API key.

#### `kiln plugins list|info`
Plugin management. `list` shows all discovered plugins. `info` shows details for a specific plugin.

---

## MCP Server Reference

### Installation

The MCP server starts via `kiln serve` or `python -m kiln serve`.

### Claude Desktop Integration

Add to `~/.config/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "kiln": {
      "command": "python",
      "args": ["-m", "kiln", "serve"],
      "env": {
        "KILN_PRINTER_HOST": "http://octopi.local",
        "KILN_PRINTER_API_KEY": "your_key",
        "KILN_PRINTER_TYPE": "octoprint"
      }
    }
  }
}
```

### Tool Catalog (Selected)

Kiln exposes **198 MCP tools** in total. The most commonly used tools are documented below by category. Run `kiln tools` for the complete list.

#### Printer Control

| Tool | Input | Output |
|---|---|---|
| `printer_status` | — | Printer state, temps, job progress |
| `start_print` | `filename` | Confirmation or error |
| `cancel_print` | — | Confirmation or error |
| `pause_print` | — | Confirmation or error |
| `resume_print` | — | Confirmation or error |
| `set_temperature` | `tool_temp`, `bed_temp` | Confirmation |
| `send_gcode` | `commands` | Response lines |
| `validate_gcode` | `commands` | Validation result |
| `preflight_check` | `filename`, `material` | Pass/fail with details |

#### File Management

| Tool | Input | Output |
|---|---|---|
| `printer_files` | — | File list with sizes and dates |
| `upload_file` | `local_path` | Upload confirmation |

#### Slicing

| Tool | Input | Output |
|---|---|---|
| `slice_model` | `input_path`, `profile`, `output_dir` | G-code path, slicer info |
| `find_slicer_tool` | — | Detected slicer path and version |
| `slice_and_print` | `input_path`, `profile` | Slice + upload + print result |

#### Monitoring

| Tool | Input | Output |
|---|---|---|
| `printer_snapshot` | `save_path` | Image bytes or base64 |

#### Fleet Management

| Tool | Input | Output |
|---|---|---|
| `fleet_status` | — | All printer states |
| `register_printer` | `name`, `type`, `host`, ... | Confirmation |

#### Job Queue

| Tool | Input | Output |
|---|---|---|
| `submit_job` | `filename`, `printer`, `priority`, `metadata` | Job ID (routes to best printer via historical success rate when printer is unspecified) |
| `job_status` | `job_id` | Job state and progress |
| `queue_summary` | — | Queue overview |
| `cancel_job` | `job_id` | Confirmation |

#### Model Discovery

| Tool | Input | Output |
|---|---|---|
| `search_all_models` | `query`, `page`, `sources` | Interleaved results from all marketplaces |
| `marketplace_info` | — | Connected sources, setup hints |
| `search_models` | `query`, `page` | Single-marketplace model list (Thingiverse — deprecated) |
| `model_details` | `thing_id` | Model metadata |
| `model_files` | `thing_id` | File list |
| `download_model` | `thing_id`, `file_id` | Local path |
| `download_and_upload` | `file_id`, `source`, `printer_name` | Download + upload in one step |
| `browse_models` | `sort`, `category` | Model list |
| `list_model_categories` | — | Category list |

#### System

| Tool | Input | Output |
|---|---|---|
| `kiln_health` | — | Version, uptime, module status |
| `recent_events` | `limit` | Event list |
| `register_webhook` | `url`, `events`, `secret` | Webhook ID |
| `list_webhooks` | — | Webhook list |
| `delete_webhook` | `webhook_id` | Confirmation |

#### Cost Estimation

| Tool | Input | Output |
|---|---|---|
| `estimate_cost` | `file_path`, `material`, `electricity_rate`, `printer_wattage` | Cost breakdown |
| `list_materials` | — | Material profiles |

#### Material Tracking

| Tool | Input | Output |
|---|---|---|
| `set_material` | `printer_name`, `material`, `color`, `spool_id`, `tool_index` | Confirmation |
| `get_material` | `printer_name` | Loaded materials |
| `check_material_match` | `printer_name`, `expected_material` | Match result or warning |
| `list_spools` | — | Spool inventory |
| `add_spool` | `material`, `color`, `brand`, `weight`, `cost` | Spool details |
| `remove_spool` | `spool_id` | Confirmation |

#### Bed Leveling

| Tool | Input | Output |
|---|---|---|
| `bed_level_status` | `printer_name` | Leveling status and policy |
| `trigger_bed_level` | `printer_name` | Leveling result |
| `set_leveling_policy` | `printer_name`, policy params | Confirmation |

#### Webcam Streaming

| Tool | Input | Output |
|---|---|---|
| `webcam_stream` | `printer_name`, `action`, `port` | Stream status |

#### Cloud Sync

| Tool | Input | Output |
|---|---|---|
| `cloud_sync_status` | — | Sync state |
| `cloud_sync_now` | — | Sync results |
| `cloud_sync_configure` | `cloud_url`, `api_key`, `interval` | Confirmation |

#### Plugins

| Tool | Input | Output |
|---|---|---|
| `list_plugins` | — | Plugin list |
| `plugin_info` | `name` | Plugin details |

#### Billing & Payments

| Tool | Input | Output |
|---|---|---|
| `billing_status` | `user_id` | Fee policy, monthly spend, payment methods, spend limits |
| `billing_summary` | — | Aggregated billing summary |
| `billing_history` | `limit` | Recent billing charges with payment outcomes |
| `billing_setup_url` | `rail` | URL to link a payment method |
| `billing_check_setup` | — | Polls pending Stripe SetupIntent; persists payment method on success |
| `check_payment_status` | `payment_id` | Non-blocking check of Circle/Stripe payment finality |

#### Distributed Manufacturing Network *(Coming Soon)*

| Tool | Input | Output |
|---|---|---|
| `network_register_printer` | `name`, `location`, `materials` | Registration confirmation |
| `network_update_printer` | `printer_id`, `available` | Update confirmation |
| `network_list_printers` | — | Your registered network printers |
| `network_find_printers` | `material`, `location` | Available printers on the network |
| `network_submit_job` | `file_url`, `material`, `printer_id` | Network job ID |
| `network_job_status` | `job_id` | Job tracking details |

#### Safety Audit

| Tool | Input | Output |
|---|---|---|
| `safety_audit` | — | Safety compliance report |
| `get_session_log` | `session_id?` | Full tool call history for an agent session (defaults to current session) |
| `safety_settings` | — | Current safety/auto-print settings |
| `safety_status` | — | Comprehensive safety status |

#### Fulfillment Services

| Tool | Input | Output |
|---|---|---|
| `fulfillment_materials` | `provider` | Available materials from external print services |
| `fulfillment_quote` | `file_path`, `material_id`, `quantity`, `provider` | Manufacturing quote with shipping options |
| `fulfillment_order` | `quote_id`, `shipping_option_id`, `payment_hold_id` | Order confirmation with billing |
| `fulfillment_order_status` | `order_id` | Order tracking details |
| `fulfillment_cancel` | `order_id` | Cancellation confirmation |
| `compare_print_options` | `file_path`, `material` | Local vs. fulfillment cost comparison |
| `fulfillment_compare_providers` | `file_path`, `material_id`, `quantity` | Side-by-side quotes from all providers |
| `fulfillment_filter_materials` | `technology`, `color`, `finish`, `max_price_per_cm3` | Filtered material catalog |
| `fulfillment_batch_quote` | `file_paths`, `material_id`, `quantities` | Per-item quotes with aggregated total |
| `fulfillment_provider_health` | — | Health status of all fulfillment providers |
| `fulfillment_order_history` | `limit`, `provider` | Past orders for review or reorder |
| `fulfillment_reorder` | `order_id` | Past order details with reorder hints |
| `fulfillment_insurance_options` | `order_value` | Tiered shipping insurance options |

#### Consumer Workflow (No Printer Required)

| Tool | Input | Output |
|---|---|---|
| `consumer_onboarding` | — | Step-by-step guide from idea to delivered product |
| `validate_shipping_address` | `street`, `city`, `country`, `state`, `postal_code` | Address validation with normalization |
| `recommend_material` | `use_case`, `budget`, constraints | Ranked material recommendations with reasoning |
| `estimate_price` | `technology`, `volume_cm3` or `dimensions` | Instant price range (no API call) |
| `estimate_timeline` | `technology`, `quantity`, `country` | Order-to-delivery timeline with stage breakdown |
| `supported_shipping_countries` | — | Supported shipping countries (23+ countries) |

#### Model Generation

| Tool | Input | Output |
|---|---|---|
| `generate_model` | `prompt`, `provider`, `format`, `style` | Generation job ID |
| `generation_status` | `job_id` | Job status and progress |
| `download_generated_model` | `job_id`, `output_dir` | Local file path with mesh validation |
| `await_generation` | `job_id`, `timeout`, `poll_interval` | Completed job result |
| `generate_and_print` | `prompt`, `provider`, `printer_name`, `profile` | Full pipeline: generate → validate → slice → print |
| `validate_generated_mesh` | `file_path` | Mesh validation report |

#### Print Analysis

| Tool | Input | Output |
|---|---|---|
| `await_print_completion` | `timeout_minutes`, `poll_interval` | Final print status |
| `analyze_print_failure` | `job_id` | Failure diagnosis with causes and recommendations |
| `validate_print_quality` | `job_id` | Quality assessment with snapshot and event analysis |

#### Firmware Updates

| Tool | Input | Output |
|---|---|---|
| `firmware_status` | — | Component versions and update availability |
| `update_firmware` | `component` | Update result |
| `rollback_firmware` | `component` | Rollback result |

#### Vision Monitoring (Agent-Delegated)

Kiln provides structured monitoring data (webcam snapshots, temperatures, print progress, phase context, failure hints) to agents. The agent's own vision model (Claude, GPT-4V, Gemini, etc.) analyzes the snapshots for defects — Kiln does not embed its own vision model. Kiln adds lightweight heuristic validation (brightness/variance checks) to detect blocked cameras or corrupted frames.

| Tool | Input | Output |
|---|---|---|
| `monitor_print_vision` | `printer_name`, `include_snapshot`, `save_snapshot` | Snapshot + state + phase + failure hints for agent vision analysis |
| `watch_print` | `printer_name`, `snapshot_interval`, `max_snapshots`, `timeout`, `poll_interval` | Starts background watcher; returns `watch_id` immediately |
| `watch_print_status` | `watch_id` | Current progress, snapshots, outcome of background watcher |
| `stop_watch_print` | `watch_id` | Stops background watcher and returns final state |

#### Cross-Printer Learning

| Tool | Input | Output |
|---|---|---|
| `record_print_outcome` | `job_id`, `outcome`, `quality_grade`, `failure_mode`, `settings`, `notes` | Confirmation (safety-validated) |
| `get_printer_insights` | `printer_name`, `limit` | Success rate, failure breakdown, material stats, confidence |
| `suggest_printer_for_job` | `file_hash`, `material_type`, `file_name` | Ranked printers by success rate + availability |
| `recommend_settings` | `printer_name`, `material_type`, `file_hash` | Median temps/speed, mode slicer profile, confidence, quality distribution |


#### Pipelines (Runtime)

| Tool | Input | Output |
|---|---|---|
| `pipeline_status` | `pipeline_id` | Current step, progress, elapsed time |
| `pipeline_pause` | `pipeline_id` | Confirmation (pauses at step boundary) |
| `pipeline_resume` | `pipeline_id` | Confirmation |
| `pipeline_abort` | `pipeline_id` | Confirmation with cleanup summary |
| `pipeline_retry_step` | `pipeline_id` | Retry result for the failed step |

#### Model Cache

| Tool | Input | Output |
|---|---|---|
| `cache_model` | `file_path`, `source`, `source_id`, `tags` | Cache entry with local path |
| `search_cached_models` | `query`, `source`, `tags` | Matching cached models |
| `get_cached_model` | `cache_id` | Model details and local path |
| `list_cached_models` | `limit`, `sort` | All cached models with metadata |
| `delete_cached_model` | `cache_id` | Confirmation |

#### Database Management

| Tool | Input | Output |
|---|---|---|
| `backup_database` | `output_dir` | Backup file path and size |
| `verify_audit_integrity` | — | Integrity report (pass/fail, broken links) |
| `clean_agent_memory` | `max_age_days`, `dry_run` | Pruned entry count |

#### Printer Trust

| Tool | Input | Output |
|---|---|---|
| `list_trusted_printers` | — | Trusted printers with fingerprints and verification status |
| `trust_printer` | `printer_name`, `fingerprint` | Trust confirmation |
| `untrust_printer` | `printer_name` | Removal confirmation |

### MCP Resources

Read-only resources for agent context:

| URI | Description |
|---|---|
| `kiln://status` | System-wide snapshot |
| `kiln://printers` | Fleet listing |
| `kiln://printers/{name}` | Single printer detail |
| `kiln://queue` | Job queue summary |
| `kiln://queue/{job_id}` | Single job detail |
| `kiln://events` | Recent events (last 50) |

---

## Printer Adapters

### OctoPrint

Communicates via HTTP REST. Requires an API key (generated in OctoPrint settings).

**Configuration:**
```yaml
type: octoprint
host: http://octopi.local
api_key: YOUR_KEY
```

**State Mapping:** OctoPrint returns state as a set of boolean flags (`printing`, `paused`, `error`, `ready`, `operational`). The adapter maps flag combinations to `PrinterStatus`.

**Webcam:** Snapshots via `GET /webcam/?action=snapshot`.

### Moonraker

Communicates via HTTP REST. No authentication required by default (Moonraker trusts local network).

**Configuration:**
```yaml
type: moonraker
host: http://voron.local:7125
```

**State Mapping:** Moonraker returns Klipper state as a string (`ready`, `printing`, `paused`, `error`, `shutdown`). Direct mapping to `PrinterStatus`.

**Webcam:** Discovers cameras via `GET /server/webcams/list`, then fetches the snapshot URL.

### Bambu Lab

Communicates via MQTT over LAN. Requires access code and serial number (found in printer settings).

**Configuration:**
```yaml
type: bambu
host: 192.168.1.100
access_code: "12345678"
serial: "01P00A000000001"
```

**Note:** Requires `paho-mqtt` package. Kiln gracefully handles its absence.

### Prusa Link

Communicates via Prusa Link REST API (`/api/v1/`). Requires a Prusa Link API key.

**Configuration:**
```yaml
type: prusaconnect
host: http://prusa-mk4.local
api_key: YOUR_KEY
```

**State Mapping:** Prusa Link returns state as a string (`IDLE`, `PRINTING`, `PAUSED`, `FINISHED`, `ERROR`, etc.). Direct mapping to `PrinterStatus`.

**Limitations:** No raw G-code or direct temperature control — these are managed through print files.

### Elegoo (SDCP)

Communicates via the SDCP (Smart Device Control Protocol) over WebSocket on port 3030. No authentication required on local network. Covers Elegoo printers with cbd-tech/ChituBox mainboards: Centauri Carbon, Centauri Carbon 2, Saturn, Mars series.

> **Note:** Elegoo Neptune 4 and OrangeStorm Giga run Klipper/Moonraker. Use the `moonraker` adapter type for those printers (port 4408 for Fluidd, 7125 for Moonraker API).

**Configuration:**
```yaml
type: elegoo
host: 192.168.1.50
mainboard_id: ABCD1234ABCD1234  # optional, auto-discovered
```

**Environment variables:**
```bash
export KILN_PRINTER_HOST=192.168.1.50
export KILN_PRINTER_TYPE=elegoo
export KILN_PRINTER_MAINBOARD_ID=ABCD1234  # optional
```

**State Mapping:** SDCP returns numeric status codes. Mapping: 0=IDLE, 5/8/9/20=BUSY, 10=PAUSED, 13=PRINTING. Unknown codes default to UNKNOWN.

**File Upload:** SDCP uses a pull-based upload model — Kiln starts a temporary HTTP server, sends the printer a download URL, and the printer fetches the file. The printer must be able to reach the host machine on the ephemeral port.

**Discovery:** UDP broadcast of `M99999` on port 3000. All SDCP printers on the network respond with their details.

**Dependencies:** Requires `websocket-client` package. Install with `pip install 'kiln[elegoo]'`.

---

## Model Marketplaces

Kiln provides a `MarketplaceAdapter` interface (mirroring the printer adapter pattern) for searching and downloading 3D models from external repositories. A `MarketplaceRegistry` manages connected adapters and exposes `search_all()` for parallel fan-out across all sources.

### Supported Marketplaces

| Marketplace | Protocol | Auth | Download Support |
|---|---|---|---|
| Thingiverse | HTTP REST | Bearer token | Yes | *Deprecated — acquired by MyMiniFactory (Feb 2026). API may be sunset or merged. Prefer MyMiniFactory adapter for new integrations.* |
| MyMiniFactory | HTTP REST v2 | API key (`?key=`) | Yes |
| Cults3D | GraphQL | HTTP Basic | No (metadata-only) |

### Configuration

Set environment variables for each marketplace you want to enable:

```bash
export KILN_THINGIVERSE_TOKEN=your_token       # Deprecated — Thingiverse acquired by MyMiniFactory (Feb 2026)
export KILN_MMF_API_KEY=your_key               # MyMiniFactory developer key
export KILN_CULTS3D_USERNAME=your_username      # Cults3D account username
export KILN_CULTS3D_API_KEY=your_key            # https://cults3d.com/en/api/keys
```

Adapters are auto-registered at server startup based on available credentials. Only configured marketplaces participate in searches.

### Unified Search

`search_all_models` fans out the query to all connected marketplaces in parallel using a thread pool. Results are interleaved round-robin across sources for variety. If one marketplace fails (rate limit, timeout), results from the others still return.

Each result includes a `source` field identifying the marketplace, plus print-readiness hints:
- `is_free` — whether the model is free to download
- `has_sliceable_files` — has STL/3MF/OBJ files that need slicing
- `has_printable_files` — has ready-to-print G-code

### Download and Upload

`download_and_upload` combines marketplace file download with printer upload in a single tool call. Accepts a `source` parameter to target any marketplace that supports downloads. Cults3D is excluded (metadata-only).

---

## Safety Systems

### Pre-flight Checks

Every print job should pass through `preflight_check()`:

1. **Printer online** — Adapter can reach the printer
2. **Printer idle** — No active job running
3. **File exists** — Target file is on the printer
4. **Temperature safe** — Targets within safe bounds
5. **Material validation** — When `--material` specified, temperatures match expected ranges

### G-code Validation

The `validate_gcode()` function screens commands before they reach hardware:

- **Blocked:** Firmware reset, EEPROM wipe, unsafe axis movements
- **Warned:** High temperatures, rapid moves without homing
- **Passed:** Standard print commands, temperature sets within limits

### Temperature Limits

| Limit | Value |
|---|---|
| Hotend maximum | 300C |
| Bed maximum | 130C |
| PLA range (tool) | 180-220C |
| PETG range (tool) | 220-260C |
| ABS range (tool) | 230-270C |

---

## Authentication

Disabled by default. Enable for multi-user or network-exposed setups.

```bash
export KILN_AUTH_ENABLED=1
export KILN_AUTH_KEY=your_secret_key
```

**Scopes:** `read`, `write`, `admin`.

Read-only tools (`printer_status`, `printer_files`, `fleet_status`) never require authentication.

---

## Webhooks

Register HTTP endpoints for real-time event notifications:

```
register_webhook(url="https://example.com/hook", events=["job.completed", "print.failed"])
```

**Event types:** `job.submitted`, `job.dispatched`, `job.completed`, `job.failed`, `job.cancelled`, `print.started`, `print.paused`, `print.resumed`, `print.failed`, `printer.online`, `printer.offline`, `printer.error`, `stream.started`, `stream.stopped`, `sync.completed`, `sync.failed`, `leveling.triggered`, `leveling.completed`, `leveling.failed`, `leveling.needed`, `material.loaded`, `material.mismatch`, `material.spool_low`, `material.spool_empty`, `plugin.loaded`, `plugin.error`, `vision.check`, `vision.alert`.

Payloads are signed with HMAC-SHA256 when a secret is provided.

---

## Configuration

### Config File

Location: `~/.kiln/config.yaml`

```yaml
active_printer: my-voron

printers:
  my-voron:
    type: moonraker
    host: http://voron.local:7125

  ender3:
    type: octoprint
    host: http://octopi.local
    api_key: ABC123

  x1c:
    type: bambu
    host: 192.168.1.100
    access_code: "12345678"
    serial: "01P00A000000001"

settings:
  timeout: 30
  retries: 3
```

### Environment Variables

| Variable | Description |
|---|---|
| `KILN_PRINTER_HOST` | Printer URL (fallback when no config) |
| `KILN_PRINTER_API_KEY` | API key for OctoPrint |
| `KILN_PRINTER_TYPE` | Backend type |
| `KILN_SLICER_PATH` | Explicit path to slicer binary |
| `KILN_THINGIVERSE_TOKEN` | Thingiverse API token *(deprecated — acquired by MyMiniFactory, Feb 2026)* |
| `KILN_MMF_API_KEY` | MyMiniFactory API key |
| `KILN_CULTS3D_USERNAME` | Cults3D account username |
| `KILN_CULTS3D_API_KEY` | Cults3D API key |
| `KILN_AUTH_ENABLED` | Enable API key auth (1/0) |
| `KILN_AUTH_KEY` | Secret key for auth |

### Precedence

CLI flags > Environment variables > Config file.

---

## Development

### Setup

```bash
git clone https://github.com/codeofaxel/Kiln.git
cd kiln
pip install -e "./kiln[dev]"
pip install -e "./octoprint-cli[dev]"
```

### Running Tests

```bash
cd kiln && python3 -m pytest tests/ -v    # 5,064 tests
cd ../octoprint-cli && python3 -m pytest tests/ -v  # 223 tests
```

### Adding a New Printer Adapter

1. Create `kiln/src/kiln/printers/yourbackend.py`
2. Implement all abstract methods from `PrinterAdapter` in `base.py`
3. Return typed dataclasses — never raw dicts
4. Map all backend states to `PrinterStatus` enum
5. Handle connection failures gracefully (return `OFFLINE`)
6. Add to the adapter factory in `cli/config.py`
7. Write tests following the pattern in `tests/test_server.py`

### Project Structure

```
kiln/src/kiln/
    __init__.py
    __main__.py          # Entry point
    server.py            # MCP server + all tools
    slicer.py            # PrusaSlicer/OrcaSlicer integration
    registry.py          # Fleet printer registry
    queue.py             # Priority job queue
    scheduler.py         # Background job dispatcher with smart routing
    events.py            # Pub/sub event bus
    persistence.py       # SQLite storage
    webhooks.py          # Webhook delivery
    auth.py              # API key authentication
    billing.py           # Fee tracking
    gcode.py             # G-code safety validator (per-printer limits)
    safety_profiles.py   # Bundled safety database (28 printer models)
    slicer_profiles.py   # Bundled slicer profiles (auto .ini generation)
    printer_intelligence.py  # Printer knowledge base (quirks, materials, fixes)
    pipelines.py         # Pre-validated print pipelines (quick_print, calibrate, benchmark)
    cost_estimator.py    # Print cost estimation
    materials.py         # Multi-material tracking
    bed_leveling.py      # Bed leveling trigger system
    streaming.py         # MJPEG webcam proxy
    cloud_sync.py        # Cloud sync manager
    heater_watchdog.py   # Auto-cooldown watchdog for idle heaters
    licensing.py         # License tier management (Free/Pro/Business)
    model_metadata.py    # Model metadata management
    wallets.py           # Crypto wallet configuration (Solana/Ethereum donations)
    plugins.py           # Plugin system
    backup.py            # Database backup and restore
    log_config.py        # Structured logging configuration
    model_cache.py       # Local model cache with metadata tracking
    payments/
        base.py          # PaymentProvider interface, PaymentRail enum
        manager.py       # Payment orchestration across providers
        stripe_provider.py   # Stripe payment provider
        circle_provider.py   # Circle USDC payment provider
    gateway/
        network.py       # Distributed manufacturing network client (coming soon)
    data/
        safety_profiles.json     # Per-printer safety limits (temps, feedrates, flow)
        slicer_profiles.json     # Per-printer slicer settings (INI key-values)
        printer_intelligence.json  # Firmware quirks, materials, failure modes
    printers/
        base.py          # Abstract PrinterAdapter + dataclasses
        octoprint.py     # OctoPrint REST adapter
        moonraker.py     # Moonraker REST adapter
        bambu.py         # Bambu Lab MQTT adapter
        prusaconnect.py  # Prusa Link adapter
        elegoo.py        # Elegoo SDCP adapter (WebSocket)
    fulfillment/
        base.py          # Fulfillment adapter interface
        registry.py      # Provider registry and factory
        craftcloud.py    # Craftcloud v5 API client (upload → price → cart → order)
        sculpteo.py      # Sculpteo partner API client
    marketplaces/
        base.py          # Marketplace adapter interface
        myminifactory.py # MyMiniFactory API client (primary)
        cults3d.py       # Cults3D API client
        thingiverse.py   # Thingiverse API client (deprecated — acquired by MMF)
    cli/
        main.py          # Click CLI entry point
        config.py        # Config management
        discovery.py     # mDNS printer scanning
        output.py        # JSON/text formatting
```
