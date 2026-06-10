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

For QIDI X-Plus 3 printers exposing Moonraker, configure Kiln with `type: moonraker` and `printer_model: qidi_x_plus3`. X-Max 3 is not claimed by this profile; use `klipper_generic` until a separate verified profile exists.

### Creality (Moonraker-backed)

| Item | Version |
|---|---|
| Tested path | Local Moonraker API when reachable on the printer LAN |
| Authentication | Optional `X-Api-Key` header for Moonraker auth setups |
| Protocol | HTTP/HTTPS via Moonraker |
| Features | Full Moonraker-backed support (state, temps, upload, print control, G-code, webcam, firmware/update surfaces exposed by Moonraker) |

**Supported path:** use `type: creality` for modern Creality FDM printers only when local Moonraker is reachable on the printer LAN. Kiln's Creality adapter is a Creality-branded Moonraker endpoint adapter; a Creality profile in the data files is not a promise that stock firmware exposes Moonraker by default. Older Marlin-based Creality printers remain supported through `serial` or `octoprint`.

Notes: Creality is a brand layer, not a single protocol. The `creality` adapter probes common Moonraker ports (`7125`, `80`, `4408`) and then delegates to the Moonraker adapter. If `/server/info` is not reachable, configure the printer through `serial`/`octoprint`, use Creality Print/Creality Cloud/local app paths outside Kiln, or enable/verify the printer's local Moonraker service where that model supports it.

`kiln doctor-creality` classifies common reachability misses as `network_or_port_unreachable`, `moonraker_auth_required`, `firmware_locked_or_wrong_port`, or `moonraker_backend_unavailable`. Its guidance is intentionally local-network-first: same Wi-Fi/LAN, correct printer IP, correct Moonraker port, API key if required, and a cautious stock-firmware note when the printer answers HTTP but does not expose Moonraker at `/server/info`. Pass `--model <profile_key>` for model-specific evidence hints.

**Local-control evidence matrix:**

