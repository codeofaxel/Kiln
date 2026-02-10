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
    |
    | PrinterAdapter abstraction
    v
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
| **kiln** | CLI + MCP server for multi-printer control | `kiln` or `python -m kiln` |
| **octoprint-cli** | Standalone CLI for OctoPrint printer management | `octoprint-cli` |

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

# Monitor a running print
kiln print --status

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
kiln print <file> [--json]                 # Start printing a file
kiln print --status [--json]               # Check print progress
kiln cancel [--json]                       # Cancel current print
kiln pause [--json]                        # Pause current print
kiln resume [--json]                       # Resume paused print
kiln temp [--tool N] [--bed N] [--json]    # Get/set temperatures
kiln gcode <cmds>... [--json]              # Send raw G-code
kiln printers [--json]                     # List saved printers
kiln use <name>                            # Switch active printer
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
| `search_models` | Search Thingiverse for 3D models |
| `model_details` | Get details for a Thingiverse model |
| `model_files` | List files for a Thingiverse model |
| `download_model` | Download a model file from Thingiverse |
| `browse_models` | Browse popular/newest/featured models |
| `list_model_categories` | List Thingiverse categories |

## Supported Printers

| Backend | Status | Printers |
|---------|--------|----------|
| **OctoPrint** | Stable | Any OctoPrint-connected printer (Prusa, Ender, custom) |
| **Moonraker** | Stable | Klipper-based printers (Voron, Ratrig, etc.) |
| **Bambu** | Stable | Bambu Lab X1C, P1S, A1 (via LAN MQTT) |
| **Prusa Connect** | Planned | Prusa MK4, XL, Mini |

## Thingiverse Integration

Kiln includes a built-in Thingiverse client for discovering and downloading 3D models. Set your API token to enable:

```bash
export KILN_THINGIVERSE_TOKEN=your_token
```

Agents can search for models, inspect details, and download files directly to the printer — enabling a full design-to-print workflow without human intervention.

## Development

```bash
# Install both packages in dev mode
pip install -e "./kiln[dev]"
pip install -e "./octoprint-cli[dev]"

# Run tests
cd kiln && python -m pytest tests/ -v
cd ../octoprint-cli && python -m pytest tests/ -v
```

## Safety

Kiln is safety-first infrastructure for controlling physical machines:

- **Pre-flight checks** validate printer state, temperatures, and files before every print
- **G-code validation** blocks dangerous commands (firmware reset, unsafe temperatures)
- **Temperature limits** enforce safe maximums (300C hotend, 130C bed)
- **Confirmation required** for destructive operations (cancel, raw G-code)
- **Structured errors** ensure agents always know when something fails

## License

MIT
