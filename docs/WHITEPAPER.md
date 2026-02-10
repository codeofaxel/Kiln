# Kiln: A Protocol for Agent-Operated Physical Manufacturing

**Version 1.0 — February 2026**

## Abstract

We present Kiln, a protocol and reference implementation that enables autonomous AI agents to control physical manufacturing hardware — specifically 3D printers — through a unified adapter interface. Kiln bridges the gap between digital intelligence and physical fabrication by exposing printer operations, model discovery, slicing, job scheduling, and fleet management through the Model Context Protocol (MCP) and a conventional CLI. The system operates locally with zero cloud dependency, supports heterogeneous printer backends (OctoPrint, Moonraker, Bambu Lab), and enforces safety invariants at the protocol level to prevent physical damage. We describe the adapter abstraction that normalizes disparate printer APIs, the safety validation layer that gates all physical operations, and the scheduling architecture that enables multi-printer job dispatch.

## 1. Introduction

### 1.1 The Problem

3D printing has matured as a manufacturing technology, but the control interface has not. Every printer ecosystem — OctoPrint, Klipper/Moonraker, Bambu Lab — exposes its own REST, WebSocket, or MQTT API with incompatible data formats, authentication schemes, and state models. An agent (or human) who wants to operate a mixed fleet must write and maintain integrations for each backend independently.

Meanwhile, AI agents have become capable enough to plan and execute multi-step physical tasks: selecting a design, choosing materials, slicing geometry, scheduling jobs, and monitoring prints. But no protocol exists to give these agents safe, structured access to printer hardware.

### 1.2 Our Contribution

Kiln solves both problems simultaneously:

1. **Unified Adapter Layer.** A single `PrinterAdapter` abstract interface normalizes OctoPrint, Moonraker, and Bambu Lab APIs into consistent Python dataclasses. Adding a new backend requires implementing ~12 methods; all upstream consumers (CLI, MCP server, scheduler) work automatically.

2. **Agent-Native Interface.** Every operation is exposed as a typed MCP tool with structured JSON input/output, making Kiln a first-class tool for any MCP-compatible agent (Claude, GPT, custom). The same operations are available via CLI with `--json` flags for scripting.

3. **Safety-First Design.** Pre-flight checks, G-code validation, temperature limits, and confirmation gates are enforced at the protocol layer — not left to the caller. An agent cannot bypass safety checks even if instructed to.

## 2. Architecture

### 2.1 System Overview

```
                    +-----------+
                    |  AI Agent |
                    +-----------+
                         |
              MCP (stdio) or CLI
                         |
                    +-----------+
                    |   Kiln    |
                    |  Server   |
                    +-----------+
                    /     |     \
           Adapter /  Adapter  \ Adapter
                  /       |      \
          +----------+ +--------+ +-------+
          | OctoPrint| |Moonraker| | Bambu |
          +----------+ +--------+ +-------+
               |           |          |
           HTTP/REST    HTTP/REST   MQTT/LAN
               |           |          |
          [Printer]   [Printer]  [Printer]
```

The server is stateless with respect to printer communication — all state lives on the printers themselves. Kiln maintains local state only for job queuing, event history, and webhook registrations via SQLite.

### 2.2 The Adapter Contract

Every printer backend implements `PrinterAdapter`, an abstract base class defining the complete surface area of printer control:

| Method | Description |
|---|---|
| `get_status()` | Returns `PrinterState` with temperatures, flags, job progress |
| `get_files()` | Lists available files as `List[PrinterFile]` |
| `upload_file()` | Uploads local G-code to printer storage |
| `start_print()` | Begins printing a named file |
| `cancel_print()` | Cancels the active job |
| `pause_print()` | Pauses the active job |
| `resume_print()` | Resumes a paused job |
| `set_temperature()` | Sets tool and/or bed targets |
| `send_gcode()` | Executes raw G-code commands |
| `get_snapshot()` | Captures a webcam image (optional) |

Each method returns typed dataclasses — never raw dicts or API-specific JSON. State is normalized to a `PrinterStatus` enum (`IDLE`, `PRINTING`, `PAUSED`, `ERROR`, `OFFLINE`). This normalization is critical: OctoPrint encodes state as a set of boolean flags, Moonraker uses string identifiers, and Bambu uses numeric codes. The adapter translates all of these into a single enum.

### 2.3 Safety Layer

Physical machines can be damaged by software errors. Kiln enforces safety at three levels:

**Level 1: G-code Validation.** Before any G-code reaches a printer, it passes through `validate_gcode()`. This blocks commands known to be dangerous without explicit context: firmware reset (`M502`), EEPROM wipe (`M500` without prior `M501`), and movements beyond axis limits.

**Level 2: Pre-flight Checks.** The `preflight_check()` gate runs before every print job. It validates: printer is online and idle, target file exists on the printer, temperatures are within safe ranges for the declared material, and no existing job is active.

