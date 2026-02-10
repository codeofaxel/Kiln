# Kiln — Completed Tasks

Record of finished features and milestones, newest first.

## 2026-02-10

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
