# Changelog

All notable changes to Kiln are documented here.

## [0.1.0] - 2026-02-10

Initial release.

### Printer Adapters
- **OctoPrint** — Full REST adapter for OctoPrint-connected printers
- **Moonraker** — Full REST adapter for Klipper-based printers (Voron, Ratrig, etc.)
- **Bambu Lab** — MQTT adapter for X1C, P1S, A1 over LAN
- **Prusa Connect** — REST adapter for MK4, XL, Mini+ via Prusa Link

### MCP Server (79 tools)
- Printer control: status, files, upload, print, cancel, pause, resume, temperatures, G-code
- Fleet management: multi-printer registry, fleet status, idle printer detection
- Job queue: priority scheduling with background dispatch, auto-retry with exponential backoff
- Safety: preflight checks (auto-run before prints), G-code validation, temperature limits
- Slicer integration: PrusaSlicer/OrcaSlicer auto-detection, slice-and-print pipeline
- Model marketplaces: Thingiverse, MyMiniFactory, Cults3D search and download
- Fulfillment services: Craftcloud, Shapeways, Sculpteo (quote, order, track)
- Text-to-model generation: Meshy AI (cloud) and OpenSCAD (local)
- Mesh validation: STL/OBJ parsing, manifold check, dimension limits
- Cost estimation: filament usage analysis, local vs. fulfillment comparison
- Material tracking: spool inventory, per-printer loaded material, mismatch warnings
- Bed leveling: auto-level policies, mesh variance monitoring
- Firmware updates: OctoPrint and Moonraker OTA update/rollback
- Webcam: snapshot capture, MJPEG stream proxy
- Print monitoring: await completion, failure analysis, quality validation
- Infrastructure: webhooks (HMAC-signed), event bus, cloud sync, plugin system, billing

### CLI (47 commands)
- Printer discovery via mDNS and HTTP probing
- Full printer control matching MCP tool capabilities
- Config management via `~/.kiln/config.yaml`
- `--json` flag on every command for agent consumption
- Firmware update commands: `kiln firmware status|update|rollback`
- Model generation: `kiln generate`, `kiln generate-status`, `kiln generate-download`

### Safety
- Mandatory preflight checks before print jobs
- G-code safety validation (dangerous commands blocked)
- Temperature range enforcement per material
- Print-in-progress guards on destructive operations (firmware update, etc.)
- Config file permission validation

### Testing
- 2,093 tests (kiln) + 239 tests (octoprint-cli)
- Adapter, MCP tool, CLI, and integration test coverage
