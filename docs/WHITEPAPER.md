<p align="center">
  <img src="assets/kiln-logo-dark.svg" alt="Kiln" width="200">
</p>

# Kiln: A Protocol for Agent-Operated Physical Manufacturing

**Version 0.1.0 — February 2026**

## Abstract

We present Kiln, a protocol and reference implementation that enables autonomous AI agents to control physical manufacturing hardware — specifically 3D printers — through a unified adapter interface. Kiln bridges the gap between digital intelligence and physical fabrication by exposing printer operations, model discovery, slicing, job scheduling, and fleet management through the Model Context Protocol (MCP) and a conventional CLI. The system operates locally with zero cloud dependency, supports heterogeneous printer backends (OctoPrint, Moonraker, Bambu Lab), and enforces safety invariants at the protocol level to prevent physical damage. We describe the adapter abstraction that normalizes disparate printer APIs, the safety validation layer that gates all physical operations, and the scheduling architecture that enables multi-printer job dispatch.

## 1. Introduction

### 1.1 The Problem

3D printing has matured as a manufacturing technology, but the control interface has not. Every printer ecosystem — OctoPrint, Klipper/Moonraker, Bambu Lab — exposes its own REST, WebSocket, or MQTT API with incompatible data formats, authentication schemes, and state models. An agent (or human) who wants to operate a mixed fleet must write and maintain integrations for each backend independently.

Meanwhile, AI agents have become capable enough to plan and execute multi-step physical tasks: selecting a design, choosing materials, slicing geometry, scheduling jobs, and monitoring prints. But no protocol exists to give these agents safe, structured access to printer hardware.

### 1.2 Our Contribution

Kiln solves both problems simultaneously:

1. **Unified Adapter Layer.** A single `PrinterAdapter` abstract interface normalizes OctoPrint, Moonraker, Bambu Lab, and Prusa Connect APIs into consistent Python dataclasses. Adding a new backend requires implementing ~12 methods; all upstream consumers (CLI, MCP server, scheduler) work automatically.

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
           Adapter /  Adapter  | Adapter  \ Adapter
                  /       |      |        \
          +----------+ +--------+ +-------+ +-------------+
          | OctoPrint| |Moonraker| | Bambu | |Prusa Connect|
          +----------+ +--------+ +-------+ +-------------+
               |           |          |           |
           HTTP/REST    HTTP/REST   MQTT/LAN   HTTP/REST
               |           |          |           |
          [Printer]   [Printer]  [Printer]   [Printer]
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
| `get_stream_url()` | Returns the upstream MJPEG stream URL (optional) |
| `get_bed_mesh()` | Returns bed mesh probing data (optional) |

Each method returns typed dataclasses — never raw dicts or API-specific JSON. State is normalized to a `PrinterStatus` enum (`IDLE`, `PRINTING`, `PAUSED`, `ERROR`, `OFFLINE`). This normalization is critical: OctoPrint encodes state as a set of boolean flags, Moonraker uses string identifiers, Bambu uses numeric codes, and Prusa Connect uses nine string states. The adapter translates all of these into a single enum.

### 2.3 Safety Layer

Physical machines can be damaged by software errors. Kiln enforces safety at three levels:

**Level 1: G-code Validation.** Before any G-code reaches a printer, it passes through `validate_gcode()`. This blocks commands known to be dangerous without explicit context: firmware reset (`M502`), EEPROM wipe (`M500` without prior `M501`), and movements beyond axis limits.

**Level 2: Pre-flight Checks.** The `preflight_check()` gate runs before every print job. It validates: printer is online and idle, target file exists on the printer, temperatures are within safe ranges for the declared material, and no existing job is active.

**Level 3: Temperature Limits.** Hard limits (300C hotend, 130C bed) are enforced regardless of what the caller requests. Material-specific ranges (e.g., PLA 180-220C hotend, 40-70C bed) generate warnings when targets fall outside expected bounds.

An agent may request any operation, but Kiln will refuse operations that violate safety invariants and return a structured error explaining why.

## 3. Model Discovery and Slicing Pipeline

### 3.1 Marketplace Integration

Kiln mirrors its printer adapter pattern for model marketplaces. A `MarketplaceAdapter` abstract base class defines a uniform interface — `search()`, `get_details()`, `get_files()`, `download_file()` — implemented by concrete adapters for Thingiverse (REST), MyMiniFactory (REST v2), and Cults3D (GraphQL). Each adapter normalizes API-specific JSON into shared dataclasses (`ModelSummary`, `ModelDetail`, `ModelFile`) with computed properties like `is_printable` (ready-to-print G-code) and `needs_slicing` (STL/3MF that requires slicing first).

