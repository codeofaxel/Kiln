# Kiln Compatibility Matrix

Tested configurations and supported versions for the Kiln MCP server and CLI.

## Python

| Requirement | Version |
|---|---|
| Minimum | 3.10 |
| Tested | 3.10, 3.11, 3.12, 3.13 |

Source: `pyproject.toml` (`requires-python = ">=3.10"`, classifiers list 3.10 through 3.13).

## Printer Firmware / Backends

### OctoPrint

| Item | Version |
|---|---|
| Tested with | 1.9+ |
| API version | OctoPrint REST API (no explicit version check in code) |
| Authentication | `X-Api-Key` header |
| Protocol | HTTP/HTTPS |
| Features | Full (state, temps, upload, print control, G-code, webcam, firmware updates via Software Update plugin, bed mesh via Bed Level Visualizer plugin) |

Notes: The adapter uses standard OctoPrint REST endpoints (`/api/printer`, `/api/job`, `/api/files`, `/api/printer/command`). These endpoints have been stable since OctoPrint 1.4+. Tested with 1.9+.

### Moonraker / Klipper

| Item | Version |
|---|---|
| Tested with | Moonraker 0.8+, Klipper (current) |
| Authentication | Optional `X-Api-Key` header (trusted-client or API-key setups) |
| Protocol | HTTP/HTTPS |
| Features | Full (state, temps, upload, print control, G-code, webcam via `/server/webcams/list`, firmware updates via `/machine/update/status`, bed mesh, rollback) |

Notes: The adapter queries `/printer/info` for klippy state and `/printer/objects/query` for temperatures and print stats. It supports the full Moonraker update manager API including component-level updates and rollbacks. Webcam discovery uses the Moonraker webcam API (`/server/webcams/list`). Compatible with any Klipper installation fronted by Moonraker.

### Bambu Lab

| Item | Details |
|---|---|
| Protocol | MQTT (port 8883, TLS) + FTPS (port 990, implicit TLS) |
| Authentication | LAN Access Code (printer LCD > Settings > Network) |
| MQTT client library | paho-mqtt 2.0+ (MQTTv3.1.1) |
| Requires | LAN Mode enabled on printer |

**Supported models** (via MQTT/FTPS local-LAN protocol):

| Model | Notes |
|---|---|
| Bambu Lab X1 Carbon | Full support, enclosed, AMS, LiDAR |
| Bambu Lab P1S | Full support, enclosed |
| Bambu Lab P1P | Full support, open-frame |
| Bambu Lab A1 | Full support, sends uppercase state values |
| Bambu Lab A1 Mini | Full support, sends uppercase state values |

Notes: The adapter communicates exclusively over local LAN -- it does not use the Bambu Cloud API. Printer must have LAN Mode enabled. File management uses implicit FTPS with TLS session reuse. The A1 and A1 Mini send uppercase state values (e.g. `RUNNING` instead of `running`), which the adapter normalizes. Webcam access attempts an HTTP snapshot endpoint and also provides the RTSP stream URL (`rtsps://<host>:322/streaming/live/1`).

### Prusa Link (PrusaConnect)

| Item | Version |
|---|---|
| Tested with | Prusa Link 2.0+ (firmware on MK4, MK3.9, XL, Mini+) |
| API version | Prusa Link HTTP API v1 (`/api/v1/`) |
| Authentication | `X-Api-Key` header |
| Protocol | HTTP over LAN (Ethernet or Wi-Fi) |
| Features | Partial (state, upload, print start/pause/resume/cancel, file listing, webcam snapshot) |

**Limitations:**
- No direct temperature control endpoint (temperatures managed through G-code in print files)
- No raw G-code sending endpoint
- Emergency stop falls back to job cancellation (no M112 support)
- Supports `.bgcode` (Prusa binary G-code) in addition to standard `.gcode`

## Slicers

| Slicer | Minimum Version | Auto-Detection |
|---|---|---|
| PrusaSlicer | 2.6+ | `prusa-slicer`, `PrusaSlicer`, `prusaslicer` on PATH; macOS app bundle paths |
| OrcaSlicer | 2.0+ | `orca-slicer`, `OrcaSlicer`, `orcaslicer` on PATH; macOS app bundle path |

Notes: Both slicers are invoked via their `--export-gcode` CLI flag for headless slicing. Version detection uses `--version`. The `KILN_SLICER_PATH` environment variable can override auto-detection. Supported input formats: `.stl`, `.3mf`, `.step`, `.stp`, `.obj`, `.amf`.

## Supported Printer Models (Safety Profiles)

Kiln ships 26 curated safety profiles with per-printer temperature limits, feedrate limits, volumetric flow limits, and build volumes:

