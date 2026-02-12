# Kiln

### Litepaper -- February 2026

---

Kiln is free, open-source infrastructure that lets AI agents control 3D printers. It sits between any AI -- Claude, GPT, Llama, or others -- and your printers, giving the agent the ability to find models, slice files, start prints, monitor progress, and manage an entire fleet. Everything runs locally on your network. No cloud accounts, no telemetry, no subscriptions required.

## The Problem

3D printing is powerful, but the software side hasn't kept up. Every printer brand speaks a different language -- OctoPrint, Klipper, Bambu Lab, and Prusa Connect each have their own incompatible interfaces. Managing even a small fleet means juggling multiple dashboards, manually queuing jobs, and babysitting prints. Meanwhile, AI agents are increasingly capable of planning and executing multi-step physical tasks, but there's no safe, standardized way to connect them to real hardware.

## The Solution

Kiln acts as a universal translator between AI agents and 3D printers. One interface, any printer, any AI.

- **Unified printer control.** Kiln works with OctoPrint, Moonraker (Klipper), Bambu Lab, and Prusa Connect. Your agent doesn't need to know which firmware a printer runs -- Kiln handles the translation.

- **Full print workflow.** An agent can search for 3D models across Thingiverse, MyMiniFactory, and Cults3D; generate models from a text description; slice them into printer-ready files; upload to a printer; start, monitor, and cancel prints -- all without human intervention.

- **Fleet management.** A priority job queue routes work across multiple printers, favoring the machine with the best track record for each material and file type. Batch production, scheduling, and cross-printer learning come built in.

- **Vision monitoring.** During a print, the agent can analyze webcam snapshots using its own vision capabilities to detect failures early -- spaghetti, layer shifts, adhesion problems -- and decide whether to pause or cancel.

- **Outsourced manufacturing.** When your local printers can't handle a job (wrong material, at capacity), Kiln can broker the order to services like Craftcloud, Shapeways, or Sculpteo, or route it through the 3DOS distributed manufacturing network.

## How It Works

The flow is simple:

```
You (or your agent) --> Kiln --> Your Printers
```

Kiln uses the Model Context Protocol (MCP), an open standard for connecting AI agents to external tools. Any MCP-compatible agent can talk to Kiln natively. For those who prefer a terminal, there's also a full command-line interface with over 50 commands and a REST API for custom integrations.

All communication between Kiln and your printers happens over your local network. Your print data never leaves your machines.

## Safety

Agents are powerful, but they shouldn't be trusted blindly with physical hardware. Kiln enforces safety at the protocol level -- not as an afterthought, but as a core design constraint.

Before any print starts, Kiln runs pre-flight checks: validating temperatures against per-printer limits, scanning G-code for dangerous commands, and confirming the printer is in a safe state. These checks cannot be bypassed by the agent, even if explicitly instructed to skip them. A background watchdog also auto-cools idle heaters after 30 minutes to prevent fire hazards.

The agent operates within strict guardrails. It has the autonomy to be useful, but not enough rope to cause damage.

## Beyond 3D Printing

Kiln's adapter architecture isn't limited to FDM printers. The same pattern generalizes to CNC routers, laser cutters, and resin (SLA) printers. The system already includes forward-compatible hooks for these device types, and third-party plugins can extend Kiln to new hardware via Python entry points.

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