A `MarketplaceRegistry` manages connected adapters and provides `search_all()`, which fans out queries in parallel using a thread pool and interleaves results round-robin across sources for variety. Per-adapter failures are caught and logged without poisoning the aggregate response — if one marketplace is down, results from the others still return. Not all adapters support downloads: Cults3D is metadata-only due to API limitations, and its adapter signals this via `supports_download = False`.

The `download_and_upload` MCP tool combines marketplace download and printer upload into a single action, accepting any supported source as a parameter. This reduces the design-to-printer pipeline to a single tool call for agents that already know which file they want.

### 3.2 Slicer Integration

Raw STL/3MF geometry must be sliced into G-code before printing. Kiln wraps PrusaSlicer and OrcaSlicer CLIs with automatic detection:

1. Check `PATH` for known binary names
2. Check macOS application bundle paths
3. Check `KILN_SLICER_PATH` environment variable

The `slice_and_print` operation combines slicing, upload, and print start into a single atomic action — the primary workflow for agents converting a design idea into a physical object.

### 3.3 Model Generation

Kiln extends beyond model discovery with text-to-3D generation. A `GenerationProvider` abstract base class mirrors the marketplace adapter pattern, defining `generate()`, `get_job_status()`, and `download_result()` methods. Two concrete providers ship:

**Meshy (cloud).** Integrates with the Meshy API for AI-powered text-to-3D generation. The provider submits preview tasks via REST, polls for completion, and downloads the resulting mesh. Jobs are asynchronous (typically 30s–5min). The `await_generation` MCP tool handles polling with configurable timeouts, matching the pattern established by `await_print_completion`.

**OpenSCAD (local).** For parametric and geometric models, agents can write OpenSCAD code directly. The provider compiles `.scad` scripts to STL using the local OpenSCAD binary, auto-detected from PATH or macOS application bundles. Jobs are synchronous — the result is available immediately. This path has zero API cost and produces deterministic, parametric geometry ideal for mechanical parts.

A mesh validation pipeline runs after generation, checking structural integrity without external dependencies. It parses binary and ASCII STL files using Python's `struct` module, validates triangle counts, computes bounding boxes, and performs manifold (watertight) analysis via edge counting. Validation issues are categorized as fatal errors (unparseable, zero triangles) or warnings (non-manifold, extreme dimensions) — allowing agents to make informed decisions about print readiness.

### 3.4 End-to-End Workflows

The complete agent workflows are:

```
Discovery path: Idea → Search marketplace → Download STL → Slice → Print
Generation path: Idea → Generate model → Validate mesh → Slice → Print
```

The `generate_and_print` MCP tool collapses the generation path into a single tool call: text prompt in, physical object out. Each step is also available individually for agents that need finer control.

## 4. Fleet Management and Job Scheduling

### 4.1 Printer Registry

The `PrinterRegistry` maintains a map of named printers with their adapter configurations. Printers can be added via CLI (`kiln auth`) or MCP tool (`register_printer`). The registry supports heterogeneous fleets — a single Kiln instance can manage OctoPrint, Moonraker, and Bambu printers simultaneously.

### 4.2 Job Queue

The `JobQueue` accepts print jobs with optional priority levels and dispatches them to available printers. Jobs progress through states: `PENDING` → `DISPATCHED` → `PRINTING` → `COMPLETED` (or `FAILED`, `CANCELLED`). The queue persists to SQLite, surviving server restarts.

### 4.3 Scheduler

The `Scheduler` runs a background loop that matches pending jobs to idle printers. When a printer finishes a job, the scheduler automatically dispatches the next job in the queue. Failed jobs are retried up to a configurable limit (default 2 retries) before being permanently marked as failed — transient errors like network timeouts don't kill a batch run. This enables unattended batch production.

For unassigned jobs (no explicit printer target), the scheduler applies **history-based smart routing**: it queries the persistence layer for each candidate printer's historical success rate with the job's file hash and material type, then dispatches to the printer with the highest success rate. Printers without relevant history fall back to default ordering. This allows the fleet to self-optimize over time — printers that consistently succeed with a given material or geometry are automatically preferred.

## 5. Event System and Webhooks

### 5.1 Event Bus

All significant state changes emit events through a pub/sub event bus: job state transitions, printer status changes, errors. Events are persisted to SQLite for historical queries.

### 5.2 Webhook Delivery

External systems can register HTTP webhook endpoints for specific event types. Payloads are signed with HMAC-SHA256 when a secret is provided, enabling verification of event authenticity.

## 6. Monitoring

### 6.1 Webcam Snapshots and Live Streaming

Agents can capture point-in-time webcam images via the `printer_snapshot` MCP tool or `kiln snapshot` CLI command. OctoPrint and Moonraker backends support snapshot capture. Images are returned as raw bytes or base64-encoded JSON.

For continuous monitoring, Kiln includes an MJPEG proxy (`kiln.streaming`) that reads the upstream MJPEG stream from a printer and re-serves it over a local HTTP endpoint. Multiple clients can connect without adding load to the printer. The proxy tracks connected clients, frames served, and uptime.

