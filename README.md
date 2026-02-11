# Kiln

Agentic infrastructure for physical fabrication via 3D printing.

Kiln lets AI agents design, queue, and execute physical manufacturing jobs on real 3D printers with zero human intervention. It exposes printer control through both a **CLI** and the **Model Context Protocol (MCP)**, making any MCP-compatible agent (Claude, GPT, custom) a first-class operator of your print farm.

## Architecture

```
AI Agent (Claude, GPT, custom)
    |
    | CLI or MCP (Model Context Protocol)
    v
+-------------------+
|       Kiln        |  <-- CLI + MCP server for printer control
+-------------------+
    |           |            |
    |   PrinterAdapter   Marketplaces (Thingiverse, MMF, Cults3D)
    |        |               |
    v        v               v
+------------+  +------------+  +--------+
| OctoPrint  |  | Moonraker  |  | Bambu  |
+------------+  +------------+  +--------+
    |                |              |
    v                v              v
  Prusa i3        Voron          Bambu X1C
```

## Packages

This monorepo contains two packages:

| Package | Description | Entry Point |
|---------|-------------|-------------|
| **kiln** | CLI + MCP server for multi-printer control (OctoPrint, Moonraker, Bambu, Prusa Connect) | `kiln` or `python -m kiln` |
| **octoprint-cli** | Lightweight standalone CLI for OctoPrint-only setups | `octoprint-cli` |

## Quick Start

### Kiln CLI

```bash
# Install
pip install -e ./kiln

# Discover printers on your network
kiln discover

# Add a printer
kiln auth --name my-printer --host http://octopi.local --type octoprint --api-key YOUR_KEY

# Check printer status
kiln status

# Upload and print a file
kiln upload model.gcode
kiln print model.gcode

# Slice an STL and print in one step
kiln slice model.stl --print-after

# Batch print multiple files
kiln print *.gcode --queue

# Monitor a running print
kiln wait

# Take a webcam snapshot
kiln snapshot --save photo.jpg

# View print history
kiln history --status completed

# All commands support --json for agent consumption
kiln status --json
```

### CLI Commands

```
kiln discover                              # Scan network for printers (mDNS)
kiln auth --name N --host H --type T       # Save printer credentials
kiln status [--json]                       # Printer state + job progress
kiln files [--json]                        # List files on printer
kiln upload <file> [--json]                # Upload G-code file
kiln print <files>... [--queue] [--json]   # Start printing (supports batch + queue)
kiln cancel [--json]                       # Cancel current print
kiln pause [--json]                        # Pause current print
kiln resume [--json]                       # Resume paused print
kiln temp [--tool N] [--bed N] [--json]    # Get/set temperatures
kiln gcode <cmds>... [--json]              # Send raw G-code
kiln printers [--json]                     # List saved printers
kiln use <name>                            # Switch active printer
kiln remove <name>                         # Remove a saved printer
kiln preflight [--material MAT] [--json]   # Pre-print safety checks
kiln slice <file> [--print-after] [--json] # Slice STL/3MF to G-code
kiln snapshot [--save PATH] [--json]       # Capture webcam snapshot
kiln wait [--timeout N] [--json]           # Wait for print to finish
kiln history [--status S] [--json]         # View past prints
kiln order materials [--json]              # List fulfillment materials
kiln order quote <file> -m MAT [--json]   # Get manufacturing quote
kiln order place <quote_id> [--json]      # Place a fulfillment order
kiln order status <order_id> [--json]     # Track order status
kiln order cancel <order_id> [--json]     # Cancel an order
kiln cost <file> [--material PLA] [--json]    # Estimate print cost
kiln material set|show|spools|add-spool       # Material tracking
kiln level [--status] [--trigger] [--json]    # Bed leveling triggers
kiln stream [--port 8081] [--stop] [--json]   # Webcam MJPEG proxy
kiln sync status|now|configure                # Cloud sync
kiln plugins list|info                        # Plugin management
kiln generate "a phone stand" --provider meshy --json   # Generate 3D model from text
kiln generate-status <job_id> --json                    # Check generation status
kiln generate-download <job_id> -o ./models --json      # Download generated model
kiln serve                                 # Start MCP server
```