| Model family | Profile key(s) | Stock firmware basis | Local Moonraker status | Port hints | Evidence level | Practical Kiln stance | Confidence | Gap / next action |
|---|---|---|---|---|---|---|---|---|
| Ender-3 / Pro / V2 | `ender3`, `ender3_v2` | Marlin | No stock Moonraker | None | Official/legacy Marlin class | Use `serial` or `octoprint` | 4 | Keep Creality examples away from these profiles |
| Ender-3 S1 / S1 Pro | `ender3_s1` | Marlin | No stock Moonraker | None | Official/legacy Marlin class | Use `serial` or `octoprint` | 4 | Keep Creality examples away from these profiles |
| Ender-5 / Ender-5 Plus | `ender5` | Marlin-era stock firmware | No stock Moonraker | None | Legacy Creality class | Use `serial` or `octoprint` | 4 | Use custom profile for externally hosted Klipper |
| CR-10 / CR-10S | `cr10` | Marlin-era stock firmware | No stock Moonraker | None | Legacy Creality class | Use `serial` or `octoprint` | 4 | Use custom profile for externally hosted Klipper |
| Ender-3 V3 SE | `ender3_v3_se` | Marlin-class stock firmware | No stock Moonraker found | None | Official support page; no local Moonraker evidence | Keep out of Creality/Moonraker examples | 3 | Hardware-check only if a user reports LAN API behavior |
| Ender-3 V3 | `ender3_v3` | CrealityOS/Klipper-class firmware | Local Fluidd officially documented; Moonraker API still must be verified | `4408` Fluidd; probe `/server/info` on Kiln ports | Official Creality Wiki | Strongest stock-local UI claim; save in Kiln only after Moonraker JSON responds | 4 | Hardware-check whether `/server/info` is reachable stock at `:4408` or via proxy |
| Ender-3 V3 Plus | `ender3_v3_plus` | CrealityOS/Klipper-class inferred | Unknown / likely related to V3 | Unknown, try `4408` only as a hint | Inferred from product family | Keep "Moonraker if reachable" | 1 | Do not inherit Ender-3 V3 claim until official docs or hardware confirm it |
| Ender-3 V3 KE | `ender3_v3_ke` | Official Creality Klipper repo | Yes after root/service install | `4408` Fluidd, `4409` Mainsail, `7125` likely when Moonraker service is reachable | Official root/Annex implication | Add root/service-enabling hint | 4 | Hardware-check stock default vs rooted/service-enabled firmware versions |
| CR-10 SE | `cr10_se` | Official Creality Klipper repo | Yes after root/service install | `4408` Fluidd, `4409` Mainsail, `7125` likely when Moonraker service is reachable | Official root/Annex implication | Add root/service-enabling hint | 4 | Hardware-check stock default vs rooted/service-enabled firmware versions |
| K1 | `k1` | Official K1-series Klipper repo | Yes after root/service install | `4408` Fluidd, `4409` Mainsail, `7125` likely when Moonraker service is reachable | Official root/Annex implication | Add root/service-enabling hint; CFS-C is retrofit only | 4 | Hardware-check stock default vs rooted/service-enabled firmware versions |
| K1 Max | `k1_max`, `creality_k1_max` | Official K1-series Klipper repo | Yes after root/service install | `4408` Fluidd, `4409` Mainsail, `7125` likely when Moonraker service is reachable | Official root/Annex implication | Stronger than generic "unknown", but not stock-default Moonraker | 4 | K1 Max can be stronger than "unknown", not stronger than service-enabled without hardware proof |
| K1C | `k1c`, `creality_k1c` | Official K1-series Klipper repo | Yes after root/service install | `4408` Fluidd, `4409` Mainsail, `7125` likely when Moonraker service is reachable | Official root/Annex implication | Add root/service-enabling hint; CFS-C is retrofit only | 4 | Hardware-check stock default vs rooted/service-enabled firmware versions |
| K1 SE | `k1_se`, `creality_k1_se` | CrealityOS/Klipper-class inferred | Unknown / likely after service enabling | Unknown | Product support plus CFS-C compatibility; no official Moonraker page found | Keep "Moonraker if reachable" | 1 | Find official K1 SE local-control docs or validate hardware |
| K2 | `k2`, `creality_k2` | Official K2-series Klipper repo | Likely; not officially documented as Moonraker | `4408` community/third-party hint | Official Klipper source plus community Fluidd/Moonraker reports | Use `4408` as a diagnostic hint, not a stock guarantee | 3 | Get official Moonraker docs or hardware `/server/info` result |
| K2 Pro | `k2_pro`, `creality_k2_pro` | Official K2-series Klipper repo | Likely; not officially documented as Moonraker | `4408` community/third-party hint | Official Klipper source plus community Fluidd/Moonraker reports | Keep "Moonraker if reachable" | 3 | Get official Moonraker docs or hardware `/server/info` result |
| K2 Plus | `k2_plus`, `creality_k2_plus` | Official K2-series Klipper repo | Likely; official LAN HTTP/WebSocket support, community Moonraker/Fluidd detail | `4408` community/third-party hint | Official LAN protocol note plus community Fluidd/Moonraker reports | Use `4408` as a diagnostic hint, then require `/server/info` | 3 | Verify whether official LAN API exposes Moonraker JSON on stock firmware |
| K2 SE | `k2_se`, `creality_k2_se` | CrealityOS/Klipper-class inferred | Unknown | Unknown | Official support page; no official Moonraker page found | Keep "Moonraker if reachable" | 1 | Find official K2 SE local-control docs or validate hardware |
| Creality Hi | `creality_hi` | Official Hi Klipper repo | Likely; not officially documented as Moonraker | `4408` community/third-party hint | Official Klipper source plus community Fluidd/Moonraker reports | Use `4408` as a diagnostic hint, then require `/server/info` | 3 | Get official Moonraker docs or hardware `/server/info` result |
| SPARKX i7 | `sparkx_i7` | CrealityOS/Klipper-class inferred | Unknown | Unknown | Official support confirms camera/CFS, not Moonraker | Keep "Moonraker if reachable" | 1 | Find official LAN/Moonraker docs or validate hardware |
| Ender-3 V4 | `ender3_v4` | CrealityOS/Klipper-class inferred | Unknown | Unknown | Official store confirms CFS/Wi-Fi, not Moonraker | Keep "Moonraker if reachable" | 1 | Find official LAN/Moonraker docs or validate hardware |
| Ender-5 Max | `ender5_max` | CrealityOS/Klipper-class inferred | Unknown | Unknown | Official support confirms CoreXY/Wi-Fi/optional camera, not Moonraker | Keep "Moonraker if reachable" | 1 | Find official LAN/Moonraker docs or validate hardware |

