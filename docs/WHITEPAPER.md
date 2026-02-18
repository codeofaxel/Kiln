<p align="center">
  <img src="assets/kiln-logo-dark.svg" alt="Kiln" width="200">
</p>

# Kiln: A Protocol for Agent-Operated Physical Manufacturing

**Version 0.1.0 — February 2026**

## Abstract

We present Kiln, a protocol and reference implementation that enables autonomous AI agents to control physical manufacturing hardware — specifically 3D printers — through a unified adapter interface. Kiln bridges the gap between digital intelligence and physical fabrication by exposing three co-equal manufacturing paths through a single interface: (1) direct control of local printers via OctoPrint, Moonraker, Bambu Lab, Elegoo, and Prusa Link adapters; (2) outsourced manufacturing through fulfillment providers such as Craftcloud and Sculpteo; and (3) distributed peer-to-peer manufacturing via decentralized printer networks (coming soon). All three modes are accessible through the Model Context Protocol (MCP) and a conventional CLI, allowing agents to seamlessly route jobs based on material availability, capacity, cost, or geographic proximity. The system enforces safety invariants at the protocol level to prevent physical damage. We describe the adapter abstraction that normalizes disparate printer APIs, the fulfillment and distributed manufacturing integrations, the safety validation layer, and the scheduling architecture that enables multi-printer job dispatch.

## 1. Introduction

### 1.1 The Problem

3D printing has matured as a manufacturing technology, but the control interface has not. Every printer ecosystem — OctoPrint, Klipper/Moonraker, Bambu Lab, Elegoo — exposes its own REST, WebSocket, or MQTT API with incompatible data formats, authentication schemes, and state models. An agent (or human) who wants to operate a mixed fleet must write and maintain integrations for each backend independently.

Meanwhile, AI agents have become capable enough to plan and execute multi-step physical tasks: selecting a design, choosing materials, slicing geometry, scheduling jobs, and monitoring prints. But no protocol exists to give these agents safe, structured access to printer hardware.

### 1.2 Our Contribution

Kiln solves both problems simultaneously:

1. **Unified Adapter Layer.** A single `PrinterAdapter` abstract interface normalizes OctoPrint, Moonraker, Bambu Lab, Elegoo, and Prusa Link APIs into consistent Python dataclasses. Adding a new backend requires implementing ~12 methods; all upstream consumers (CLI, MCP server, scheduler) work automatically.

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
                        /      |      \
              ---------/       |       \---------
             /                 |                 \
   [Your Printers]     [Fulfillment]     [Distributed Network]
    /   |   \  \  \       /       \           |
  OP  MR  BL  PL  EG  Craftcloud  Sculpteo  Remote Printers
   |   |   |   |   |      |          |           |
  HTTP HTTP MQTT HTTP WS  HTTPS     HTTPS       HTTPS
   |   |   |   |   |      |          |           |
  🖨️  🖨️  🖨️  🖨️  🖨️   150+ svcs  75+ mats   P2P fleet

  OP = OctoPrint, MR = Moonraker, BL = Bambu Lab, PL = Prusa Link, EG = Elegoo
