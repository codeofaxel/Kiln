<p align="center">
  <img src="assets/kiln-logo-dark.svg" alt="Kiln" width="200">
</p>

# Kiln

### Litepaper -- April 2026

> Describe it or draw it. Kiln makes it real.
>
> Kiln turns a conversation with an AI agent into a printed object. One install, any printer, start to finish.

---

Kiln is the intelligence layer between an idea and a physical object. It connects AI agents to 3D printers, design tools, and fulfillment services through a single unified interface -- turning natural-language descriptions or napkin sketches into real, printed parts. Everything runs locally on your network. No cloud accounts, no telemetry, no subscriptions required.

## The Problem

The gap between "I need a part" and holding that part in your hand is filled with fragmented, incompatible tooling. Every printer brand speaks a different language -- OctoPrint, Klipper/Moonraker, Creality, Bambu Lab, Prusa Link, Elegoo, and USB-connected Marlin printers each have their own interfaces. Designing parts requires specialized CAD skills. Validating printability requires tribal knowledge. Managing even a small fleet means juggling multiple dashboards. Meanwhile, AI agents are increasingly capable of planning and executing multi-step physical tasks, but there's no safe, standardized way to connect them to design tools and real hardware.

## The Solution

Kiln acts as a universal intelligence layer between AI agents and physical fabrication. One interface, any idea, any printer. With <!-- KILN_MCP_CAPABILITY_COUNT:OLD --> 798 MCP capabilities and <!-- KILN_CLI_COUNT:OLD --> 220 CLI commands, agents have everything they need to go from concept to physical object.

**From idea to object.** Kiln gives agents multiple paths to turn a thought into a physical part:

- **Text-to-3D generation.** Describe what you need in plain language. Kiln routes to the best generation backend -- Gemini Deep Think for precise parametric geometry via OpenSCAD, Meshy, Tripo3D, or Stability AI for organic shapes.

- **Sketch-to-3D generation.** Draw it on a napkin. Gemini Deep Think interprets rough sketches and converts them into precise, printable geometry.

- **Design Intelligence.** A knowledge base of 25 FDM materials with full engineering properties, 45 brand-specific filament profiles, and 18 proven design templates informs every design decision -- from material selection to structural reinforcement to joint design.

- **Printability Engine.** Before anything reaches a slicer, Kiln runs 7-dimension printability analysis: overhangs, thin walls, bridging, bed adhesion, supports, warping, and thermal stress. The gate fires on every print path -- direct slice-and-print, AI-generated meshes, smart-retry-after-failure, and benchmarks -- and auto-repairs non-manifold geometry before slicing. Problems are caught and fixed in the design phase, not after a failed print.

- **Design DNA.** Every generated model preserves its parametric source code alongside the cached mesh. Need to tweak a dimension? The agent modifies the source and regenerates -- no starting from scratch.

**Three ways to print.** Once a design is ready, Kiln routes it through three co-equal paths:

1. **Your printers.** Control OctoPrint, Moonraker (Klipper), Creality printers with reachable local Moonraker, Bambu Lab, Prusa Link, Elegoo, or any Marlin/RepRap printer via Direct USB on your local network -- or remotely via Bambu Cloud. Kiln handles the protocol translation.

2. **Search 3rd-party marketplaces.** Find existing models across Thingiverse, MyMiniFactory, and Cults3D. Download, slice, and print -- or use them as starting points for custom designs.

3. **Route to fulfillment providers.** Send jobs to professional manufacturing services. Craftcloud aggregates quotes from 150+ print services worldwide across FDM, SLA, SLS, MJF, and metal (DMLS). No printer required.

An agent can prototype on your desk printer, send the production version to Craftcloud for SLS nylon, and iterate the design -- all from the same conversation.

**Beyond printing:**