Global option: `--printer <name>` to target a specific printer per-command.

### MCP Server

```bash
# Start the MCP server
kiln serve

# Or with environment variables
export KILN_PRINTER_HOST=http://octopi.local
export KILN_PRINTER_API_KEY=your_api_key
export KILN_PRINTER_TYPE=octoprint
kiln serve
```

#### Claude Desktop Integration

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

### OctoPrint CLI

```bash
# Install
pip install -e ./octoprint-cli

# Initialize config
octoprint-cli init --host http://octopi.local --api-key YOUR_KEY

# Use
octoprint-cli status
octoprint-cli files
octoprint-cli print myfile.gcode --confirm
```

## MCP Tools

The Kiln MCP server (`kiln serve`) exposes these tools to agents:

| Tool | Description |
|------|-------------|
| `printer_status` | Get printer state, temperatures, job progress |
| `printer_files` | List available G-code files |
| `upload_file` | Upload a local G-code file to the printer |
| `start_print` | Start printing a file |
| `cancel_print` | Cancel the active print job |
| `pause_print` | Pause the active print |
| `resume_print` | Resume a paused print |
| `set_temperature` | Set hotend and/or bed temperature |
| `preflight_check` | Run safety checks before printing |
| `send_gcode` | Send raw G-code commands |
| `validate_gcode` | Validate G-code without sending |
| `fleet_status` | Get status of all registered printers |
| `register_printer` | Add a printer to the fleet |
| `submit_job` | Submit a print job to the queue |
| `job_status` | Check status of a queued job |
| `queue_summary` | Overview of the job queue |
| `cancel_job` | Cancel a queued or running job |
| `recent_events` | Get recent events from the event bus |
| `kiln_health` | System health check (version, uptime, modules) |
| `register_webhook` | Register a webhook for event notifications |
| `list_webhooks` | List all registered webhooks |
| `delete_webhook` | Remove a webhook endpoint |
| `search_all_models` | Search Thingiverse, MyMiniFactory, and Cults3D simultaneously |
| `marketplace_info` | Show connected marketplaces and setup hints |
| `search_models` | Search Thingiverse for 3D models |
| `model_details` | Get details for a Thingiverse model |
| `model_files` | List files for a Thingiverse model |
| `download_model` | Download a model file from Thingiverse |
| `download_and_upload` | Download from any marketplace and upload to printer in one step |
| `browse_models` | Browse popular/newest/featured models |
| `list_model_categories` | List Thingiverse categories |
| `slice_model` | Slice an STL/3MF file to G-code |
| `find_slicer_tool` | Detect installed slicer (PrusaSlicer/OrcaSlicer) |
| `slice_and_print` | Slice a model then upload and print in one step |
| `printer_snapshot` | Capture a webcam snapshot from the printer |
| `fulfillment_materials` | List materials from external print services (Craftcloud, Shapeways, Sculpteo) |
| `fulfillment_quote` | Get a manufacturing quote for a 3D model |
| `fulfillment_order` | Place an order based on a quote |
| `fulfillment_order_status` | Track a fulfillment order |
| `fulfillment_cancel` | Cancel a fulfillment order |
| `estimate_cost` | Estimate print cost from G-code file |
| `list_materials` | List available material profiles |
| `set_material` | Set loaded material on a printer |
| `get_material` | Get loaded material for a printer |
| `check_material_match` | Verify material matches expected |
| `list_spools` | List spool inventory |
| `add_spool` | Add a spool to inventory |
| `remove_spool` | Remove a spool from inventory |
| `bed_level_status` | Get bed leveling status for a printer |
| `trigger_bed_level` | Trigger bed leveling on a printer |
| `set_leveling_policy` | Configure auto-leveling policy |
| `webcam_stream` | Start/stop/status MJPEG stream proxy |
| `cloud_sync_status` | Get cloud sync status |
| `cloud_sync_now` | Trigger immediate sync |
| `cloud_sync_configure` | Configure cloud sync settings |
| `list_plugins` | List installed plugins |
| `plugin_info` | Get details for a specific plugin |
| `await_print_completion` | Poll until a print job finishes (completed/failed/cancelled/timeout) |
| `compare_print_options` | Side-by-side local vs. fulfillment cost comparison |
| `analyze_print_failure` | Diagnose a failed print job with causes and recommendations |
| `validate_print_quality` | Post-print quality assessment with snapshot and event analysis |
| `generate_model` | Generate a 3D model from a text description (Meshy AI or OpenSCAD) |
| `generation_status` | Check the status of a model generation job |
| `download_generated_model` | Download a completed generated model with mesh validation |
| `await_generation` | Wait for a generation job to complete (polling) |
| `generate_and_print` | Full pipeline: generate -> validate -> slice -> upload -> print |
| `validate_generated_mesh` | Validate an STL/OBJ mesh for printing readiness |

