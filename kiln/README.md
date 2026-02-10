# Kiln

Agentic infrastructure for physical fabrication. Kiln enables AI agents to design, slice, queue, monitor, and fulfill 3D print jobs through a unified MCP (Model Context Protocol) server.

## What Kiln Does

An agent can go from intent to physical object with zero human intervention:

```
Agent: "Print this sensor mount"
  → Kiln validates the file
  → Checks printer readiness
  → Uploads G-code
  → Starts print
  → Monitors progress
  → Reports completion
```

## Quick Start

### 1. Install

```bash
cd kiln
pip install -e ".[dev]"
```

### 2. Configure

```bash
export KILN_PRINTER_TYPE=octoprint
export KILN_PRINTER_HOST=http://octopi.local
export KILN_PRINTER_API_KEY=your_api_key
```

### 3. Run the MCP Server

```bash
python -m kiln
```

### 4. Connect from Claude Desktop

Add to your Claude Desktop MCP config (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "kiln": {
      "command": "python",
      "args": ["-m", "kiln"],
      "env": {
        "KILN_PRINTER_TYPE": "octoprint",
        "KILN_PRINTER_HOST": "http://octopi.local",
        "KILN_PRINTER_API_KEY": "your_api_key"
      }
    }
  }
}
```

## MCP Tools

| Tool | Description |
|------|-------------|
| `printer_status` | Get printer state, temperatures, and job progress |
| `printer_files` | List available G-code files |
| `upload_file` | Upload a G-code file to the printer |
| `start_print` | Start printing a file |
| `cancel_print` | Cancel current print |
| `pause_print` | Pause current print |
| `resume_print` | Resume paused print |
| `set_temperature` | Set hotend and/or bed temperature |
| `preflight_check` | Run safety checks before printing |
| `send_gcode` | Send raw G-code commands |

## Architecture

```
┌─────────────────────────────────────────┐
│           AI Agent (Claude, etc.)       │
│              via MCP Protocol           │
└─────────────────┬───────────────────────┘
                  │
┌─────────────────▼───────────────────────┐
│            Kiln MCP Server              │
│  ┌──────────────────────────────────┐   │
│  │     Printer Abstraction Layer    │   │
│  ├──────────┬───────────┬───────────┤   │
│  │ OctoPrint│ Moonraker │  Bambu    │   │
│  │ Adapter  │ (planned) │ (planned) │   │
│  └──────────┴───────────┴───────────┘   │
│  ┌──────────────────────────────────┐   │
│  │     3DOS Gateway (planned)       │   │
│  └──────────────────────────────────┘   │
│  ┌──────────────────────────────────┐   │
│  │   Payment Layer (planned)        │   │
│  └──────────────────────────────────┘   │
└─────────────────────────────────────────┘
```

## Supported Printers

### Now
- **OctoPrint** - Full support via REST API

### Planned
- Klipper / Moonraker
- Bambu Lab (X1C, P1S, A1)
- Prusa Connect
- Duet / RepRap

## Project Structure

```
kiln/
├── src/kiln/
│   ├── server.py           # MCP server (main entry point)
│   ├── printers/
│   │   ├── base.py         # Abstract printer interface
│   │   └── octoprint.py    # OctoPrint adapter
│   └── __main__.py         # python -m kiln entry point
├── pyproject.toml
└── README.md
```

## Roadmap

- [x] OctoPrint adapter
- [x] MCP server with core printing tools
- [ ] Klipper/Moonraker adapter
- [ ] 3DOS distributed manufacturing gateway
- [ ] Payment abstraction (fiat + crypto)
- [ ] Job queue and fleet orchestration
- [ ] Print profile marketplace

## License

MIT
