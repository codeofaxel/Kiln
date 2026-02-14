# Kiln

Agentic infrastructure for physical fabrication. Kiln enables AI agents to design, slice, queue, monitor, and fulfill 3D print jobs through a unified MCP (Model Context Protocol) server and CLI.

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
# From PyPI
pip install kiln3d

# From source (development)
pip install -e ".[dev]"
```

### 2. Configure

```bash
# Option A: Environment variables
export KILN_PRINTER_TYPE=octoprint
export KILN_PRINTER_HOST=http://octopi.local
export KILN_PRINTER_API_KEY=your_api_key

# Option B: CLI auth (saves to ~/.kiln/config.yaml)
kiln auth --name my-printer --host http://octopi.local --type octoprint --api-key YOUR_KEY
```

### 3. Run

```bash
# CLI
kiln status
kiln print model.gcode

# MCP Server
kiln serve
```

### 4. Connect from Claude Desktop

Add to `~/.config/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "kiln": {
      "command": "kiln",
      "args": ["serve"],
      "env": {
        "KILN_PRINTER_TYPE": "octoprint",
        "KILN_PRINTER_HOST": "http://octopi.local",
        "KILN_PRINTER_API_KEY": "your_api_key"
      }
    }
  }
}
```

## Supported Printers

| Backend | Status | Printers |
|---------|--------|----------|
| **OctoPrint** | Stable | Any OctoPrint-connected printer (Prusa, Ender, custom) |
| **Moonraker** | Stable | Klipper-based printers (Voron, Ratrig, etc.) |
| **Bambu Lab** | Stable | X1C, P1S, A1 (via LAN MQTT + FTPS) |
| **Prusa Link** | Stable | MK4, XL, Mini+ (local REST API — type: `prusaconnect`) |

## Features

- **79+ MCP tools** for full printer control, fleet management, slicing, model generation, and fulfillment
- **25+ CLI commands** with `--json` output for agent consumption
- **Multi-printer fleet** management with job queue and background scheduler
- **Model marketplaces** — search/download from MyMiniFactory, Cults3D (Thingiverse deprecated — acquired by MMF, Feb 2026)
- **Slicer integration** — PrusaSlicer and OrcaSlicer with auto-detection
- **Text-to-model generation** — Meshy AI and OpenSCAD providers
- **Fulfillment services** — outsource to Craftcloud or Sculpteo
- **Safety first** — pre-flight checks, G-code validation, temperature limits, optional auth
- **Webhooks** — HMAC-signed event notifications for job lifecycle
- **OTA firmware updates** — check, update, and rollback printer firmware

## Architecture

```
AI Agent (Claude, GPT, custom)
    |
    | CLI or MCP (Model Context Protocol)
    v
+--------------------+
|        Kiln        |
+--------------------+
  |       |        |        |
  v       v        v        v
OctoPrint Moonraker Bambu  PrusaConnect
  |       |        |        |
  v       v        v        v
Prusa   Voron    X1C/P1S  MK4/XL
```

## Development

```bash
pip install -e ".[dev]"
cd kiln && python -m pytest tests/ -v  # 5,004 tests
```

## License

MIT
