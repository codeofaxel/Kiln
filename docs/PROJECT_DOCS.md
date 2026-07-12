<p align="center">
  <img src="assets/kiln-horizontal-dark.svg" alt="Kiln" width="400">
</p>

# Kiln Documentation

## About Kiln

### Overview

Kiln gives AI agents a safe, unified way to design, validate, and manufacture 3D-printed parts. Connect it to Claude (or any MCP-compatible agent) and your assistant can take a part from description to done: design it, check that it will actually print, slice it, print it on your printer, watch the print, and help recover when something goes wrong — through <!-- KILN_MCP_CAPABILITY_COUNT --> 867 MCP capabilities and a <!-- KILN_CLI_COUNT --> 233-command CLI.

**Clarification:** Kiln does **not** operate its own marketplace or manufacturing network. It integrates with third-party marketplaces for model discovery and third-party fulfillment providers for outsourced manufacturing. Kiln is orchestration and agent infrastructure, not a supply-side platform.

**Three ways to get to a print:**

- **🖨️ Your printers.** Control OctoPrint, Moonraker, Creality, Bambu Lab, Prusa Link, Elegoo, or Direct USB/Serial machines on your LAN.
- **🌐 Third-party model search.** Search external model repositories such as MakerWorld and MyMiniFactory, then download where the provider permits, slice, and print.
- **🏭 Fulfillment providers.** Route jobs to third-party fulfillment services like Craftcloud for overflow, specialty materials, or when you don't own a printer.

