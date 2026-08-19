"""Stage-look still renders through a local headless browser.

WHAT THIS IS
------------
The beauty backend for :func:`kiln.model_visualizer.visualize_model`.
Kiln has exactly one calibrated "look" — the three.js stage the web
viewer, the inline conversation viewer, and the ``/view`` page all
render.  When this machine has a chromium-family browser and a cached
copy of that stage document, a still image is produced by *photographing
the stage itself*: load the document headlessly, hand it the mesh
payload, screenshot the settled frame.  The lighting recipe is never
re-implemented here — a still is a picture of the stage, so stills can
never drift from what users see in the viewer.

WHEN IT RUNS (and when it must not)
-----------------------------------
Everything here is best-effort and silent.  The OpenSCAD renderer in
``model_visualizer`` remains the canonical, always-available path; this
backend runs only when EVERY precondition holds:

* a chromium-family binary is discoverable (``KILN_STAGE_BROWSER``
  override, else Playwright's chrome-headless-shell; on non-macOS also
  Playwright's Chromium, a system Chrome/Chromium/Edge/Brave, or PATH —
  on macOS a full ``.app`` browser is never auto-picked, because
  launching one headlessly bounces a Dock icon; see
  ``_browser_candidates``) — never downloaded, never required;
* the cached stage document (``kiln.stage_cache``) is present AND
  still-capable (carries the ``__KILN_STILL__`` still-mode block — an
  older cached document simply means OpenSCAD until the next refresh);
* the mesh converts to a full-fidelity viewer payload within generous
  caps (a mesh too large to inline honestly falls back rather than
  shipping a downgraded ghost);
* every requested view screenshots successfully AND passes the
  blank-frame guard below.

Any miss returns ``None`` and the caller runs OpenSCAD exactly as it
always has.  ``KILN_NO_STAGE_STILLS=1`` opts out entirely.

THE BLANK-FRAME GUARD
---------------------
A headless browser that half-works is worse than one that is absent: it
exits 0 and writes a picture of an empty stage.  The stage's still mode
deliberately hides its own waiting/error cards so a failed render reads
as a near-empty frame, and this module refuses any frame whose largest
per-channel variation is below ``_MIN_STDDEV`` (an empty stage measures
~10; a real still measures 40+).  Per-CHANNEL, because a vivid dark
colour barely varies in brightness against the stage's dark background
— grading by luminance discarded a correct red render as blank.
Without Pillow the guard degrades to a bytes-floor heuristic — a blank
dark PNG compresses far below any real still.

All-or-nothing: if ANY view fails, every view is discarded and the
OpenSCAD path renders the full set — one result must not mix two looks.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: A caller-requested filament colour the stage can honour.  OpenSCAD also
#: accepts names ("red") and other spellings; the stage takes hex, so
#: anything else declines to the OpenSCAD path rather than rendering a
#: colour the caller did not ask for.
_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

#: Path override / opt-out knobs.  ``KILN_STAGE_DOC`` points at a local
#: stage document (tests and stage development); absent, the cached copy
#: from :mod:`kiln.stage_cache` is used.
_BROWSER_ENV = "KILN_STAGE_BROWSER"
_DOC_ENV = "KILN_STAGE_DOC"
_OPT_OUT_ENV = "KILN_NO_STAGE_STILLS"

#: The still-mode marker in the stage document.  A cached document from
#: before still mode shipped renders interactively but cannot pose or
#: self-deliver a payload — detect that HERE and fall back, instead of
#: screenshotting a stage that never got the memo.
_STILL_MARKER = "__KILN_STILL__"

#: Colour support arrived one release AFTER still mode, so a cached stage
#: document can honestly support stills and ignore a colour — and ignoring
#: it means rendering the caller's red part in default grey and calling it
#: done.  A wrong colour is worse than an old look, so a colour request
#: against a stage that cannot honour it declines to OpenSCAD.
_STILL_COLOR_MARKER = "STILL.color"

#: Payload caps for a LOCAL render.  The inline-conversation caps
#: (80k triangles) exist to protect chat transport and model context;
#: a headless browser on this machine has neither constraint.  Meshes
#: beyond even these caps fall back to OpenSCAD rather than rendering
#: a silently decimated ghost of the user's part.
_STILL_MAX_TRIANGLES = 600_000
_STILL_MAX_BYTES = 64 * 1024 * 1024

#: Virtual-time budget handed to the browser.  Virtual time fast-forwards
#: timers and animation frames deterministically, so this is generous
#: headroom, not wall-clock waiting.
_VIRTUAL_TIME_MS = 9_000

#: Wall-clock ceiling per view — SwiftShader on a big mesh is CPU-bound
#: and a hung browser must never hang a preview call.
_VIEW_TIMEOUT_S = 60

#: Wall-clock budget for the whole photograph SET, not one view.  MCP
#: clients bound the tool CALL (~60 s observed 2026-08-19), and each
#: angle here is its own browser launch + document load + three.js
#: parse — ~17 s cold, ~6 s warm per angle, measured on a small mesh —
#: so a multi-angle autofire could spend the client's entire budget
#: photographing and time out a call the server then completes anyway:
#: the user sees a failure, the artifact never reaches them.  Spending
#: past this budget declines the SET (all-or-nothing) and the software
#: painter, ~2.6 s/angle at calibrated visual parity, takes every
#: angle.  Env-tunable; 0 disables.  Chosen so the worst case — budget
#: spent, plus the one in-flight angle, plus the painter's full set —
#: still lands inside the client budget with room for the caller's own
#: work around the render.
_STILL_SET_BUDGET_S = float(os.environ.get("KILN_STAGE_STILL_BUDGET_S", "20") or 0)

#: Blank-frame guard threshold, measured as the LARGEST per-channel
#: standard deviation — never luminance.  Calibrated against real
#: captures (2026-08-10): an empty stage measures 9.7, a grey part
#: 43-59, and a saturated red part 40.4 — but that same red frame is
#: only 12.6 in luminance, because a dark-but-vivid colour on the
#: stage's dark background barely varies in brightness.  Grading it by
#: luminance threw away a perfect render as "blank"; a colour the user
#: asked for must not read as a failure.
_MIN_STDDEV = 15.0
_MIN_BYTES_PER_25_PX = 1  # floor = (width * height) // 25 bytes


#: Whether auto-discovery must avoid ``.app``-bundled browsers (macOS).
#: Module-level so tests can exercise both branches without faking
#: ``sys.platform`` process-wide.
_MAC_DOCK = sys.platform == "darwin"


def _browser_candidates() -> list[Path]:
    """Chromium-family binaries this machine might have, best first.

    Playwright caches lead (headless-shell is purpose-built and has no
    profile/UI baggage), newest build first.  Nothing is ever downloaded.

    ON macOS, AUTO-DISCOVERY OFFERS headless-shell ONLY -- never a full
    ``.app`` browser.  Launching a ``.app``-bundled Chromium headlessly
    puts a second browser icon in the user's Dock for the life of the
    process, and no flag prevents it.  Measured 2026-08-18 on macOS
    26.5.2 with Chrome 151, ten launches: bare ``--headless`` and
    ``--headless=new`` both register with LaunchServices and both bounce
    the Dock icon (confirmed by a human watching the Dock), even though
    ``lsappinfo`` types the entry ``BackgroundOnly``.  The mode flag was
    the tidy story and it is not the mechanism; the bundle is.
    chrome-headless-shell carries no app bundle and no GUI code, so it
    is the one binary that provably never touches the Dock -- which is
    why Google ships it as a separate binary at all.  A machine with
    Chrome but no Playwright cache therefore gets the OpenSCAD look
    rather than a render that spams the Dock; anyone who wants stage
    stills through a full browser anyway can say so explicitly with
    ``KILN_STAGE_BROWSER``, which is honored unchanged.

    Elsewhere (Linux: no Dock, and the PATH binaries are bare
    executables) the wider search stays: Playwright's Chromium, system
    installs, then PATH.
    """
    out: list[Path] = []
    caches = (
        Path.home() / "Library" / "Caches" / "ms-playwright",
        Path.home() / ".cache" / "ms-playwright",
    )
    for cache in caches:
        out.extend(sorted(
            cache.glob("chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell"),
            reverse=True,
        ))
    if _MAC_DOCK:
        return out
    for cache in caches:
        for pattern in (
            "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
            "chromium-*/chrome-linux/chrome",
        ):
            out.extend(sorted(cache.glob(pattern), reverse=True))
    out.extend(
        Path(p)
        for p in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        )
    )
    for name in ("chromium", "chromium-browser", "google-chrome",
                 "google-chrome-stable", "chrome"):
        found = shutil.which(name)
        if found:
            out.append(Path(found))
    return out


def find_browser() -> Path | None:
    """The browser to photograph with, or ``None`` for "use OpenSCAD".

    An explicit ``KILN_STAGE_BROWSER`` that does not exist returns
    ``None`` rather than silently scanning on — a stated override that
    is wrong should surface as "stills stopped being stage-look", not
    as a mystery browser being used instead.
    """
    if os.environ.get(_OPT_OUT_ENV, "").strip():
        return None
    override = os.environ.get(_BROWSER_ENV, "").strip()
    if override:
        p = Path(override)
        if p.is_file() and os.access(p, os.X_OK):
            return p
        logger.debug("stage stills: %s=%s is not an executable", _BROWSER_ENV, override)
        return None
    for cand in _browser_candidates():
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand
    return None


def _stage_document() -> str | None:
    """The still-capable stage document, or ``None``.

    Missing document ⇒ kick the background cache warm so the NEXT render
    can upgrade, and fall back now — a preview call never waits on a
    download.
    """
    override = os.environ.get(_DOC_ENV, "").strip()
    if override:
        try:
            doc = Path(override).read_text(encoding="utf-8")
        except OSError:
            return None
        return doc if _STILL_MARKER in doc else None

    from kiln import stage_cache

    doc = stage_cache.document()
    if doc is None:
        stage_cache.warm()
        return None
    if _STILL_MARKER not in doc:
        # Cached from before still mode shipped; refresh in the background.
        stage_cache.warm()
        return None
    return doc


def _openscad_rotation_to_orbit(rx: float, rz: float) -> tuple[float, float]:
    """OpenSCAD camera rotation → stage orbit (azimuth°, elevation°).

    OpenSCAD's ``--camera=..,rx,ry,rz,dist`` tilts from straight-down
    (``rx=0`` top view, ``90`` horizontal, ``170`` under); the stage
    orbits by elevation above the horizon.  Azimuth follows the spin
    angle directly — the payload's baked z-up→y-up rotation keeps the
    model's front on the stage's front.
    """
    return float(rz), 90.0 - float(rx)


def _frame_ok(png_path: str, width: int, height: int) -> bool:
    """The blank-frame guard (see module docstring)."""
    try:
        size = os.path.getsize(png_path)
    except OSError:
        return False
    if size <= 0:
        return False
    # A COMPLETE png, not merely a non-empty one: _shoot stops as soon as
    # the file size holds steady, and a browser stalled mid-write (a
    # loaded machine pauses between chunks) can hold it steady while
    # truncated.  The IEND trailer is the file's own end-of-stream mark.
    try:
        with open(png_path, "rb") as fh:
            fh.seek(-8, os.SEEK_END)
            if fh.read(4) != b"IEND":
                logger.debug("stage stills: truncated PNG (no IEND)")
                return False
    except OSError:
        return False
    try:
        from PIL import Image, ImageStat
    except ImportError:
        return size >= (width * height) // 25
    try:
        with Image.open(png_path) as im:
            spread = ImageStat.Stat(im.convert("RGB")).stddev
    except Exception:  # noqa: BLE001 — an unreadable frame is a failed frame
        return False
    return max(spread) >= _MIN_STDDEV


def _build_harness(
    document: str,
    payload: dict,
    az_deg: float,
    el_deg: float,
    color: str | None = None,
) -> str | None:
    """The stage document with this view's still config baked in.

    Data-only injection: the config script lands immediately after
    ``<body>`` so it exists before the stage script runs.  Every
    behaviour it triggers lives in the stage document itself.
    """
    if document.count("<body>") != 1:
        logger.debug("stage stills: document has no unique <body> anchor")
        return None
    still: dict = {"payload": payload, "az_deg": az_deg, "el_deg": el_deg}
    if color:
        still["color"] = color
    # JSON inside a <script> block: json.dumps does NOT escape "</script>",
    # so any string in the payload that contains it would close our tag and
    # let the rest run as markup.  No CURRENT field can: the only
    # caller-influenced string is the mesh's basename, and terminating the
    # block needs a "/", which no filesystem allows in one.  This is
    # therefore defence in depth against a field added later (a label, a
    # note, a downgrade reason) rather than a live hole — it costs one
    # string pass, is inert inside JSON, and removes the whole class.
    config = (
        json.dumps(still)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    return document.replace(
        "<body>", "<body><script>window." + _STILL_MARKER + " = " + config + ";</script>", 1
    )


#: Every flag a headless still launch needs that does not depend on the shot.
#: Exported because anything else that drives the same browser -- a test, a
#: sibling harness -- must launch it the SAME way, and the only way to
#: guarantee that is to read this list rather than retype it.  A hand copy is
#: how the keychain flag below came to be missing from one caller and present
#: in the other: two lists claiming to describe one launch, free to disagree.
STILL_BROWSER_FLAGS: tuple[str, ...] = (
    # Deliberately NOT "--headless=new": the mode spelling was measured to
    # make no difference to the macOS Dock-icon problem (see
    # _browser_candidates), and on current Chrome the two spellings select
    # the same mode anyway.  Bare --headless is the one every
    # chromium-family binary accepts, chrome-headless-shell included.
    "--headless",
    "--hide-scrollbars",
    # WebGL in CLI screenshot mode needs the software rasterizer
    # spelled out; without these the stage gets no GL context at all.
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
    "--no-first-run",
    # The throwaway --user-data-dir has no encryption key, so Chrome reaches
    # for the OS keyring to mint one.  A headless run has no keyring session
    # to reach, so macOS throws a modal "a keychain cannot be found to store
    # Chrome" at whoever is sitting at the machine -- once per still, in
    # front of a user who never asked for a browser.  A disposable
    # screenshot has nothing worth encrypting, so it stays out entirely.
    "--use-mock-keychain",
    "--disable-extensions",
    "--disable-crash-reporter",
    "--mute-audio",
)


def _shoot(browser: Path, harness_path: Path, png_path: str,
           width: int, height: int, profile_dir: Path) -> bool:
    """One headless screenshot.  True only for exit 0 + a written file."""
    cmd = [
        str(browser),
        *STILL_BROWSER_FLAGS,
        f"--screenshot={png_path}",
        f"--window-size={width},{height}",
        f"--virtual-time-budget={_VIRTUAL_TIME_MS}",
        f"--user-data-dir={profile_dir}",
        f"file://{harness_path}",
    ]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        logger.debug("stage stills: browser failed to launch: %s", exc)
        return False
    # The FILE is the deliverable, the process is disposable: the
    # headless shell exits cleanly after writing, but full Chrome in CLI
    # screenshot mode writes the PNG and then never exits (measured
    # 2026-08-09 — the screenshot lands in seconds, the process hangs
    # forever).  So poll for a written-and-stable file, and kill the
    # browser once it exists; waiting for exit would turn every view
    # into a full timeout on the most common browser there is.
    deadline = time.monotonic() + _VIEW_TIMEOUT_S
    last_size = -1
    try:
        while time.monotonic() < deadline:
            size = os.path.getsize(png_path) if os.path.isfile(png_path) else 0
            rc = proc.poll()
            if rc is not None:
                return rc == 0 and size > 0
            if size > 0 and size == last_size:
                return True
            last_size = size
            time.sleep(0.3)
        logger.debug("stage stills: browser timed out (%ss)", _VIEW_TIMEOUT_S)
        return os.path.isfile(png_path) and os.path.getsize(png_path) > 0
    finally:
        if proc.poll() is None:
            proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(5)


def try_render_stage_views(
    file_path: str,
    selected: list[tuple[str, str]],
    rotations: dict[str, tuple[float, float, float]],
    *,
    output_dir: str,
    width: int,
    height: int,
    color: str | None = None,
) -> list[dict] | None:
    """Render every requested view as a stage photograph, or ``None``.

    ``None`` — never a partial list, never an exception — means "run the
    OpenSCAD path"; the two renderers must not mix inside one result.
    Arguments mirror the caller's own angle machinery: ``selected`` is
    its ``(label, description)`` list, ``rotations`` its aspect-adapted
    ``label → (rx, ry, rz)`` map, so the stage inherits the exact same
    angle intelligence the OpenSCAD path has always had.

    ``color`` is the caller's requested filament colour.  A hex value is
    handed to the stage; any other spelling declines to OpenSCAD, which
    accepts colour names this renderer does not — a render must never
    quietly come back in a colour nobody asked for.
    """
    try:
        if color and not _HEX_COLOR.match(color.strip()):
            logger.debug("stage stills: colour %r is not hex — using OpenSCAD", color)
            return None
        browser = find_browser()
        if browser is None:
            return None
        document = _stage_document()
        if document is None:
            return None
        if color and _STILL_COLOR_MARKER not in document:
            logger.debug("stage stills: cached stage predates colour — using OpenSCAD")
            return None

        from kiln.mesh_payload import mesh_to_viewer_payload

        try:
            payload = mesh_to_viewer_payload(
                file_path,
                max_triangles=_STILL_MAX_TRIANGLES,
                max_bytes=_STILL_MAX_BYTES,
            )
        except Exception as exc:  # noqa: BLE001 — any unreadable source → OpenSCAD
            logger.debug("stage stills: no payload for %s: %s", file_path, exc)
            return None
        if not payload or payload.get("downgraded"):
            return None

        # Supersample exactly like the OpenSCAD path: shoot oversized,
        # Lanczos-downscale to the requested size (the shared knob in
        # kiln.preview_render governs BOTH renderers, so every preview
        # surface has one crispness policy).  A raw 1x browser frame under
        # the software rasterizer reads visibly soft.
        from kiln.preview_render import downscale_png, effective_supersample

        ss = effective_supersample()
        shot_w, shot_h = width * ss, height * ss

        stem = Path(file_path).stem
        # The front door pre-creates output_dir; a direct caller may not,
        # and a missing directory here fails as a silent per-view decline
        # that reads like a browser problem (it cost two misdiagnoses in
        # one afternoon).  Same line stage_paint carries.
        os.makedirs(output_dir, mode=0o700, exist_ok=True)
        views: list[dict] = []
        # The photograph runs inside a live tool call, and MCP clients
        # enforce a request budget of their own (~60 s observed).  Each
        # angle is a full browser launch + document load + three.js parse
        # — measured 2026-08-19 at ~17 s/angle cold and ~6 s warm on a
        # tiny mesh — so a 3-4 angle autofire can spend the client's whole
        # budget photographing while the painter delivers the same
        # calibrated look at ~2.6 s/angle.  The budget is checked BEFORE
        # each shot: overrunning it declines the whole set (the
        # all-or-nothing contract above), and the painter takes every
        # angle.  A budget of 0 disables the check.
        deadline = (
            time.monotonic() + _STILL_SET_BUDGET_S if _STILL_SET_BUDGET_S else None
        )
        tmp = Path(tempfile.mkdtemp(prefix="kiln_stage_still_"))
        try:
            profile_dir = tmp / "profile"
            profile_dir.mkdir()
            for label, description in selected:
                if deadline is not None and time.monotonic() > deadline:
                    logger.debug(
                        "stage stills: set budget (%.0fs) spent after %d/%d "
                        "angle(s) — declining to the painter",
                        _STILL_SET_BUDGET_S, len(views), len(selected),
                    )
                    return None
                rx, _ry, rz = rotations[label]
                az_deg, el_deg = _openscad_rotation_to_orbit(rx, rz)
                harness = _build_harness(
                    document, payload, az_deg, el_deg,
                    color=color.strip() if color else None,
                )
                if harness is None:
                    return None
                harness_path = tmp / f"still_{label}.html"
                harness_path.write_text(harness, encoding="utf-8")
                png_path = os.path.join(output_dir, f"{stem}_{label}.png")
                if not _shoot(browser, harness_path, png_path, shot_w, shot_h, profile_dir):
                    return None
                if not _frame_ok(png_path, shot_w, shot_h):
                    logger.debug("stage stills: blank frame for %s — falling back", label)
                    return None
                if ss > 1:
                    downscale_png(png_path, width, height)
                views.append({"angle": label, "description": description, "path": png_path})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return views
    except Exception as exc:  # noqa: BLE001 — this backend must never break a preview
        logger.debug("stage stills: unexpected failure: %s", exc)
        return None
