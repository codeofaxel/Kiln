<p align="center">
  <img src="assets/kiln-horizontal-dark.svg" alt="Kiln" width="400">
</p>

# Kiln Documentation

## About Kiln

### Overview

Kiln is the intelligence layer between idea and physical object. It provides a unified interface for AI agents to design, validate, and manufacture 3D-printed parts — controlling local printers, searching third-party model marketplaces (Thingiverse, MyMiniFactory, Cults3D), and routing jobs to fulfillment providers (Craftcloud) — all through the Model Context Protocol (MCP) or a <!-- KILN_CLI_COUNT:OLD --> 220-command CLI.

**Clarification:** Kiln does **not** operate its own marketplace or manufacturing network. It integrates with third-party marketplaces for model discovery and third-party fulfillment providers for outsourced manufacturing. Kiln is orchestration and agent infrastructure, not a supply-side platform.

**Three ways to get to a print:**

- **🖨️ Your printers.** Control OctoPrint, Moonraker, Creality, Bambu Lab, Prusa Link, Elegoo, or Direct USB/Serial machines on your LAN — or remotely via Bambu Cloud when configured.
- **🌐 Third-party model search.** Search external model repositories such as MyMiniFactory, Cults3D, and Thingiverse, then download where the provider permits, slice, and print.
- **🏭 Fulfillment providers.** Route jobs to third-party fulfillment services like Craftcloud, or through connected provider APIs for overflow and specialty materials.

All three modes use the same MCP tools and CLI commands.

**Non-goals:**

- Operating a first-party marketplace or manufacturing network
- Replacing third-party supply-side platforms
- Owning marketplaces instead of integrating with them
- Acting as merchant of record for provider-routed manufacturing orders

**Key properties:**

- **Local-first.** Local printer communication stays on your network. No cloud relay, no accounts, no telemetry.
- **Adapter-based.** One interface covers OctoPrint, Moonraker, Creality, Bambu Lab, Prusa Link, Elegoo, and Direct USB/Serial. New backends plug in without changing upstream consumers.
- **Safety-enforced.** Pre-flight checks, G-code validation, and temperature limits are protocol-level — not optional.
- **Agent-native.** Every operation returns structured JSON. Every error includes machine-readable status codes. `--json` on every CLI command.

### Supported Printers

| Backend | Protocol | Printers | Status |
|---|---|---|---|
| OctoPrint | HTTP REST | Any OctoPrint-connected printer | Stable |
| Moonraker | HTTP REST | Klipper-based (Voron, RatRig, etc.) | Stable |
| Creality | HTTP REST via Moonraker | K1/K2/Hi/Ender V3 KE-class printers when local Moonraker is reachable; older Marlin Creality machines use OctoPrint or Direct USB | Stable when Moonraker is reachable |
| Bambu Lab | MQTT/LAN | X1C, P1S, A1 | Stable |
| Prusa Link | HTTP REST | MK4, XL, Mini+ | Stable |
| Elegoo | WebSocket/SDCP | Centauri Carbon, Saturn, Mars series. Neptune 4/OrangeStorm Giga use Moonraker. | Stable |
| Direct USB | Serial | Any Marlin/RepRapFirmware printer over USB (Ender 3, Prusa MK3, CR-10, etc.) | Stable |

### Key Concepts

**PrinterAdapter** — Abstract base class defining the contract for all printer backends. Implements: status, files, upload, print, cancel, pause, resume, temperature, G-code, snapshot, stream URL, and optional bed mesh where the backend supports it.

**PrinterStatus** — Normalized enum: `IDLE`, `PRINTING`, `PAUSED`, `ERROR`, `OFFLINE`. Every backend maps its native state model to this enum.

**MCP Tools** — Typed functions exposed to agents via the Model Context Protocol. Each tool has a defined input schema and returns structured JSON.

**MarketplaceAdapter** — Abstract base class for searching third-party 3D model repositories. Implements: search, details, files, download. Concrete adapters for Thingiverse (deprecated — acquired by MyMiniFactory, Feb 2026), MyMiniFactory, and Cults3D (search only). Kiln does not host models — it searches external marketplaces.

**MarketplaceRegistry** — Manages connected marketplace adapters. Provides `search_all()` for parallel fan-out search across all third-party sources with round-robin result interleaving.

**GenerationProvider** — Abstract base class for text-to-3D model generation backends. Implements: generate, get_job_status, download_result. Concrete providers for Meshy (cloud AI), Tripo3D (cloud AI), Stability AI (synchronous cloud), Gemini Deep Think (AI-reasoned text/sketch-to-3D via Gemini + OpenSCAD), and OpenSCAD (local parametric). A `GenerationRegistry` auto-discovers providers from `KILN_*_API_KEY` environment variables.

**Mesh Validation** — Pipeline that checks generated STL/OBJ files for 3D-printing readiness: geometry parsing, manifold checks, dimension limits, polygon count validation. Uses pure Python (no external mesh libraries).