### 6.2 Print Tracking

The `kiln wait` command blocks until a print completes, with configurable polling interval and timeout. `kiln history` queries the SQLite database for past print records with status filtering.

### 6.3 Print Cost Estimation

Agents can estimate printing costs before committing to a job. The `cost_estimator` module parses G-code to compute filament length, weight, and cost based on material profiles (PLA, PETG, ABS, TPU, ASA, Nylon, PC). It also extracts estimated print time from slicer comments and calculates electricity costs. The `estimate_cost` MCP tool and `kiln cost` CLI command expose this analysis.

### 6.4 Multi-Material Tracking

The `materials` module tracks which filament is loaded in each printer extruder and maintains a spool inventory. Agents can set loaded materials, check for mismatches against expected materials in G-code, and track remaining filament. The system emits events when spools run low (< 10% remaining) or empty, enabling proactive filament management.

### 6.5 Bed Leveling Automation

Bed leveling can be triggered automatically based on configurable policies: maximum prints since last level, maximum hours elapsed, or before the first print. The `bed_leveling` module subscribes to job completion events and evaluates trigger conditions. Mesh probing data is persisted and analyzed for variance. Moonraker printers expose bed mesh data via the adapter interface.

### 6.6 Cloud Sync

The `cloud_sync` module synchronizes printer configurations, job history, and events to a remote REST API. A background daemon thread pushes local changes incrementally using cursor-based tracking. Payloads are HMAC-SHA256 signed for integrity verification.

### 6.7 Plugin System

Kiln supports third-party extensions through a plugin system based on Python entry points (`kiln.plugins` group). Plugins can register MCP tools, subscribe to events, add CLI commands, and hook into pre/post-print lifecycle events. The plugin manager handles discovery, activation, and fault isolation — exceptions in plugin hooks do not crash the host system.

## 7. Revenue Model

Local printer control is free and unrestricted. Kiln charges a 5% platform fee on orders placed through external manufacturing services (Craftcloud, and future providers), with the first 5 outsourced orders per month free and a $0.25 minimum / $200 maximum per-order cap. The fee is surfaced transparently in every quote response before the user commits to an order.

Kiln uses a three-tier licensing model: **Free** (all local printing, up to 2 printers, 10-job queue, billing visibility), **Pro** ($29/mo — unlimited printers, fleet orchestration, analytics, unlimited queue, cloud sync), and **Business** ($99/mo — fulfillment brokering, hosted deployment, priority support). The free tier is designed to be excellent for solo operators, with the paywall boundary at multi-printer fleet orchestration rather than individual feature gating. License keys are validated offline-first via key prefix detection (`kiln_pro_`, `kiln_biz_`) with cached remote validation. The licensing system never blocks printer operations if the validation API is unreachable. Billing is tracked through a `BillingLedger` with `FeeCalculation` structs that record fee type, amount, and associated order metadata.

Kiln includes a multi-rail payment processing layer. A `PaymentProvider` abstract interface supports Stripe (fiat USD/EUR), Circle (USDC stablecoin), and on-chain crypto (Solana, Ethereum, Base L2). The `PaymentManager` orchestrates provider selection, spend limit enforcement, and auth-and-capture flows — placing holds at quote time and capturing at order time. All payment transactions are persisted to SQLite and emit events through the event bus. Spend limits (per-order and monthly caps) are enforced before any charge. Crypto donations are accepted at kiln3d.sol (Solana) and kiln3d.eth (Ethereum).

## 8. Security Considerations

- **Network isolation.** Kiln communicates with printers over the local network only. No cloud relay.
- **API key authentication.** Optional scope-based authentication gates MCP tools when enabled.
- **Credential storage.** API keys and access codes are stored in `~/.kiln/config.yaml` with permission warnings for world-readable files.
- **Input validation.** All file paths, G-code commands, and temperature values are validated before reaching printer hardware.
- **Heater watchdog.** A background daemon monitors heater state and automatically cools down idle heaters after a configurable timeout (default 30 minutes, via `KILN_HEATER_TIMEOUT`). Prevents fire hazards from heaters left on when no print is active.

## 9. Closed-Loop Vision Monitoring

### 9.1 Design Philosophy

Rather than embedding a separate computer vision model, Kiln leverages the agent itself as the vision system. Modern multimodal agents (Claude, GPT-4V) can analyze images directly. Kiln's role is to provide structured context alongside each snapshot: printer state, job progress, print phase classification, and phase-specific failure hints.

### 9.2 Phase Detection

Prints are classified into three phases based on completion percentage:
- **First layers** (< 10%) — Critical for adhesion. Failure hints focus on bed adhesion, first layer height, and elephant's foot.
- **Mid print** (10–90%) — Bulk of the print. Failure hints focus on stringing, layer shifts, and temperature stability.
- **Final layers** (> 90%) — Finishing. Failure hints focus on top surface quality, cooling, and overhangs.

