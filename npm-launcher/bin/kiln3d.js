#!/usr/bin/env node
"use strict";

/*
 * npx kiln3d — thin launcher for Kiln, the open-source MCP server for 3D printing.
 *
 * The real program is the Python package `kiln3d` on PyPI. This launcher just
 * finds a Python runner (uv/uvx first, then pipx) and hands off, passing
 * through every argument. Because it always runs the LATEST published
 * `kiln3d`, this npm package does not need re-publishing when Kiln ships a new
 * version — a `pip`/`uvx` release reaches npx users automatically.
 *
 * With no arguments it defaults to `serve` (start the MCP server), so a bare
 * `npx kiln3d` in an MCP client config just works.
 */

const { spawnSync } = require("node:child_process");

const passthrough = process.argv.slice(2);
const args = passthrough.length ? passthrough : ["serve"];

function exists(bin) {
  const probe = process.platform === "win32" ? "where" : "which";
  return spawnSync(probe, [bin], { stdio: "ignore" }).status === 0;
}

function handOff(bin, binArgs) {
  const result = spawnSync(bin, binArgs, { stdio: "inherit" });
  process.exit(result.status == null ? 1 : result.status);
}

if (exists("uvx")) {
  handOff("uvx", ["kiln3d", ...args]);
} else if (exists("pipx")) {
  handOff("pipx", ["run", "kiln3d", ...args]);
} else {
  process.stderr.write(
    [
      "",
      "Kiln runs on Python and needs `uv` (recommended) or `pipx` to launch.",
      "",
      "  Install uv (macOS/Linux):  curl -LsSf https://astral.sh/uv/install.sh | sh",
      '  Install uv (Windows):      powershell -c "irm https://astral.sh/uv/install.ps1 | iex"',
      "",
      "Then re-run:                 npx kiln3d",
      "",
      "Full install guide: https://kiln3d.com/install",
      "",
    ].join("\n")
  );
  process.exit(1);
}