**Level 3: Temperature Limits.** Hard limits (300C hotend, 130C bed) are enforced regardless of what the caller requests. Material-specific ranges (e.g., PLA 180-220C hotend, 40-70C bed) generate warnings when targets fall outside expected bounds.

An agent may request any operation, but Kiln will refuse operations that violate safety invariants and return a structured error explaining why.

## 3. Model Discovery and Slicing Pipeline

### 3.1 Marketplace Integration

Kiln includes adapters for model marketplaces (Thingiverse, MyMiniFactory, Cults3D) that expose search, browse, and download operations. This enables an agent to find a design by description, inspect its details, and download it — all without human navigation.

### 3.2 Slicer Integration

Raw STL/3MF geometry must be sliced into G-code before printing. Kiln wraps PrusaSlicer and OrcaSlicer CLIs with automatic detection:

1. Check `PATH` for known binary names
2. Check macOS application bundle paths
3. Check `KILN_SLICER_PATH` environment variable

The `slice_and_print` operation combines slicing, upload, and print start into a single atomic action — the primary workflow for agents converting a design idea into a physical object.

### 3.3 End-to-End Workflow

The complete agent workflow becomes:

```
Idea → Search marketplace → Download STL → Slice to G-code →
Pre-flight check → Upload → Print → Monitor → Complete
```

Each step is a single MCP tool call. An agent can execute this entire pipeline in under 10 tool calls.

## 4. Fleet Management and Job Scheduling

### 4.1 Printer Registry

The `PrinterRegistry` maintains a map of named printers with their adapter configurations. Printers can be added via CLI (`kiln auth`) or MCP tool (`register_printer`). The registry supports heterogeneous fleets — a single Kiln instance can manage OctoPrint, Moonraker, and Bambu printers simultaneously.

### 4.2 Job Queue

The `JobQueue` accepts print jobs with optional priority levels and dispatches them to available printers. Jobs progress through states: `PENDING` → `DISPATCHED` → `PRINTING` → `COMPLETED` (or `FAILED`, `CANCELLED`). The queue persists to SQLite, surviving server restarts.

### 4.3 Scheduler

The `Scheduler` runs a background loop that matches pending jobs to idle printers. When a printer finishes a job, the scheduler automatically dispatches the next job in the queue. This enables unattended batch production.

## 5. Event System and Webhooks

### 5.1 Event Bus

All significant state changes emit events through a pub/sub event bus: job state transitions, printer status changes, errors. Events are persisted to SQLite for historical queries.

### 5.2 Webhook Delivery

External systems can register HTTP webhook endpoints for specific event types. Payloads are signed with HMAC-SHA256 when a secret is provided, enabling verification of event authenticity.

## 6. Monitoring

### 6.1 Webcam Snapshots

Agents can capture point-in-time webcam images via the `printer_snapshot` MCP tool or `kiln snapshot` CLI command. OctoPrint and Moonraker backends support snapshot capture. Images are returned as raw bytes or base64-encoded JSON.

### 6.2 Print Tracking

The `kiln wait` command blocks until a print completes, with configurable polling interval and timeout. `kiln history` queries the SQLite database for past print records with status filtering.

## 7. Revenue Model

Local printer control is free and unrestricted. Kiln charges a 5% fee only on jobs routed through the distributed manufacturing network (3DOS), with the first 5 network jobs per month free and a $0.25 minimum / $50 maximum per-job cap.

## 8. Security Considerations

- **Network isolation.** Kiln communicates with printers over the local network only. No cloud relay.
- **API key authentication.** Optional scope-based authentication gates MCP tools when enabled.
- **Credential storage.** API keys and access codes are stored in `~/.kiln/config.yaml` with permission warnings for world-readable files.
- **Input validation.** All file paths, G-code commands, and temperature values are validated before reaching printer hardware.

## 9. Future Work

- **Prusa Connect adapter.** Third printer backend for MK4, XL, Mini via Prusa's REST API.
- **Fulfillment service integration.** Route jobs to Craftcloud, Xometry, or Sculpteo when local printers lack the required material or capability.
- **OTA firmware updates.** Expose Moonraker's firmware update API through the adapter interface.
- **Live video streaming.** MJPEG stream proxy for real-time monitoring beyond point-in-time snapshots.
- **Multi-material tracking.** AMS/MMU filament slot management for Bambu and Prusa.

## 10. Conclusion

Kiln demonstrates that AI agents can safely operate physical manufacturing hardware given the right protocol abstractions. By normalizing heterogeneous printer APIs into a typed adapter interface, enforcing safety invariants at the protocol level, and exposing all operations through MCP, Kiln transforms any MCP-compatible agent into a manufacturing operator. The system is local-first, open-source, and extensible to new printer backends and manufacturing services.

---

*Kiln is open-source software released under the MIT License.*