### 9.3 Monitoring Tools

`monitor_print_vision` captures a single snapshot with full metadata: printer status, temperatures, job progress percentage, phase classification, and phase-specific failure hints. The agent receives everything needed to make an informed decision about print quality.

`watch_print` creates a continuous monitoring loop. It polls printer state at a configurable interval and captures snapshots periodically (default every 60 seconds). After accumulating a batch of snapshots (default 5), it returns them to the agent for review. The agent analyzes the batch, decides whether to pause or continue, and calls `watch_print` again — closing the feedback loop.

Both tools work gracefully without a webcam — metadata is still returned for state-based monitoring.

## 10. Cross-Printer Learning

### 10.1 Outcome Recording

Agents record structured print outcomes after each job: success/failure/partial, quality grade, failure mode (from a validated set: spaghetti, warping, layer shift, etc.), print settings, and environmental conditions. All recorded data passes through safety validation — temperature values exceeding hardware-safe maximums (320°C hotend, 140°C bed, 500mm/s speed) are rejected with a `SAFETY_VIOLATION` error code.

### 10.2 Insight Queries

`get_printer_insights` aggregates outcome history for a specific printer: success rate, failure mode breakdown, material performance, and recent outcomes. Results include a confidence level based on sample size and carry a safety notice marking them as advisory only.

`suggest_printer_for_job` ranks available printers by historical success rate for a given file hash and material type. Printers with few data points are penalized in scoring to prevent overconfidence from small samples. Results are cross-referenced with current printer availability from the fleet registry.

### 10.3 Safety Posture

All learning data is advisory — it informs agent decisions but never overrides safety limits. The system rejects outcome records with physically dangerous parameter values. Every insight response carries an explicit safety notice stating that data reflects past outcomes only and must not be used to bypass safety validation.

## 11. Physical Fabrication Generalization

### 11.1 Evolutionary Extension

Kiln's adapter pattern was designed for 3D printers but the abstraction generalizes to any computer-controlled fabrication device. Rather than a breaking rename, Kiln introduces forward-compatible extension points:

- **`DeviceType` enum** — Classifies devices: `FDM_PRINTER`, `SLA_PRINTER`, `CNC_ROUTER`, `LASER_CUTTER`, `GENERIC`.
- **`DeviceAdapter` alias** — `DeviceAdapter = PrinterAdapter`. Existing code continues to reference `PrinterAdapter`; new integrations can use `DeviceAdapter`.
- **Extended `PrinterCapabilities`** — Adds `device_type` and `can_snapshot` fields with backward-compatible defaults.
- **Optional device methods** — `set_spindle_speed()`, `set_laser_power()`, `get_tool_position()` provide hooks for non-printer devices. Default implementations raise `PrinterError` or return `None`, so existing adapters are unaffected.

### 11.2 Compatibility Guarantee

All four existing adapters (OctoPrint, Moonraker, Bambu, Prusa Connect) continue to work without modification. The new fields default to `device_type="fdm_printer"` and `can_snapshot=False` (overridden to `True` for OctoPrint and Moonraker which support webcam capture). No existing tests break; the extensions are purely additive.

### 11.3 Distributed Manufacturing Network

Kiln integrates with the 3DOS distributed manufacturing network, enabling peer-to-peer job routing across independent printer operators. Local printers can be registered on the network with material capabilities and location metadata. The `ThreeDOSClient` gateway handles printer registration, availability updates, job submission, and status polling. Agents can discover available printers on the network by material type or location, submit jobs for remote fabrication, and track order status — extending Kiln's reach beyond locally-connected hardware.

## 12. Future Work
- **Remote agent collaboration.** Enable multiple agents to coordinate across a shared printer fleet.
- **CNC and laser adapter implementations.** Concrete adapters for Grbl, LinuxCNC, and LightBurn backends.
- **Federated learning.** Aggregate anonymized print outcomes across Kiln instances (opt-in) for community-level printer insights.

## 13. Conclusion

Kiln demonstrates that AI agents can safely operate physical manufacturing hardware given the right protocol abstractions. By normalizing heterogeneous printer APIs into a typed adapter interface, enforcing safety invariants at the protocol level, and exposing all operations through MCP, Kiln transforms any MCP-compatible agent into a manufacturing operator. Closed-loop vision monitoring lets agents observe and intervene during prints. Cross-printer learning enables data-driven printer selection with safety-first guardrails. The generalized device abstraction positions Kiln to expand beyond 3D printing to CNC, laser, and resin fabrication. The system is local-first, open-source, and extensible to new device backends and manufacturing services.

---

*Kiln is open-source software released under the MIT License.*
