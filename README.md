# Kiln

Agentic infrastructure for physical fabrication via 3D printing.

Kiln lets AI agents design, queue, and execute physical manufacturing jobs on real 3D printers with zero human intervention. It exposes printer control through the **Model Context Protocol (MCP)**, making any MCP-compatible agent (Claude, GPT, custom) a first-class operator of your print farm.

## Architecture

```
AI Agent (Claude, GPT, custom)
    |
    | MCP (Model Context Protocol)
    v
+-------------------+
|   Kiln MCP Server  |  <-- 10+ tools for printer control
+-------------------+
    |
    | PrinterAdapter abstraction
    v
+------------+  +------------+  +--------+
| OctoPrint  |  | Moonraker  |  | Bambu  |  ...
+------------+  +------------+  +--------+
    |                |              |
    v                v              v
  Prusa i3        Voron          Bambu X1C
```

## Packages

This monorepo contains two packages:

| Package | Description | Entry Point |
|---------|-------------|-------------|
| **kiln** | MCP server exposing printer control as agent tools | `kiln` or `python -m kiln` |
| **octoprint-cli** | Agent-friendly CLI for OctoPrint printer management | `octoprint-cli` |

## Quick Start

### Kiln MCP Server

```bash
# Install
pip install -e ./kiln

# Configure
export KILN_PRINTER_HOST=http://octopi.local
export KILN_PRINTER_API_KEY=your_api_key
export KILN_PRINTER_TYPE=octoprint  # or "moonraker"

# Run
kiln
```

#### Claude Desktop Integration

Add to `~/.config/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "kiln": {
      "command": "python",
      "args": ["-m", "kiln"],
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

The Kiln MCP server exposes these tools to agents:

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

## Supported Printers

| Backend | Status | Printers |
|---------|--------|----------|
| **OctoPrint** | Stable | Any OctoPrint-connected printer (Prusa, Ender, custom) |
| **Moonraker** | Beta | Klipper-based printers (Voron, Ratrig, etc.) |
| **Bambu** | Planned | Bambu Lab X1C, P1S, A1 |
| **Prusa Connect** | Planned | Prusa MK4, XL, Mini |

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
