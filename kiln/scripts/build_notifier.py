#!/usr/bin/env python3
"""Build what shows Kiln's print codes as Kiln: ``Kiln.app`` and ``Kiln.png``.

Writes ``kiln/src/kiln/data/notifier/Kiln.app`` and, for Windows toasts,
``Kiln.png`` beside it.  The app is the native helper built from
``kiln/native/notifier/main.swift`` for both Mac chip families, its icon, and
its Info.plist, signed ad hoc.  macOS lets an app post a notification only
after the person allows it, and only a native app can ask; a banner posted
through the system script runner arrives as "Script Editor" instead.  The
bundle is generated: edit this script or the Swift source, never the output.

The icon is the brand's small-size mark (``docs/assets/kiln-favicon-32.svg``):
the kiln outline with the orange band, on a dark tile lit from above.  The
tile follows the macOS app-icon grid (824 of 1024, corner radius 185), the
mark spans 62% of it, and every stroke is floored so the outline still reads
at 16 pixels.  Corners are mitred like the logo's.

Needs macOS with Xcode's command-line tools (``swiftc``, ``lipo``,
``iconutil``, ``codesign``) and Pillow.  Usage::

    python3 kiln/scripts/build_notifier.py
"""

from __future__ import annotations

import itertools
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

#: The ``kiln/`` package directory (this script lives in ``kiln/scripts``).
PACKAGE = Path(__file__).resolve().parent.parent
SOURCE = PACKAGE / "native" / "notifier" / "main.swift"
APP = PACKAGE / "src" / "kiln" / "data" / "notifier" / "Kiln.app"
#: The same mark as a picture, for Windows: the icon its toasts show for Kiln.
PNG = APP.parent / "Kiln.png"
PNG_SIZE = 256
EXECUTABLE = "kiln-notifier"
BUNDLE_ID = "com.kiln3d.notifier"
#: Bumped when the helper's behaviour changes, so an installed copy is replaced.
VERSION = "1"
#: The oldest macOS the helper runs on (UserNotifications on the Mac: 10.14).
MIN_MACOS = "12.0"
TARGETS = ("arm64-apple-macos12.0", "x86_64-apple-macos12.0")

#: The brand mark in the 32-unit box of kiln-favicon-32.svg.
OUTLINE = [(12.76, 7.0), (7.0, 7.0), (1.6, 25.0), (30.4, 25.0), (25.0, 7.0), (19.24, 7.0)]
OUTLINE_STROKE = 1.26
BAND = [(3.72, 17.94), (3.26, 19.46), (28.74, 19.46), (28.28, 17.94)]
MARK_LEFT, MARK_RIGHT, MARK_TOP, MARK_BOTTOM = 1.6, 30.4, 7.0, 25.0

#: The tile is lit from above, like the system's own dark icons: a gentle
#: fall from TILE_TOP to TILE_BOTTOM, with a hairline RIM so the tile keeps
#: its edge on a dark banner.
TILE_TOP = (40, 40, 40)
TILE_BOTTOM = (20, 20, 20)
RIM = (255, 255, 255, 30)
#: Below this size the rim is noise, not an edge.
RIM_FROM_PX = 48
LINE = (204, 204, 204, 255)
ORANGE = (255, 107, 43, 255)

#: macOS app-icon grid, in 1024ths of the canvas.
BODY_INSET = 100 / 1024
CORNER = 185 / 1024
MARK_SHARE = 0.62
#: The thinnest a stroke may be at the size it is shown, in pixels.
MIN_STROKE_PX = 1.35
SUPERSAMPLE = 4

#: (file name in the iconset, pixel size) — the set iconutil expects.
ICONSET = [
    ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
]


def _intersect(p, d, q, e):
    """Where the line through *p* along *d* meets the line through *q* along *e*."""
    cross = d[0] * e[1] - d[1] * e[0]
    if abs(cross) < 1e-9:
        return (q[0], q[1])
    t = ((q[0] - p[0]) * e[1] - (q[1] - p[1]) * e[0]) / cross
    return (p[0] + t * d[0], p[1] + t * d[1])


def _stroke_polygon(points, half):
    """The outline of an open polyline stroked *half* each side, mitred at
    every joint and cut square at both ends — the logo's own stroke."""
    segs = []
    for (x0, y0), (x1, y1) in itertools.pairwise(points):
        dx, dy = x1 - x0, y1 - y0
        length = (dx * dx + dy * dy) ** 0.5
        d = (dx / length, dy / length)
        segs.append(((x0, y0), (x1, y1), d, (-d[1], d[0])))

    def side(sign):
        path = []
        for i, (a, b, d, n) in enumerate(segs):
            off = (sign * half * n[0], sign * half * n[1])
            start = (a[0] + off[0], a[1] + off[1])
            if i == 0:
                path.append(start)
            else:
                pa, _pb, pd, pn = segs[i - 1]
                prev = (pa[0] + sign * half * pn[0], pa[1] + sign * half * pn[1])
                path.append(_intersect(prev, pd, start, d))
            if i == len(segs) - 1:
                path.append((b[0] + off[0], b[1] + off[1]))
        return path

    return side(1) + list(reversed(side(-1)))


