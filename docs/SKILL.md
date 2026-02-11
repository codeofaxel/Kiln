---
name: kiln
description: 3D printer control and monitoring via Kiln MCP server
metadata: {"openclaw":{"requires":{"env":["KILN_PRINTER_HOST"]}}}
---

# Kiln — Agent Skill Definition

You are controlling a physical 3D printer through Kiln, an MCP server.
**Physical actions are irreversible and can damage hardware.** Follow
these rules strictly.

## Safety Model

Kiln enforces **physical safety** — it will hard-block commands that
exceed temperature limits, contain dangerous G-code, or fail pre-flight
checks. You cannot bypass these.

**You** enforce **operational judgment** — deciding when to ask the
human for confirmation vs. acting autonomously. This document defines
those rules.

## Tool Safety Levels

Every tool has a safety level. Follow the expected behavior exactly.

| Level | Meaning | Your Behavior |
|-------|---------|---------------|
| `safe` | Read-only, no physical effect | Call freely. No confirmation needed. |
| `guarded` | Has physical effect but low-risk. Kiln enforces limits. | Call without asking. Report what you did. |
| `confirm` | Irreversible or significant state change. | **Ask the human first.** State what you will do and why. Wait for approval. |
| `emergency` | Safety-critical. | **Ask the human** unless you detect active danger (thermal runaway, collision). |

## Tool Classifications

### Safe (read-only, call freely)

- `printer_status` — Current printer state, temps, progress
- `printer_files` — List files on the printer
- `preflight_check` — Pre-print safety validation
- `validate_gcode` — Dry-run G-code validation (no side effects)
- `validate_gcode_safe` — Printer-specific G-code validation
- `fleet_status` — Multi-printer overview
- `discover_printers` — mDNS network scan
- `job_status` — Check a queued job
- `queue_summary` — Queue overview
- `job_history` — Past job records
- `recent_events` — Event log
- `billing_summary` / `billing_status` / `billing_history` — Billing info
- `health_check` / `kiln_health` — Server health
- `safety_settings` — Current safety configuration
- `marketplace_info` — Marketplace status
- `model_details` / `model_files` — Model metadata
- `list_model_categories` — Browse categories
- `search_models` / `search_all_models` / `browse_models` — Search models
- `find_slicer_tool` — Locate slicer binary
- `list_materials` / `get_material` / `get_material_recommendation` — Material info
- `check_material_match` — Material compatibility
- `list_spools` — Spool inventory
- `bed_level_status` — Bed leveling data
- `cloud_sync_status` — Sync status
- `list_plugins` / `plugin_info` — Plugin info
- `fulfillment_materials` / `fulfillment_order_status` — Fulfillment info
- `list_webhooks` — Webhook endpoints
- `list_safety_profiles` / `get_safety_profile` — Safety profile data
- `list_slicer_profiles_tool` / `get_slicer_profile_tool` — Slicer profiles
- `get_printer_intelligence` / `get_printer_insights` — Printer knowledge
- `list_print_pipelines` — Available pipelines
- `list_generation_providers` — 3D model generation providers
- `generation_status` — Check generation progress
- `get_agent_context` — Retrieve agent memory
- `printer_stats` — Printer statistics
- `print_history` — Past print records
- `estimate_cost` — Cost estimation
- `compare_print_options` — Compare print strategies
- `suggest_printer_for_job` — Fleet job routing
- `validate_generated_mesh` — Mesh integrity check
- `firmware_status` — Firmware version info
- `analyze_print_failure` — Failure diagnosis
- `troubleshoot_printer` — Guided troubleshooting
- `list_generation_providers` — Available generators
- `printer_snapshot` — Camera still image
- `webcam_stream` — Camera stream URL

### Guarded (low-risk physical effect, report what you did)

- `pause_print` — Pause current print (reversible)
- `resume_print` — Resume paused print (reversible)
- `upload_file` — Upload G-code to printer (Kiln validates content)
- `upload_file_confirm` — Confirm a pending upload
- `slice_model` — Slice a 3D model (CPU only, no printer effect)
- `register_printer` — Add printer to fleet (reversible)
- `save_agent_note` / `delete_agent_note` — Agent memory management
- `add_spool` / `remove_spool` — Spool inventory management
- `set_material` — Set active material
- `register_webhook` — Add a webhook endpoint
- `cloud_sync_configure` / `cloud_sync_now` — Cloud sync
- `set_leveling_policy` — Set bed leveling policy
- `download_model` — Download a model file
- `record_print_outcome` — Log print result
- `annotate_print` — Add notes to a print
- `validate_print_quality` — Quality assessment
- `billing_setup_url` — Get billing portal URL
- `await_print_completion` / `watch_print` — Wait for print to finish
- `monitor_print_vision` — Vision-based print monitoring

### Confirm (ask human first)

- `start_print` — **Begins physical printing.** Always confirm file name and material.
- `cancel_print` — **Irreversible.** Print cannot be resumed. Confirm with human.
- `set_temperature` — **Physical effect.** Tell the human what temp you're setting and why. If Kiln returns warnings, relay them.
- `send_gcode` — **Raw commands.** Explain what you're sending and why.
- `delete_file` — **Irreversible** file deletion.
- `submit_job` — Queues a job for execution.
- `cancel_job` — Removes a queued job.
- `delete_webhook` — Removes a webhook endpoint.
- `trigger_bed_level` — Initiates physical bed probing.
- `download_and_upload` — Downloads and uploads to printer in one step.
- `slice_and_print` — Full slice-to-print pipeline.
- `run_quick_print` — Full pipeline: slice + upload + print.
- `run_calibrate` — Runs calibration routine on printer.
- `run_benchmark` — Runs benchmark print.
- `generate_model` — Generates a 3D model via AI.
- `generate_and_print` — Generates and prints (high risk).
- `download_generated_model` — Downloads AI-generated model.
- `await_generation` — Waits for model generation.
- `fulfillment_order` / `fulfillment_quote` / `fulfillment_cancel` — External orders.
- `update_firmware` — **High risk.** Firmware changes.
- `rollback_firmware` — **High risk.** Firmware rollback.