```

The three manufacturing paths are co-equal: an agent can print locally, outsource to a fulfillment center, or route to a peer-to-peer network printer — all through the same MCP tools and CLI commands. The server is stateless with respect to printer communication — all state lives on the printers themselves. Kiln maintains local state only for job queuing, event history, and webhook registrations via SQLite.

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

Each method returns typed dataclasses — never raw dicts or API-specific JSON. State is normalized to a `PrinterStatus` enum (`IDLE`, `PRINTING`, `PAUSED`, `ERROR`, `OFFLINE`). This normalization is critical: OctoPrint encodes state as a set of boolean flags, Moonraker uses string identifiers, Bambu uses numeric codes, Elegoo uses SDCP numeric status codes over WebSocket, and Prusa Link uses nine string states. The adapter translates all of these into a single enum.

### 2.3 Safety Layer

Physical machines can be damaged by software errors. Kiln enforces safety at three levels:

**Level 1: G-code Validation.** Before any G-code reaches a printer, it passes through `validate_gcode()`. This blocks commands known to be dangerous without explicit context: firmware reset (`M502`), EEPROM wipe (`M500` without prior `M501`), and movements beyond axis limits.

**Level 2: Pre-flight Checks.** The `preflight_check()` gate runs before every print job. It validates: printer is online and idle, target file exists on the printer, temperatures are within safe ranges for the declared material, and no existing job is active.

**Level 3: Temperature Limits.** Hard limits (300C hotend, 130C bed) are enforced regardless of what the caller requests. Material-specific ranges (e.g., PLA 180-220C hotend, 40-70C bed) generate warnings when targets fall outside expected bounds.

An agent may request any operation, but Kiln will refuse operations that violate safety invariants and return a structured error explaining why.

## 3. Model Discovery and Slicing Pipeline

### 3.1 Marketplace Integration

Kiln mirrors its printer adapter pattern for model marketplaces. A `MarketplaceAdapter` abstract base class defines a uniform interface — `search()`, `get_details()`, `get_files()`, `download_file()` — implemented by concrete adapters for Thingiverse (REST; deprecated since MyMiniFactory's acquisition of Thingiverse in February 2026), MyMiniFactory (REST v2), and Cults3D (GraphQL). Each adapter normalizes API-specific JSON into shared dataclasses (`ModelSummary`, `ModelDetail`, `ModelFile`) with computed properties like `is_printable` (ready-to-print G-code) and `needs_slicing` (STL/3MF that requires slicing first).

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

Local printer control is free and unrestricted. Kiln charges a 5% platform fee on orders placed through external manufacturing services (Craftcloud, Sculpteo, and distributed manufacturing networks), with the first 5 outsourced orders per month free and a $0.25 minimum / $200 maximum per-order cap. The fee is surfaced transparently in every quote response before the user commits to an order.

Kiln uses a three-tier licensing model: **Free** (all local printing, up to 2 printers, 10-job queue, billing visibility), **Pro** ($29/mo — unlimited printers, fleet orchestration, analytics, unlimited queue, cloud sync), and **Business** ($99/mo — fulfillment brokering, hosted deployment, priority support). The free tier is designed to be excellent for solo operators, with the paywall boundary at multi-printer fleet orchestration rather than individual feature gating. License keys are validated offline-first via key prefix detection (`kiln_pro_`, `kiln_biz_`) with cached remote validation. The licensing system never blocks printer operations if the validation API is unreachable. Billing is tracked through a `BillingLedger` with `FeeCalculation` structs that record fee type, amount, and associated order metadata.

Kiln includes a multi-rail payment processing layer. A `PaymentProvider` abstract interface supports Stripe (fiat USD/EUR), Circle (USDC stablecoin), and on-chain crypto (Solana, Ethereum, Base L2). The `PaymentManager` orchestrates provider selection, spend limit enforcement, and auth-and-capture flows — placing holds at quote time and capturing at order time. All payment transactions are persisted to SQLite and emit events through the event bus. Spend limits (per-order and monthly caps) are enforced before any charge. Crypto donations are accepted at kiln3d.sol (Solana) and kiln3d.eth (Ethereum).

## 8. Security Considerations

Kiln controls physical hardware, processes untrusted agent output, and accepts network requests from external clients. The security model is organized into six layers, each addressing a distinct threat surface.

### 8.1 Physical Safety Enforcement

The most consequential class of vulnerability in a manufacturing protocol is one that causes physical damage. Kiln enforces safety invariants at three levels described in Section 2.3: G-code command validation, pre-flight checks before every print job, and hard temperature ceilings. Additionally, per-printer safety profiles (`data/safety_profiles.json`) define model-specific limits for 30 printer models — maximum hotend and bed temperatures, axis travel bounds, and maximum feedrates. These limits are loaded at startup and checked on every `set_temperature` and `send_gcode` call; requests exceeding them are rejected with a structured error before any command reaches the printer. A background heater watchdog monitors thermal state and automatically cools idle heaters after a configurable timeout (default 30 minutes, via `KILN_HEATER_TIMEOUT`) to prevent fire hazards.

### 8.2 Authentication and Authorization

Authentication is optional and disabled by default for local-only deployments. When enabled (`KILN_AUTH_ENABLED=1`), the `AuthManager` enforces API key verification on every MCP tool call and REST endpoint. Keys are SHA-256 hashed at rest and never stored in plaintext. Each key carries a set of scopes (`read`, `write`, `admin`) that gate access to tool categories — a read-scoped key cannot start prints or send G-code. The key lifecycle supports generation (with a `sk_kiln_` prefix), rotation with a configurable grace period (default 24 hours of dual-key validity), deprecation, and immediate revocation. When authentication is enabled without an explicit key, Kiln auto-generates a session key and logs it at startup, ensuring the system is never accidentally left unprotected.

### 8.3 Network Security

The REST API layer (`rest_api.py`) implements a sliding-window rate limiter that tracks per-client request timestamps and enforces configurable limits (default 60 requests/minute via `KILN_RATE_LIMIT`). Exceeded clients receive `429` responses with standard `Retry-After` and `X-RateLimit-Reset` headers. CORS origins are explicitly whitelisted — no wildcard origins are permitted. Webhook delivery includes SSRF prevention: before dispatching an event payload, `_validate_webhook_url` resolves the target hostname and checks the resulting IP against a block list of private and reserved networks (RFC 1918, link-local, loopback, and IPv6 equivalents). Webhook payloads are signed with HMAC-SHA256 when a secret is provided, allowing receivers to verify authenticity and integrity.

### 8.4 Agent Safety

Tool results returned from printer APIs may contain arbitrary strings that, if passed verbatim to an LLM, could constitute prompt injection. The `_sanitize_tool_output` function in the agent loop strips common injection patterns — phrases attempting to impersonate system messages, override instructions, or redefine the agent's role — and truncates output to a configurable maximum (default 50,000 characters) to prevent context-window flooding. A privacy mode (enabled by default via `KILN_LLM_PRIVACY_MODE`) redacts private IP addresses, port numbers, Bearer tokens, and Authorization headers from all tool results before they enter the LLM context, preventing unintentional leakage of network topology. Tool tiers further restrict exposure: weaker models receive only 15 essential tools, mid-tier models receive ~40, and only strong models (Claude, GPT-4) receive the full tool set — reducing the attack surface for models with less reliable function-calling behavior.

### 8.5 Data Protection

All SQL queries in the persistence layer use parameterized statements — no string interpolation touches query construction. The codebase contains no calls to `eval()` or `exec()`. The plugin system enforces an explicit allow-list: only plugins named in the user's configuration are loaded, preventing arbitrary code execution from unexpected entry points. Plugin hooks run inside fault-isolation wrappers so that exceptions in third-party code do not crash the host process or corrupt shared state. Credentials and API keys stored in `~/.kiln/config.yaml` are checked for file permissions at load time, with a warning emitted if the file is world-readable.

### 8.6 Audit Trail

Safety-critical operations are recorded through two mechanisms. The event bus persists all significant state changes — job transitions, printer status changes, temperature warnings — to SQLite, providing a queryable history of system behavior. A dedicated `safety_audit` tool reviews recent safety-relevant actions (guarded commands, emergency stops, temperature limit rejections) and surfaces them to agents or operators on demand. Together, these provide a tamper-evident record of every physical action the system has taken.

## 9. Agent-Delegated Vision Monitoring

### 9.1 Design Philosophy

Kiln does not embed its own computer vision model. Instead, it provides structured monitoring data — webcam snapshots, temperature readings, print progress, layer metadata, and phase-specific failure hints — and delegates visual analysis to the agent's own vision model (Claude, GPT-4V, Gemini, or any future multimodal model). This is an intentional architectural choice: it keeps Kiln model-agnostic, avoids coupling to a specific vision backend, and automatically benefits from improvements in the agent's underlying vision capabilities.

Kiln adds lightweight heuristic validation to each captured frame — brightness and variance checks that detect blocked cameras, corrupted images, or lens obstructions — so agents can trust that the snapshot they receive is usable before running inference.

### 9.2 Phase Detection

Prints are classified into three phases based on completion percentage:
- **First layers** (< 10%) — Critical for adhesion. Failure hints focus on bed adhesion, first layer height, and elephant's foot.
- **Mid print** (10–90%) — Bulk of the print. Failure hints focus on stringing, layer shifts, and temperature stability.
- **Final layers** (> 90%) — Finishing. Failure hints focus on top surface quality, cooling, and overhangs.

### 9.3 Monitoring Tools

`monitor_print_vision` captures a single snapshot with full structured context: printer status, temperatures, job progress percentage, phase classification, phase-specific failure hints, and image quality heuristics. The agent's vision model analyzes the snapshot for defects; the agent then decides what action to take (pause, cancel, continue, or alert the operator).

`watch_print` creates a continuous monitoring loop. It polls printer state at a configurable interval and captures snapshots periodically (default every 60 seconds). After accumulating a batch of snapshots (default 5), it returns them to the agent for review. The agent's vision model analyzes the batch, the agent decides whether to pause or continue, and calls `watch_print` again — closing the feedback loop.

Both tools work gracefully without a webcam — metadata is still returned for state-based monitoring.

## 10. Cross-Printer Learning

### 10.1 Outcome Recording

Agents record structured print outcomes after each job: success/failure/partial, quality grade, failure mode (from a validated set: spaghetti, warping, layer shift, etc.), print settings, and environmental conditions. All recorded data passes through safety validation — temperature values exceeding hardware-safe maximums (320°C hotend, 140°C bed, 500mm/s speed) are rejected with a `SAFETY_VIOLATION` error code.

### 10.2 Insight Queries

`get_printer_insights` aggregates outcome history for a specific printer: success rate, failure mode breakdown, material performance, and recent outcomes. Results include a confidence level based on sample size and carry a safety notice marking them as advisory only.

`suggest_printer_for_job` ranks available printers by historical success rate for a given file hash and material type. Printers with few data points are penalized in scoring to prevent overconfidence from small samples. Results are cross-referenced with current printer availability from the fleet registry.

### 10.3 Safety Posture

All learning data is advisory — it informs agent decisions but never overrides safety limits. The system rejects outcome records with physically dangerous parameter values. Every insight response carries an explicit safety notice stating that data reflects past outcomes only and must not be used to bypass safety validation.

## 11. Distributed Manufacturing Network

*(Coming soon.)* Kiln's architecture supports integration with decentralized peer-to-peer manufacturing networks, enabling job routing across independent printer operators. Local printers can be registered on a network with material capabilities and location metadata. The gateway layer handles printer registration, availability updates, job submission, and status polling. Agents can discover available printers on the network by material type or location, submit jobs for remote fabrication, and track order status — extending Kiln's reach beyond locally-connected hardware. Specific network integrations will be announced as partnerships are finalized.

## 12. Consumer Manufacturing Workflow

Kiln extends beyond printer owners to serve users who have never touched a 3D printer. The consumer workflow enables agents to handle the complete journey from a natural-language request to a delivered physical product.

**Guided Onboarding.** A seven-step onboarding pipeline walks agents through model discovery or generation, material recommendation, price estimation, quoting, address validation, order placement, and delivery tracking. Each step maps to a specific MCP tool.

**Material Recommendation Engine.** A knowledge base maps ten consumer use cases (decorative, functional, mechanical, prototype, miniature, jewelry, enclosure, wearable, outdoor, food-safe) to ranked material recommendations across FDM, SLA, SLS, and MJF technologies. Agents filter by budget tier, weather resistance, food safety, detail level, and strength requirements.

**Instant Price Estimation.** Before requesting a full API quote, agents estimate price ranges from part volume or bounding box dimensions using per-technology cost models. This enables agents to set user expectations without network round-trips.

**Timeline Estimation.** Per-stage timeline breakdowns (order confirmation, production, quality check, packaging, shipping) give agents and users visibility into the full delivery pipeline. Production time scales with quantity and technology; shipping estimates vary by destination region.

**Multi-Provider Intelligence.** A fulfillment intelligence layer sits above individual providers, adding health monitoring (consecutive failure detection with automatic skip), cross-provider quote comparison (cheapest, fastest, and recommended), batch quoting for multi-part assemblies, retry with provider fallback, and persistent order history for reordering.

**Address Validation.** Country-specific postal code validation (US ZIP, Canadian postal, UK postcode) catches formatting errors before orders are placed. The system supports 23 countries across North America, Europe, and Asia-Pacific.

**Shipping Insurance.** Tiered protection options (loss-only, loss+damage, full protection with reprint guarantee) are priced as a percentage of order value, giving users and agents clear risk management choices.

## 13. Future Work
- **Remote agent collaboration.** Enable multiple agents to coordinate across a shared printer fleet.
- **Federated learning.** Aggregate anonymized print outcomes across Kiln instances (opt-in) for community-level printer insights.

## 14. Conclusion

Kiln demonstrates that AI agents can safely operate physical manufacturing hardware given the right protocol abstractions. By normalizing heterogeneous printer APIs into a typed adapter interface, enforcing safety invariants at the protocol level, and exposing all operations through MCP, Kiln transforms any MCP-compatible agent into a manufacturing operator. Agent-delegated vision monitoring provides structured snapshot data and context so agents can observe and intervene during prints using their own vision models. Cross-printer learning enables data-driven printer selection with safety-first guardrails. The system is local-first, open-source, and extensible to new device backends and manufacturing services.

---

*Kiln is open-source software released under the MIT License.*
