<p align="center">
  <img src="assets/kiln-logo-dark.svg" alt="Kiln" width="200">
</p>

# Kiln

### Litepaper -- March 2026

> Describe it or draw it. Kiln makes it real.

---

Kiln is the intelligence layer between an idea and a physical object. It connects AI agents to 3D printers, design tools, and fulfillment services through a single unified interface -- turning natural-language descriptions or napkin sketches into real, printed parts. Everything runs locally on your network. No cloud accounts, no telemetry, no subscriptions required.

## The Problem

The gap between "I need a part" and holding that part in your hand is filled with fragmented, incompatible software. Every printer brand speaks a different language -- OctoPrint, Klipper, Bambu Lab, Elegoo, and Prusa Link each have their own interfaces. Designing parts requires specialized CAD skills. Validating printability requires tribal knowledge. Managing even a small fleet means juggling multiple dashboards. Meanwhile, AI agents are increasingly capable of planning and executing multi-step physical tasks, but there's no safe, standardized way to connect them to design tools and real hardware.

## The Solution

Kiln acts as a universal intelligence layer between AI agents and physical fabrication. One interface, any idea, any printer. With 461+ MCP tools and 136 CLI commands, agents have everything they need to go from concept to physical object.

**From idea to object.** Kiln gives agents multiple paths to turn a thought into a physical part:

- **Text-to-3D generation.** Describe what you need in plain language. Kiln routes to the best generation backend -- Gemini Deep Think for precise parametric geometry via OpenSCAD, Meshy, Tripo3D, or Stability AI for organic shapes. An OpenSCAD parametric catalog with 14 component types lets agents compose complex assemblies from proven primitives.

- **Sketch-to-3D generation.** Draw it on a napkin. Gemini Deep Think interprets rough sketches and converts them into precise, printable geometry.

- **Design Intelligence.** A knowledge base of 52 materials, 20 design patterns, and 70+ templates informs every design decision -- from material selection to structural reinforcement to joint design.

- **Printability Engine.** Before anything reaches a slicer, Kiln runs 7-dimension printability analysis: overhangs, thin walls, bridging, bed adhesion, supports, warping, and thermal stress. Problems are caught and fixed in the design phase, not after a failed print.

- **Design DNA.** Every generated model preserves its parametric source code alongside the cached mesh. Need to tweak a dimension? The agent modifies the source and regenerates -- no starting from scratch.

**Three ways to print.** Once a design is ready, Kiln routes it through three co-equal paths:

1. **Your printers.** Control OctoPrint, Moonraker (Klipper), Bambu Lab, Elegoo, or Prusa Link machines on your local network -- or remotely via Bambu Cloud. Kiln handles the protocol translation.

2. **Search 3rd-party marketplaces.** Find existing models across Thingiverse, MyMiniFactory, and Cults3D. Download, slice, and print -- or use them as starting points for custom designs.

3. **Route to fulfillment providers.** Send jobs to professional manufacturing services. Craftcloud aggregates quotes from 150+ print services worldwide across FDM, SLA, SLS, MJF, and metal (DMLS). No printer required.

An agent can prototype on your desk printer, send the production version to Craftcloud for SLS nylon, and iterate the design -- all from the same conversation.

**Beyond printing:**

- **Multi-part assembly.** Split large designs into printable sections with clearance validation, joint detection, tolerance stacking analysis, and split planning (10 parts free).

- **Cost estimation.** Material cost, electricity, and time estimates with smart recommendations -- before you commit to a print.

- **Fleet management.** A priority job queue routes work across multiple printers, favoring the machine with the best track record for each material and geometry. Batch production, scheduling, and cross-printer learning come built in.

- **Vision monitoring.** During a print, the agent analyzes webcam snapshots using its own vision capabilities to detect failures early -- spaghetti, layer shifts, adhesion problems -- and decides whether to pause or cancel.

- **Structural analysis.** Load-bearing estimation and design reinforcement recommendations help agents create parts that actually survive real-world use.

## Safety

Agents are powerful, but they shouldn't be trusted blindly with physical hardware. Kiln enforces safety at the protocol level -- not as an afterthought, but as a core design constraint.

Before any print starts, Kiln runs pre-flight checks: validating temperatures against per-printer limits, scanning G-code for dangerous commands, and confirming the printer is in a safe state. These checks cannot be bypassed by the agent. 29 safety profiles cover popular printer models with hardware-specific limits. A background watchdog auto-cools idle heaters after 30 minutes to prevent fire hazards.

## How It Works

```
You (or your agent) --> Kiln --> Design Intelligence (materials, patterns, templates)
                                 Generation (Gemini, Meshy, Tripo3D, Stability, OpenSCAD)
                                 Printability Analysis (7-dimension validation)
                                 Your Printers (OctoPrint, Moonraker, Bambu, Elegoo, Prusa)
                                 Fulfillment Providers (Craftcloud)
```

Kiln uses the Model Context Protocol (MCP), an open standard for connecting AI agents to external tools. Any MCP-compatible agent can talk to Kiln natively. There's also a full CLI with 136 commands and a REST API for custom integrations.

## Business Model

Local printing with Kiln is free and always will be. The core infrastructure is released under the AGPL-3.0 license. Commercial licensing is available for companies that need proprietary use.

Revenue comes from optional services:

- **Free tier** -- All local printing features, up to 2 printers and a 10-job queue. No cost, no account required.
- **Pro ($29/month)** -- Unlimited printers, fleet orchestration, analytics, and cloud sync. Annual: $23/mo.
- **Business ($99/month)** -- Up to 50 printers, 5 team seats, fulfillment brokering, shared hosted MCP server, priority support, custom safety profiles. Annual: $79/mo.
- **Enterprise (from $499/month)** -- Unlimited printers (20 included, $15/mo each additional) and unlimited team seats, dedicated single-tenant MCP server, on-premises deployment via Kubernetes and Helm (air-gapped support included), single sign-on via OIDC and SAML, role-based access control, full audit trail with JSON/CSV export, lockable safety profiles, encrypted G-code at rest, 99.9% uptime SLA, and dedicated support channel with onboarding. Annual: $399/mo.

Outsourced manufacturing orders carry a 5% orchestration fee (first 3 per month are free, with a $0.25 minimum and $200 maximum per order). Fulfillment providers remain merchant of record.

Crypto donations are accepted at kiln3d.sol (Solana) and kiln3d.eth (Ethereum).

## Get Started

Install Kiln with a single command:

```
git clone https://github.com/codeofaxel/Kiln.git ~/.kiln/src && ~/.kiln/src/install.sh
```

Full documentation, CLI reference, and the technical whitepaper are available in the project repository at [github.com/codeofaxel/Kiln](https://github.com/codeofaxel/Kiln).

---

Kiln is open-source infrastructure released under the AGPL-3.0 License. Version 0.4.2, March 2026.
Kiln is a project of Hadron Labs Inc.