**Job Queue** — Priority queue backed by SQLite. Jobs are dispatched to idle printers by a background scheduler with history-based smart routing (best-performing printer for the job's file/material is preferred).

**DeviceType** — Enum classifying physical devices. Currently covers FDM printers with room for future device types.

**DeviceAdapter** — Alias for `PrinterAdapter`. Alternative import name.

**Cross-Printer Learning** — Agent-curated outcome database (`print_outcomes` table) that records success/failure/quality per printer and file. Safety-validated: rejects physically dangerous parameter values.

**PaymentProvider** — Abstract base class for payment processing backends. Implements: create_payment, get_payment_status, refund_payment, authorize_payment, capture_payment, cancel_payment. Concrete providers for Stripe and Circle (USDC).

**PaymentManager** — Orchestrates payment collection across providers. Routes to the correct rail (Stripe/Circle/crypto), enforces spend limits, persists transactions, and emits payment events.

**HeaterWatchdog** — Background daemon that monitors heater state and auto-cools idle heaters after a configurable timeout (default 30 min). Prevents heaters from being left on when no print is active.

**PreviewGate** — Single-use confirmation-token registry for print starts. A new non-resume `start_print` call must be preceded by a rendered preview that the user has approved; the token is bound to the file and printer and expires after a short TTL.

**Pro Tool Manifest** — Generated kiln-pro capability catalog bundled into public Kiln as `pro_tool_manifest.json`. It lets free-tier agents discover paid features, return structured tier-required messages, and proxy eligible calls through the hosted Kiln REST endpoint when a machine is paired, without moving proprietary kiln-pro source into the public repository.

**LicenseManager** — Provided by kiln-pro when installed. Public Kiln treats missing licensing as Free tier, while kiln-pro resolves Free, Pro, Business, and Enterprise entitlements and gates paid tools. Enterprise unlocks RBAC, SSO (OIDC/SAML), SCIM provisioning endpoints, audit trail export, lockable safety profiles, encrypted G-code at rest, per-printer overage billing, uptime SLA monitoring, and on-prem deployment. Local printer safety checks remain active regardless of license state.

---

## How Kiln Works

A high-level map of Kiln's four core subsystems. These are what the "Before and After Kiln" comparison on the home page refers to. Deeper per-tool tables live further down this page; this section is for the reader who wants to understand the *shape* of the system before digging in.

### 1. The Validation Pipeline

Before a model ever reaches a printer, Kiln runs it through a printability analysis. The goal is simple: **catch problems early so you don't waste filament, time, or bed adhesion on a print that was never going to work.**

The pipeline scores a mesh across seven dimensions and returns a 0–100 score with a letter grade and a list of recommended fixes:

| Dimension | What it catches |
|---|---|
| Overhangs | Triangles steeper than the printer can bridge without supports |
| Thin walls | Walls thinner than the nozzle diameter |
| Bridging | Horizontal spans too long to print reliably |
| Bed adhesion | Contact area too small or too sparse for the first layer to grip |
| Support volume | How much scaffolding the print would need |
| Thermal stress | Sharp cross-section changes that crack or warp |
| Adhesion force | Predicted peel/shear force at the bed for the chosen material and printer |

The last two dimensions are **material-aware**. Kiln knows ABS/ASA/PA/PC tend to warp, bed-slinger printers have less adhesion than CoreXY, and enclosures change everything. The output isn't a warning list — it's concrete slicer overrides: `brim_width`, `raft_layers`, reorientation suggestions, support placement.

Pure Python, no external mesh libraries. Runs locally on your laptop. Part of the patent-pending Printability Validation Pipeline.

### 2. The Decoration Engine

Kiln's decoration engine accepts many kinds of input and turns them into fabrication instructions for an FDM printer. The core idea: **many inputs → one normalized heightmap → a printable decorated mesh.**

Inputs the engine accepts:

- **Photographs** — your dog, a logo, a team photo. Auto-background removal, posterization to FDM-resolution depth tiers, heightmap conversion.
- **Natural language** — "add a wave texture to the side" or "emboss 'Happy Birthday' on top".
- **Voice** — the same, spoken aloud and transcribed.
- **SVG** — brand logos, icons, lettering.
- **QR codes** — generated to the right module size for the pocket's physical dimensions so a phone camera can actually scan the print.
- **Procedural textures** — tiger stripe, marble, snakeskin, wood grain, carbon fiber, honeycomb, lava, ocean, tie-dye, galaxy. Generated at face-centroid resolution; no UV mapping required.
- **Brand assets** — registered logos and imagery from a per-tenant brand asset registry for repeatable POS / merch / kiosk production.

Every input funnels through the same normalized-heightmap intermediate, then out to a CSG boolean on your mesh (emboss or deboss) that slices and prints on any Kiln-supported printer.

Covered by patent-pending Decoration Engine (KILN-017).

### 3. Git for 3D Printing

Version control for designs, decorations, and mechanical features — modeled after git, but operating on manufacturing-domain semantics instead of byte diffs.

**Three artifact types, one substrate:**

- **Designs** — the mesh itself.
- **Decorations** — logos, textures, photo-emboss, brand assets applied to a mesh.
- **Features** — chamfers, fillets, pockets, holes, bosses, ribs — the mechanical modifications.

All three get the same operations: branch, merge, pull request, release, cherry-pick, A/B analysis. You can:

- **Branch in parallel** — try three emboss depths without touching your proven version.
- **Three-way semantic merge** — merges operate on the manufacturing-domain fingerprint (Z-levels, pockets, bounding-box), not raw bytes. A conflict means two changes touched the same physical feature — which means something real.
- **Sign a release with Ed25519** — after a version prints well, sign it. Anyone downstream can verify later that the mesh hasn't changed since you signed.
- **Cross-branch A/B** — correlate print outcomes with design changes across branches to see which variant actually printed best.
- **Cherry-pick modifications** — pull a single chamfer or a single decoration from one branch into another, conflict-aware.
- **Local-first, optional sync** — everything runs offline on your laptop. If you want to sync across devices or collaborate, push to your own Supabase-backed cloud — not a vendor's.

Covered by patent-pending Semantic Version Control for 3D Manufacturing (KILN-028) and Design Provenance + Manufacturing Intelligence (KILN-021).

### 4. The Cross-Printer Intelligence Layer

Every print outcome Kiln sees — success, failure, material, printer, settings, quality grade — goes into an outcome database. The intelligence layer reads from this database to make every subsequent print smarter:

- **Proven recipes** — the best-performing settings Kiln has ever seen for a given material-plus-printer combination. When you start a similar print, the agent gets these as a starting point instead of guessing.
- **Regression alerts** — if a design that used to print well suddenly stops, the system flags it with the version diff so you can see what changed.
- **Cross-printer learning** — a recovery strategy that worked on one Bambu A1 propagates as a candidate fix for a Prusa MK4 hitting the same symptom class. Fleet-wide learning, not per-machine.
- **Predictive maintenance** — failure-pattern analysis flags likely hardware issues (belt tension drift, hot-end clogging, bed-leveling regression) before they cause a scrapped print.
- **Print confidence scoring** — before a job starts, Kiln reports a confidence number based on historical outcomes for this material-printer-model combination.

Covered by patent-pending filings KILN-003 (autonomous failure detection + layer-accurate resume + cross-device learning), KILN-010 (closed-loop failure-to-prompt constraint synthesis), KILN-014 (cross-printer learning), and KILN-027 (cross-printer material learning).

### How These Fit Together

A typical session strings all four together:

1. You describe or sketch what you want → **a design is generated** (text/image to 3D, OpenSCAD parametric, or search a third-party marketplace).
2. The mesh flows through **the validation pipeline** → score, grade, and any auto-fixes applied before slicing.
3. You **branch**, add a **decoration** or **mechanical feature**, maybe tweak depth or size, and start a print.
4. The **intelligence layer** suggests proven settings for your material and printer and flags any regression against past prints of this design family.
5. For multi-part work, the assembly JSON can also produce a PDF manual, page PNGs, BOM, verification gates, and a signed/embeddable 3MF manual manifest when kiln-pro is installed.
6. The print outcome is recorded against the version, so the next iteration is smarter than this one. If you like the result, **sign the release** — any downstream party can verify the exact mesh that shipped.

Every subsystem is optional. Free tier gets validation and basic design tools; decoration presets, mid-print modification, git-for-3D, assembly manuals, and cross-printer learning unlock with Pro. Business adds team approval gates, co-branded/multi-language manuals, QR product workflows, and per-part plate economics. Nothing about local printer control requires a cloud account — sync is opt-in.

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

# Creality K1/K2/Hi-class local Moonraker
kiln doctor-creality --host 192.168.1.55 --model k1_max
kiln auth --name k1max --host 192.168.1.55 --type creality --printer-model k1_max

# Prusa Link
kiln auth --name prusa-mini --host http://192.168.1.44 --type prusaconnect --api-key YOUR_KEY

# Bambu Lab
kiln auth --name x1c --host 192.168.1.100 --type bambu --access-code 12345678 --serial 01P00A000000001

# Elegoo SDCP
kiln auth --name saturn --host 192.168.1.50 --type elegoo

# Direct USB / Serial
kiln auth --name ender3-usb --host /dev/tty.usbserial-0001 --type serial
```

### Ethernet-Only Setup (No Wi-Fi)

Kiln works the same over Ethernet and Wi-Fi because it talks to printer APIs over LAN IP.

```bash
# Verify endpoint from your host:
curl http://192.168.1.44/api/v1/status                  # Prusa Link
curl http://192.168.1.50:7125/server/info               # Moonraker
curl http://192.168.1.55:7125/server/info               # Creality when local Moonraker is reachable
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
| `--type` | Yes | Backend: `octoprint`, `moonraker`, `creality`, `bambu`, `prusaconnect`, `elegoo`, `serial` |
| `--api-key` | OctoPrint, Moonraker/Creality, Prusa Link | API key when the backend requires one |
| `--access-code` | Bambu | Bambu Lab access code |
| `--serial` | Bambu, Elegoo optional | Bambu Lab serial number or SDCP mainboard identifier |
| `--printer-model` | Optional | Safety/profile hint such as `k1_max` or `bambu_a1` |

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

#### `kiln install-mcp` / `kiln uninstall-mcp`
Install or remove Kiln from supported MCP clients. The installer detects common client config locations and writes the `kiln serve` entry for the current environment.

#### `kiln preview <file>` / `kiln validate <file>` / `kiln repair <file>`
Render a model preview, check print readiness, or repair common STL/3MF mesh defects before slicing.

#### `kiln assembly-manual --assembly <assembly.json> --out <dir>`
Generate a printable assembly manual when kiln-pro is installed. Public Kiln defines the assembly JSON contract; kiln-pro generates the PDF, per-page PNGs, BOM, and verification gates.

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

Kiln exposes **<!-- KILN_MCP_TOOL_COUNT:OLD --> 790 MCP tools** and **<!-- KILN_MCP_CAPABILITY_COUNT:OLD --> 797 total MCP capabilities**. The most commonly used tools are documented below by category. Run `kiln tools` for the complete list.

kiln-pro tools are discoverable through the public `pro_tool_manifest.json` even when kiln-pro is not installed locally. Free-tier callers receive structured `TIER_REQUIRED` responses with the required tier and pricing URL; paired machines can proxy eligible paid-tier tool calls through the hosted Kiln REST endpoint so the paid code stays server-side.

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
| `find_slicer` | — | Detected slicer path and version |
| `slice_and_print` | `input_path`, `profile` | Slice + upload + print result |

#### Monitoring

| Tool | Input | Output |
|---|---|---|
| `printer_snapshot` | `save_path` | Image bytes or base64 |
| `monitor_print` | `printer_name`, `include_snapshot` | Standardized print status report (progress, temps, speed, camera, comments) |
| `multi_copy_print` | `model_path`, `copies`, `spacing_mm` | Arrange N copies on plate, slice, and print (PrusaSlicer `--duplicate` or STL fallback) |

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

#### Cost Estimation (Quick Reference)

See [Cost Estimation](#cost-estimation) below for the full tool set including `slice_and_estimate`, `compare_print_options`, and material cost from mesh geometry.

| Tool | Input | Output |
|---|---|---|
| `estimate_cost` | `file_path`, `material`, `electricity_rate`, `printer_wattage` | Filament cost + electricity cost + total |
| `list_materials` | — | 9 material profiles with densities and costs |

#### Material Tracking

| Tool | Input | Output |
|---|---|---|
| `set_material` | `printer_name`, `material`, `color`, `spool_id`, `tool_index` | Confirmation |
| `get_material` | `printer_name` | Loaded materials |
| `check_material_match` | `printer_name`, `expected_material` | Match result or warning |
| `list_spools` | — | Spool inventory |
| `add_spool` | `material`, `color`, `brand`, `weight`, `cost` | Spool details |
| `remove_spool` | `spool_id` | Confirmation |

#### Material Intelligence

| Tool | Input | Output |
|---|---|---|
| `get_material_properties` | `material` | Full property sheet: thermal, mechanical, chemical, design limits |
| `compare_materials` | `material_a`, `material_b` | Side-by-side comparison across all properties |
| `suggest_material` | `requirements` (strength, flexibility, heat_resistance, etc.) | Ranked material recommendations with reasoning |
| `build_material_overrides` | `material`, `printer_model` | Slicer override dict for material+printer combination |
| `reprint_with_material` | `file_path`, `material`, `printer_name` | Re-slice and print with auto-generated material overrides |
| `smart_reprint` | `file_path`, `material`, `printer_name` | Find file, detect AMS slot, switch material, and print |
| `multi_material_print` | `objects` (file_path + material pairs), `printer_name` | Print multiple objects in different materials/colors on one plate |

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

#### Cloud Sync (Pro Tier)

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

#### Billing & Payments (Business Tier)

| Tool | Input | Output |
|---|---|---|
| `billing_status` | `user_id` | Fee policy, monthly spend, payment methods, spend limits |
| `billing_summary` | — | Aggregated billing summary |
| `billing_history` | `limit` | Recent billing charges with payment outcomes |
| `billing_setup_url` | `rail` | URL to link a payment method |
| `billing_check_setup` | — | Polls pending Stripe SetupIntent; persists payment method on success |
| `check_payment_status` | `payment_id` | Non-blocking check of Circle/Stripe payment finality |

#### External Provider Integrations *(As Integrations Launch)*

Kiln does not operate its own marketplace or manufacturing network. These tools integrate with third-party provider APIs for capacity discovery and job routing.

| Tool | Input | Output |
|---|---|---|
| `connect_provider_account` | `name`, `location`, `capabilities`, `price_per_gram` | Connects local listing to provider integration account |
| `sync_provider_capacity` | `printer_id?`, `available?` | Syncs provider-side availability/capacity snapshot |
| `list_provider_capacity` | — | Connected provider listings/capacity |
| `find_provider_capacity` | `material`, `location` | Available provider capacity by material/location |
| `submit_provider_job` | `file_url`, `material`, `printer_id` | Provider-managed remote job ID |
| `provider_job_status` | `job_id` | Provider job tracking details |

#### Safety Audit

| Tool | Input | Output |
|---|---|---|
| `safety_audit` | — | Safety compliance report |
| `get_session_log` | `session_id?` | Full tool call history for an agent session (defaults to current session) |
| `safety_settings` | — | Current safety/auto-print settings |
| `safety_status` | — | Comprehensive safety status |

#### G-code Interception

| Tool | Description |
|---|---|
| `start_gcode_interception` | Begin intercepting G-code commands sent to a printer |
| `stop_gcode_interception` | Stop G-code interception |
| `add_interception_rule` | Add a rule to modify, block, or log specific G-code commands |
| `remove_interception_rule` | Remove an interception rule |
| `intercept_gcode_command` | Manually intercept and modify a specific G-code command |
| `get_interception_status` | Check current interception session status |
| `get_interception_history` | View history of intercepted commands |
| `list_interception_sessions` | List all interception sessions |
| `load_safety_interception_rules` | Load predefined safety rules for G-code interception |
| `update_interception_telemetry` | Update telemetry data for an interception session |

#### Emergency System

| Tool | Description |
|---|---|
| `emergency_stop` | Immediately stop a printer (hardware e-stop) |
| `emergency_status` | Check emergency stop state for a printer or the fleet |
| `clear_emergency_stop` | Clear an emergency stop and allow printing to resume |
| `emergency_trip_input` | Trigger an emergency stop from an external input source |

#### Fulfillment Services (Business Tier) — Third-Party Providers

Routes manufacturing to third-party fulfillment providers (Craftcloud). Kiln acts as an API client to these providers — it does not operate manufacturing capacity or act as merchant of record.

| Tool | Input | Output |
|---|---|---|
| `fulfillment_materials` | `provider` | Available materials from third-party print services |
| `fulfillment_quote` | `file_path`, `material_id`, `quantity`, `provider` | Manufacturing quote + provider ownership metadata (`provider_name`, `provider_terms_url`, `support_owner=provider`, `merchant_of_record=provider`) |
| `save_shipping_profile` | `name`, `shipping_address`, `consent_to_store` | Locally saved shipping/contact profile after explicit user consent |
| `list_shipping_profiles` | `include_addresses` | Saved profile summaries; full addresses only when requested for review |
| `delete_shipping_profile` | `name` | Local shipping profile deletion |
| `issue_shipping_confirmation_token` | `quote_id`, `shipping_option_id`, `shipping_address` or `shipping_profile_name` | Single-use proof that the user reviewed contact/shipping details |
| `fulfillment_order` | `quote_id`, `shipping_option_id`, `preview_token`, `preview_file_path`, `shipping_confirmation_token`, `payment_hold_id` | Order confirmation with billing + provider metadata (`provider_order_id`) after preview and shipping confirmation |
| `fulfillment_order_status` | `order_id` | Order tracking details + provider ownership metadata |
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
| `suggest_material_for_order` | `use_case`, `budget`, constraints | Ranked material recommendations with reasoning |
| `estimate_price` | `technology`, `volume_cm3` or `dimensions` | Instant price range (no API call) |
| `estimate_timeline` | `technology`, `quantity`, `country` | Order-to-delivery timeline with stage breakdown |
| `supported_shipping_countries` | — | Supported shipping countries (23+ countries) |

#### Model Generation

Supported providers: `meshy`, `gemini`, `tripo3d`, `stability`, `openscad`. Set `KILN_GEMINI_API_KEY`, `KILN_MESHY_API_KEY`, `KILN_TRIPO3D_API_KEY`, or `KILN_STABILITY_API_KEY` to enable cloud providers. For original idea-to-part creation, prefer `generate_original_design`, which chooses idea-to-CAD backends and explicitly does not treat raw `openscad` compilation as natural-language generation.

| Tool | Input | Output |
|---|---|---|
| `generate_model` | `prompt`, `provider`, `format`, `style` | Generation job ID |
| `generate_original_design` | `requirements`, `provider`, `material`, `printer_model`, `max_attempts` | Best generated candidate plus readiness audit and retry history |
| `generation_status` | `job_id` | Job status and progress |
| `download_generated_model` | `job_id`, `output_dir` | Local file path with mesh validation |
| `await_generation` | `job_id`, `timeout`, `poll_interval` | Completed job result |
| `generate_and_print` | `prompt`, `provider`, `printer_name`, `profile` | Full pipeline: generate → validate → slice → print |
| `validate_generated_mesh` | `file_path` | Mesh validation report |

#### Mesh Analysis & Repair

Pure-Python mesh operations — no external mesh libraries required. Operates directly on STL binary geometry for analysis, repair, transformation, and composition.

| Tool | Input | Output |
|---|---|---|
| `analyze_mesh_geometry` | `file_path` | Volume, surface area, center of mass, overhangs, components, printability score (0-100) |
| `repair_mesh` | `file_path`, `output_path` | Degenerate triangle removal, normal recomputation, repair stats |
| `repair_mesh_advanced` | `file_path`, `output_path`, `close_holes` | Advanced repair: degenerate removal + boundary hole closing via fan triangulation |
| `compose_models` | `file_paths`, `output_path` | Merge multiple mesh files into one STL (no booleans — geometry concatenation) |
| `export_model_3mf` | `file_path`, `output_path` | Convert STL/OBJ/GLB to 3MF format (preferred by modern slicers) |
| `optimize_print_orientation` | `file_path`, `output_path` | Auto-rotate mesh to minimize overhangs and maximize bed contact |
| `estimate_support_material` | `file_path` | Support volume (mm³), weight (g), overhang triangle stats |
| `compare_mesh_versions` | `file_a`, `file_b` | Volume/surface area delta, Hausdorff distance, dimension changes, `meshes_identical` flag |
| `predict_print_failure` | `file_path`, thresholds | Risk score (0-100), verdict, per-failure details (thin walls, bridges, overhangs, top-heavy) |
| `simplify_mesh_model` | `file_path`, `target_ratio`, `output_path` | Vertex-clustering decimation — reduce triangle count while preserving shape |
| `mesh_quality_scorecard` | `file_path` | Multi-factor quality score: printability (35%), structural (25%), efficiency (20%), quality (20%). Letter grade A-F |
| `estimate_material_cost` | `file_path`, `material`, `infill_pct`, `wall_layers` | Filament weight (g), length (m), cost ($) for 9 materials |
| `remove_mesh_floating_regions` | `file_path`, `output_path`, `keep_largest` | Detect and remove disconnected geometry (fragments, support pillars) |
| `check_print_readiness` | `file_path`, `auto_fix`, bed dimensions | Single-call readiness gate: manifold, overhangs, build volume, degenerates. Optional auto-repair |
| `mirror_mesh_model` | `file_path`, `axis`, `output_path` | Mirror (reflect) mesh along X/Y/Z axis with correct winding order |
| `hollow_mesh_model` | `file_path`, `wall_thickness_mm`, `output_path` | Create hollow shell to save material, reports savings percentage |
| `center_model_on_bed` | `file_path`, `bed_x_mm`, `bed_y_mm`, `output_path` | Center mesh on build plate with z=0 grounding |
| `analyze_non_manifold_edges` | `file_path` | Edge classification: boundary (1 tri), manifold (2 tri), T-junction (3+). Watertight status |
| `scale_mesh_to_fit` | `file_path`, `max_x/y/z_mm`, `output_path` | Uniform scale to fit build volume while preserving aspect ratio |
| `merge_mesh_files` | `file_paths`, `output_path` | Combine multiple STL files into one (for multi-part assemblies) |
| `split_mesh_by_component` | `file_path`, `output_dir` | Split multi-body mesh into separate STL files per connected component |
| `estimate_mesh_print_time` | `file_path`, `layer_height`, `print_speed`, `material` | Geometry-based print time estimate (before slicing) |
| `extract_model_from_3mf` | `file_path`, `output_path` | Extract embedded mesh from .3mf or .gcode.3mf to STL (handles multi-object, Bambu files) |
| `diagnose_mesh` | `file_path` | Advanced defect analysis: self-intersections, normals, degenerate faces, floating fragments, holes |

#### Parametric Design Templates

| Tool | Input | Output |
|---|---|---|
| `list_design_templates` | — | Available parametric templates (phone stand, hook, box, etc.) |
| `generate_from_template` | `template_id`, parameters | Generate model from template with custom parameters via OpenSCAD |
| `generate_template_variations` | `template_id`, `variation_count`, `parameter_ranges` | Batch-generate N variations across parameter space |

#### Design Advisor & Iteration

| Tool | Input | Output |
|---|---|---|
| `design_advisor` | `prompt`, `printer_model` | Recommend approach (template, OpenSCAD, or AI), material, and constraints |
| `iterate_design` | `prompt`, `provider`, `max_iterations`, `material` | Closed-loop: generate → validate → improve prompt → regenerate until passing |
| `validate_openscad_code` | `code` | Compile-check OpenSCAD code with structured error/warning output |
| `estimate_print_time` | `file_path`, `profile`, `slicer_path` | Slicer-based print time and filament estimate (requires slicer installed) |
| `get_feedback_loop_status` | `model_id` | Feedback loop iteration history and resolution status |

#### Original Design Audit & Design DNA

The `generate_original_design` / `audit_original_design` pipeline produces idea-to-part designs and validates them through a multi-gate readiness audit. Each audit gate (mesh validity, printability, design constraints, orientation, build volume) independently passes or blocks with severity levels.

When designs are generated via OpenSCAD (parametric), the pipeline preserves the parametric source code alongside the compiled mesh. This "Design DNA" allows agents to iterate on the design by modifying parameters rather than regenerating from scratch. The `audit_original_design` function chains: mesh validation, printability analysis (7-dimension), design intelligence constraint checking, auto-orientation scoring, and prompt improvement feedback — returning a single `OriginalDesignAudit` with a readiness score, blockers list, and next-action recommendations.

| Tool | Input | Output |
|---|---|---|
| `generate_original_design` | `requirements`, `provider`, `material`, `printer_model`, `max_attempts` | Best candidate + readiness audit + retry history |
| `audit_original_design` | `file_path`, `requirements`, `material`, `printer_model` | Multi-gate audit with readiness score, blockers, next actions |

#### Print Analysis

| Tool | Input | Output |
|---|---|---|
| `await_print_completion` | `timeout_minutes`, `poll_interval` | Final print status |
| `analyze_print_failure` | `job_id` | Failure diagnosis with causes and recommendations |
| `validate_print_quality` | `job_id` | Quality assessment with snapshot and event analysis |

#### Print Recovery & Failure Analysis

| Tool | Description |
|---|---|
| `analyze_print_failure_smart` | Automated root cause analysis of a print failure |
| `get_recovery_plan` | Get recovery options for a specific failure type |
| `start_print_recovery` | Begin a guided print recovery session |
| `confirm_print_recovery` | Confirm a recovery step has been completed |
| `complete_print_recovery` | Mark a recovery session as complete |
| `plan_print_recovery` | Plan recovery from checkpoint (printer_name + job_id) |
| `plan_failure_recovery` | Plan recovery from detected failure (failure_id) |
| `cancel_print_recovery` | Cancel an in-progress recovery session |
| `get_recovery_session_status` | Check status of a recovery session |
| `get_recovery_statistics` | View historical recovery success rates |
| `record_recovery_check` | Record a recovery checkpoint |
| `retry_print_with_fix` | Re-slice with corrections and reprint |
| `detect_print_failure` | Detect if a print has failed |
| `diagnose_print_failure_live` | Real-time failure diagnosis during printing |
| `predict_print_failure` | Pre-print failure risk prediction |
| `save_print_checkpoint` | Save a mid-print state snapshot for potential recovery |
| `firmware_resume_print` | Resume a print after power loss via firmware recovery |
| `failure_history` | View history of print failures and their resolutions |

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

#### Cross-Printer Learning (Pro Tier)

| Tool | Input | Output |
|---|---|---|
| `record_print_outcome` | `job_id`, `outcome`, `quality_grade`, `failure_mode`, `settings`, `notes` | Confirmation (safety-validated) |
| `get_printer_insights` | `printer_name`, `limit` | Success rate, failure breakdown, material stats, confidence |
| `suggest_printer_for_job` | `file_hash`, `material_type`, `file_name` | Ranked printers by success rate + availability |
| `recommend_settings` | `printer_name`, `material_type`, `file_hash` | Median temps/speed, mode slicer profile, confidence, quality distribution |

#### Printability Engine

7-dimension analysis of STL/OBJ meshes for FDM printing readiness. Pure Python (no external mesh libraries). Each dimension produces a typed dataclass result; the composite report includes a 0-100 score, letter grade (A-F), and actionable recommendations.

| Dimension | What it measures | Key output fields |
|---|---|---|
| Overhangs | Triangles exceeding the max overhang angle (default 45 deg) | `max_overhang_angle`, `overhang_percentage`, `needs_supports`, `worst_regions` |
| Thin walls | Walls below nozzle diameter (default 0.4mm) | `min_wall_thickness_mm`, `thin_wall_count`, `problematic_regions` |
| Bridging | Unsupported horizontal spans | `max_bridge_length_mm`, `bridge_count`, `needs_supports_for_bridges` |
| Bed adhesion | Contact area at z-min as percentage of bounding-box footprint | `contact_area_mm2`, `contact_percentage`, `adhesion_risk` (low/medium/high) |
| Support volume | Estimated support material required | `estimated_support_volume_mm3`, `support_percentage`, `support_regions` |
| Thermal stress | Stress concentration from sharp cross-section changes and tall thin features | `stress_risk`, `concentration_zones`, `recommended_mitigations` |
| Adhesion force | Estimated peel/shear force at the bed interface for a given material and printer | `estimated_force_n`, `force_margin`, `detachment_risk` |

The last two dimensions (thermal stress and adhesion force) are material-aware heuristics. Thermal stress accounts for layer bonding weakness at sharp geometric transitions. Adhesion force estimation combines contact area, material shrinkage coefficients, and printer type (bed-slinger vs. CoreXY) to predict detachment risk.

| Tool | Input | Output |
|---|---|---|
| `analyze_printability` | `file_path`, `nozzle_diameter`, `layer_height`, `max_overhang_angle`, `build_volume_*` | Full `PrintabilityReport` with score, grade, per-dimension results, recommendations |
| `recommend_adhesion_settings` | `file_path`, `material`, `printer_model`, `has_enclosure` | Brim/raft recommendation with slicer overrides dict |
| `auto_orient_model` | `file_path`, `output_path` | Auto-rotated mesh minimizing overhangs and maximizing bed contact |
| `estimate_supports` | `file_path` | Support volume and weight estimate |

Adhesion intelligence includes a decision matrix combining contact percentage, material warp tendency (`_HIGH_WARP_MATERIALS`: ABS, ASA, PA, PC, etc.), bed-slinger detection, enclosure status, and model height to produce concrete slicer overrides (`brim_width`, `brim_type`, `raft_layers`).

#### Adaptive Slicing

| Tool | Description |
|---|---|
| `generate_adaptive_slicing_plan` | Analyze model geometry and generate per-region layer height optimization |
| `quick_adaptive_plan` | All-in-one analyze + plan for adaptive slicing |
| `export_adaptive_slicer_config` | Export adaptive plan to PrusaSlicer, OrcaSlicer, or Cura format |
| `estimate_adaptive_time_savings` | Estimate print time savings vs uniform layer height |
| `get_adaptive_plan_summary` | Human-readable summary of an adaptive slicing plan |

#### Warping Analysis (Pro Tier)

| Tool | Description |
|---|---|
| `analyze_warping_risk` | Physics-based warping risk analysis using thermal simulation |

#### Multi-Part Assembly & Job Splitting

Splits large models or multi-part assemblies across multiple printers for parallel printing. Three split modes: build volume overflow (model exceeds one printer), multi-copy distribution (N copies across fleet), and assembly splitting (multi-STL archive across printers with reassembly instructions).

| Tool | Input | Output |
|---|---|---|
| `plan_multi_copy_split` | `file_path`, `copies`, `material` | `SplitPlan` with per-printer assignments, time savings percentage |
| `plan_assembly_split` | `file_paths`, `material` | `SplitPlan` with part-to-printer mapping and assembly instructions |
| `split_plan_status` | `plan_id` | `SplitProgress` with per-part status and estimated remaining time |
| `cancel_split_plan` | `plan_id` | Cancellation confirmation |

#### Assembly Manuals (Pro Tier)

Public Kiln owns the assembly JSON contract: parts, positions, materials, mating interfaces, clearances, optional fastener metadata, and magnet polarity when relevant. When kiln-pro is installed, the manual tools turn that contract into PDF instructions and page PNG previews. Multi-language and co-brand options require Business+.

| Tool | Description |
|---|---|
| `generate_assembly_manual` | Generate a PDF manual with cover render, BOM, per-step isometric pages, verification gates, sidecar JSON, and page PNGs |
| `auto_generate_manual_for_print` | Trigger manual generation for a multi-part print, returning cached/expected PDF paths or a pending background status |
| `embed_manual_in_3mf` | Embed the manual and signed manifest into a 3MF package |
| `verify_manual_authenticity` | Verify a manual or 3MF manual manifest against the signed release metadata |
| `list_manual_history` | List manual revisions for a design |
| `get_manual_revision` | Fetch one manual revision and its cached PDF path |
| `find_manual_siblings` | Find manuals for sibling parameter values of the same design |
| `diff_assembly_manuals` | Generate a "what changed" PDF between two manual revisions |
| `retire_manual_revision` | Soft-delete a manual revision while preserving audit history |

#### Cost Estimation

G-code and 3MF cost analysis. Parses extrusion totals, calculates filament weight and cost from a 9-material database (PLA, PETG, ABS, TPU, ASA, Nylon, PC, PVA, HIPS), and optionally includes electricity cost from slicer-embedded time estimates. For 3MF files (including Bambu `.gcode.3mf`), extracts slicer metadata directly from the archive.

| Tool | Input | Output |
|---|---|---|
| `estimate_cost` | `file_path`, `material`, `electricity_rate`, `printer_wattage` | Filament length/weight/cost, electricity cost, total cost, warnings |
| `estimate_material_cost` | `file_path`, `material`, `infill_pct`, `wall_layers` | Filament weight (g), length (m), cost ($) for mesh geometry |
| `compare_print_options` | `file_path`, `material` | Local vs. fulfillment provider cost comparison |
| `slice_and_estimate` | `input_path`, `material`, `printer_model` | Slice + cost + printability + adhesion recommendation (no print) |
| `list_materials` | -- | All material profiles with densities and costs |

#### Part Economics (Business Tier)

| Tool | Input | Output |
|---|---|---|
| `estimate_plate_part_costs` | `.gcode.3mf` file, plate number, default material, optional per-object materials | Per-object time, filament, material cost, electricity cost, totals, and warnings for quote/margin work |

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

#### Design Intelligence

Constraint-aware design reasoning — agents query material properties, design templates, and functional constraints before generating geometry.

| Tool | Input | Output |
|---|---|---|
| `get_design_brief` | `requirements`, `material` | Complete design brief (materials, patterns, constraints, orientation) |
| `get_material_design_profile` | `material` | Full engineering properties for a printing material |
| `list_design_materials` | — | All available materials with summary properties |
| `recommend_design_material` | `requirements`, constraints | Best material recommendation with reasoning |
| `estimate_structural_load` | `material`, dimensions | Safe structural load estimate for cantilevered sections |
| `check_material_environment` | `material`, `environment` | Material-environment compatibility check |
| `get_printer_design_capabilities` | `printer_id` | Design capability profile for a printer |
| `list_printer_design_profiles` | — | All known printer design capability profiles |
| `get_design_template_info` | `pattern` | Detailed design rules for a functional pattern |
| `list_design_templates_catalog` | — | All available design templates with descriptions |
| `find_design_templates` | `use_case` | Design patterns that apply to a use case |
| `match_design_requirements` | `description` | Functional requirements that apply to a design task |
| `validate_design_for_requirements` | `design`, requirements | Validation against functional design requirements |
| `troubleshoot_print_issue` | `material`, `symptom` | Print problem diagnosis by material and symptom |
| `check_printer_material_compatibility` | `printer_id`, `material` | Printer-material compatibility check |
| `get_post_processing_guide` | `material` | Post-processing techniques for finishing a printed part |
| `check_multi_material_pairing` | `material_a`, `material_b` | Dual extrusion co-print compatibility check |
| `get_print_diagnostic` | `material`, `printer`, `symptom` | Comprehensive diagnostic combining multiple knowledge sources |

#### Procedural Textures (Pro Tier)

| Tool | Description |
|---|---|
| `apply_procedural_texture` | Apply a named procedural texture (tiger_stripe, marble, lava, ocean, wood, etc.) to a mesh surface |
| `list_procedural_textures` | List all available procedural texture types and their presets |
| `preview_texture_2d` | Render a 2D preview of a procedural texture before applying |
| `save_custom_palette` | Save a custom color palette for procedural textures |
| `list_custom_palettes` | List saved custom palettes |
| `delete_custom_palette` | Delete a saved custom palette |

#### Surface Decoration (Pro Tier)

| Tool | Description |
|---|---|
| `decorate_surface` | Apply embossed/debossed decoration (image, SVG, or text) to any face of a model |
| `apply_decoration` | Apply a saved decoration preset to a model |
| `save_decoration` | Save a decoration configuration as a reusable preset |
| `list_decorations` | List all saved decoration presets |
| `decoration_info` | Get details about a saved decoration preset |
| `iterate_decoration` | Iteratively refine a decoration based on print results |
| `rollback_decoration` | Roll back to a previous decoration version |
| `decoration_history` | View the iteration history of a decoration |
| `record_decoration_success` | Record that a decoration printed successfully for learning |

#### QR Code & Product Generation (Pro Tier)

| Tool | Description |
|---|---|
| `add_qr_to_product` | Embed a QR code onto a product surface as a raised/recessed feature |
| `print_qr_product` | Generate and print a QR-coded product in one step |
| `generate_coaster` | Generate a printable coaster with optional decoration |
| `generate_keychain` | Generate a printable keychain |
| `generate_bookmark` | Generate a printable bookmark |
| `generate_fridge_magnet` | Generate a printable fridge magnet |
| `generate_ornament` | Generate a printable ornament |
| `generate_pet_tag` | Generate a printable pet tag |
| `generate_pet_bowl` | Generate a printable pet bowl |
| `generate_jewelry_tray` | Generate a printable jewelry tray |
| `generate_ashtray` | Generate a printable ashtray |
| `generate_wall_plaque` | Generate a printable wall plaque |
| `batch_generate_products` | Bulk-generate multiple products from templates |
| `generate_decorated_product` | Generate a decorated product in one step |

#### Enterprise Features (Enterprise Tier)

| Tool | Input | Output |
|---|---|---|
| `export_audit_trail` | `format` (json/csv), `start_date`, `end_date`, `tool`, `action`, `session_id` | Audit trail export (JSON or CSV) |
| `lock_safety_profile` | `profile_name` | Lock confirmation (prevents agent modifications) |
| `unlock_safety_profile` | `profile_name` | Unlock confirmation |
| `manage_team_member` | `email`, `role` (admin/engineer/operator), `action` (add/remove/update) | Team member status |
| `printer_usage_summary` | — | Per-printer usage counts and overage billing details |
| `uptime_report` | — | Rolling uptime metrics (1h/24h/7d/30d) with SLA compliance |
| `encryption_status` | — | G-code encryption status and key configuration |
| `report_printer_overage` | — | Printer overage billing breakdown (20 included, $15/mo each additional) |
| `configure_sso` | `provider_type` (oidc/saml), IdP settings | SSO configuration confirmation |
| `sso_login_url` | `provider_type` | Authorization URL for SSO login |
| `sso_exchange_code` | `code`, `state` | Session token |
| `sso_status` | — | SSO configuration and provider connectivity status |
| `rotate_encryption_key` | `old_passphrase`, `new_passphrase`, `directory` | Key rotation results (rotated/skipped/failed counts) |
| `database_status` | — | Database backend health (SQLite/PostgreSQL), connection info |
| `list_fleet_sites` | — | All physical sites with printer counts |
| `fleet_status_by_site` | — | Fleet status grouped by physical site |
| `update_printer_site` | `printer_name`, `site` | Site assignment confirmation |
| `create_project` | `project_id`, `name`, `client`, `budget_usd` | Project creation confirmation |
| `log_project_cost` | `project_id`, `category`, `amount`, `description` | Cost entry confirmation |
| `project_cost_summary` | `project_id` | Aggregate costs with category breakdown and budget tracking |
| `client_cost_report` | `client` | Cross-project cost aggregation for a client |

#### Procedural Textures (Pro Tier)

| Tool | Description |
|---|---|
| `apply_procedural_texture` | Apply a procedural texture (tiger stripe, marble, camo, wood, honeycomb, lava, etc.) to any model for multicolor FDM printing |
| `list_procedural_textures` | List all available procedural texture presets with descriptions |
| `preview_texture_2d` | Fast 2D preview of a texture before 3D application |
| `apply_image_texture` | Apply a photographic or image-based texture with cylinder detection |
| `apply_geometric_texture` | Apply geometric tile patterns (snakeskin, bamboo, carbon fiber) |

#### Mid-Print Modification & Resume (Pro Tier)

| Tool | Description |
|---|---|
| `plan_mid_print_decoration` | Plan a decoration injection on a paused/running print |
| `apply_mid_print_decoration_plan` | Execute a planned mid-print decoration with atomic dual-artifact revert |
| `resume_interrupted_print` | One-call resume from the exact layer a print stopped — works on any FDM printer (OctoPrint, Moonraker/Klipper, Creality when exposed through Moonraker, Bambu, Prusa Connect, Elegoo, Serial) |
| `analyze_mid_print_impact` | Structural impact analysis of a proposed mid-print modification |
| `preview_mid_print_session` | Preview what a mid-print modification will look like before committing |
| `cancel_print_recovery` | Cancel an in-progress recovery session |
| `generate_recovery_gcode` | Generate safe resume G-code with loaded-bed preamble (heat → Z-lift → home X/Y only → descend) |

#### Design Provenance & Version Intelligence (Pro Tier)

| Tool | Description |
|---|---|
| `save_design_version` | Save a design version with parametric source and provenance metadata |
| `get_design_version` | Retrieve a design version with its full ancestry chain |
| `diff_design_versions` | Geometric diff between two design versions |
| `compare_design_versions` | Side-by-side comparison of two design versions |
| `check_design_regression` | Detect if a design change caused a regression in print success |
| `record_design_version_outcome` | Record a print outcome against a specific design version |
| `get_proven_recipe` | Query provenance history for the best-performing parameters for a given material and printer |
| `get_regression_alerts` | Get active regression alerts for designs that stopped printing well |
| `dismiss_regression_alert` | Dismiss a regression alert after investigation |
| `best_design_version` | Find the best-performing design version for a material/printer combination |

#### Print Learning & Cross-Printer Intelligence (Pro Tier)

| Tool | Description |
|---|---|
| `get_print_learning_summary` | Aggregate learning insights: what works, what fails, material success rates |
| `get_optimal_settings` | Recommend optimal settings from historical outcomes for a material/printer |
| `get_material_success_rates` | Success/failure rates per material across your fleet |
| `get_maintenance_prediction` | Predictive maintenance alerts based on failure pattern analysis |
| `get_print_confidence` | Confidence score for an upcoming print based on historical data |
| `record_print_outcome` | Record a print outcome (success/failure/partial) with settings and quality grade |

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

### Creality

Modern CrealityOS/Klipper FDM printers are supported through local Moonraker when it is reachable on the printer LAN. The `creality` backend probes common Creality Moonraker ports (`7125`, `80`, `4408`) and then delegates standard printer operations to the Moonraker adapter. Older Marlin-based Creality machines should use `octoprint` or `serial`.

**Configuration:**
```yaml
type: creality
host: 192.168.1.55
printer_model: k1_max
```

**Diagnostics:** Run `kiln doctor-creality --host 192.168.1.55 --model k1_max` to verify `/server/info`, see the resolved Moonraker URL, and get same-LAN/IP/port/API-key guidance when local Moonraker is not reachable.

**Caveat:** Kiln should not claim every stock Creality firmware exposes local Moonraker. K1/K2/Hi/Ender V3 KE-class support depends on `/server/info` answering locally.

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

## Model Marketplaces (Third-Party Search)

Kiln provides a `MarketplaceAdapter` interface (mirroring the printer adapter pattern) for searching and downloading 3D models from third-party repositories. Kiln does not host or serve models itself — it searches external marketplaces on the user's behalf. A `MarketplaceRegistry` manages connected adapters and exposes `search_all()` for parallel fan-out across all sources.

### Supported Marketplaces

| Marketplace | Protocol | Auth | Download Support | Notes |
|---|---|---|---|---|
| MyMiniFactory | HTTP REST v2 | API key (`?key=`) | Yes | *Primary marketplace — recommended for new integrations.* |
| Cults3D | GraphQL | HTTP Basic | No (metadata-only — provides URLs for manual download) | |
| Thingiverse | HTTP REST | Bearer token | Yes | *Deprecated — acquired by MyMiniFactory (Feb 2026). API may be sunset; prefer MyMiniFactory.* |

### Configuration

Set environment variables for each marketplace you want to enable:

```bash
export KILN_MMF_API_KEY=your_key               # MyMiniFactory developer key (recommended)
export KILN_CULTS3D_USERNAME=your_username      # Cults3D account username
export KILN_CULTS3D_API_KEY=your_key            # https://cults3d.com/en/api/keys
export KILN_THINGIVERSE_TOKEN=your_token        # Deprecated — Thingiverse acquired by MyMiniFactory (Feb 2026)
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

### Preview Confirmation

New non-resume print starts require a preview-confirmation token:

1. Render a model preview with `visualize_model`, `preview_generated_model`, or `kiln preview`.
2. Show the preview to the user and get approval.
3. Issue a preview token for the file/printer.
4. Pass that token to `start_print`.

Tokens are single-use, short-lived, and bound to the file plus printer. `KILN_SKIP_PREVIEW_GATE=1` is the explicit advanced-user bypass and is logged whenever used.

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
| `KILN_PRINTER_API_KEY` | API key for OctoPrint, Moonraker/Creality, or Prusa Link when required |
| `KILN_PRINTER_TYPE` | Backend type |
| `KILN_PRINTER_SERIAL` | Bambu serial number or Elegoo mainboard ID |
| `KILN_SLICER_PATH` | Explicit path to slicer binary |
| `KILN_BAMBU_TLS_MODE` | Bambu TLS validation mode: `pin` (default), `ca`, or `insecure` |
| `KILN_BAMBU_TLS_FINGERPRINT` | Optional explicit Bambu certificate pin |
| `KILN_SKIP_PREVIEW_GATE` | Advanced-user bypass for preview confirmation; logged when used |
| `KILN_MMF_API_KEY` | MyMiniFactory API key |
| `KILN_CULTS3D_USERNAME` | Cults3D account username |
| `KILN_CULTS3D_API_KEY` | Cults3D API key |
| `KILN_THINGIVERSE_TOKEN` | Thingiverse API token *(deprecated — acquired by MyMiniFactory, Feb 2026)* |
| `KILN_AUTH_ENABLED` | Enable API key auth (1/0) |
| `KILN_AUTH_KEY` | Secret key for auth |
| `KILN_PROXY_URL` | Hosted Kiln REST endpoint for paired-machine pro tool proxying |
| `KILN_ENCRYPTION_KEY` | Encryption key for G-code at rest (Enterprise) |
| `KILN_SSO_PROVIDER` | SSO provider type: `oidc` or `saml` (Enterprise) |
| `KILN_SSO_ISSUER` | OIDC issuer URL or SAML IdP metadata URL (Enterprise) |
| `KILN_SSO_CLIENT_ID` | OIDC client ID (Enterprise) |
| `KILN_SSO_CLIENT_SECRET` | OIDC client secret (Enterprise) |

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
cd kiln && python3 -m pytest tests/ -v    # public Kiln test suite
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
    server.py                 # FastMCP server, core tools, pro manifest stubs
    plugin_loader.py          # Auto-discovers public plugin modules
    preview_gate.py           # Preview-token gate for print starts
    slicer.py                 # PrusaSlicer/OrcaSlicer integration
    registry.py               # Fleet printer registry
    queue.py                  # Priority job queue
    scheduler.py              # Background dispatcher with smart routing
    events.py                 # Pub/sub event bus
    persistence.py            # SQLite storage
    webhooks.py               # HMAC-signed webhook delivery
    auth.py                   # API key authentication
    gcode.py                  # G-code safety validator
    safety_profiles.py        # Bundled safety database (44 named printer models)
    slicer_profiles.py        # Bundled slicer profiles
    printer_intelligence.py   # Printer knowledge base
    design_intelligence.py    # Material/pattern/constraint queries
    design_reasoning.py       # Design brief and reasoning helpers
    design_recipe.py          # Parametric design recipe persistence
    design_versions.py        # Public/local version metadata
    original_design.py        # Original design audit pipeline
    printability.py           # 7-dimension printability engine
    assembly.py               # Assembly JSON contract, joints, fasteners
    cost_estimator.py         # Whole-job cost estimation
    job_splitter.py           # Multi-copy, assembly, build-volume splitting
    model_visualizer.py       # Preview rendering and framing
    model_cache.py            # Local model cache
    backup.py                 # Database backup and restore
    skill_manifest.py         # Agent-facing capability manifest
    pro_tool_manifest.json    # Generated paid-tier discovery/proxy manifest
    plugins/                  # Auto-discovered public MCP tool plugins
        assembly_tools.py
        slicer_tools.py
        monitoring_tools.py
        printer_management_tools.py
        printability_tools.py
        recovery_tools.py
        estimate_tools.py
        smart_print_tools.py
        design_tools.py
        generation_tools.py
        fulfillment_tools.py
        marketplace_tools.py
        mesh_tools.py
        utility_tools.py
        ...
    printers/
        base.py               # PrinterAdapter + dataclasses
        octoprint.py
        moonraker.py
        bambu.py
        prusaconnect.py
        elegoo.py
        serial_adapter.py
    marketplaces/
        base.py
        myminifactory.py
        cults3d.py
        thingiverse.py
    generation/
        base.py
        registry.py
        gemini.py
        meshy.py
        tripo3d.py
        stability.py
        openscad.py
    cli/
        main.py
        config.py
        discovery.py
        install_mcp.py
        output.py

deploy/                  # On-prem Enterprise deployment
    k8s/                 # Kubernetes manifests (9 files: namespace, deployment, service, ingress, configmap, secrets, PVC, network policies, HPA)
    helm/kiln/           # Helm chart (12 files: Chart.yaml, values.yaml, templates/)

octoprint-cli/           # Auxiliary OctoPrint-only CLI package

docs/
    WHITEPAPER.md
    LITEPAPER.md
    PROJECT_DOCS.md
    THREAT_MODEL.md
    site/                # Astro docs site; whitepaper/litepaper/project docs render from these markdown files
```

The private kiln-pro companion repo extends the public surface through `kiln_pro.bridge`, `kiln_pro.generate_manifest`, and `kiln_pro.rest_api`. Its feature directories include paid-tier plugins, assembly manuals, design versioning, recovery intelligence, cloud sync, enterprise identity/compliance, billing/spend caps, fulfillment proxying, and the web workshop. Public Kiln should depend only on the bridge, generated manifest, and documented response contracts.

*Kiln is a project of Hadron Labs Inc.*
