# Kiln — Open Tasks

Prioritized backlog of features and improvements.

## High Priority

### Prusa Connect Adapter
Third printer backend. Fills out the "Planned" row in the README. Prusa Connect exposes a REST API for MK4, XL, Mini. Would follow the same PrinterAdapter pattern as OctoPrint/Moonraker/Bambu.

### Craftcloud Integration
Order fulfillment via Craftcloud (All3DP). Aggregates 100+ manufacturing partners. Use case: "I don't have the right printer/material for this — route to a service." Would add a `kiln order` CLI command and `order_print` MCP tool that submits STL + material + finish to Craftcloud's API and returns a quote/tracking link.

### Other Fulfillment Services to Evaluate
- **Xometry** — Professional manufacturing, CNC + 3D printing. Enterprise-focused API.
- **Sculpteo** — On-demand 3D printing with good material selection.
- **Shapeways** — Consumer/prosumer 3D printing marketplace.
- **Polar Cloud** — Open network of printers (educational/maker spaces). Closer to 3DOS model.

## Medium Priority

### OTA Firmware Updates
Moonraker supports firmware updates natively via its API. Could expose as `kiln firmware-update` CLI command and `update_firmware` MCP tool. OctoPrint has a plugin for this too. Safety-critical — needs confirmation gates.

### Webcam Streaming / Live View
Beyond snapshots (now implemented), full MJPEG stream proxy for real-time monitoring dashboards. Lower priority than snapshots since agents primarily need point-in-time checks.

## Low Priority / Future Ideas

- **Cloud sync** — Sync printer configs across machines via Supabase or similar
- **Print cost estimation** — Estimate filament usage and cost from G-code analysis
- **Automatic bed leveling triggers** — Detect when mesh needs re-probing
- **Multi-material support** — Track AMS/MMU filament slots for Bambu and Prusa
- **Plugin system** — Let users extend Kiln with custom adapters and tools