## Supported Printers

| Backend | Status | Printers |
|---------|--------|----------|
| **OctoPrint** | Stable | Any OctoPrint-connected printer (Prusa, Ender, custom) |
| **Moonraker** | Stable | Klipper-based printers (Voron, Ratrig, etc.) |
| **Bambu** | Stable | Bambu Lab X1C, P1S, A1 (via LAN MQTT) |
| **Prusa Connect** | Stable | Prusa MK4, XL, Mini+ (via Prusa Link REST API) |

## MCP Resources

The server also exposes read-only resources that agents can use for context:

| Resource URI | Description |
|---|---|
| `kiln://status` | System-wide snapshot (printers, queue, events) |
| `kiln://printers` | Fleet listing with idle printers |
| `kiln://printers/{name}` | Detailed status for a specific printer |
| `kiln://queue` | Job queue summary and recent jobs |
| `kiln://queue/{job_id}` | Detail for a specific job |
| `kiln://events` | Recent events (last 50) |

## Modules

| Module | Description |
|---|---|
| `server.py` | MCP server with tools, resources, and subsystem wiring |
| `printers/` | Printer adapter abstraction (OctoPrint, Moonraker, Bambu, Prusa Connect) |
| `marketplaces/` | Model marketplace adapters (Thingiverse, MyMiniFactory, Cults3D) |
| `slicer.py` | Slicer integration (PrusaSlicer, OrcaSlicer) with auto-detection |
| `registry.py` | Fleet registry for multi-printer management |
| `queue.py` | Priority job queue with status tracking |
| `scheduler.py` | Background job dispatcher (queue -> idle printers) |
| `events.py` | Pub/sub event bus with history |
| `persistence.py` | SQLite storage for jobs, events, and settings |
| `webhooks.py` | Event-driven webhook delivery with HMAC signing |
| `auth.py` | Optional API key authentication with scope-based access |
| `billing.py` | Fee tracking for 3DOS network-routed jobs |
| `discovery.py` | Network printer discovery (mDNS + HTTP probe) |
| `generation/` | Text-to-model generation providers (Meshy AI, OpenSCAD) with mesh validation |
| `fulfillment/` | External manufacturing service adapters (Craftcloud, Shapeways, Sculpteo) |
| `cost_estimator.py` | Print cost estimation from G-code analysis |
| `materials.py` | Multi-material and spool tracking |
| `bed_leveling.py` | Automated bed leveling trigger system |
| `streaming.py` | MJPEG webcam streaming proxy |
| `cloud_sync.py` | Cloud sync for printer configs and job history |
| `plugins.py` | Plugin system with entry-point discovery |
| `gcode.py` | G-code safety validator |
| `cli/` | Click CLI with 25+ subcommands and JSON output |

## Authentication (Optional)

Kiln supports optional API key authentication for MCP tools. Disabled by default.

```bash
# Enable auth
export KILN_AUTH_ENABLED=1
export KILN_AUTH_KEY=your_secret_key

# Clients provide their key via
export KILN_MCP_AUTH_TOKEN=your_secret_key
```

Scopes: `print`, `files`, `queue`, `temperature`, `admin`. Read-only tools (status, list) never require auth.

## Webhooks

Register HTTP endpoints to receive real-time event notifications:

```
register_webhook(url="https://example.com/hook", events=["job.completed", "print.failed"])
```

Payloads are signed with HMAC-SHA256 when a secret is provided.

## Printer Discovery

Kiln can automatically find printers on your local network:

