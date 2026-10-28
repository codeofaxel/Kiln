# Kiln for Hermes Agent

Design a part in a Hermes chat, turn it over in 3D, check it will print, slice
it, and run it on your own 3D printer. Works with Bambu Lab, Creality, Prusa,
Elegoo, Klipper/Moonraker, OctoPrint, Duet and USB-connected printers, and is
still useful with no printer at all.

## Install

```bash
hermes plugins install kiln
```

Then start a new Hermes chat and describe a part.

## What you need

- [uv](https://astral.sh/uv). The package starts Kiln with `uvx`, at the Kiln
  release it was reviewed with.
- To slice: PrusaSlicer, OrcaSlicer or Bambu Studio. Kiln says which is missing.
- To print: a printer this computer can reach on your network or by USB.
- To make parts from a description: OpenSCAD.

## What it does on your computer

- Talks to your printers on your local network or by USB, and starts a print
  only after you approve it.
- Runs your slicer and OpenSCAD as local programs.
- Keeps its settings in `~/.kiln`, including printer access codes and your Kiln
  sign-in if you sign in.
- Sends requests that need Kiln's servers, and the parts behind 3D-view links,
  to api.kiln3d.com. A free Kiln account works; paid plans add depth to some
  answers.
- Searches model sites only when you ask it to.
- Shares nothing on its own until you say yes. Kiln asks you once whether
  to share anonymous print results and a daily usage report;
  `uvx --from kiln3d kiln sharing on` or `off` changes your answer any time.
- Never updates itself. Updates come from `hermes plugins update kiln`.

Kiln asks before every print.

More: [kiln3d.com/hermes](https://kiln3d.com/hermes). Kiln is not affiliated
with or endorsed by Nous Research.