### Emergency (ask human unless active danger)

- `emergency_stop` — Firmware-level halt. **Only for genuine emergencies** (thermal runaway, collision, fire risk). If you detect active danger via monitoring, you may call this and then immediately inform the human.

## Recommended Workflows

### Upload and Print

```
1. preflight_check()              [safe — verify printer ready]
2. upload_file(path)              [guarded — Kiln validates G-code]
3. IF warnings: relay to human
4. start_print(file)              [confirm — "Ready to print {file}. Proceed?"]
5. printer_status() periodically  [safe — monitor progress]

After starting a print:
6. Wait 2-3 minutes for the first layer to complete
7. Call monitor_print_vision() or printer_snapshot() to check first layer
8. If vision detects issues (spaghetti, adhesion failure, warping):
   - Confidence >= 0.8: pause_print() and alert the human
   - Confidence < 0.8: continue monitoring, check again in 2 minutes
9. If first layer looks good, continue to periodic monitoring
```

### Temperature Adjustment

```
1. printer_status()               [safe — check current temps]
2. set_temperature(tool=X, bed=Y) [confirm — "Setting hotend to X°C, bed to Y°C for {material}. OK?"]
3. IF Kiln returns warnings: relay them ("Kiln warns: large temperature delta")
```

### Emergency Response

```
1. Detect issue via printer_status() or monitor_print_vision()
2. IF thermal runaway or imminent physical danger:
   → emergency_stop()             [emergency — may bypass confirmation]
   → Immediately tell human: "Emergency stop triggered because: {reason}"
3. IF quality issue (spaghetti, adhesion failure) but no immediate danger:
   → Ask human: "Detected potential failure. Cancel print?"
   → cancel_print() only after confirmation
```

### Print Monitoring Loop

Recommended monitoring pattern after print starts:

```
1. After start_print, wait 2-3 minutes for first layer
2. Call printer_snapshot() to visually check first layer adhesion
3. If using vision: monitor_print_vision() for automated failure detection
4. During print, check printer_status() every 5-10 minutes for:
   - Temperature anomalies (sudden drops = heater failure, spikes = thermal runaway)
   - Progress stalls (same percentage for >10 minutes = possible jam)
   - Error states (OFFLINE, ERROR)
5. On completion: log to print_history, turn off heaters
```

#### When to Escalate vs Auto-Handle

| Situation | Action | Tool |
|-----------|--------|------|
| First layer failure (high confidence) | Pause + alert human | pause_print() |
| Temperature out of range | Alert human | printer_status() |
| Filament runout detected | Pause + alert human | pause_print() |
| Print progress stalled | Alert human (do NOT cancel) | printer_status() |
| Spaghetti / complete detach | emergency_stop() | emergency_stop() |
| Normal completion | Log + cool down | set_tool_temp(0), set_bed_temp(0) |

## Operational Policies

### Heater Idle Protection
Never set temperatures above 0°C on a printer that is idle with no
print job queued, unless the human explicitly asks for pre-heating.
If you do set temperatures, remind the human: "Heaters are on.
Remember to turn them off when done."

### Relay All Warnings
When Kiln returns `warnings` in any response, always relay them to the
human verbatim. Never silently ignore warnings.

### Never Generate G-code
Never write, generate, or modify G-code directly. Use pre-sliced files
from the printer's storage, or use the `slice_model` / `run_quick_print`
pipeline which uses validated slicer profiles.

### Material Awareness
Before starting a print, check what material the printer has loaded
(`get_material`). If the G-code expects a different material, warn the
human before proceeding.

### First-Layer Monitoring
If the printer has a camera (`printer_snapshot`), check the first few
minutes of a new print for adhesion issues. If something looks wrong,
ask the human before taking action.

## What Kiln Enforces (you cannot bypass)

| Protection | How |
|-----------|-----|
| Max temperature per printer model | `KILN_PRINTER_MODEL` safety profiles |
| Blocked G-code commands | M112, M500-502, M552-554, M997 always rejected |
| Pre-flight before printing | Mandatory — `start_print` runs it automatically |
| G-code validation on upload | Full file scanned for blocked commands |
| G-code validation on send | Every `send_gcode` call is validated |
| Rate limiting | Dangerous tools have cooldowns to prevent spam |
| File size limits | 500MB upload max |

## Configuration

| Env Var | Purpose | Default |
|---------|---------|---------|
| `KILN_PRINTER_HOST` | Printer URL | (required) |
| `KILN_PRINTER_API_KEY` | Printer API key | (required for OctoPrint/Bambu) |
| `KILN_PRINTER_TYPE` | `octoprint` / `moonraker` / `bambu` / `prusaconnect` | `octoprint` |
| `KILN_PRINTER_MODEL` | Safety profile id (e.g. `ender3`, `bambu_x1c`) | (generic 300/130 limits) |
| `KILN_CONFIRM_UPLOAD` | Require confirmation before uploads | `false` |