Official sources that matter most: Creality publishes Klipper source/release notes for [K1 series](https://github.com/CrealityOfficial/K1_Series_Klipper), [K2 series](https://github.com/CrealityOfficial/K2_Series_Klipper), [Creality Hi](https://github.com/CrealityOfficial/Hi_Klipper), [Ender-3 V3 KE](https://github.com/CrealityOfficial/Ender-3_V3_KE_Klipper), and [CR-10 SE](https://github.com/CrealityOfficial/CR-10SE_Klipper). The [K1 Annex](https://github.com/CrealityOfficial/K1_Series_Annex), [Ender-3 V3 KE Annex](https://github.com/CrealityOfficial/Ender-3_V3_KE_Annex), and [CR-10 SE Annex](https://github.com/CrealityOfficial/CR-10SE_Annex) document Fluidd/Mainsail/Moonraker installation paths, which is an official implication of a service-enabled Moonraker path, not proof that stock firmware exposes Moonraker by default. Creality's own [Ender-3 V3 Wiki page](https://wiki.creality.com/en/ender-series/ender-3-v3/troubleshooting/after-modifying-the-mesh-bed-via-fluidd-the-screen-freezes-or-goes-black/how-to-control-the-ender-3-v3-using-fluidd-over-a-local-area-network) documents local Fluidd browser control at `http://<printer-ip>:4408`. [K2 Plus official support](https://www.creality.com/support/creality-k2-plus-cfs-combo) documents LAN HTTP/WebSocket control and WebRTC video, but does not name Moonraker.

Community/third-party-only sources that are useful but not authoritative: [Creality Helper Script](https://guilouz.github.io/Creality-Helper-Script-Wiki/configurations/access-to-web-interface/) documents common Fluidd/Mainsail ports (`4408`/`4409`) and Moonraker/Nginx installation for K1 and Ender-3 V3 families. SimplyPrint's [K2 Plus](https://simplyprint.io/setup-guide/creality/k2-plus) and [Creality Hi](https://simplyprint.io/setup-guide/creality/hi) setup guides provide concrete `:4408`/Moonraker configuration behavior. Treat those as diagnostic hints until Creality documents the same behavior or Kiln verifies hardware directly.

**Officially confirmed local-control evidence:**

- Ender-3 V3: official Creality Wiki documents browser control through Fluidd at `http://<printer-ip>:4408`. This is a stock-local UI claim, not by itself proof that Kiln's required Moonraker `/server/info` API is exposed.
- K1/K1 Max/K1C, Ender-3 V3 KE, and CR-10 SE: official Creality Klipper repositories plus official Annex repositories document root, Fluidd, Mainsail, and Moonraker installation paths. This is official evidence for a root/service-enabled path, not proof of stock-default Moonraker exposure.
- K2/K2 Pro/K2 Plus and Creality Hi: official Creality Klipper repositories confirm the firmware basis. K2 Plus official support also documents LAN HTTP/WebSocket control and WebRTC video, but does not name Moonraker.

**Not officially findable yet:**

- No official Creality source found that says K1/K1 Max/K1C stock firmware exposes Moonraker locally by default.
- No official Creality source found that names Moonraker/Fluidd ports for K2/K2 Pro/K2 Plus, K2 SE, Creality Hi, SPARKX i7, Ender-3 V4, Ender-3 V3 Plus, Ender-5 Max, or K1 SE.
- K2/Hi `:4408` behavior is useful as a diagnostic hint, but it is community/third-party evidence until Creality documents it or Kiln validates hardware directly.

**Local connection test for K1 Max / K1 / Ender-3 V3 / K2 / Hi:**

1. Put the computer and printer on the same LAN. Avoid guest Wi-Fi or networks with client/device isolation.
2. Find the printer IP on the printer screen or router device list.
3. In a browser on the computer, open `http://<printer-ip>:7125/server/info`.
4. If that fails, try `http://<printer-ip>/server/info` and `http://<printer-ip>:4408/server/info`.
5. A JSON response with Moonraker/Klipper fields means Kiln can use the local path. A browser timeout usually means wrong IP, different network, router isolation, or local Moonraker disabled. HTTP 401/403 means Moonraker answered but needs an API key.
6. For Ender-3 V3 specifically, Creality documents Fluidd at `http://<printer-ip>:4408`; if Fluidd loads but `/server/info` does not return Moonraker JSON, Kiln should still treat the Moonraker API as unverified.

CLI setup:

```bash
kiln doctor-creality --host <printer-ip> --model k1_max
kiln auth --name k1max --type creality --host <printer-ip> --printer-model k1_max
```

**Creality camera and multicolor capability notes:**

| Model family | Camera | Multicolor |
|---|---|---|
| SPARKX i7 | Built-in AI monitoring camera | Built-in four-color CFS path |
| K1 Max | Built-in AI camera/LiDAR | Optional CFS-C retrofit, four colors max |
| K1C | Built-in AI monitoring camera | Optional CFS-C retrofit, four colors max |
| K1 | Optional AI camera accessory | Optional CFS-C retrofit, four colors max |
| K1 SE | No stock camera assumed | Optional CFS-C retrofit, four colors max |
| K2 / K2 Pro / K2 Plus | K2 has AI monitoring; K2 Pro/Plus add nozzle AI camera | CFS-capable, bundle/accessory-dependent, up to four CFS units / sixteen colors |
| K2 SE | Optional AI camera accessory | CFS-capable, bundle/accessory-dependent, up to four CFS units / sixteen colors |
| Creality Hi | AI camera on the Hi Combo path | CFS-capable; confirm base-vs-combo hardware before multicolor jobs |
| Ender-3 V4 | No stock camera assumed | CFS-compatible; CFS is sold separately / bundle-dependent, up to four CFS units / sixteen colors |
| Ender-3 V3 KE / Ender-5 Max | Optional AI camera accessory | No stock CFS path assumed |
| Older Ender/CR models | External host/webcam only | Manual M600 or third-party add-ons, no Creality CFS path assumed |

For CFS jobs, Kiln records the hardware caveat instead of pretending all SKUs are identical: installed CFS unit count, firmware, slot mapping, and material path must be verified before unattended multicolor printing.

`cfs_status()` is read-only discovery, not a Bambu AMS-equivalent control promise. It checks Moonraker object lists, candidate CFS/filament objects, and G-code help, then returns any visible slots/macros with `hardware_unverified: true` and `active_slot_control_supported: false`. Use Creality Print or the printer UI to verify CFS slot mapping until a real K1/K2/Hi/SparkX device validates active slot commands.

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

Notes: The adapter communicates exclusively over local LAN -- it does not use the Bambu Cloud API. Printer must have LAN Mode enabled. File management uses implicit FTPS with TLS session reuse. TLS defaults to certificate pinning (`KILN_BAMBU_TLS_MODE=pin`) with first-use pin capture in `~/.kiln/bambu_tls_pins.json`; `ca` and `insecure` modes are also available for stricter/legacy behavior. The A1 and A1 Mini send uppercase state values (e.g. `RUNNING` instead of `running`), which the adapter normalizes. Webcam access attempts an HTTP snapshot endpoint and also provides the RTSP stream URL (`rtsps://<host>:322/streaming/live/1`).

### Prusa Link

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

Kiln ships curated safety profiles with per-printer temperature limits, feedrate limits, volumetric flow limits, and build volumes:

| Profile ID | Display Name | Max Hotend | Max Bed | Build Volume |
|---|---|---|---|---|
| `default` | Generic / Unknown Printer | 300 C | 130 C | -- |
| `ender3` | Creality Ender 3 / Ender 3 Pro / Ender 3 V2 | 260 C | 110 C | 220x220x250 |
| `ender3_v2` | Creality Ender 3 V2 | 260 C | 110 C | 220x220x250 |
| `ender3_s1` | Creality Ender 3 S1 / S1 Pro | 300 C | 110 C | 220x220x270 |
| `ender5` | Creality Ender 5 / Ender 5 Plus | 260 C | 110 C | 220x220x300 |
| `cr10` | Creality CR-10 / CR-10S | 260 C | 110 C | 300x300x400 |
| `sparkx_i7` | Creality SPARKX i7 | 300 C | 100 C | 260x260x255 |
| `k1` | Creality K1 | 300 C | 100 C | 220x220x250 |
| `k1_max` / `creality_k1_max` | Creality K1 Max | 300 C | 100 C | 300x300x300 |
| `k1c` / `creality_k1c` | Creality K1C | 300 C | 100 C | 220x220x250 |
| `k1_se` / `creality_k1_se` | Creality K1 SE | 300 C | 100 C | 220x220x250 |
| `k2` / `creality_k2` | Creality K2 | 300 C | 100 C | 260x260x260 |
| `k2_pro` / `creality_k2_pro` | Creality K2 Pro | 300 C | 110 C | 300x300x300 |
| `k2_plus` / `creality_k2_plus` | Creality K2 Plus | 350 C | 120 C | 350x350x350 |
| `k2_se` / `creality_k2_se` | Creality K2 SE | 300 C | 100 C | 220x215x245 |
| `creality_hi` | Creality Hi | 300 C | 100 C | 260x260x300 |
| `ender3_v4` | Creality Ender-3 V4 | 300 C | 100 C | 220x220x235 |
| `ender3_v3` | Creality Ender-3 V3 | 300 C | 110 C | 220x220x250 |
| `ender3_v3_ke` | Creality Ender-3 V3 KE | 300 C | 100 C | 220x220x240 |
| `ender3_v3_se` | Creality Ender-3 V3 SE | 260 C | 100 C | 220x220x250 |
| `ender3_v3_plus` | Creality Ender-3 V3 Plus | 300 C | 100 C | 300x300x330 |
| `ender5_max` | Creality Ender-5 Max | 300 C | 100 C | 400x400x400 |
| `cr10_se` | Creality CR-10 SE | 300 C | 110 C | 220x220x265 |
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
| `sovol_sv07` | Sovol SV07 | 300 C | 110 C | 220x220x250 |
| `sovol_sv07_plus` | Sovol SV07 Plus | 300 C | 110 C | 300x300x350 |
| `flashforge_adventurer5m` | FlashForge Adventurer 5M / 5M Pro | 280 C | 110 C | 220x220x220 |
| `qidi_x_plus3` | QIDI X-Plus 3 | 350 C | 120 C | 280x280x270 |
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