- **Multi-part assembly.** Split large designs into printable sections with clearance validation, joint detection, tolerance stacking analysis, and split planning (10 parts free). With Kiln Pro (https://kiln3d.com/pricing), tolerance stacking gets engineering-grade chain analysis with per-printer offsets auto-imported from your slicer profile (OrcaSlicer, Bambu Studio, PrusaSlicer), plus printable PDF assembly manuals with a bill of materials, step renders, verification gates, and optional 3MF embedding.

- **Cost estimation.** Material cost, electricity, and time estimates with smart recommendations -- before you commit to a print.

- **Fleet management.** A priority job queue routes work across multiple printers, favoring the machine with the best track record for each material and geometry. Batch production, scheduling, and cross-printer learning come built in.

- **Vision monitoring.** During a print, the agent analyzes webcam snapshots using its own vision capabilities to detect failures early -- spaghetti, layer shifts, adhesion problems -- and decides whether to pause or cancel.

- **Adaptive slicing.** Analyzes model geometry to vary layer height per region -- thinner layers for fine detail, thicker for flat surfaces -- saving print time without sacrificing quality.

- **Multi-color printing.** Automatic AMS slot detection, color-split generation, and G-code merging for printers with filament changers.

- **Print recovery.** Automatic root cause analysis when prints fail, guided recovery workflows, and the ability to retry with corrective settings applied.

- **Cloud sync.** Printer settings, print history, and agent knowledge can stay synchronized across devices when kiln-pro is installed.

- **Webhooks.** Notifications for print events enable integration with external monitoring, alerting, and automation systems.

- **Structural analysis.** Load-bearing estimation and design reinforcement recommendations help agents create parts that actually survive real-world use.

## Safety

Agents are powerful, but they shouldn't be trusted blindly with physical hardware. Kiln enforces safety at the protocol level -- not as an afterthought, but as a core design constraint.

Before any print starts, Kiln runs pre-flight checks: validating temperatures against per-printer limits, scanning G-code for dangerous commands, confirming the printer is in a safe state, and requiring a preview-confirmation token for new non-resume print starts. These checks cannot be bypassed by the agent. 44 named safety profiles cover popular printer models with hardware-specific limits. A background watchdog auto-cools idle heaters after 30 minutes to prevent fire hazards. Real-time G-code interception lets agents monitor and modify printer commands on the fly -- blocking unsafe operations before they reach the hardware.

## How It Works

```
You (or your agent) --> Kiln --> Design Intelligence (materials, patterns, templates)
                                 Generation (Gemini, Meshy, Tripo3D, Stability, OpenSCAD)
                                 Printability Analysis (7-dimension validation)
                                 Your Printers (OctoPrint, Moonraker, Creality, Bambu, Elegoo, Prusa, Direct USB)
                                 Fulfillment Providers (Craftcloud)
```

Kiln uses the Model Context Protocol (MCP), an open standard for connecting AI agents to external tools. Any MCP-compatible agent can talk to Kiln natively. There's also a full CLI for scripts and operators; when kiln-pro is installed or a machine is paired, Kiln can expose the same tool surface through REST for custom integrations.

## Business Model

Local printing with Kiln is free and always will be. The core infrastructure is released under the AGPL-3.0 license. Commercial licensing is available for companies that need proprietary use.

Revenue comes from optional services:

- **Free tier** -- Unlimited local printing, slicing, marketplace search, safety profiles, a 10-job queue, one printer, personal/non-commercial use, design intelligence, printability analysis, and local linear design history.
- **Pro ($49/month)** -- Everything in Free plus one-printer personal use, unlimited queue depth, unlimited assembly parts, auto-generated assembly manuals, Git-for-3D branch/merge/cherry-pick/signed releases, solo cloud sync, product templates, procedural textures, unlimited non-QR decorations, mid-print modification, failure recovery, print resume, design provenance, manual speed control, and print learning. Annual: $39/mo.
- **Business ($199/month)** -- Everything in Pro plus commercial use, 3 printers, 3 team seats, multi-printer fleet management, cross-printer learning, co-branded and multilingual assembly manuals, QR code generation, layer-scheduled speed adjustments, team pull requests, approval gates, federated design sharing, unlimited fulfillment orders, material compliance tracking, priority support, custom safety profiles, and webhooks. Annual: $159/mo.
- **Enterprise (custom)** -- Everything in Business plus large fleets, unlimited team seats, SSO via OIDC/SAML, SCIM, RBAC, full audit trail with long-term retention, lockable safety profiles, encrypted G-code at rest, white-label manuals, buyer-side authenticity verification, 99.9% uptime SLA, dedicated CSM, on-prem/VPC deployment, custom integrations, and procurement terms.

Outsourced manufacturing orders carry a 5% orchestration fee (first 3 per month are free, with a $0.25 minimum and $200 maximum per order). Fulfillment providers remain merchant of record.

Crypto donations are accepted at kiln3d.sol (Solana) and kiln3d.eth (Ethereum).

## Get Started

Install Kiln with a single command:

```
git clone https://github.com/codeofaxel/Kiln.git ~/.kiln/src && ~/.kiln/src/install.sh
```

Full documentation, CLI reference, and the technical whitepaper are available in the project repository at [github.com/codeofaxel/Kiln](https://github.com/codeofaxel/Kiln).

---

Kiln is open-source infrastructure released under the AGPL-3.0 License. Version 1.0.0, April 2026.
Kiln is a project of Hadron Labs Inc.
