<p align="center">
  <img src="assets/kiln-logo-dark.svg" alt="Kiln" width="200">
</p>

# Kiln

### Litepaper -- February 2026

---

Kiln is free, open-source infrastructure that lets AI agents control 3D printers. It sits between any AI -- Claude, GPT, Llama, or others -- and your printers, giving the agent the ability to find models, slice files, start prints, monitor progress, and manage an entire fleet. Everything runs locally on your network. No cloud accounts, no telemetry, no subscriptions required.

## The Problem

3D printing is powerful, but the software side hasn't kept up. Every printer brand speaks a different language -- OctoPrint, Klipper, Bambu Lab, Elegoo, and Prusa Link each have their own incompatible interfaces. Managing even a small fleet means juggling multiple dashboards, manually queuing jobs, and babysitting prints. Meanwhile, AI agents are increasingly capable of planning and executing multi-step physical tasks, but there's no safe, standardized way to connect them to real hardware.

## The Solution

Kiln acts as a universal translator between AI agents and 3D printers. One interface, any printer, any AI.

**Three ways to print.** Kiln gives agents three co-equal paths to turn a digital file into a physical object -- and they can mix and match within a single workflow:

1. **Your printers.** Control OctoPrint, Moonraker (Klipper), Bambu Lab, Elegoo (Centauri Carbon, Saturn, Mars via SDCP; Neptune 4 and OrangeStorm Giga via Moonraker), or Prusa Link machines on your local network -- or remotely via Bambu Cloud. Your agent doesn't need to know which firmware a printer runs; Kiln handles the translation.

2. **Fulfillment centers.** Send jobs to professional manufacturing services. Craftcloud aggregates quotes from over 150 print services worldwide across FDM, SLA, SLS, MJF, and metal (DMLS) — works out of the box with no API key required. Sculpteo *(API access required — in partner onboarding)* provides direct access to 75+ materials with professional finishing. No printer required -- but printer owners use this too for overflow, specialty materials, or production-quality parts.

3. **Distributed network.** *(Coming soon.)* Route jobs to decentralized peer-to-peer printer networks, or register your own printer to earn revenue from incoming jobs. Agents search the network by material, location, and machine type.

An agent can start a PLA prototype on your desk printer, send the SLA version to Craftcloud, and route overflow to a distributed printer network -- all from the same conversation.

**Beyond printing:**

- **Full print workflow.** An agent can search for 3D models across MyMiniFactory, Cults3D, and other marketplaces; slice them into printer-ready files; upload to a printer; start, monitor, and cancel prints -- all without human intervention.

- **Agent-designed models.** Kiln includes two model generation paths: cloud-based AI text-to-3D (via Meshy) that turns a natural-language description into a printable mesh, and local parametric generation (via OpenSCAD) where the agent writes code to produce precise, dimensionally accurate parts. Generated models are automatically validated for printability -- manifold checks, wall thickness, bounding box -- before they ever reach a slicer. An agent can go from "I need a 40mm fan duct with a 30-degree deflection" to a sliced, printing G-code file with no human in the loop.

- **Fleet management.** A priority job queue routes work across multiple printers, favoring the machine with the best track record for each material and file type. Batch production, scheduling, and cross-printer learning come built in.

- **Vision monitoring.** During a print, the agent can analyze webcam snapshots using its own vision capabilities to detect failures early -- spaghetti, layer shifts, adhesion problems -- and decide whether to pause or cancel.

## How It Works

```
You (or your agent) --> Kiln --> 🖨️ Your Printers  (local or remote via Bambu Cloud)
                                 🏭 Fulfillment     (Craftcloud · Sculpteo)
                                 🌐 Distributed Network (coming soon)
```

Kiln uses the Model Context Protocol (MCP), an open standard for connecting AI agents to external tools. Any MCP-compatible agent can talk to Kiln natively. For those who prefer a terminal, there's also a full command-line interface with over 80 commands and a REST API for custom integrations.

All three printing modes use the same interface. An agent doesn't need to know whether a job is printing on your desk, at a factory in Germany, or on someone's Bambu or Elegoo in Texas -- Kiln abstracts the routing. Communication with local printers stays on your network; fulfillment and network jobs use HTTPS to the respective provider APIs.

## Safety

Agents are powerful, but they shouldn't be trusted blindly with physical hardware. Kiln enforces safety at the protocol level -- not as an afterthought, but as a core design constraint.

Before any print starts, Kiln runs pre-flight checks: validating temperatures against per-printer limits, scanning G-code for dangerous commands, and confirming the printer is in a safe state. These checks cannot be bypassed by the agent, even if explicitly instructed to skip them. A background watchdog also auto-cools idle heaters after 30 minutes to prevent fire hazards.

The agent operates within strict guardrails. It has the autonomy to be useful, but not enough rope to cause damage.

## Business Model

Local printing with Kiln is free and always will be. The core software is released under the MIT license.

Revenue comes from optional services:

- **Free tier** -- All local printing features, up to 2 printers and a 10-job queue. No cost, no account required.
- **Pro ($29/month)** -- Unlimited printers, fleet orchestration, analytics, and cloud sync for remote access.
- **Business ($99/month)** -- Fulfillment brokering to outside manufacturers, hosted deployment, and priority support.

Outsourced manufacturing orders carry a 5% platform fee (first 5 per month are free, with a $0.25 minimum and $200 maximum per order).

Crypto donations are also accepted at kiln3d.sol (Solana) and kiln3d.eth (Ethereum).

## Get Started

Install Kiln with a single command:

```
git clone https://github.com/codeofaxel/Kiln.git ~/.kiln/src && ~/.kiln/src/install.sh
```

Full documentation, CLI reference, and the technical whitepaper are available in the project repository at [github.com/codeofaxel/Kiln](https://github.com/codeofaxel/Kiln).

---

Kiln is open-source software released under the MIT License. Version 0.1.0, February 2026.
