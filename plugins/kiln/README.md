# Kiln — Claude Code plugin

Wires the full [Kiln](https://kiln3d.com) MCP server into Claude Code in one
command. Kiln lets an AI agent design a part, slice it, print it on a real
printer on your own network, monitor the camera, and recover from failures —
across Bambu Lab, Creality, Prusa, Elegoo, OctoPrint, Moonraker/Klipper, and
Direct USB.

## Install

```bash
claude plugin marketplace add codeofaxel/Kiln
claude plugin install kiln@kiln
```

Then start a Claude Code session — the Kiln tools are available immediately.

## Prerequisite: uv

The plugin launches Kiln with [`uv`](https://astral.sh/uv) (`uvx kiln3d serve`),
which fetches and runs `kiln3d` in an isolated environment using platform-native
wheels — no manual `pip install`, and Kiln stays up to date. Install uv once:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # macOS / Linux
# Windows: see https://astral.sh/uv
```

Local design generation additionally needs [OpenSCAD](https://openscad.org) on
your machine; everything else (printer control, slicing, marketplace search,
material intelligence) works without it.

## Why a plugin as well as the pip/uvx install?

Same server, one-command wiring. `uv tool install kiln3d` + `kiln install-mcp`
already sets Kiln up for Claude Code, Claude Desktop, and Codex. This plugin is
the marketplace-native path for Claude Code users who prefer
`claude plugin install` and versioned updates.

## Local-first, by design

Kiln runs on your machine and talks directly to printers on your network —
printer hosts, API keys, and the models you make stay on your device. Optional
account-backed features (sign-in, cloud sync, licensed Pro tools, professional
print fulfillment) reach Kiln's own services under the
[privacy policy](https://kiln3d.com/privacy). Questions: adam@kiln3d.com.

## The bundle

* `.claude-plugin/plugin.json` — plugin manifest (kept in lockstep with the
  `kiln3d` package version by `kiln/tests/test_version.py`).
* `.mcp.json` — the MCP server entry (`uvx kiln3d serve`).

The marketplace catalog that lists this plugin lives at the repo root:
`.claude-plugin/marketplace.json`.
