# Kiln

<!-- mcp-name: io.github.codeofaxel/kiln -->

**Describe it or draw it. Kiln makes it real.**

Kiln turns a conversation with an AI agent into a printed object. One install, any printer, start to finish — design, slice, queue, monitor, and fulfill 3D print jobs through a unified MCP (Model Context Protocol) server and CLI.

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

- **<!-- KILN_MCP_CAPABILITY_COUNT:OLD --> 801 MCP capabilities** for design intelligence, model generation, printability analysis, slicing, printing, monitoring, failure recovery, fleet management, and fulfillment
- **<!-- KILN_CLI_COUNT:OLD --> 221 CLI commands** with `--json` output for agent consumption
- **Multi-printer fleet** management with job queue and background scheduler
- **Model marketplaces** — search/download from MyMiniFactory, Cults3D (Thingiverse deprecated — acquired by MMF, Feb 2026)
- **Slicer integration** — PrusaSlicer and OrcaSlicer with auto-detection
- **Text-to-model generation** — Meshy AI, Tripo3D, Stability AI, OpenSCAD with auto-discovery registry
- **Printability analysis** — overhang detection, thin wall analysis, auto-orientation, support estimation, failure prediction, quality scorecard
- **Mesh operations** — mirror, hollow, center, scale-to-fit, merge, split, simplify, repair, non-manifold analysis — all pure Python, no external mesh libraries
- **Print DNA** — model fingerprinting, crowd-sourced print settings, intelligent settings prediction
- **Marketplace publish** — one-click publish to Thingiverse/MyMiniFactory with print "birth certificate"
- **Revenue tracking** — per-model creator analytics, 2.5% platform fee on Kiln-published models
- **Print-as-a-Service** — local vs fulfillment cost comparison, order lifecycle management
- **Failure recovery** — 9 failure types classified, automated recovery planning
- **Multi-printer splitting** — round-robin and assembly-based job distribution across fleets
- **Generation feedback loop** — failed print → improved prompt with printability constraints
- **Smart material routing** — intent-based material recommendations (45 brand-specific filament profiles across 11 material families, printer capability aware)
- **Decoration engine** — QR codes, photos, logos, text, SVGs, and brand assets embossed or debossed onto any model with one command (patent pending)
- **Procedural textures** — tiger stripe, marble, digital camo, snakeskin, wood grain, carbon fiber, honeycomb, lava — applied directly to any model for multicolor FDM printing (Pro)
- **Product templates** — one-command generation of coasters, keychains, ornaments, pet tags, pet bowls, bookmarks, magnets, jewelry trays, wall plaques, and ashtrays with built-in decoration support (Pro)
- **Mid-print modification** — decorate, add features, or swap material on a live print with atomic dual-artifact revert (patent pending, Pro)
- **Resume interrupted prints** — resume from the exact layer after cancel on any supported FDM printer, no wasted filament (patent pending, Pro)
- **Design provenance** — version tracking, regression detection, outcome-correlated genealogy across design iterations (patent pending, Pro)
- **Cross-printer learning** — fleet-wide outcome learning, predictive settings, closed-loop AI generation feedback where failed prints auto-improve future generations (patent pending)
- **AMS auto-routing** — automatic Bambu AMS tray detection, color/type identification, and slot routing when starting prints
- **Community print registry** — opt-in crowd-sourced settings and success rates
- **Fulfillment services** — outsource to Craftcloud through the hosted proxy, or use direct mode with your own provider credentials
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
  |       |        |        |       |       |
  v       v        v        v       v       v
OctoPrint Moonraker Bambu  PrusaLink Elegoo Serial
  |       |        |        |       |       |
  v       v        v        v       v       v
Prusa   Voron    X1C/P1S  MK4/XL  Saturn  Ender
```

## Development

```bash
pip install -e ".[dev]"
cd kiln && python3 -m pytest tests/ -v
```

## License

AGPL-3.0-or-later. Commercial licensing is available for companies that need proprietary use.