All three modes use the same MCP tools and CLI commands. No printer and no install? You can also design in the browser — see [The Web App](#the-web-app) below.

**Non-goals:**

- Operating a first-party marketplace or manufacturing network
- Replacing third-party supply-side platforms
- Acting as merchant of record for provider-routed manufacturing orders

**Key properties:**

- **Local-first.** Printer communication stays on your network, and printer control never requires an account. Cloud touches are explicit and controllable: a version-update check (`KILN_NO_UPDATE_CHECK=1` disables it), anonymized community print-outcome sharing (controlled via telemetry preferences; never your designs or files), and opt-in signed-in features like cloud sync and stats.
- **One interface, many printers.** The same commands and tools work across every supported backend. New printer families plug in without changing how you use Kiln.
- **Safety-enforced.** Pre-flight checks, G-code validation, and temperature limits are built into the protocol — not optional.
- **Agent-native.** Every operation returns structured JSON. Every error includes machine-readable status codes. `--json` on every CLI command.

### What Kiln Does

The short version of the capability map. Everything below is reachable through the same MCP tools and CLI.

- **Design.** Describe a part in plain language and Kiln designs it — no external AI service or API key required — then previews it and iterates with you. Common items (coasters, keychains, trays, nameplates, and more) route to dedicated generators. Parametric templates, STEP/STL import, and opt-in cloud generation providers round out the ways a design can start.
- **Validate before you print.** Every model can be checked for printability before it reaches a printer: overhangs, thin walls, bridging, bed adhesion, support needs, warping risk, and more — scored, graded, and paired with concrete fixes. Parts that physically can't succeed (too big for the plate, a material your hotend can't melt) are caught up front, free.
- **Print and monitor.** Slice with your installed slicer, print on any supported printer, and monitor with camera snapshots, progress, temperatures, and health checks. Failed prints get diagnosis, guided recovery, and — on supported setups — resume from the layer where the print stopped.
- **Decorate.** Photos, logos, QR codes, text, and procedural textures can be applied to models for single- or multi-color printing (paid tiers; see [pricing](https://kiln3d.com/pricing)).
- **Learn.** Print outcomes feed material recommendations, per-printer settings, and calibration so the next print is smarter than the last. Community learning is anonymized and opt-in.
- **Version.** Designs, decorations, and mechanical features can be branched, merged, reviewed, released, and signed — version control that understands manufacturing artifacts (Kiln Pro).

Parts of Kiln's intelligence stack are patent-pending.

### The Web App

[kiln3d.com](https://kiln3d.com) is Kiln in the browser. Sign in for free and make a real, printable object on the spot — pick a starter (coaster, keychain, nameplate, and more), watch it build in 3D, apply a color or texture, and download it to print. No install, no printer, no API key required. The cloud workshop — version history, branches, reviews, and sharing for your designs — is a Kiln Pro feature. Nothing about local printer control requires an account.

### Supported Printers

| Backend | Protocol | Covers | Status |
|---|---|---|---|
| OctoPrint | HTTP REST | Any OctoPrint-connected printer | Stable |
| Moonraker | HTTP REST | Klipper-based machines (Voron, RatRig, Sovol, and more) | Stable |
| Creality | HTTP REST via Moonraker | K1/K2/Hi-class and other CrealityOS machines when local Moonraker is reachable; older Marlin Creality machines use OctoPrint or Direct USB | Stable when Moonraker is reachable |
| Bambu Lab | MQTT/LAN | X1, P1, P2, A1, A2, H2 families — including AMS support | Stable |
| Prusa Link | HTTP REST | MK4, XL, Mini+ | Stable |
| Elegoo | WebSocket/SDCP | Centauri Carbon, Saturn, Mars series; Neptune 4 / OrangeStorm Giga use Moonraker | Stable |
| Direct USB | Serial | Any Marlin/RepRapFirmware printer over USB | Stable |

The full, always-current list of printer models Kiln ships a tuned profile for lives at [kiln3d.com/printers](https://kiln3d.com/printers).

### Tiers

The core loop — design, validate, slice, print, monitor — is free and open source. Kiln Pro, Business, and Enterprise add depth on top: decoration and versioning, per-printer calibration and learning, production drawings, team workflows, and compliance features. See [kiln3d.com/pricing](https://kiln3d.com/pricing) for what each tier includes.

---

## Getting Started

### Installation

```bash
# Recommended
uv tool install kiln3d

# Also works
pip install kiln3d
pipx install kiln3d
```

Requirements: Python 3.10+. The pip package is `kiln3d`; the CLI command is `kiln`. OS-specific walkthroughs (Windows, WSL 2, Linux) live at [kiln3d.com/install](https://kiln3d.com/install).

To update later: `kiln self-update` (or ask your AI assistant — it will offer when a new version is out).

### Connect Your AI App

```bash
kiln signin        # create your free account (optional, unlocks stats and saved designs)
kiln install-mcp   # connects Kiln to Claude Desktop, Claude Code, and Codex automatically
```

Restart your AI app and it can run your printer. Using a different MCP client? `kiln install-mcp --print` prints the config — or paste it yourself:

```json
{
  "mcpServers": {
    "kiln": {
      "command": "python3",
      "args": ["-m", "kiln"],
      "env": {
        "KILN_PRINTER_HOST": "http://your-printer:80",
        "KILN_PRINTER_API_KEY": "your-api-key"
      }
    }
  }
}
```

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
kiln auth --name prusa-mini --host http://192.168.1.44 --type prusalink --api-key YOUR_KEY

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
curl http://192.168.1.60/api/version                    # OctoPrint

# Register directly by IP (no discovery required):
kiln auth --name printer --host http://192.168.1.44 --type prusalink --api-key YOUR_KEY
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

### Make Something From Scratch

The easiest way to design with Kiln is through your AI assistant. Just describe what you want:

> "Make me a phone stand for my desk — my phone is 78 mm wide."

Kiln captures the goal, designs the part itself (no external AI service or API key needed), shows you a preview, and iterates until it's right — then slices and prints it. Saved goals travel with the design, so monitoring, reprints, and later versions know what the part was for.

Cloud text-to-3D providers (Meshy, Tripo3D, Stability AI, Gemini) are available as opt-in alternatives — bring your own API key and ask for them explicitly. They are never required.

### No Printer?

You can still go from idea to object: design through the agent or the [web app](https://kiln3d.com), then route the job to a third-party fulfillment provider (`fulfillment_quote` → `fulfillment_order`), or just download your file and print it anywhere.

---

## CLI Reference

### Global Options

| Flag | Description |
|---|---|
| `--printer NAME` | Target a specific printer (overrides active) |
| `--json` | Output structured JSON (for agents/scripts) |
| `--help` | Show command help |

The CLI has <!-- KILN_CLI_COUNT --> 233 commands in total; this section documents the everyday core. Run `kiln --help` for the full command tree.

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
| `--type` | Yes | Backend: `octoprint`, `moonraker`, `creality`, `bambu`, `prusalink`, `elegoo`, `serial` |
| `--api-key` | OctoPrint, Moonraker/Creality, Prusa Link | API key when the backend requires one |
| `--access-code` | Bambu | Bambu Lab access code |
| `--serial` | Bambu, Elegoo optional | Bambu Lab serial number or SDCP mainboard identifier |
| `--printer-model` | Optional | Safety/profile hint such as `k1_max` or `bambu_a1` |

#### `kiln signin`
Create or sign in to your Kiln account (free). Optional — unlocks stats, saved design goals, and paid-tier features when you have them.

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

#### `kiln health`
Check Kiln's own setup: MCP client config drift, stale paths, environment problems — and self-heal where it can.

#### `kiln doctor` / `kiln doctor-creality`
Diagnose connection problems. The Creality variant verifies local Moonraker reachability and gives same-LAN/IP/port guidance.

#### `kiln self-update`
Update Kiln to the latest version. Holds off while a print is running.

#### `kiln accept-terms`
Show Kiln's Terms of Use and record your acceptance (one time, remembered everywhere you use Kiln).

#### `kiln cost <file> [--material MAT] [--electricity-rate N] [--printer-wattage N]`
Estimate the cost of printing a G-code file. Analyzes extrusion, calculates filament weight and cost, and optionally includes electricity cost.

#### `kiln material set|show|spools|add-spool`
Material tracking commands. `set` records loaded material on a printer. `show` displays current material. `spools` lists spool inventory. `add-spool` registers a new spool.

#### `kiln level [--status] [--trigger] [--set-prints N] [--set-hours N] [--enable/--disable]`
Bed leveling management. Check status, manually trigger leveling, or configure auto-leveling policy.

#### `kiln stream [--port 8081] [--stop]`
Start or stop the MJPEG webcam streaming proxy. Proxies the upstream printer webcam stream to a local HTTP endpoint.

#### `kiln events tail|summary`
Read-only access to the local event log.

#### `kiln goals`
List your saved design goals — the "what was this part for" record that designs, prints, and reprints attach to.

#### `kiln sync status|now|configure`
Cloud sync management. `status` shows sync state. `now` triggers immediate sync. `configure` sets it up (Kiln Pro).

#### `kiln plugins list|info`
Plugin management. `list` shows all discovered plugins. `info` shows details for a specific plugin.

#### `kiln install-mcp` / `kiln uninstall-mcp`
Install or remove Kiln from supported MCP clients. The installer detects common client config locations and writes the `kiln serve` entry for the current environment.

#### `kiln preview <file>` / `kiln validate <file>` / `kiln repair <file>`
Render a model preview, check print readiness, or repair common STL/3MF mesh defects before slicing.

#### `kiln assembly-manual --assembly <assembly.json> --out <dir>`
Generate a printable assembly manual when kiln-pro is installed. Public Kiln defines the assembly JSON contract; kiln-pro generates the PDF.

---

## MCP Server Reference

### Installation

The MCP server starts via `kiln serve` or `python -m kiln serve`. The fastest setup is `kiln install-mcp` (see [Getting Started](#connect-your-ai-app)).

### Tool Catalog (Selected)

Kiln exposes **<!-- KILN_MCP_TOOL_COUNT --> 860 MCP tools** and **<!-- KILN_MCP_CAPABILITY_COUNT --> 867 total MCP capabilities**. The everyday core is documented below by category; agents see the full, current catalog at connect time (`get_started` and `get_skill_manifest` return the complete map).

Paid-tier tools are discoverable too: agents without a license receive a structured response naming the required tier, so they can tell you what's possible and where it lives ([pricing](https://kiln3d.com/pricing)).

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
| `slice_and_print` | `input_path`, `profile` | Slice + upload + print result (AMS-aware on Bambu) |

#### Monitoring

| Tool | Input | Output |
|---|---|---|
| `printer_snapshot` | `save_path` | Image bytes or base64 |
| `monitor_print` | `printer_name`, `include_snapshot` | Standardized print status report (progress, temps, speed, camera) |
| `watch_print` | `printer_name`, `snapshot_interval`, ... | Background watcher with periodic snapshots |
| `check_print_health` | `printer_name` | Temperature-drift and health assessment |
| `multi_copy_print` | `model_path`, `copies`, `spacing_mm` | Arrange N copies on plate, slice, and print |

#### Fleet & Job Queue

| Tool | Input | Output |
|---|---|---|
| `fleet_status` | — | All printer states (includes per-printer nozzle info where reported) |
| `register_printer` | `name`, `type`, `host`, ... | Confirmation |
| `submit_job` | `filename`, `printer`, `priority` | Job ID (routes to the best printer when unspecified) |
| `job_status` | `job_id` | Job state and progress |
| `queue_summary` | — | Queue overview |
| `cancel_job` | `job_id` | Confirmation |
| `cancel_queued_jobs` | `printer_name?`, `dry_run` | Bulk-cancel waiting jobs, with preview — never touches a running print |

#### Model Discovery

| Tool | Input | Output |
|---|---|---|
| `search_all_models` | `query`, `page`, `sources` | Interleaved results from all connected marketplaces |
| `marketplace_info` | — | Connected sources, setup hints |
| `model_details` | `thing_id` | Model metadata |
| `model_files` | `thing_id` | File list |
| `download_model` | `thing_id`, `file_id` | Local path |
| `download_and_upload` | `file_id`, `source`, `printer_name` | Download + upload in one step |
| `browse_models` | `sort`, `category` | Model list |

Each search result carries print-readiness hints: `is_free`, `has_sliceable_files` (STL/3MF/OBJ that need slicing), and `has_printable_files` (ready-to-print G-code).

#### Materials

| Tool | Input | Output |
|---|---|---|
| `set_material` | `printer_name`, `material`, `color`, `spool_id` | Confirmation |
| `get_material` | `printer_name` | Loaded materials |
| `check_material_match` | `printer_name`, `expected_material` | Match result or warning |
| `list_spools` / `add_spool` / `remove_spool` | spool details | Spool inventory management |
| `get_material_properties` | `material` | Property sheet: thermal, mechanical, design limits |
| `compare_material_properties` | `material_a`, `material_b` | Side-by-side comparison |
| `recommend_material` | requirements | Ranked material recommendations with reasoning |
| `build_material_overrides` | `material`, `printer_model` | Slicer override dict for material+printer |
| `reprint_with_material` | `file_path`, `material`, `printer_name` | Re-slice and print with material overrides |
| `smart_reprint` | `file_path`, `material`, `printer_name` | Find file, detect AMS slot, switch material, print |
| `multi_material_print` | `objects`, `printer_name` | Multiple objects in different materials/colors on one plate |
| `ams_status` | `printer_name` | AMS units, trays, humidity, and dryer status (Bambu) |
| `ams_filament_drying` | `printer_name`, `action`, ... | Start/stop an AMS dryer cycle, with safety checks (free) |

Deeper material intelligence — drying schedules per filament, chemical-exposure verdicts, adhesive recommendations for joining printed parts, and datasheet ingestion — ships with paid tiers; see [Paid-Tier Tool Families](#paid-tier-tool-families-kiln-pro).

#### Design & Generation

| Tool | Input | Output |
|---|---|---|
| `design_session` | `verb`, `idea`, ... | The front door for making something new: captures your goal, designs, previews, iterates |
| `list_design_templates` | — | Parametric templates (phone stand, hooks, boxes, and more) |
| `generate_from_template` | `template_id`, parameters | Model from template via OpenSCAD |
| `generate_template_variations` | `template_id`, ranges | Batch variations across parameter space |
| `design_advisor` | `prompt`, `printer_model` | Recommends approach, material, and constraints |
| `analyze_design_requirements` | `requirements`, `material` | Functional-requirements analysis |
| `iterate_design` | `prompt`, `max_iterations` | Closed-loop generate → validate → improve |
| `generate_model` | `prompt`, `provider`, `format` | Generation job (opt-in cloud providers) |
| `generate_and_print` | `prompt`, `printer_name` | Full pipeline: generate → validate → slice → print |
| `validate_openscad_code` | `code` | Compile-check with structured errors |
| `import_step_file` | `file_path` | STEP import to printable mesh |
| `compose_part_from_primitives` | parts spec | Build a part from geometric primitives |

Design intelligence queries — material recommendations for a duty, printer design capabilities, template design rules, environment compatibility, structural load estimates, post-processing guides, and print-issue troubleshooting — are available as individual tools (`recommend_design_material`, `get_printer_design_capabilities`, `troubleshoot_print_issue`, and friends).

#### Printability & Mesh Tools

| Tool | Input | Output |
|---|---|---|
| `analyze_printability` | `file_path`, printer/material params | Printability report: score, grade, per-dimension findings, recommended fixes |
| `recommend_adhesion_settings` | `file_path`, `material`, `printer_model` | Brim/raft recommendation with slicer overrides |
| `auto_orient_model` | `file_path`, `output_path` | Auto-rotated mesh minimizing overhangs |
| `estimate_supports` | `file_path` | Support volume and weight estimate |
| `analyze_mesh_geometry` | `file_path` | Volume, surface area, overhangs, components, printability score |
| `repair_mesh` / `repair_mesh_advanced` | `file_path` | Mesh repair with stats |
| `check_print_readiness` | `file_path`, `auto_fix` | Single-call readiness gate with optional auto-repair |
| `diagnose_mesh` | `file_path` | Defect analysis: self-intersections, degenerate faces, holes |
| `mesh_quality_scorecard` | `file_path` | Multi-factor quality score with letter grade |
| `predict_print_failure` | `file_path` | Pre-print risk score with per-failure details |
| `scale_mesh_to_fit` / `center_model_on_bed` / `mirror_mesh_model` / `hollow_mesh_model` / `simplify_mesh_model` | `file_path`, params | Mesh transformations |
| `merge_mesh_files` / `split_mesh_by_component` / `compose_models` | `file_paths` | Composition and splitting |
| `export_model_3mf` / `extract_model_from_3mf` | `file_path` | 3MF conversion and extraction |
| `estimate_mesh_print_time` / `estimate_material_cost` / `estimate_support_material` | `file_path`, params | Pre-slice estimates |
| `compare_mesh_versions` | `file_a`, `file_b` | Geometric diff between two meshes |

All mesh analysis runs locally.

#### Cost Estimation

| Tool | Input | Output |
|---|---|---|
| `estimate_cost` | `file_path`, `material`, `electricity_rate` | Filament cost + electricity + total |
| `slice_and_estimate` | `input_path`, `material`, `printer_model` | Slice + cost + printability in one call (no print) |
| `compare_print_options` | `file_path`, `material` | Local vs. fulfillment cost comparison |
| `list_materials` | — | Material profiles with densities and costs |

#### Print Analysis & Recovery

| Tool | Input | Output |
|---|---|---|
| `await_print_completion` | `timeout_minutes` | Final print status |
| `analyze_print_failure` | `job_id` | Failure diagnosis with causes and recommendations |
| `analyze_print_failure_smart` | `job_id` | Automated root-cause analysis |
| `validate_print_quality` | `job_id` | Quality assessment with snapshot analysis |
| `detect_print_failure` | `printer_name` | Is this print failing right now? |
| `diagnose_print_failure_live` | `printer_name` | Real-time diagnosis during printing |
| `get_recovery_plan` / `start_print_recovery` / `complete_print_recovery` | failure context | Guided recovery session |
| `resume_interrupted_print` | `printer_name` | Resume from the layer a print stopped — works across supported FDM printers |
| `check_power_loss_recovery` | `printer_name` | Firmware power-loss recovery status |
| `failure_history` | `printer_name` | Past failures and their resolutions |

Wet-filament detection is built in: failures that look like moisture (popping, stringing, weak layers) get named as such, for any printer and material.

#### Vision Monitoring (Agent-Delegated)

Kiln provides structured monitoring data (webcam snapshots, temperatures, progress, phase context, failure hints) to agents. The agent's own vision model analyzes the snapshots — Kiln does not embed its own vision model.

| Tool | Input | Output |
|---|---|---|
| `monitor_print_vision` | `printer_name`, `include_snapshot` | Snapshot + state + failure hints for agent analysis |
| `watch_print` / `watch_print_status` / `stop_watch_print` | `watch_id` | Background watching with event-aware wake-up |

#### Safety & System

| Tool | Input | Output |
|---|---|---|
| `safety_status` / `safety_settings` | — | Current safety configuration and status |
| `safety_audit` | — | Safety compliance report |
| `get_session_log` | `session_id?` | Full tool-call history for an agent session |
| `emergency_stop` / `emergency_status` / `clear_emergency_stop` | `printer_name` | Hardware e-stop control |
| `kiln_health` | — | Version, uptime, module status |
| `check_my_tier` | — | "What plan am I on, and why" — plain-English tier diagnostic |
| `report_issue` | description | File a bug or feedback report (anonymous by default) |
| `recent_events` | `limit` | Event list |
| `register_webhook` / `list_webhooks` / `delete_webhook` | webhook params | Webhook management |
| `upgrade_kiln` | — | Update Kiln to the latest version |

G-code interception (rule-based command filtering between Kiln and the printer) is available for advanced setups: `start_gcode_interception`, `add_interception_rule`, `load_safety_interception_rules`, and friends.

#### Fulfillment (Third-Party Providers)

Routes manufacturing to third-party fulfillment providers (Craftcloud). Kiln acts as an API client — it does not operate manufacturing capacity or act as merchant of record. Orders require an explicit preview confirmation and a shipping-address confirmation before placing, so an agent can never order something you haven't seen.

| Tool | Input | Output |
|---|---|---|
| `fulfillment_materials` | `provider` | Available materials from the provider |
| `fulfillment_quote` | `file_path`, `material_id`, `quantity` | Manufacturing quote with provider ownership metadata |
| `fulfillment_order` | `quote_id`, confirmation tokens | Order confirmation |
| `fulfillment_order_status` / `fulfillment_cancel` | `order_id` | Tracking and cancellation |
| `save_shipping_profile` / `list_shipping_profiles` / `delete_shipping_profile` | profile params | Locally saved shipping profiles (explicit consent required) |
| `validate_shipping_address` | address fields | Validation with normalization |
| `estimate_price` / `estimate_timeline` | technology, size, quantity | Instant estimates before quoting |
| `supported_shipping_countries` | — | Where fulfillment can ship |
| `consumer_onboarding` | — | Step-by-step guide from idea to delivered product |

#### Paid-Tier Tool Families (kiln-pro)

The families below ship through [kiln-pro](https://kiln3d.com/pricing), Kiln's paid companion. Agents discover these tools even without a license and receive a structured pointer to the required tier.

- **Decoration & textures (Pro+).** Emboss or deboss photos, logos, SVG, QR codes, and text onto any face of a model; procedural and image-based textures with custom palettes for multicolor printing. Flagships: `decorate_surface`, `apply_procedural_texture`, `smart_decorate`.
- **Product generators (free with monthly quota; unlimited on Pro+).** One-call generators for coasters, keychains, nameplates, pet tags, trays, ornaments, magnets, and more — engraving-ready and printer-aware, each stating its assumptions (material, sizing) in plain English so you can redirect it in one line. Flagships: `generate_coaster`, `generate_keychain`, `generate_nameplate`.
- **Version control for designs (Pro+).** Branch, merge, review, release, and sign designs, decorations, and mechanical features; pull requests with team review (Business+ adds enforced approval gates). Flagships: `create_design_branch`, `merge_design_branches`, `sign_design_release`.
- **Provenance & learning (Pro+).** Design version history with print outcomes attached, regression alerts when a design stops printing well, proven recipes per material and printer, and print-confidence scoring. Flagships: `save_design_version`, `get_proven_recipe`, `get_regression_alerts`.
- **Per-machine calibration (Pro+).** Kiln learns each printer's real-world offsets from your prints and applies them when slicing — with freshness tracking and a plain-English answer to "why is this dimension what it is?" Flagships: `get_calibration_freshness`, `explain_calibration_for_design`, `ingest_measurement`.
- **Nozzle intelligence (Pro+).** Per-printer nozzle tracking (material, diameter, filament run through it), wear warnings before a print that would push it past its window, and wear-aware failure analysis. Flagships: `nozzle_wear_status`, `get_nozzle_state`, `record_nozzle_replacement`.
- **Material intelligence depth.** Drying schedules per filament with one-tap AMS dryer handoff (Pro+); chemical-exposure verdicts for fuels, solvents, cleaners, and outdoor duty (Business+; the safety warnings are always free); skin-contact verdicts for jewelry, wearables, and watch straps (always free — paid tiers add risk-reduction guidance and what it takes to sell a skin-contact product); adhesive recommendations with prep and cure schedules for joining printed parts (Pro+); manufacturer-datasheet ingestion into your own material library (Business+). Flagships: `drying_advisor`, `check_chemical_resistance`, `check_skin_contact_suitability`, `recommend_adhesive`, `ingest_material_datasheet`.
- **Production drawings (Business+).** Dimensioned technical drawings — aligned orthographic views with real measurements, PDF and DXF — from any design. Flagship: `generate_technical_drawing`.
- **Part Passport (Pro+).** Everything about one design in a single call: versions, materials, manual, BOM, outcomes, releases. Flagship: `get_part_passport`.
- **Sourcing risk for bought parts (Enterprise).** Flags the purchased components a design depends on that are going end-of-life, single-source, or long-lead — and proposes swaps — before you commit to a bill of materials you can't buy in a year. Flagships: `assess_bom_sourcing_risk`, `propose_sourcing_remediation`.
- **Assembly manuals (Pro+).** Printable step-by-step PDF manuals for multi-part designs, with BOM and per-step renders. Enterprise adds controlled, multi-language factory work instructions with torque specs, inspection gates, and sign-off. Flagships: `generate_assembly_manual`, `embed_manual_in_3mf`.
- **Mid-print modification & deep recovery (Pro+).** Add features or decorations to a paused print, resume across power loss, and (Business+) triage a whole fleet after an outage. Flagships: `decorate_during_print`, `recover_power_loss_print`, `assess_fleet_power_loss`.
- **Business & Enterprise operations.** Billing and spend caps, team and org management, SSO, audit-trail export, multi-site fleet views, and project cost tracking. See [pricing](https://kiln3d.com/pricing) for the full breakdown.

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

**Webcam:** Snapshots supported via the OctoPrint webcam interface.

### Moonraker

Communicates via HTTP REST. No authentication required by default (Moonraker trusts the local network).

**Configuration:**
```yaml
type: moonraker
host: http://voron.local:7125
```

**Webcam:** Cameras are discovered automatically from Moonraker's webcam list.

### Creality

Modern CrealityOS/Klipper FDM printers are supported through local Moonraker when it is reachable on the printer's LAN. The `creality` backend probes common Creality Moonraker ports and then behaves like a Moonraker printer. Older Marlin-based Creality machines should use `octoprint` or `serial`.

**Configuration:**
```yaml
type: creality
host: 192.168.1.55
printer_model: k1_max
```

**Diagnostics:** Run `kiln doctor-creality --host 192.168.1.55 --model k1_max` to verify reachability and get same-LAN/IP/port/API-key guidance when local Moonraker is not reachable.

**Caveat:** Not every stock Creality firmware exposes local Moonraker. Support depends on the printer answering on your LAN.

### Bambu Lab

Communicates via MQTT over LAN. Requires the printer's access code and serial number (found in printer settings).

**Configuration:**
```yaml
type: bambu
host: 192.168.1.100
access_code: "12345678"
serial: "01P00A000000001"
```

**Notes:** Requires the `paho-mqtt` package. AMS material routing, AMS dryer control (AMS 2 Pro / HT), and network discovery are supported. On A-series printers, enable LAN Developer Mode for full control.

### Prusa Link

Communicates via the Prusa Link REST API. Requires a Prusa Link API key.

**Configuration:**
```yaml
type: prusalink
host: http://prusa-mk4.local
api_key: YOUR_KEY
```

**Limitations:** No raw G-code or direct temperature control — these are managed through print files. (Configs saved under the old `prusaconnect` type name are auto-detected and migrated.)

### Elegoo (SDCP)

Communicates via the SDCP protocol over WebSocket. No authentication required on the local network. Covers Elegoo printers with SDCP mainboards: Centauri Carbon, Saturn, Mars series.

> **Note:** Elegoo Neptune 4 and OrangeStorm Giga run Klipper/Moonraker — use the `moonraker` adapter type for those.

**Configuration:**
```yaml
type: elegoo
host: 192.168.1.50
mainboard_id: ABCD1234ABCD1234  # optional, auto-discovered
```

**File upload:** the printer fetches files from your machine over the local network, so your firewall must allow local inbound connections from the printer.

**Dependencies:** Requires the `websocket-client` package. Install with `pip install 'kiln[elegoo]'`.

### Direct USB / Serial

Any Marlin or RepRapFirmware printer over a USB serial connection.

**Configuration:**
```yaml
type: serial
host: /dev/tty.usbserial-0001
```

---

## Model Marketplaces (Third-Party Search)

Kiln searches third-party model repositories on your behalf — it does not host or serve models itself. `search_all_models` queries every connected marketplace at once and interleaves the results; if one source fails, the others still return.

### Supported Marketplaces

| Marketplace | Auth | Download Support | Notes |
|---|---|---|---|
| MakerWorld | None required | Metadata + link-out (download on makerworld.com) | Connected out of the box |
| MyMiniFactory | API key | Yes | Recommended for direct-download workflows |
| Cults3D | Username + API key | No (metadata-only — provides URLs for manual download) | |
| Thingiverse | Bearer token | Yes | *Deprecated — acquired by MyMiniFactory (Feb 2026). Prefer MyMiniFactory.* |

### Configuration

MakerWorld works with no setup. For the others, set environment variables for each marketplace you want to enable:

```bash
export KILN_MMF_API_KEY=your_key               # MyMiniFactory developer key
export KILN_CULTS3D_USERNAME=your_username      # Cults3D account username
export KILN_CULTS3D_API_KEY=your_key            # https://cults3d.com/en/api/keys
export KILN_THINGIVERSE_TOKEN=your_token        # Deprecated
```

Adapters are auto-registered at server startup based on available credentials. Only configured marketplaces participate in searches.

### Download and Upload

`download_and_upload` combines marketplace file download with printer upload in a single tool call, for marketplaces that support direct downloads.

---

## Safety Systems

### Pre-flight Checks

Every print job should pass through `preflight_check()`:

1. **Printer online** — Kiln can reach the printer
2. **Printer idle** — No active job running
3. **File exists** — Target file is on the printer
4. **Temperature safe** — Targets within safe bounds
5. **Material validation** — When `--material` is specified, temperatures match expected ranges
6. **Physical feasibility** — A part that can't fit the build volume, or a material the hotend can't melt, is caught before filament is wasted. Kiln rotates a part to fit when it can; `force_print_oversize` is the explicit override for slicing-for-another-printer workflows.

### Preview Confirmation

New print starts require a preview-confirmation step:

1. Render a model preview (`kiln preview` or the preview tools).
2. Show the preview to the user and get approval.
3. Start the print with the issued confirmation.

Confirmations are single-use, short-lived, and bound to the file and printer, so an agent can never quietly start a print you haven't seen. `KILN_SKIP_PREVIEW_GATE=1` is the explicit advanced-user bypass and is logged whenever used.

### G-code Validation

`validate_gcode()` screens commands before they reach hardware:

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

Per-printer safety profiles tighten these further for specific machines, and idle heaters are automatically cooled by a background watchdog if left on with no active print.

---

## Authentication

Local API-key auth for the Kiln server is disabled by default. Enable it for multi-user or network-exposed setups.

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

**Event types** cover the job lifecycle (`job.submitted`, `job.completed`, `job.failed`, ...), print state (`print.started`, `print.paused`, `print.failed`, ...), printer availability (`printer.online`, `printer.offline`, `printer.error`), materials (`material.loaded`, `material.mismatch`, `material.spool_low`, ...), leveling, sync, recovery, and vision checks. `recent_events` lists what your install emits.

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

The common ones:

| Variable | Description |
|---|---|
| `KILN_PRINTER_HOST` | Printer URL (fallback when no config) |
| `KILN_PRINTER_API_KEY` | API key for backends that require one |
| `KILN_PRINTER_TYPE` | Backend type |
| `KILN_PRINTER_SERIAL` | Bambu serial number or Elegoo mainboard ID |
| `KILN_PRINTER_ACCESS_CODE` | Bambu access code |
| `KILN_SLICER_PATH` | Explicit path to slicer binary |
| `KILN_BAMBU_TLS_MODE` | Bambu TLS validation mode: `pin` (default), `ca`, or `insecure` |
| `KILN_NO_UPDATE_CHECK` | Disable the version-update check |
| `KILN_SKIP_PREVIEW_GATE` | Advanced-user bypass for preview confirmation; logged when used |
| `KILN_MMF_API_KEY` | MyMiniFactory API key |
| `KILN_CULTS3D_USERNAME` / `KILN_CULTS3D_API_KEY` | Cults3D credentials |
| `KILN_AUTH_ENABLED` / `KILN_AUTH_KEY` | Local server API-key auth |
| `KILN_GEMINI_API_KEY` / `KILN_MESHY_API_KEY` / `KILN_TRIPO3D_API_KEY` / `KILN_STABILITY_API_KEY` | Opt-in cloud generation providers |

### Precedence

CLI flags > Environment variables > Config file.

---

## Development

Kiln's public package is open source (AGPL-3.0) and developed at [github.com/codeofaxel/Kiln](https://github.com/codeofaxel/Kiln).

### Setup

```bash
git clone https://github.com/codeofaxel/Kiln.git
cd Kiln
pip install -e "./kiln[dev]"
```

### Running Tests

```bash
cd kiln && python3 -m pytest tests/ -v
```

### Adding a New Printer Adapter

1. Create `kiln/src/kiln/printers/yourbackend.py`
2. Implement the abstract methods from `PrinterAdapter` in `base.py`
3. Return typed dataclasses — never raw dicts
4. Map all backend states to the `PrinterStatus` enum
5. Handle connection failures gracefully (return `OFFLINE`)
6. Add to the adapter factory in `cli/config.py`
7. Write tests following the existing adapter test patterns

See `CONTRIBUTING.md` in the repository for the full contributor guide.

### About kiln-pro

Kiln's paid tiers ship through the private `kiln-pro` companion package ([kiln3d.com/pricing](https://kiln3d.com/pricing)). Public Kiln defines the interface contracts — the tool manifest, response shapes, and the assembly JSON contract — and works fully without it; kiln-pro provides the licensed features behind those contracts.

*Kiln is a project of Hadron Labs Inc.*

<!-- DOCS_REVIEWED_FOR: 1.1.9 -->
