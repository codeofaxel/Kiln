# Kiln

AI agent control of 3D printers — <!-- KILN_MCP_CAPABILITY_COUNT:OLD --> 835 MCP capabilities + <!-- KILN_CLI_COUNT:OLD --> 223 CLI commands.

## What it does

Kiln gives AI agents (Claude, GPT, OpenClaw, custom) safe, direct control of 3D printers through the Model Context Protocol. One install and your agent can search model marketplaces, generate designs from text/sketches, slice STL/3MF files, queue prints, monitor progress with camera snapshots, manage multi-printer fleets, and estimate costs — all from a single conversation.

## Supported printers

- **Bambu Lab** — A1, A1 Mini, P1S, P1P, X1C (MQTT + FTP)
- **OctoPrint** — Any printer running OctoPrint
- **Moonraker/Klipper** — Voron, Ender, etc.
- **Creality** — K1/K2/Hi/Ender V3 KE-class printers when local Moonraker is reachable; older Marlin models via OctoPrint or serial
- **Prusa Link** — MK4, Mini, XL
- **Elegoo** — Saturn, Mars, Centauri Carbon (SDCP V2 + V3)

## Install

```bash
pip install kiln3d
```

Or with pipx:

```bash
pipx install kiln3d
```

## Key capabilities

- **<!-- KILN_MCP_TOOL_COUNT:OLD --> 828 MCP tools** for full printer lifecycle control
- **<!-- KILN_CLI_COUNT:OLD --> 223 CLI commands** for human and agent use
- **Model search** across MyMiniFactory, Cults3D, Thangs, GrabCAD, Etsy
- **Text/sketch-to-3D generation** with multiple provider backends
- **Auto-slicing** via PrusaSlicer or OrcaSlicer
- **Fleet management** with multi-site, multi-printer support
- **Safety enforcement** — pre-flight checks, G-code validation, temp limits, confirmation gates
- **Print monitoring** with camera snapshots and failure detection
- **Cost estimation** from G-code and Bambu 3MF metadata
- **Material intelligence** — catalog, compatibility, substitution recommendations

## Links

- [GitHub](https://github.com/codeofaxel/Kiln)
- [PyPI](https://pypi.org/project/kiln3d/)
- [Website](https://kiln3d.com)
- [Docs](https://kiln3d.com/docs)
