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

## Low Priority / Future Ideas

- **Cloud sync** — Sync printer configs across machines via Supabase or similar
- **Print cost estimation** — Estimate filament usage and cost from G-code analysis
- **Automatic bed leveling triggers** — Detect when mesh needs re-probing
- **Multi-material support** — Track AMS/MMU filament slots for Bambu and Prusa
- **Plugin system** — Let users extend Kiln with custom adapters and tools