```bash
kiln discover
```

Discovery uses mDNS/Bonjour and HTTP subnet probing to find OctoPrint, Moonraker, and Bambu printers.

## Model Marketplaces

Kiln includes adapters for discovering and downloading 3D models from popular marketplaces:

| Marketplace | Status | Features |
|---|---|---|
| **Thingiverse** | Stable | Search, browse, download, categories |
| **MyMiniFactory** | Stable | Search, details, download |
| **Cults3D** | Stable | Search, details (metadata-only, no direct download) |

Configure credentials for the marketplaces you use:

```bash
export KILN_THINGIVERSE_TOKEN=your_token       # Thingiverse
export KILN_MMF_API_KEY=your_key               # MyMiniFactory
export KILN_CULTS3D_USERNAME=your_username      # Cults3D
export KILN_CULTS3D_API_KEY=your_key            # Cults3D
```

All configured marketplaces are searched simultaneously via `search_all_models`. Agents can inspect details, download files, and upload directly to a printer — enabling a full design-to-print workflow without human intervention.

## Slicer Integration

Kiln wraps PrusaSlicer and OrcaSlicer for headless slicing. Auto-detects installed slicers on PATH, macOS app bundles, or via `KILN_SLICER_PATH`.

```bash
# Slice an STL to G-code
kiln slice model.stl

# Slice and immediately print
kiln slice model.stl --print-after

# Supported formats: STL, 3MF, STEP, OBJ, AMF
```

## Webcam Snapshots

Capture point-in-time images from printer webcams for monitoring and quality checks:

```bash
# Save snapshot to file
kiln snapshot --save photo.jpg

# Get base64-encoded snapshot (for agents)
kiln snapshot --json
```

Supported on OctoPrint, Moonraker, and Prusa Connect backends. Agents use the `printer_snapshot` MCP tool.

## Fulfillment Services

Outsource prints to external manufacturing services when local printers lack the material, capacity, or technology:

```bash
# List available materials (FDM, SLA, SLS, MJF, etc.)
kiln order materials

# Get a quote for a model
kiln order quote model.stl --material pla-white --quantity 2

# Place the order
kiln order place q-abc123 --shipping std

# Track order status
kiln order status o-def456
```

Configure your fulfillment provider:

```bash
# Option 1: Auto-detect from API key (Craftcloud is default)
export KILN_CRAFTCLOUD_API_KEY=your_key

# Option 2: Shapeways (OAuth2)
export KILN_SHAPEWAYS_CLIENT_ID=your_id
export KILN_SHAPEWAYS_CLIENT_SECRET=your_secret

# Option 3: Sculpteo
export KILN_SCULPTEO_API_KEY=your_key

# Optional: explicitly select a provider
export KILN_FULFILLMENT_PROVIDER=shapeways  # or craftcloud, sculpteo
```

Agents use `fulfillment_quote` and `fulfillment_order` MCP tools for the same workflow.

## Development

```bash
# Install both packages in dev mode
pip install -e "./kiln[dev]"
pip install -e "./octoprint-cli[dev]"

# Run tests (2231+ total)
cd kiln && python3 -m pytest tests/ -v        # 1992 tests
cd ../octoprint-cli && python3 -m pytest tests/ -v  # 239 tests
```

## Revenue Model

All local printing is **free forever** — status checks, file management, slicing, fleet control, and printing to your own printers costs nothing.

Kiln charges a **5% platform fee** on orders placed through external manufacturing services (`kiln order` / fulfillment MCP tools), with:

- First 5 outsourced orders per month **free**
- $0.25 minimum / $50 maximum per-order cap

The fee is shown transparently in every quote before you commit.

## Safety

Kiln is safety-first infrastructure for controlling physical machines:

- **Pre-flight checks** validate printer state, temperatures, and files before every print
- **G-code validation** blocks dangerous commands (firmware reset, unsafe temperatures)
- **Temperature limits** enforce safe maximums (300C hotend, 130C bed)
- **Confirmation required** for destructive operations (cancel, raw G-code)
- **Optional authentication** with scope-based API keys for multi-user setups
- **Structured errors** ensure agents always know when something fails

## License

MIT
