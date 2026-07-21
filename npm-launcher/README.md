# kiln3d (npm launcher)

**Kiln is the open-source MCP server for 3D printing** — it lets AI agents
(Claude, Codex, or any MCP client) design, slice, print, monitor, and recover
real 3D prints on Bambu Lab, Creality, Prusa, Elegoo, Klipper/Moonraker,
OctoPrint, and more.

This npm package is a thin **launcher**. The actual server is the Python
package [`kiln3d`](https://pypi.org/project/kiln3d/); this just runs it for you
via [`uv`](https://docs.astral.sh/uv/) (or `pipx`), so it works from a Node/npx
setup:

```bash
npx kiln3d
```

That launches the Kiln MCP server (`serve`). Any arguments are passed straight
through, e.g. `npx kiln3d --help`.

## Requirements

Kiln is a Python program, so you need one of:

- **uv** (recommended): `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **pipx**: `pipx` on your PATH

If neither is present, the launcher prints install instructions and exits.

## Why a launcher instead of a rewrite

The launcher always runs the **latest** published `kiln3d` from PyPI, so new
Kiln releases reach `npx kiln3d` users automatically — this npm package rarely
needs a new version.

## Prefer a native install?

- Python: `uvx kiln3d` (zero-install run) or `pip install kiln3d`
- Full guide: <https://kiln3d.com/install>

License: AGPL-3.0. Source: <https://github.com/codeofaxel/Kiln>