| Profile ID | Display Name | Max Hotend | Max Bed | Build Volume |
|---|---|---|---|---|
| `default` | Generic / Unknown Printer | 300 C | 130 C | -- |
| `ender3` | Creality Ender 3 / Ender 3 Pro / Ender 3 V2 | 260 C | 110 C | 220x220x250 |
| `ender3_s1` | Creality Ender 3 S1 / S1 Pro | 300 C | 110 C | 220x220x270 |
| `ender5` | Creality Ender 5 / Ender 5 Plus | 260 C | 110 C | 350x350x400 |
| `cr10` | Creality CR-10 / CR-10S | 260 C | 110 C | 300x300x400 |
| `k1` | Creality K1 / K1 Max | 300 C | 120 C | 300x300x300 |
| `prusa_mk3s` | Prusa i3 MK3S / MK3S+ | 300 C | 120 C | 250x210x210 |
| `prusa_mk4` | Prusa MK4 / MK4S | 300 C | 120 C | 250x210x220 |
| `prusa_xl` | Prusa XL | 300 C | 120 C | 360x360x360 |
| `prusa_mini` | Prusa Mini / Mini+ | 280 C | 100 C | 180x180x180 |
| `bambu_x1c` | Bambu Lab X1 Carbon | 300 C | 120 C | 256x256x256 |
| `bambu_p1s` | Bambu Lab P1S | 300 C | 120 C | 256x256x256 |
| `bambu_p1p` | Bambu Lab P1P | 300 C | 110 C | 256x256x256 |
| `bambu_a1_mini` | Bambu Lab A1 Mini | 300 C | 80 C | 180x180x180 |
| `bambu_a1` | Bambu Lab A1 | 300 C | 100 C | 256x256x256 |
| `voron_0` | Voron 0 / 0.2 | 300 C | 120 C | 120x120x120 |
| `voron_2` | Voron 2.4 / Trident | 300 C | 120 C | 350x350x350 |
| `ratrig_vcore3` | Rat Rig V-Core 3 / V-Core 3.1 | 300 C | 120 C | 300x300x300 |
| `elegoo_neptune3` | Elegoo Neptune 3 / 3 Pro / 3 Plus | 260 C | 110 C | 220x220x280 |
| `elegoo_neptune4` | Elegoo Neptune 4 / 4 Pro / 4 Max | 300 C | 110 C | 235x235x265 |
| `anker_m5` | AnkerMake M5 / M5C | 300 C | 110 C | 235x235x250 |
| `artillery_sw_x3` | Artillery Sidewinder X3 Plus / X3 Pro | 300 C | 110 C | 300x300x400 |
| `sovol_sv06` | Sovol SV06 / SV06 Plus | 300 C | 110 C | 220x220x250 |
| `sovol_sv07` | Sovol SV07 / SV07 Plus | 300 C | 110 C | 220x220x250 |
| `flashforge_adventurer5m` | FlashForge Adventurer 5M / 5M Pro | 280 C | 110 C | 220x220x220 |
| `qidi_x_plus3` | QIDI X-Plus 3 / X-Max 3 | 350 C | 120 C | 280x280x270 |
| `klipper_generic` | Generic Klipper Printer (Moonraker) | 300 C | 120 C | 235x235x250 |

## MCP Clients

| Client | Status |
|---|---|
| Claude Desktop | Tested |
| Other MCP clients | Should work (standard MCP protocol) -- not yet verified |

## Operating Systems

| OS | Status | Notes |
|---|---|---|
| macOS | Supported | Primary development platform. Slicer auto-detection includes macOS app bundle paths. |
| Linux (x86_64) | Supported | Includes WSL. Slicer auto-detection via PATH. |
| Windows | Partial | Python package installs and runs. Slicer auto-detection via PATH only (no registry lookup). Config file permission checks (`chmod 600`) are skipped on Windows. |

## Key Dependencies

| Package | Minimum Version | Purpose |
|---|---|---|
| `mcp` | 1.0 | MCP protocol implementation |
| `requests` | 2.25 | HTTP client for OctoPrint, Moonraker, Prusa Link, marketplaces |
| `paho-mqtt` | 2.0 | MQTT client for Bambu Lab printers |
| `pyyaml` | 5.4 | Config file parsing |
| `pydantic` | 2.0 | Data validation |
| `click` | 8.0 | CLI framework |
| `zeroconf` | 0.80 | mDNS printer discovery |
| `rich` | 12.0 | Terminal output formatting |

### Optional Dependencies

| Extra | Packages | Purpose |
|---|---|---|
| `rest` | `fastapi>=0.100`, `uvicorn>=0.25` | REST API wrapper for MCP tools |
| `stripe` / `payments` | `stripe>=5.0` | Payment processing integration |
| `dev` | `pytest>=7.0`, `pytest-mock>=3.6`, `responses>=0.20` | Development and testing |
