# Kiln — Open Tasks

Prioritized backlog of features and improvements.

## High Priority

### Additional Fulfillment Providers
Craftcloud is integrated. Evaluate and add more providers using the `FulfillmentProvider` ABC:
- **Xometry** — Professional manufacturing, CNC + 3D printing. Enterprise-focused API.
- **Sculpteo** — On-demand 3D printing with good material selection.
- **Shapeways** — Consumer/prosumer 3D printing marketplace.
- **Polar Cloud** — Open network of printers (educational/maker spaces). Closer to 3DOS model.

## Medium Priority

### OTA Firmware Updates
Moonraker supports firmware updates natively via its API. Could expose as `kiln firmware-update` CLI command and `update_firmware` MCP tool. OctoPrint has a plugin for this too. Safety-critical — needs confirmation gates.

### Webcam Streaming / Live View
Beyond snapshots (now implemented), full MJPEG stream proxy for real-time monitoring dashboards. Lower priority than snapshots since agents primarily need point-in-time checks.

### CLI Test Coverage for Advanced Features
Only core commands have CLI-layer tests (~3.7% coverage). Add Click CLI tests for:
- `slice`, `snapshot`, `wait`, `history`, `cost`, `compare-cost`
- `material` subcommands: `set`, `show`, `spools`, `add-spool`
- `level` subcommands: `trigger`, `status`, `set-prints`, `set-hours`
- `stream`, `sync` (status/now/configure), `plugins` (list/info)
- `order` subcommands: `materials`, `quote`, `place`, `status`, `cancel`
- `billing` subcommands: `setup`, `status`, `history`
The underlying library code IS tested (1,726+ tests), but regressions in Click argument parsing, flag handling, or output formatting won't be caught without CLI-layer tests.

### End-to-End Integration Test
No test covers the full agent workflow: discover → configure → slice → upload → print → wait → history. Each piece works in isolation but the chain is untested. Add at least one integration test with a mock printer that exercises the complete pipeline.

### Bambu Webcam Support
`get_snapshot()` and `get_stream_url()` are not implemented for the Bambu adapter (returns None). Bambu printers do have RTSP streams — investigate adding support via the MQTT push or direct RTSP URL extraction.

### Print Failure Analysis Tool
Add `analyze_print_failure(job_id)` — examine job history, printer logs, and temperature data to suggest root causes and parameter changes for failed prints.

### Text-to-Model Generation
Integrate with AI-powered CAD/3D model generation services (e.g. OpenSCAD scripting, Meshy, Tripo3D, or similar APIs) to let agents generate 3D models from text descriptions. This would close the gap between "idea" and "model file" in the agent workflow — currently agents must find existing models on marketplaces. High-value feature for monetization (generation credits, premium models) but significant lift: requires evaluating generation APIs, handling async generation jobs, mesh quality validation, and printability checks.

### Post-Print Quality Validation
Integrate webcam snapshot analysis (or external dimensional measurement) to validate print quality after completion. Could use vision models to detect obvious defects (spaghetti, layer shift, warping) and flag for human review.

## Low Priority / Future Ideas

- **Resumable downloads** — Large STL downloads don't checkpoint; add resume support for interrupted transfers
- **OctoPrint bed mesh** — `get_bed_mesh()` not implemented for OctoPrint adapter (Moonraker has it)
- **Bambu "cancelling" state** — No explicit mapping for the cancelling state in the Bambu adapter state map