def draw(size: int) -> Image.Image:
    canvas = size * SUPERSAMPLE
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    pen = ImageDraw.Draw(img)
    inset = canvas * BODY_INSET
    box = [inset, inset, canvas - inset, canvas - inset]
    mask = Image.new("L", (canvas, canvas), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=canvas * CORNER, fill=255)
    fall = Image.new("RGBA", (1, canvas))
    span = max(1.0, canvas - 2 * inset)
    for y in range(canvas):
        t = min(1.0, max(0.0, (y - inset) / span))
        fall.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(TILE_TOP, TILE_BOTTOM, strict=True)) + (255,))
    img.paste(fall.resize((canvas, canvas)), (0, 0), mask)
    if size >= RIM_FROM_PX:
        pen.rounded_rectangle(box, radius=canvas * CORNER, outline=RIM, width=max(1, round(SUPERSAMPLE * size / 512)))
    unit = (canvas - 2 * inset) * MARK_SHARE / (MARK_RIGHT - MARK_LEFT)
    cx = (MARK_LEFT + MARK_RIGHT) / 2
    cy = (MARK_TOP + MARK_BOTTOM) / 2

    def at(x, y):
        return (canvas / 2 + (x - cx) * unit, canvas / 2 + (y - cy) * unit)

    floor = MIN_STROKE_PX * SUPERSAMPLE
    # The orange band, thickened about its middle when it would be thinner
    # than a readable line at this size.
    band = [at(x, y) for x, y in BAND]
    top, bottom = band[0][1], band[1][1]
    if bottom - top < floor:
        mid, grow = (top + bottom) / 2, floor / 2
        band = [(band[0][0], mid - grow), (band[1][0], mid + grow), (band[2][0], mid + grow), (band[3][0], mid - grow)]
    pen.polygon(band, fill=ORANGE)
    half = max(OUTLINE_STROKE * unit, floor) / 2
    pen.polygon(_stroke_polygon([at(x, y) for x, y in OUTLINE], half), fill=LINE)
    return img.resize((size, size), Image.LANCZOS)


def _run(*argv: str) -> None:
    subprocess.run(list(argv), check=True)


def _icns(dest: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "Kiln.iconset"
        iconset.mkdir()
        for name, size in ICONSET:
            draw(size).save(iconset / name)
        _run("iconutil", "-c", "icns", str(iconset), "-o", str(dest))


def _binary(dest: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        slices = []
        for target in TARGETS:
            out = Path(tmp) / target
            _run("xcrun", "swiftc", "-O", "-target", target, str(SOURCE), "-o", str(out))
            slices.append(str(out))
        _run("lipo", "-create", *slices, "-output", str(dest))


def _info_plist() -> dict:
    return {
        "CFBundleName": "Kiln",
        "CFBundleDisplayName": "Kiln",
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": EXECUTABLE,
        "CFBundleIconFile": "Kiln",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": MIN_MACOS,
        # A helper, not an app a person opens: no Dock icon, no menu bar.
        "LSUIElement": True,
    }


def main() -> int:
    missing = [tool for tool in ("xcrun", "lipo", "iconutil", "codesign") if not shutil.which(tool)]
    if missing:
        print("needs macOS with Xcode's command-line tools; missing: " + ", ".join(missing), file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "Kiln.app"
        (staged / "Contents" / "MacOS").mkdir(parents=True)
        (staged / "Contents" / "Resources").mkdir(parents=True)
        _binary(staged / "Contents" / "MacOS" / EXECUTABLE)
        _icns(staged / "Contents" / "Resources" / "Kiln.icns")
        with open(staged / "Contents" / "Info.plist", "wb") as fh:
            plistlib.dump(_info_plist(), fh)
        _run("codesign", "--force", "--sign", "-", "--identifier", BUNDLE_ID, str(staged))
        _run("codesign", "--verify", "--strict", str(staged))
        if APP.exists():
            shutil.rmtree(APP)
        APP.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(staged, APP, symlinks=True)
    draw(PNG_SIZE).save(PNG)
    for stale in (APP.parent / "Kiln.icns",):
        if stale.exists():
            stale.unlink()
    size = sum(f.stat().st_size for f in APP.rglob("*") if f.is_file())
    print(f"wrote {APP.relative_to(PACKAGE)} ({size} bytes) and {PNG.relative_to(PACKAGE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
