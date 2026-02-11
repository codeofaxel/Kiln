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

### Text-to-Model Generation
Integrate with AI-powered CAD/3D model generation services (e.g. OpenSCAD scripting, Meshy, Tripo3D, or similar APIs) to let agents generate 3D models from text descriptions. This would close the gap between "idea" and "model file" in the agent workflow — currently agents must find existing models on marketplaces. High-value feature for monetization (generation credits, premium models) but significant lift: requires evaluating generation APIs, handling async generation jobs, mesh quality validation, and printability checks.


## Low Priority / Future Ideas
