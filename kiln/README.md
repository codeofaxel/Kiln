# Kiln

<!-- mcp-name: io.github.codeofaxel/kiln -->

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

- **273 MCP tools** for full printer control, fleet management, slicing, model generation, marketplace publishing, and fulfillment
- **107 CLI commands** with `--json` output for agent consumption
- **Multi-printer fleet** management with job queue and background scheduler
- **Model marketplaces** — search/download from MyMiniFactory, Cults3D (Thingiverse deprecated — acquired by MMF, Feb 2026)
- **Slicer integration** — PrusaSlicer and OrcaSlicer with auto-detection
- **Text-to-model generation** — Meshy AI, Tripo3D, Stability AI, OpenSCAD with auto-discovery registry
- **Printability analysis** — overhang detection, thin wall analysis, auto-orientation, support estimation
- **Print DNA** — model fingerprinting, crowd-sourced print settings, intelligent settings prediction
- **Marketplace publish** — one-click publish to Thingiverse/MyMiniFactory/Thangs with print "birth certificate"
- **Revenue tracking** — per-model creator analytics, 2.5% platform fee on Kiln-published models
- **Print-as-a-Service** — local vs fulfillment cost comparison, order lifecycle management
- **Failure recovery** — 9 failure types classified, automated recovery planning
- **Multi-printer splitting** — round-robin and assembly-based job distribution across fleets
- **Generation feedback loop** — failed print → improved prompt with printability constraints
- **Smart material routing** — intent-based material recommendations (8 materials, printer capability aware)
- **Community print registry** — opt-in crowd-sourced settings ("Waze for 3D printing")
- **Fulfillment services** — outsource to Craftcloud (150+ print services, no API key required)
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
cd kiln && python -m pytest tests/ -v  # 6,339 tests
```

## License

MIT
