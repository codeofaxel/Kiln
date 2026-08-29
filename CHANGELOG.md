# Changelog

All notable changes to Kiln are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Changed

- **A printability report now names its placement fault, and says what the
  part's shape scored on its own.** When a part is judged unprintable for
  hanging below the plate or overrunning the bed, the report carries a
  stable name for that fault beside the sentence describing it, plus the
  grade the part would have earned sitting correctly. The wording you
  already see is unchanged, word for word — this is what lets a viewer say
  "hangs below the bed" instead of just turning red, and lets anything
  reading the report tell a badly made part from a well made one in the
  wrong place.

### Added

- **Your printer's own start-up routine, run automatically.** If your
  Klipper printer (Voron, Creality K1/K2, QIDI, Sovol, and friends) has a
  `PRINT_START` or `START_PRINT` routine set up — the one that heats the
  chamber, levels the bed, and wipes the nozzle — Kiln now finds it by
  asking the printer itself and uses it when slicing, with your exact
  temperatures filled in. Requires kiln-pro; without it (or whenever the
  routine can't be verified safe) Kiln keeps its built-in safe warm-up.
  The result says which one was used.

- **When Kiln changes your file's format, it says so.** If what comes back
  isn't the format you handed over, the result names what changed, what the
  new format can't hold, and where your untouched original still is.

- **The web workshop takes OBJ files now.** A dropped OBJ used to render fine
  and then offer no way in — it now goes through the same door as STL and 3MF.

- **The mesh checkup reads 3MF.** diagnose_mesh said "run this before slicing"
  and then refused one of the two formats slicers eat.

- **Four more QIDI printers.** The Plus 5, Max 4, Q2C and X-Smart 3. *Free tells
  you a material won't work on your machine; Kiln Pro tells you why, and what to
  run instead, plus more depth.* See https://kiln3d.com/pricing.
- **Tell the Kiln team something.** A new Support page in the web app takes
  bugs, ideas, printers you want supported, and questions — or just say it to
  your agent, which can send any of them for you. A person reads every one, and
  you can ask to be emailed when yours is fixed.

- **Paint a carved logo and get the logo, not a filled-in blob.** Ask for a
  debossed mark in a second color and Kiln paints the carved strokes only — a
  logo with a closed outline keeps its middle in the body color instead of
  flooding solid.

- **Give each carved decoration its own color.** A model with more than one
  carved mark — a name and a title, a logo and a border — can now take a
  different color per decoration in a single painted model, ready for a
  multi-filament printer. Name the ones you want colored and the rest stay in
  the body color.

- **Ask for a bracket, get a bracket.** Kiln ships 65 ready-made
  parametric parts — brackets, bins, stands, cases, hooks, jigs,
  clamps — and nothing could find them until now. Describe what you
  need in your own dimensions and your agent offers one, then builds
  it. No API key needed.

- **Your logo rides into the CAD file.** Put an SVG on a generated product and
  the STEP export carries it as real CAD geometry — same size and depth as the
  print, letter counters and enclosed islands intact. Photo reliefs still
  refuse honestly.

### Fixed

- **Tell Kiln which printer, and it slices for that printer.** With more than
  one machine registered, naming one could use your default printer's settings.
  Fixed.

- **"Design this and print it" now actually prints it.** The one-call
  design-to-G-code pipeline died quietly at three separate steps — template
  values were never filled in, the 3D file was built through a function that
  didn't exist, and the slicer was called through another one. It now runs
  end to end: describe a part, get sliced G-code for your printer, warm-up
  included.

- **Pipeline printing works with your registered printers again.** The
  quick-print, reslice, calibrate, and benchmark pipelines all failed at the
  upload step whenever a printer was registered — they looked the printer up
  through an internal path that has never worked. They now use the same
  printer lookup every other tool uses.

- **Your calibrated print settings no longer vanish when Kiln adds a brim.**
  When the auto-adhesion or per-printer speed logic adjusted a job, the
  tuned values from your calibration profile (pressure advance, flow, and
  friends) were silently dropped from the final slice — even though the
  response said they were applied. They now survive every adjustment.


- **An SVG logo no longer vanishes from the print.** On a keychain front or a
  pen cup's inside floor, an SVG was quietly skipped and the piece printed
  plain. It now carves as crisp outlines, sized to the face.

- **Multicolor models properly print in all their colors, and much faster.** A
  model with several colors could come out in one. Now fixed. A hidden speed cap
  is gone too. Needs OrcaSlicer or BambuStudio installed; Kiln finds it on its
  own.

- **Decorations land on the face you actually look at.** Ask for a texture,
  logo, or engraving without saying where, and Kiln now picks the visible
  side — a tray gets its top, not its hidden underside. Name a face yourself
  and it still goes exactly there. Every decoration tool follows the same
  rule, and it works on 3MF models as well as STL.

- **Air-filter warnings on QIDI printers.** Several switch the filter off while
  the chamber heats — exactly when ABS and ASA need it.
- **Your printer heats up before it starts printing.** On OrcaSlicer, a print
  sliced for a Klipper machine — every K1, K2, QIDI, Voron and Ender V3 Kiln
  knows about — went straight from homing to laying down plastic without ever
  being told to heat. Kiln now guarantees the bed and nozzle are up to
  temperature, and the machine has homed, before anything extrudes. On
  PrusaSlicer only one machine was affected — the Vision Miner 22 IDEX, which
  was missing its nozzle wait there too and gained it from the same fix.

- **The generic Klipper profile can slice again.** It produced no file at all,
  on either slicer, because its start G-code referred to a temperature setting
  in a form neither one accepts.

- **Parts that printed beautifully now also work.** Twenty-three of
  the ready-made parts had flaws you'd only find after printing. All
  rebuilt and re-tested.

- **Your part now sits on your printer's bed in the 3D view**, not
  parked in a corner of it.

- **Logos and text now land on the side of a cup, box, or vase.** Decorating
  a hollow product's side quietly did nothing — the mark went into the empty
  middle instead of the wall. On a round product, Kiln now warns that side art
  is sized to the flat strip it lands on.

- **A decoration that didn't take is reported as a failure, and doesn't cost
  you one.** Kiln no longer returns an unchanged model as a success, and your
  free monthly decorations are only counted when one actually lands.

- **Paint every decoration on a model, not just the last one.** Carving a
  second decoration onto an already-decorated model no longer forgets the
  first — painting carved faces now covers every decoration, not just the
  latest. A two-line nameplate used to come out with one line colored, and
  said it had succeeded.

- **Printability grades stopped failing models that print fine.** Grades now
  follow what your printer actually does.

- **Models that can't fit or sit on your bed no longer pass.** A part
  hanging below the build plate or wider than your printer used to grade
  an A. Both now fail, and say how to fix it.

- **Multicolor and painted models now land on the print bed — yours.** A model
  that wasn't already centred produced a 3MF PrusaSlicer quietly refused to
  slice. Kiln now places it on your printer's actual plate, and when something
  genuinely doesn't fit, says so plainly instead of failing silently. An
  off-plate 3MF is caught before slicing now, with how far to move it.

## [1.4.1.1] - 2026-08-19

### Added

- **Connecting a printer to the web app now walks itself.** On a machine with
  no printer saved yet, `kiln bridge enable` scans your network, offers the
  printer it finds, asks only the details that printer needs, and saves it as
  your active one. It also says up front if you still need a slicer, instead
  of letting the first print fail. One paste from kiln3d.com now ends at a
  working print path.

### Fixed

- **Previews really do get the studio look now.** 1.4.1 said preview images
  would use the web app's studio look on any computer. On most installs they
  didn't: one of the pieces Kiln needs to draw them wasn't part of the
  install, so previews quietly fell back to the plain look with nothing said.
  It's included now, and preview images come out sharper too.

- **Preview pictures show up in seconds.** A full set of angles could take
  long enough to run past the time your app is willing to wait, and come
  back with nothing. The whole set now comes back in seconds, and Kiln keeps
  an eye on the clock so a preview finishes inside that window instead of
  being lost.

- **An expired web sign-in no longer wastes your time.** When your sign-in
  to kiln3d.com had gone stale, steps that upload your model for the web 3D
  view kept re-uploading it and getting refused, over and over, inside one
  call. Kiln now takes the first refusal as its answer and skips the rest;
  sign in again and uploads work like normal.

## [1.4.1] - 2026-08-18

### Added

- **Rebuild a saved design in one step.** `rebuild_design` re-runs a design's
  saved build recipe: parametric designs re-derive from their OpenSCAD source
  and parameters, imported models re-slice their saved parts. It replaces
  `smart_redesign`, which is retired.

### Changed

- **Previews look like the real thing on every computer.** Preview images now
  use the same studio look as the web app — print bed, lighting, shadow. That
  used to need a browser on your machine; it's built in now.

- **Running several printers at once is a Kiln Business feature, and that now
  properly applies to every printer command.** `hand_back_printer` moves Kiln
  between machines without touching a running print.

- **When Kiln asks you to update, it says what you'd get.** Update prompts
  used to give you the version number and nothing else. Now they name two or
  three of the things the new release adds, so you can decide whether it's
  worth doing right now instead of taking it on faith.

### Fixed

- **Slicing for a Bambu now names the file to upload.** It produced two
  files and pointed at the one Bambu printers can't start from, so the
  upload was refused a few steps later. Kiln now names the right one.

- **Designs and slices no longer fail at random on macOS.** OpenSCAD and
  PrusaSlicer sometimes crash on startup, before reading your model. Kiln
  retries instead of failing.

- **Preview renders no longer take over your desktop on macOS.** They could pop
  up a keychain dialog for Chrome — a browser you never opened — and bounce a
  second Chrome icon into your Dock. Neither happens now.

- **A logo with a see-through background now carves just the mark.** Kiln was
  reading the empty space around the artwork as solid ink, so it cut the logo's
  whole rectangle into the part as a sunken frame. White logos came out blank.
  Now fixed.

## [1.4.0] - 2026-08-14

### Added

- **Point a print at the printer you mean (Business+).** Starting a print,
  uploading a file, checking readiness and setting temperatures all take a
  printer name now. Naming none still means your default, and each machine is
  checked against its own limits rather than your default printer's.

- **Your printer asks you before it prints.** In apps supporting MCP's
  confirmation prompts you get a dialog your assistant can't answer for you.
  It names the file and covers that one print. Elsewhere the previous approval
  step is unchanged.

- **Stop the printer you mean, by name.** Cancel, pause and resume
  now take a printer name, so an assistant minding two machines can stop the
  right one — before this, those commands only ever reached your default
  printer, and the only way to stop a second machine remotely was the hard
  emergency stop. Naming no printer still means your default, so nothing changes
  if you have one machine. Stopping any machine you own is free at every tier;
  running two at once is Business. Each machine keeps its own books too: a
  paused printer holds its own temperatures, and a cancelled print is remembered
  as cancelled on the machine it actually happened on.

- **Clear a stuck printer error without walking over to it.** `clear_printer_error`
  works on Klipper/Moonraker, OctoPrint, Duet, Creality and USB.

- **A dropped connection can't cost you a duplicate print.** If your assistant
  queues a print and the reply goes missing, it can now retry safely: sending
  the same job again with the same one-time key gives back the original
  instead of quietly adding a second copy, and Kiln says which of the two
  happened. Jobs sent without a key behave exactly as before, and a new key is
  still a new print. Thanks [@artiehinz](https://github.com/artiehinz) for
  [reporting it](https://github.com/codeofaxel/Kiln/issues/112), with the queue
  and scheduler traced well enough to reproduce.

- **Kiln works with OrcaSlicer and BambuStudio now.** If one of those is the
  slicer you have, Kiln can finally use it. Kiln had listed them as supported
  for a long time while only ever being able to drive PrusaSlicer, so anyone
  without PrusaSlicer hit a confusing error instead of a print. There's
  nothing to set up: Kiln works out which slicer you have and talks to it
  properly, and your existing printer settings carry over untouched.

- **Telling us something's broken no longer needs an account.** Reporting a
  problem from inside a chat now works whether or not you've signed in — the
  install least able to connect is usually the one with the most to say.

- **Your assistant can send back what actually fixed a problem.** When it works
  around a Kiln bug, it can now tell us what it tried, what worked, and how it
  knew — so the fix reaches everyone with that printer instead of living in one
  chat's memory. Kiln asks for this in a set form, because a workaround that
  says what it tested is one we can act on.

- **Your assistant now knows what's free before you ask for it.** Every Kiln
  tool tells your assistant exactly what it costs — free and unlimited, free
  with a monthly allowance, or part of a paid plan — so you get a straight
  answer instead of a guess. Ask "can I do this on free?" and it knows.
  Paid features also stopped repeating themselves: the upgrade note was
  appearing twice in a row, in two different wordings. Where a tool needs a
  paid plan, it now says what that plan gets you as well — so you can tell
  whether it's worth having before you run into it, instead of only at the
  moment you're turned away.

- **Open a CAD file and see the real part.** A STEP now opens labelled as CAD,
  with the exact dimensions the file itself defines. When those exact figures
  can't be read on your setup, Kiln says so plainly instead of showing an
  estimate as though it were exact.

- **Ask the print questions about your CAD file directly.** Which way up
  should this print, will it warp, how much support material will it need,
  what are its dimensions — every one takes a STEP file directly now, with no
  converting it to a mesh yourself first. If your machine has no CAD converter
  installed, Kiln says so and tells you how to add one.

- **A CAD part says how closely it matches what you sent (Pro+).** Reading a
  STEP is an approximation, and repairing the shape can move it again. Kiln
  measures both and keeps them with the part, so its passport still answers
  months later — and tells you when the answer has stopped applying. See
  https://kiln3d.com/pricing.

- **Parts that need to move get a gap that suits your printer (Pro+).** On
  anything that has to turn or slide, that gap decides whether it works or
  comes off the plate as one solid lump — and it used to be the same number
  for every printer and every material. Kiln now sizes it for yours, and
  leaves more room when it knows less about your setup. Without Kiln Pro you
  keep the standard clearance. See https://kiln3d.com/pricing.

- **Kiln names the printer you actually have.** Bambu, PrusaLink, Elegoo and
  USB-connected Marlin printers now report their own model, so your printer
  list shows the real machine — an A1, an MK4S, an Ender-3 V2 — not just the
  brand, with nothing to set up. If you've told Kiln your model that stays the
  last word, and when a printer's own answers disagree Kiln reports nothing
  rather than name the wrong machine — and `kiln doctor` tells you which
  sources disagree so you can settle it. Klipper and OctoPrint setups still
  show the brand only — they genuinely can't say what they are.

- **Your designs can leave as real CAD files.** Any Kiln design exports as a
  STEP — one clean solid a machine shop can open and measure. When a design
  can't honestly be CAD (a photo relief, wrapped text), Kiln says so instead
  of faking it. Free, like STL and 3MF.

- **The drawing knows how it prints (Business+).** Production drawings label
  features at the size they'll actually print, not just the size you asked
  for — checked against the design's own CAD. See https://kiln3d.com/pricing.

- **Split a big model and Kiln remembers the kit (Pro+).** Every piece is kept
  in the design's history with the seam and fastening that hold it to its
  neighbours — so months later you can reprint just the piece that broke. See
  https://kiln3d.com/pricing.

- **Save a split kit from the design page (Pro+).** One button puts every piece
  and the whole assembly onto the design's record.

- **Assembly manuals use a kit's real joints (Business+).** Steps come from the
  decisions Kiln made when it cut the part, not from re-measuring afterwards.

- **See what Kiln read off a customer's engineering drawing (Business+).** Send
  back the marked-up sheet: every dimension Kiln pulled is boxed on the drawing
  itself and color-coded by how well it checked out, with the key printed on
  the image. One look tells you whether the read is right before you quote
  from it.

- **Kiln checks where features are, not just how many and how big (Pro).** Say
  the holes go 10mm in from each corner; Kiln measures whether they actually
  did, and names anything that landed somewhere else and by how far. See https://kiln3d.com/pricing.

- **Check a file Kiln didn't make (Pro).** Point at a model you already have,
  list the numbers it should hit, get the same plain receipt back. See https://kiln3d.com/pricing.

- **Paint a model by its own surfaces.** Ask Kiln to colour a part of a model —
  the handle, the lid, the base — and it finds the model's natural surfaces,
  shows you which is which, and paints the ones you choose. Out comes a
  printable 3MF your slicer reads natively, with a suggested filament slot per
  colour. Works on models you made, CAD you brought in, or a mesh you
  downloaded.

- **Paint by clicking, right in the browser.** Click a surface on the model,
  pick a colour — every click paints while a colour's armed. One Keep writes
  the printable file with the colours built in.

- **Tell Kiln your printer's real hardware.** Upgraded to an all-metal hotend
  or a PT1000? Pick your setup by name and Kiln uses its checked limit for it —
  you choose the hardware, Kiln supplies the number. Each option lists what it
  assumes, including any firmware change. Stock machines are unaffected.

- **Report a problem and Kiln sends the evidence with it.** Bug reports now
  include the tail of Kiln's activity log, so we can see what actually broke
  instead of guessing from a description. Only when you report something,
  never in the background — and your account name, folder paths, and local
  network addresses are stripped on your machine first. No setting can turn
  that scrubbing off — and if you'd rather send only what you write, set
  `KILN_REPORT_NO_LOG=1`.

- **Fans and lights from the command line.** `kiln fan` sets part, aux, or
  chamber fans; `kiln light` switches chamber and work lights on, off, or
  flashing; `kiln emergency-trip` names the safety input that tripped.

- **`kiln temp` respects your machine's own limits.** It allowed anything up to
  a fixed 300°C hotend and 130°C bed whatever machine you were on, so a printer
  rated lower could be pushed past its rating from the command line and a
  higher-rated one was refused below its own. It now reads the machine's real
  ceiling, and takes `--printer` like the rest.

- **Watch a print from the web, layer by layer.** Kiln keeps the model and
  toolpath of prints it sliced, and can send that pair to your own account so
  the Monitor at https://kiln3d.com/monitor shows the object being built
  instead of a progress bar. Nothing leaves your machine unless you ask.

- **Restarting the printer bridge is one command.** However it was set up to
  run — starting with your computer, under a service manager, or in a
  terminal — Kiln works out which and does the right thing, including the
  awkward state where it's switched on but not actually running.

- **A resent print request can't queue the job twice.** If a submission's reply
  goes missing and your script sends it again, you get the original job back
  instead of a second copy of the print.

### Changed

- **Every way of starting a print now asks first.** Slicing-and-printing,
  retrying and monitored prints used to skip the approval step `start_print`
  required. Retries only re-ask when the shape changed.

- `cancel_job` has been renamed `cancel_queued_job`. It only ever removed a job
  that was still waiting in the queue line, but sitting next to `cancel_print`,
  the name suggested otherwise. This tool also used to sometimes accept a job
  that had already started and mark it cancelled while the machine kept
  printing; now it tells you the print is already running and points you at
  `cancel_print`.

- **Printer status answers for the printer you name, at the detail you want.**
  Asking about a specific machine used to fail outright; now you can name one,
  and choose a quick reading or the full picture.

- **Preview pictures now match the 3D viewer.** Kiln's stills get the same
  studio lighting as the model viewer instead of the flat grey render — in
  your chosen filament colour, from the angles Kiln already picks for your
  model's shape. Nothing to install; where it isn't available, previews look
  exactly as before.

- **You can no longer type your own limit above Kiln's.** Upgraded your
  printer? Declare the hardware and Kiln raises the limit to the one it has
  checked for that setup. Hardware Kiln hasn't checked stays at the stock
  limit. Limits you set *lower* than Kiln's still apply.

- **Temperatures suggested from your print history stay inside your printer's
  limits.** When you ask Kiln for settings for a specific printer, a suggested
  hotend, bed or chamber temperature above that machine's limit now comes back
  at the limit. Lower suggestions are unchanged.

- **A few printer limits corrected to the maker's published figures.** Where a
  maker publishes two different figures for one machine, Kiln now uses the
  lower.

- **Voron V0/V0.2 and V2.4/Trident hotend limits corrected to 270°C.** Fitted
  an E3D Revo Voron? Declare it and Kiln uses 300°C. Hotends whose makers
  publish no temperature stay at 270 — Kiln won't guess one.

- **Jobs go to the printer that's been finishing them (Business).** When Kiln has
  more than one printer to choose from, it weighs each machine's own print
  history alongside whether the material fits, so a machine that has been
  struggling stops drawing new work. A printer Kiln hasn't seen finish
  anything yet counts as unknown, never as bad.

- **3D stage for any object Kiln makes or changes, not just what it designs.** The
  3D panel used to open for the models Kiln draws from scratch. Now it opens for
  the rest of your work — a CAD file you brought in, a mesh Kiln repaired, a part
  it scaled, hollowed, split, or merged. The model you didn't draw is the one you
  most need to see before you print it, and the bed check comes with it: too big
  for your machine and the panel says so.

### Fixed

- **Upgrading no longer stops Kiln starting.** A database from an older Kiln
  failed on the first launch after upgrading and took the server down with it.
  And a locked database now names the program holding it instead of failing
  generically.

- **A Bambu print no longer looks rejected because the last one failed.** The
  previous job's error code was being read as this job's refusal.

- **A finished or cancelled print reads as finished or cancelled**, instead of
  plain "idle".

- **A stalled print gets noticed.** A print dead at 5% while its readings
  claimed it was running is now caught, and a resume after a refused resume
  works.

- **A flat bottom is not an overhang.** A plain box was scored as having severe
  overhangs — the face it stands on.

- **Your printer stays reachable when you run Kiln in more than one chat.**
  Several open sessions could leave your own Bambu or Elegoo refusing to answer
  while sitting there powered on. Fixed, and a print already running is never
  interrupted. Other printers were never affected.

- **A printer that won't answer now tells you why.** The error used to point at
  Bambu Studio or the Handy app, so people closed software they'd already closed
  and power-cycled a printer that was fine. Kiln now names the real cause — and
  `kiln trim` clears it when it's Kiln's own leftovers.

- **Checks meant for one printer were applied to another (Business+).** With
  two machines, one could be cleared to print by the other being idle, and a
  failure on one could start cooling the other mid-print. Single-printer setups
  were never affected.

- **In confirm mode, the action you approve is exactly the action that runs.**
  Confirmed commands were replaying with some of their settings quietly reset
  to defaults: an emergency stop confirmed for one printer went to all of
  them, a cancel meant to keep the bed warm let it cool, and clearing an
  emergency stop failed outright every time. All of it now replays exactly as
  shown, and a test holds that to every confirmable command, including future
  ones.

- **Cancelled prints are recorded as cancelled, not successful.** Including the
  ones Kiln stops itself after spotting a problem.

- **Cancelling mid-levelling can leave a Bambu refusing prints.** It usually
  clears itself within a minute — Kiln now says so instead of leaving you to
  guess.

- **Sending a job to your fleet works again (Business).** Handing a print to
  the fleet, and asking Kiln which machine should take it, failed with an
  unhelpful error on every call since April. Both work now, and the routing
  reads the real state of your machines: what's loaded, what's already queued,
  and how each has done with that material before.

- **Your Bambu's screen shows the actual print again.** A recent change to how
  Kiln handles colours quietly stopped previews reaching the printer, so the
  touchscreen showed a generic icon instead of your model — the one glance that
  tells you the machine got what you meant. Previews are back, drawn in the
  colour the file says it will print in. Retried prints now get one too; they
  never had one before. When the file doesn't name a colour, the preview stays
  neutral instead of guessing one.

- **Cold starts no longer stop themselves on OctoPrint and Marlin printers.**
  Those printers report that they're printing while still heating up, and Kiln
  read the cold nozzle as a dead heater and hit the emergency stop — which
  meant switching the printer off and on again to recover. A cold start or a
  filament change is no longer mistaken for a failure. Thanks
  [@riverray121](https://github.com/riverray121), who found it on an Ender-3
  V3 SE and fixed it.

- **A heater that never comes up to temperature now gets reported.** Before,
  the print would just sit there waiting.

- **Seven Bambu printer profiles can slice again.** P2S, P1S, P1P, X1 Carbon,
  X1E, H2S and A1 mini were quietly failing every print through Kiln's own
  profile — no error message, just no file at the end. All seven work now.

- **Print estimates stopped saying your filament weighs nothing.** Asking how
  much filament a print needed answered zero grams and zero cost, every time.
  Kiln now works the weight out from the material you name, and when it isn't
  told the material it leaves the figure out rather than reporting nothing as
  a number. Time, length and layer count were always right.

- **Your stats stopped counting failed work as finished.** A tool that failed
  could still tick over your generations or decorations count for the day, so
  the totals on your stats page read higher than what you actually made.

- **Artillery Sidewinder X3 owners get settings that match their machine.** Kiln
  had the X3 down as Klipper when Artillery ships it running Marlin, and both its
  temperature limits were wrong. The Plus was sharing the Pro's entry too, so its
  owners were told their bed was smaller than it is — it's now its own machine.
  Every figure comes from Artillery's manuals.

- **Bambu P2S prints start the first time.** Kiln gave every Bambu the same
  instructions for where to find your print file, and the P2S expects a
  different form, so files uploaded fine and prints never began. The P2S now
  gets the form its firmware wants, and Kiln looks in the folder it actually
  keeps jobs in. Other Bambu models are unchanged.

- **Bambu printers without an AMS no longer stall asking about the AMS.**
  Printing from an external spool, Kiln pointed the printer at an AMS slot,
  so a machine with no AMS stopped and waited on a filament-mapping screen.

- **Kiln says how old a printer reading is.** A paused print could be reported
  as still running, because the answer came from a cached update that had
  stopped arriving. Kiln now tells you how fresh the reading is instead of
  presenting stale information as current.

- **Kiln no longer presents what it was told as what it checked.** Asked which
  filament was loaded, Kiln could repeat a material someone typed in earlier as
  though it had measured it — and a printer with nothing recorded at all looked
  like a match. Both now say which they are.

- **3MF files keep all of their objects.** A .3mf you bring to Kiln can hold
  its shapes in several separate parts, and Kiln was carrying only the first
  one into the file it sends the printer — so that file could open incomplete,
  or fail to open at all. Designs Kiln makes itself were never affected.

- **Bambu prints on the P2S, P1S, P1P, X1 Carbon, X1E, H2S and A1 mini start
  their own way now.** Every Bambu print used to open with the A1's startup
  moves, the only ones Kiln had. Each printer gets its own now. A1 and A2L
  unchanged.

  Thanks to [@hype-eth](https://github.com/hype-eth) for finding these on a
  brand-new P2S, and for digging through the printer's own error codes to prove
  what was happening.

- **Kiln stops suggesting settings from a different design.** Move a hole or
  resize a slot and Kiln now treats it as the new part it is, instead of
  recommending what worked for the previous version.

- **Text prints at the size the design asked for.** Stamped text came out a
  touch wider than intended; now it doesn't.

- **Leftover Kiln servers now actually close.** Background servers weren't
  quitting when your app closed them, so they piled up holding memory. And
  cleanup (`kiln trim`) reported them closed without checking — it could say
  it freed memory while every one kept running. Both fixed.

- **Inch drawings are now read completely (Business+).** Shop drawings write
  sub-inch sizes without a leading zero (".375", not "0.375"), and Kiln's
  completeness tally was skipping them — so a drawing could come back fully
  accounted for while real dimensions had been missed. Confirming a dimension
  is stricter now too.

- **Design suggestions no longer exceed what your printer will accept.**
  Material and design advice could suggest a hotend temperature Kiln would
  then refuse. Affected the Rat Rig V-Core 3, Ender 3 V2, and Ender 3 S1 Pro.

- **A 3D panel that never gets your model now says so** — it used to sit
  empty indefinitely with nothing to read. It now tells you the 3D view
  didn't arrive and points you at the preview image in the conversation,
  which shows what was made. A panel still working on your model is
  unaffected.

- **A crash in the 3D geometry engine now leaves a record.** OpenSCAD
  failures showed you an error and wrote nothing down, so a bug report
  carried no trace of what actually broke.

- **Previews can no longer show a different object than the one you made.**
  Running two Kiln chats at once could cross their preview pictures — one
  chat's object appearing in the other's preview, and sticking around in the
  cache. Renders no longer share a workspace, and every older preview is
  redrawn fresh.

- **Previews now put every colour where it belongs.** On a plate with several
  parts, one part's colour could bleed across another in the picture. The model
  was always correct; the preview wasn't.

- **Printing several copies in different colors now works.** Each copy gets
  its own spot on the bed — they used to all be placed in the same place.

- **Print time and filament estimates now come back with real numbers for
  anyone slicing with PrusaSlicer.** Kiln looked for the summary in the
  wrong part of the file.

- **Custom slicer settings now reach the print.** A setting you asked for —
  thicker layers, a wider brim — was silently dropped whenever Kiln didn't
  recognize your printer's model, and still reported as applied. On Bambu
  that could also leave the print file itself wrong. Now fixed.

- **When Kiln can't start, it now says why instead of just disappearing.** A
  problem at startup used to make Kiln vanish from your assistant with nothing
  but a generic "server failed to start" — now it comes up in recovery mode and
  tells you what went wrong and how to fix it, `kiln doctor` reports the same
  thing, and the full details are saved to `~/.kiln/last-startup-error.log`.

## [1.3.2] - 2026-08-01

### Added

- **Use a decoration you already saved (Pro).** A saved decoration — a logo, a
  texture, a pattern — now goes straight onto a model at the exact depth and
  placement it recorded, with fewer snags. See
  https://kiln3d.com/pricing.

### Changed

- **Your design now stands on your real print bed.** The 3D stage drew
  the same square plate for everyone; where Kiln knows which printer is yours
  it now draws that machine's actual bed, with its name on it — in the web
  studio, in shared view links, and in the panel in your chat. A part too big
  for the bed says so: on the model and in the size line.

### Fixed

- **Your logo now prints exactly the way you drew it** — gaps stay open,
  corners stay sharp, dashed lines stay dashed.

- **Repair says what it could not fix** — instead of reporting success
  on a mesh it left untouched, it names the defect and what to do about it.

- **Decorated models come out as clean, watertight solids** — carving a
  logo or text no longer leaves hairline seams that make other tools reject
  the file.

- **Material suggestions better respect your hotend.**

- **More split pieces come back with pins in them (Pro)** — Kiln used to skip
  pins on some cuts it can now handle, like a panel with a brace behind it.

- **"Trust this browser" now actually works (Business/Enterprise)** — it used
  to still demand your authenticator code on the next call. Admin actions
  still require a fresh code.

- **A cloud hiccup no longer reads as an empty library (Pro)** — if Kiln can't
  reach your saved work for a moment, it now says the read failed instead of
  answering with an empty list.

## [1.3.1] - 2026-07-31

### Added

- **See your design in 3D wherever you run Kiln — free for everyone.** The
  interactive stage used to need Kiln's hosted connection; now every install
  has it — a 3D panel right in the conversation where apps support it, and a
  browser link to the same stage everywhere else. Drag to rotate, check it
  from every angle before you print.

### Fixed

- **Kiln now says plainly when it couldn't establish something, so silence
  never reads as an all-clear.**

## [1.3.0] - 2026-07-29

### Added

- **Kiln cleans up after itself.** Every agent session starts its own
  background Kiln, and closed sessions leave theirs behind — quietly holding
  memory. Kiln now notices the pile-up, tells you in plain English, and
  closes the leftovers when you ask, checking that nothing is printing first
  and never touching the session you're in. In a terminal: `kiln trim`.

- **Too big for your printer? Kiln cuts it into a kit.** When a model won't
  fit, Kiln can split it into pieces that fit your printer and go back
  together — cutting where it makes sense and sealing every cut so each
  piece actually prints. Splitting is free; Pro works out how the pieces
  should attach and builds that into them.

- **Recommend from the spools you actually own.** Ask what to print in and
  Kiln can answer from your real shelf and loaded filament instead of the
  whole catalog. It names the printer that has it, or tells you it needs a
  swap. Nothing suitable? You get an honest "you'd need to buy this" — and
  if something you don't own fits much better, Kiln says so.

- **Drop in CAD files and print them — free for everyone.** Kiln now reads
  STEP, the format every CAD program and machine shop speaks. Hand any Kiln
  tool a `.step` and it becomes a printable model automatically — and a
  coloured multi-part assembly keeps its colours and part names, ready for
  multi-material printing. One command sets up the converter:
  `kiln install-step-backend`.

- **Kiln's hosted connection speaks the newest protocol.** Connect Kiln to
  Claude or any other AI client and it now uses the 2026-07-28 Model Context
  Protocol specification. Long-running work — texture carving, print
  repair, assembly booklets, technical drawings — hands back a ticket you
  can check on, so a slow job never blocks the conversation. Nothing
  changes for older clients.

- **Print records now work on every printer Kiln supports (free).** Outcome
  recording used to run only on the Bambu connection; Moonraker, OctoPrint,
  Klipper and serial printers now record too. The record opens when a print
  starts, so closing your session doesn't lose it, and if Kiln wasn't
  connected at the end it asks how the print went instead of assuming —
  anything unconfirmed stays out of success rates and recommended settings
  until you answer.

- **Hand Kiln a customer drawing, get a straight answer (Business+).**
  Drop in an engineering drawing (PDF or a photo) and Kiln reads every
  dimension and tolerance on the sheet, then tells you — dimension by
  dimension — whether your printers can hold them, how many good parts
  to expect per batch, and what would improve the odds: a different
  print orientation, a small tolerance ask back to your customer, or
  Kiln's per-machine compensation. The answers come from how your
  machines have actually printed, not generic 3D-printing rules of
  thumb, and they sharpen every time you report a real measurement.
  Anything Kiln can't honestly assess is listed for your review —
  never silently passed. Fleet ranking ("which of my machines is the
  right one for this job") is Enterprise. Enterprise shops can also set
  their own quality bar: out of every 100 parts you print, how many
  have to come out on-size before you'd call the job a safe "yes"?
  Kiln's default is strict (97 of 100); tell it your number once and
  it's saved to your account — every future drawing is graded against
  your bar, on any machine you sign into, and each report says which
  bar it used.

- **Kiln warns you about holes before you print them (Business+).**
  Printed holes always come out a bit smaller than the drawing asks
  for. When the drawing doesn't say how much slop is allowed, Kiln now
  tells you how far off it'll be for your material — so you can plan
  to drill them out, instead of finding out after the part is done.

- **Attach the CAD, get a price and a time (Business+).** The report
  adds a ballpark material cost and print time for a single part. Ask
  for a batch and it totals the whole run — padded for the parts Kiln
  predicts will measure outside the drawing's limits, so you're not
  quoting a perfect-world number.

- **First Article Inspection drafting (Enterprise).** As you design and
  print parts and log inspections in Kiln, it builds the record for you
  — so when an AS9102 FAIR report is due, Kiln drafts it from your own
  work. Your reviewer verifies and signs; Kiln never fills the sign-off.


- **Add Kiln to Claude Code in one command — free.** Kiln now ships as a
  Claude Code plugin. Run `claude plugin marketplace add codeofaxel/Kiln`,
  then `claude plugin install kiln@kiln`, and every Kiln tool — design,
  slice, print, monitor, recover — is wired into your Claude Code session
  with no manual setup. Requires [uv](https://astral.sh/uv); Kiln updates
  itself automatically from there.

- **Turn a model that came in pieces into a kit you can build.** Drop in a
  multi-part model and the parts keep their names and places. Kiln finds the
  biggest size everything prints at, proves the pieces really fit, and tells
  you what any overlap means.

- **Kiln packs your print plates (Pro).** How many plates a kit takes, and
  what goes on each one. With more than one printer, Kiln splits the work
  between them and tells you when the last plate finishes (Business+). See
  https://kiln3d.com/pricing.

- **Better assembly booklets that generate quicker (Business+).**

- **Manuals are kept, versioned, and synced (Business+).** Every booklet lands
  in your library with revision history, cloud sync, and proof of
  authenticity.

- **Know if a shelf will lean before you print it (Pro+).** Kiln says which
  openings need a diagonal and how few will do. Back panels count, and you
  get each brace's length to print, cut, or buy.

- **How to glue the high-temperature plastics (Pro+).** Adhesive intelligence
  now covers PPS and Ultem (PEI 1010 and 9085) — surface prep and which
  adhesive actually holds.

- **Bambu X1E and H2S reach their full nozzle temperature.** Kiln now
  respects each printer's real limit, so high-temp filaments print
  without a false "too hot" error.

- **Duet printers now work with Kiln (beta).** If your machine runs on a
  Duet board, just tell your AI — Kiln connects it and drives it like any
  other printer: print, pause, cancel, upload, set temperatures. Works
  with both current and older RepRapFirmware — including the Vision Miner
  22 IDEX v4.

- **Your texture work goes further.** Kiln's texture engine now wraps your
  own image around the whole object rather than one face, lets you shape the
  finish (invert it, deepen it, soften it, fade it into the surface), and can
  raise detail that was only painted on a model into real printable relief.
  Purely additive, so walls never thin and your original file is untouched.
  Free (3 detail recoveries a month; unlimited on Kiln Pro). The same finish
  controls are now in the web studio at kiln3d.com, behind an "Advanced
  finish" panel.

- **High-temperature engineering plastics, now first-class.** Kiln
  recognizes six advanced polymers — PEEK, PEI (1010 and 9085), PEKK, PPSU,
  and PPS — the materials used for parts that face real heat, load, and
  harsh chemicals. It knows their safe handling and tells you when a printer
  simply can't reach the temperatures they need.

- **Nine more printers.** Added the AON3D AON-M2+ and Hylo, INTAMSYS
  FUNMAT PRO 610HT, Vision Miner 22 IDEX v4, QIDI Plus 4 / Q1 Pro / Q2 /
  X-Max 3, and the Creality Ender 3 S1 Pro — from the household
  desktop up to the industrial heated-chamber class, each with advice that
  fits what your machine can actually do. Full list:
  https://kiln3d.com/printers

- **Straight answers for demanding materials (Pro+).** For the new
  high-temperature polymers, Kiln Pro adds engineering-grade properties and
  printer-specific guidance — ask what a part will hold or how to run it, and
  the answer is tuned to your exact printer and material instead of a generic
  spec.

- **Chemical answers for the high-temperature plastics.** Ask whether a
  printed PEEK, PEI or PPSU part (and other high-temp plastics) survives a
  solvent, fuel, cleaner, oil, sunlight or food contact — answered for a real
  printed part, and flagged honestly when no test exists. Everyday chemicals
  are Kiln Business; the ones that carry legal weight are Kiln Enterprise.
  Warnings are free for everyone. See https://kiln3d.com/pricing.

- **ESD-safe filaments, matched to your job.** ESD-safe (anti-static)
  filaments drain off static so a printed tray or fixture won't zap the boards
  it holds. Kiln Business picks and sources the right grade for your printer;
  Kiln Enterprise adds what a shop needs to prove its parts pass an ESD audit.
  The safety basics are free for everyone. See https://kiln3d.com/pricing.

- **Conformal-coating help for circuit boards (Business+).** Kiln helps
  you pick a coating for a populated board, flags how it could fail and
  what you still need to prove, and keeps the safety basics free on
  every tier. It never approves a coating or signs off a board — that
  stays with a qualified person.

- **Always know which supplier spec you built on (Business+).**
  Suppliers quietly revise the data sheets for their materials and
  chemicals. Give Kiln a data sheet and it locks that exact version to
  your design, keeps a copy you can pull back up anytime — and when you
  bring in a newer revision, it flags every decision that was built on
  the old one.

- **Learns from your real results (Enterprise).** Kiln remembers your
  inspection and field outcomes for a given setup and surfaces them next
  time — as context that never overrides a safety check.

- **Tidy your workshop (Pro+).** Archive a design to hide it from your list,
  and restore it anytime — nothing is ever lost. See
  https://kiln3d.com/pricing.

- **Tell Kiln how your prints fit — free.** Say how a hole came out and which
  way it printed: up, or on its side. Calipers or just plain words — either
  works. Enough reports and Kiln settles which way holes actually shrink — a
  thing makers argue about. *Pro turns it into fit guidance tuned to your
  printer and material — and now keeps the two print directions apart, so
  those numbers sharpen faster.*

- **Print checks now tune to your filament and your printer (free).**
  Warping, heat-stress and bed-grip warnings follow the material actually
  loaded and the machine that will run it — in slicing, cost estimates and
  the pre-print readiness check too.

### Changed

- **Nozzle wear is measured, not assumed (free).** If Kiln has no usage
  history for a nozzle, it now says so instead of reporting it as good as
  new. Every print adds to the count — including prints that failed, which
  wear a nozzle just the same.


- **Material intelligence now answers for every filament Kiln knows.** Skin
  contact, durability, multi-material bonding and safe-load guidance used to
  go quiet on less common materials — including everyday ones like TPU 95A,
  wood-fill and matte PLA. They answer across the catalogue now.

- **Iterating is free — a design is only spent when you keep it.** Try as many
  looks as you like; your free monthly designs are spent only when you download,
  print, or save one. *Kiln Pro makes them unlimited.*


- **More accurate strength, fit, and tolerance numbers across the Pro
  engineering tools (Pro+).** Expect more realistic results in several
  edge cases, including better support for specialty filaments like
  carbon-fiber blends.

- **When a material won't run, Kiln names what stops it — nozzle, bed or
  chamber.**

- **Sign in once, stay signed in.** Your `kiln signin` session now
  renews itself automatically in the background, so account features
  keep working without a re-login. If a session ever truly needs
  refreshing, Kiln shows you the one command to run.

### Fixed

- **Honest answers about unreadable files.** A file Kiln can't read
  (corrupted, or CAD that needs converting first) now gets told exactly
  that.

- **Filament runout sensors now work on Klipper printers.**

- **The live safety check no longer misses commands with a line number in
  front (free).** Some senders and firmwares prefix every G-code command
  with a line number. Kiln's real-time check was reading that prefix *as*
  the command, so a command that should have been flagged could pass. It
  now reads the actual command every time, and anything it can't make
  sense of is refused rather than waved through. Reported by Dmitriy
  Filatov ([Malder](https://github.com/Malder-coder)) — thank you.

- **Ordinary nozzle temperatures are no longer refused by mistake (free).**

- **Kiln now tells you when it's running on generic safety limits (free).**
  If Kiln doesn't recognise your printer it can fall back to limits looser
  than your machine's real ones. That used to be silent; it now says so up
  front instead of looking like full protection.

- **The free structural safety check is more accurate now (free).**
  Kiln Pro adds calibration and long-term-load checks for real
  projects — https://kiln3d.com/pricing.

- **A few adhesive picks' heat tolerances are now more accurate (Pro+).**

- **Enterprise printer-overage billing now works (Enterprise).** Reporting
  fleet printers beyond your included allowance to Stripe completes
  correctly.

## [1.2.0] - 2026-07-11

### Added

- **Every file Kiln touches carries proof of that — free for everyone.**
  Anything Kiln makes, modifies, or processes gets a short, honest note built
  into the file (Made / Modified / Processed with Kiln), plus a digitally
  signed record that confirms the exact bytes weren't altered afterward.
  Anyone can check a file at https://kiln3d.com/verify — drop the STL or 3MF
  and see whether it was made with Kiln, who made it, and whether it's
  untouched, no ID needed. The in-file note can be edited out of a copy, but
  the signed record still tells the truth; a file Kiln only imported or
  downloaded carries no claim at all.

- **Put your name on what you make (Pro+).** Turn on creator credit and every
  new file's signed proof carries your name, a link to where people can find
  you, and — if you want — a license notice on how the file can be used. On
  the verify page, anyone who opens your file sees exactly that. Your creator
  name is separate from your sign-in, can be changed once every 30 days, and
  an old name stays permanently yours — never handed to anyone else. See https://kiln3d.com/pricing.

- **Sharper 3D previews across the board.** Every preview Kiln renders —
  your models from all angles, AI-generated designs, decoration mockups,
  and printer thumbnails — now comes out noticeably crisper, with clean
  smooth edges instead of jagged ones. Same instant previews, just easier
  on the eyes.

- **Control your printer's fans directly (free).** Dial the part-cooling fan
  to any speed — more airflow for overhangs and bridges, gentler cooling on
  warp-prone materials. Works across Bambu Lab, OctoPrint, Moonraker/Klipper
  (Creality's K-series and Ender-3 V3 line included), direct-USB, and
  Elegoo's Centauri Carbon. Bambu Lab also gets the auxiliary and chamber
  fans, with the chamber fan available on enclosed models.

- **Look up what a Bambu error code actually means.** Paste the HMS code
  straight off your printer's screen or app into a printer troubleshooting
  check and get pointed straight to Bambu's own explanation — no more
  hunting the wiki by hand. *Free tells you where to look; Kiln Pro tells
  you what it means and what to do about it — the plain-English cause, the
  fix, and how serious it is.* See https://kiln3d.com/pricing.

- **Anonymous usage insights.** Kiln's daily anonymous check-in now includes
  which built-in tools get used most, helping us prioritize what to improve
  next. No personal data, no file contents — just tool names and counts, and
  it's fully covered by the existing opt-out.

- **Cleaner logo and image debossing.** Carved logos and images on your
  models no longer pick up a stray frame or background box, and
  text-bearing marks no longer render mirrored — the engraving now
  follows the actual shape of your artwork.

- **Decorations: real deletion, working thumbnails, safer signing.**
  - You can now actually delete a saved decoration, feature, or design
    (Pro+) — previously the only option was archive/hide. Deleting
    defaults to a safe, recoverable "retire"; permanent deletion needs a
    separate confirmation.
  - The decorations library on the web app now shows real preview images
    instead of blank placeholders (Pro+).
  - Signing a decoration release now shows you what you're signing
    before it's finalized (Pro+).

- **Use Kiln with any MCP client, no install at all.** A hosted connector
  lets Claude, Cursor, or any MCP-compatible app add Kiln by pasting a URL —
  sign in once, no download, no setup.

- **Your version history, now in the browser (Pro+).** Open any design,
  decoration, or feature at kiln3d.com to walk its branches, version timeline,
  and side-by-side comparisons — the full story of how it evolved, no install
  needed.

- **Skip a failed part mid-print, and see exactly what you save (Pro+).** When
  one object on a full plate fails, Kiln tells you which part to skip and how
  much of the print you keep by doing it, then drops just that part so the rest
  finishes. Pro covers your printer; Business skips across a whole fleet at once.

- **Designing something you'll wear? Kiln now gives you a straight answer on skin
  contact.** Make a ring, band, watch strap, earbud, or glasses and Kiln flags
  what matters about wearing a printed part against skin — honestly (no 3D print
  is truly "skin-safe"), points you to a professional for anything medical, and
  never blocks your print. Deeper material and compliance guidance is available
  on paid plans.

- **Set up Kiln in one paste — no account needed, free.** Hand your AI agent the
  setup prompt and it does the whole thing: installs Kiln, gets the right build
  of OpenSCAD (the engine Kiln designs with), connects Kiln to your agent, and
  gets you ready to make your first thing — no sign-up, no API key. A new
  `kiln install-openscad` command installs the correct OpenSCAD for your system
  in one step, and `kiln doctor` now checks the design engine honestly instead
  of waving a missing or outdated one through.

- **Repaired models now look like the original, not a rebuilt approximation.**
  The one-step repair for broken or AI-generated models got noticeably more
  faithful — creases, tips, and thin details that used to blur into a generic
  blob now come back sharp, and a small torn-open gap in a model (like under a
  brim or overhang) heals cleanly instead of leaving a ragged seam. Free users
  get a couple of fixes a month to try it; Kiln Pro removes the limit and tunes
  the repair further to your exact printer and material.

- **Make your first thing in the browser — free.** Sign in at kiln3d.com and
  make a real, printable object on the spot: pick a starter — coaster, keychain,
  desk nameplate, pet tag, fridge magnet, ornament, jewelry tray, pen cup,
  rolling tray, ashtray, bookmark, or frisbee — watch it build in 3D and
  download it to print. No install, no printer setup, no API key.

- **Color your make.** After you make something through Kiln's web app at
  kiln3d.com, give it a texture — tiger stripe, camo, ocean, marble, galaxy and
  more, in dozens of color combinations — and see it in those real colors in 3D
  before you download. (Color printing needs a multi-material printer; the
  download comes with the colors already assigned.)

- **Make organic, sculptural shapes.** Describe one in plain words and Kiln
  sculpts it — no add-on or key. New and still improving, and free to make and
  refine.

- **Finish a Klipper print after a power loss, in one call (Pro).** When power
  comes back, Kiln resumes the *same* print from the layer where it stopped —
  not a reprint. Klipper loses track of the bed height after a power-cut, so
  Kiln re-finds it safely — with a quick confirmation from you — then carries on
  finishing the object.

- **Triage a whole shop after an outage, in one call (Business+).** After a
  power outage, Kiln checks every printer in a mixed-brand, mixed-model fleet at
  once and sorts them into self-recovering, needs-a-quick-look, or reprint.

- **Print recovery gets a self-healing move (Pro+; Business+ can apply it
  fleet-wide across mixed printer brands).** When one part on a multi-object
  plate fails, Kiln's auto-recovery can now skip just that object and keep the
  rest of the plate printing — on printers that support it, and only when
  you've told it to. It also gets smarter over time: real outcomes from every
  skip feed back into how confidently Kiln recommends the move next time.

- **The Bambu A2L, dialed in (Pro+).** On top of free A2L support, Kiln now reads the printer like an expert — clog/feed-jam detection on any setup, build-plate-seated check, carbon-fiber nozzle advice, clear enclosure and AMS guidance, and more for paid tiers. See https://kiln3d.com/pricing.

### Changed

- **Clearer terms acceptance.** `kiln accept-terms` now shows the terms and asks
  you to type your acceptance, and re-shows them (with the date) if you've
  already accepted — so you can always see exactly what you agreed to.

### Fixed

- **Resume tells the truth now.** Hit resume when nothing's actually paused (say a print stopped after a power loss and the printer went idle), and Kiln now says "no paused print to resume" instead of falsely reporting that it resumed. Same honest behavior across every printer Kiln supports.

- **Recolor your design for free.** Fixed an issue where changing a single-color
  design to a new color asked for Pro. Only true multi-color designs (different
  colors on different parts) still need it.

## [1.1.9] - 2026-06-18

### Added

- **Kiln can update itself — just say the word.** When a newer version of Kiln
  is out, your AI assistant now offers to install it for you ("want me to update
  Kiln for you now?") and does it on a yes — instead of leaving you to copy a
  command. It holds off while a print is running, and tells you plainly that one
  quick restart finishes the job.

- **Decoration apply engine.** Kiln can now better render a saved decoration
  recipe — a logo, photo, or pattern — onto a model in a single step. Saving,
  versioning, and syncing those decorations across your devices are Kiln Pro
  features (https://kiln3d.com).

- **Your designs can back themselves up automatically (Pro+).** Turn on
  auto-backup and every time you save a design version, Kiln quietly pushes
  it to your private cloud workshop, so your version history is protected
  without you remembering to sync. Off by default; flip it on when you want it.
  (https://kiln3d.com)

- **Accept once, everywhere.** Accepting Kiln's Terms of Use is now a one-time
  tap, remembered everywhere you use Kiln — the CLI, your AI assistant, and the
  web app — so you're never asked twice.

- **Make objects without an API key.** A few item-creation paths used to
  nudge you toward setting up an outside AI service first. Now Kiln defaults
  to designing the model itself and previews it for you to refine — and a
  reference photo works the same keyless way.

- **Common items route straight to their dedicated maker.** Ask for something
  Kiln already supports — a coaster, tray, keychain and more — and it now
  reaches for that tuned generator automatically instead of building from
  scratch. (Pro+)

- **Turn any design into a production drawing (Business+).** Kiln can now give
  you a clean, dimensioned drawing of a part — top, front, and side views with
  real measurements, plus PDF and DXF files a machine shop or client can use.
  For something you designed in Kiln it fills in the exact measurements for you;
  for an imported model it measures what it can and says so. See https://kiln3d.com/pricing.

- **Previews now show up every time you make something, not almost every
  time.** Kiln already popped up a preview the moment you created or changed
  a model, but a few paths quietly skipped it, so you'd have to ask "show me."
  Those blind spots are fixed: the preview just appears, every time.

### Changed

- **Free accounts can sign in to the web app.** A free Kiln account now signs
  in to kiln3d.com and lands on a home that helps you connect Kiln to your AI
  app, so you can start designing and printing right away. The cloud workshop —
  version history, branches, and sharing for your designs — stays a Pro feature.

## [1.1.8] - 2026-06-12

### Added

- **Will this part survive gasoline? Vinegar? A summer in the sun?** Kiln now
  answers for the exact liquid or exposure you have in mind — and it's honest
  that printed parts take chemicals harder than solid plastic. The warnings
  are always free: if it's a bad idea, Kiln says so. The go-aheads are paid:
  everyday exposures — cleaners, oils, sunlight, weather — are Kiln Business,
  and the answers someone could get hurt by — fuels, automotive fluids, food
  contact, harsh chemicals — are Enterprise. See https://kiln3d.com/pricing.
- **Design-time sourcing-risk for bought parts (Enterprise).** Flags the
  off-the-shelf components a design depends on that are going end-of-life,
  single-source, or long-lead — before you commit a bill of materials you
  can't buy in a year. See https://kiln3d.com/pricing.
- **Part Passport (Pro+).** `get_part_passport` hands your agent everything
  about a design in one call, instead of asking tool by tool. Enterprise adds
  approval gates. See https://kiln3d.com/pricing.
- **Per-printer nozzle in `fleet_status`.** Now includes each printer's reported
  nozzle material and diameter, when the printer reports it (e.g. Bambu over
  MQTT).
- Business tier: import a material's datasheet and Kiln uses it in your designs, not just its built-in materials.
- **Won't-fit / can't-melt protection.** Kiln now stops a print that can't
  physically succeed — too big for the build plate, or a material your hotend
  can't melt — before it wastes filament or crashes the nozzle. It rotates a
  part to fit when it can, never blocks one it isn't sure about, and takes a
  one-command override (`force_print_oversize`) for when you're slicing for
  another printer. *Free tells you it won't work; Kiln Pro tells you how to
  make it — your printer's true usable area, the cleanest way to split an
  oversize part, and the material to switch to.*
- **Check if Kiln supports your printer — instantly.** A new [Supported Printers](https://kiln3d.com/printers)
  page lists every model Kiln ships a tuned profile for, grouped by brand with a
  live filter — Bambu Lab, Creality, Prusa, Elegoo, Sovol, Voron, AnkerMake, and
  more — so you can find your exact machine in seconds. The list stays in sync
  with the printer catalog automatically.
- **Sovol SV07 Plus support, plus a build-volume fix for the SV07.** Kiln now
  carries a tuned profile for the larger SV07 Plus, and corrects the standard
  SV07's build volume.
- **Deeper per-printer guidance (Pro+).** The model-specific know-how Kiln
  brings to a print — quirks, common failure modes, calibration tips — now runs
  as deep for budget and mid-range printers as for the flagships.
- **Adhesive intelligence for bonding printed parts (Pro+).** New MCP tools
  — `recommend_adhesive`, `get_adhesive_profile`, `record_bond_outcome` —
  recommend the right glue for joining two printed plastics with surface prep
  and the full cure schedule, cited to datasheets. See
  https://kiln3d.com/pricing.
- **Adhesive recommendation on glued joints (Pro+).** `validate_assembly`
  names the cited adhesive for your two materials; free tier gets a one-line
  pointer to `recommend_adhesive`.
- **Adhesive recommendations now learn from real-world bond outcomes (Pro+).**
  Free tier gives cited, datasheet-backed glue picks; Pro additionally lets
  accumulated field reports of how a glue actually held sharpen the ranking,
  marked as field data and never replacing cited specs. See
  https://kiln3d.com/pricing.
- **Factory work instructions in seven languages (Enterprise).** Controlled,
  shop-floor-grade assembly instructions — a procurable BOM, per-step torque and
  go/no-go checks, sign-off and gates — now generate end to end in English,
  German, French, Spanish, Italian, Chinese, or Japanese, each with its own page
  numbering, so a line anywhere in the world builds from the same source in its
  own language. See https://kiln3d.com/pricing.

### Changed

- **Load-bearing answers lead with the safety truth (free).** When you ask if a
  printed part can hold a load, Kiln says up front that it's a quick estimate,
  not a test of your real printed part — and names what never to trust it for
  without an engineer (overhead or climbing loads, child or vehicle parts).
  Borderline geometry now shows as a note instead of going quiet. *Free flags
  what's risky; Kiln Pro tunes the analysis to your printer — see
  https://kiln3d.com/pricing.*

## [1.1.7.1] - 2026-06-05

### Added

- **Your local tool calls now count on your stats.** When you're signed in, the
  CLI and agent work Kiln does on your own machine shows up on your /stats
  dashboard, not just activity from the web app.

## [1.1.7] - 2026-06-03

### Added

- **Clear your whole print queue in one go — free for everyone.** A new bulk
  cancel empties a backed-up queue (or just one printer's jobs) in a single
  step, with a preview so you can see exactly what'll be cancelled before you
  commit. It never touches a print that's already running — only jobs still
  waiting in line.
- **Two more Bambu Lab printers supported: the X1E and the P2S.** Kiln
  recognizes either on connect — right temperature limits, build volume,
  per-material settings, and a tuned slicer profile, no manual setup.
- **Bambu Lab A2L support.** Kiln recognizes the A2L on connect and at
  setup — temperature limits, build volume, per-material settings, and a
  tuned slicer profile, no manual setup.
- **Bambu Lab H2S support.** Kiln recognizes the enclosed H2S on connect and
  at setup — temperature limits, build volume, per-material settings across
  its full engineering-material range (ABS/ASA/PC/PA/CF), and a tuned slicer
  profile, no manual setup.
- **Start, stop, and check your AMS dryer — free for everyone.** On Bambu
  printers with an AMS 2 Pro or AMS HT, Kiln can now run a drying cycle (start
  or stop it) and show how damp the filament is, how much drying time is left,
  and the dryer's settings.
- **Kiln now tailors part designs to whatever printer you own.** When you ask it
  to design something, it factors in your machine's real build size, supported
  materials, and temperature limits — for every printer Kiln supports now, not
  just the popular flagships.
- **Kiln spots wet filament after a failed print.** If a failure looks like
  moisture (popping, stringing, weak layers), Kiln names it as a likely cause —
  works for any printer, any material.
- **A heads-up before you print a moisture-prone filament.** A gentle nudge for
  nylons, polycarbonates, CF/GF blends, supports, and other thirsty materials —
  works for any printer; never blocks the print.

### Changed

- **Emergency stop also turns off the AMS dryer (Bambu).** On Bambu printers
  with an AMS 2 Pro or AMS HT, an emergency stop also halts that printer's
  dryer — one printer or the whole fleet, matching whatever you stopped.
  Routine cancels leave the dryer running. Other printers are unaffected.

### Fixed

- **A Bambu P1P could be identified as an X1C.** Corrected the serial-number
  prefix map so each model is recognized from its serial.
- **Clearer message when a Bambu A1 nozzle-blob check pauses a print** — it now tells you what to do.
- **A jammed-extruder error now shows the real reason.** When a Bambu printer
  reports an extruder overload or clog, Kiln records it as a mechanical failure
  in your print history instead of an unlabeled error.

## [1.1.6] - 2026-05-29

### Added

- **Bambu Lab printers now appear in network discovery.** A scan
  picks them up automatically, instead of every Bambu having to be
  added by hand.
- **Prints route to the right AMS spool — and tell you which one.** On
  Bambu printers with an AMS, the everyday print now picks the correct
  loaded tray (matching your material when you name one) instead of
  guessing, and reports the tray it used. Applies to quick prints and
  reslices alike.
- **Community learning is now reliable end to end.** If your machine is
  offline when a print finishes, the anonymous outcome is saved and sent
  automatically once you're back online — it's no longer lost on a hiccup.
  And every print you monitor now contributes its result automatically,
  with no extra step.

### Fixed

- **Kiln-generated models slice reliably.** Some models Kiln built
  could fail to open in the slicer; they now load and slice every time.
- **No more lost results when several Kiln sessions run at once.** When
  more than one Kiln session shares the same machine, a busy moment could
  drop a just-finished write — a completed-print outcome or an anonymous
  community contribution — with no warning. Kiln now waits its turn and
  retries, so the write lands.

## [1.1.5.2] - 2026-05-28

### Added

- **Kiln tells you when there's a new version** — whether you're
  working through an AI assistant or on the command line — and
  `kiln self-update` does the upgrade for you.

## [1.1.5.1] - 2026-05-28

### Fixed

- **Saved `prusaconnect` printer configs keep working.** The Prusa
  adapter was renamed to `prusalink` in 1.1.5 with no alias, so existing
  configs pinning `type: prusaconnect` failed with "unsupported printer
  type." Kiln now auto-detects the old name and uses `prusalink`,
  logging a one-time notice to update your config — no manual edit
  required.

## [1.1.5] - 2026-05-27

Engravings get smarter — depth that matches your nozzle, the right
flip-axis picked for you, a sanity check that catches text which would
print backwards, and an auto preview of the finished face.  Plus:
nozzle wear becomes something Kiln watches across every print, every
printer, every recommendation — heads-up before you print on a worn
nozzle, failure analysis and recovery plans that consider wear, and
material recommendations that flag bad nozzle/filament combos up
front.

### Added

- **Engraving depth matches your nozzle.**  Smaller nozzles get
  shallower carves, larger nozzles deeper ones, so the engraving
  always survives the first layer instead of vanishing into the
  squish.  Asking for depth below the floor for your nozzle now
  fails loudly with a clear error.
- **Bottom-face flip picked automatically** based on the face
  shape — so the engraving reads correctly when you flip the
  printed part over.
- **Catches mirror-reversed text before you slice.**  A pure-math
  sanity check on every bottom-face engraving — if any transform
  in the pipeline would print the text backwards, the call fails
  with a fixable error instead of silently engraving backwards.
- **Auto preview of the finished engraving.**  Render shows what
  the underside will look like after the part comes off the bed.
- **Engraved text doesn't kiss the edges anymore.**  Default width
  tightened and a margin clamp added so "KILN" on a coaster sits
  comfortably inside the rim instead of hitting it.
- **Heads-up if a print would wear your nozzle out** *(Pro)*.
  Before a print starts, Kiln checks whether finishing this job
  would push the nozzle past its wear limit, and warns if so.
  Warns, doesn't block — your call.
- **Every supported printer feeds the wear model on every print**
  *(Pro)*.  Bambu, Moonraker, OctoPrint, Elegoo, and
  PrusaLink all report extrusion anomalies to the wear model.
- **Failure analysis considers nozzle wear** *(Pro)*.  When
  the failure symptoms line up with what a worn nozzle would
  cause, wear surfaces as a candidate instead of always pinning
  on bed adhesion or temperature.
- **Material recommendations factor your nozzle** *(Pro)*.
  Asking for an abrasive filament (carbon-fiber, glass-fiber,
  wood-fill, or metal-fill) on a brass nozzle now warns up
  front — brass wears out fast on abrasives.  Stainless gets a
  milder caution; hardened steel is the right pairing and stays
  quiet.
- **Recovery plans suggest "replace the nozzle" when that's the
  real fix** *(Pro)* — instead of defaulting to "same
  nozzle, lower temp, try again" when the part actually needs a
  fresh tip.

### Changed

- **Renamed the Prusa adapter to `prusalink`.**  Update saved
  configs that pin `type: prusaconnect`.  No back-compat alias.

### Compatibility

- Pair with `kiln-pro >= 1.1.5` — product cards inherit the new
  engraving engine, nozzle-aware verdicts, and federation opt-in.

## [1.1.4] - 2026-05-20

Native Windows support — install Kiln, connect your AI agent, and
generate designs on a stock Windows machine, with setup made easy.
The release also sharpens overhang detection for cantilevers and
islands, makes AMS Lite filament readings accurate, and puts safety
messages in plain language.

### Added

- **Native Windows support.**  The CLI, `kiln install-mcp`, and design
  generation run on a stock Windows install.  The install page
  (kiln3d.com/install) has a Windows walkthrough — Python setup through
  agent connection — with a copy-paste prompt that hands the whole
  setup to an AI agent.

### Changed

- **Plain-language safety and connection messages.**  Warnings describe
  what they mean for your print instead of leaning on internal terms.
- **Accurate AMS Lite filament readings.**  The Bambu A1 and A1 mini's
  AMS Lite can't measure remaining filament, so Kiln reports the level
  as unknown instead of a placeholder number.

### Fixed

- **Overhang detection holds for cantilevers and islands.**  Kiln
  clears supports only when an overhang is genuinely bridgeable; a
  one-sided cantilever or a floating island keeps "needs supports."
- **Clearer diagnostics.**  `kiln doctor` warnings name their check,
  and pointing Kiln at a missing model file reports "Mesh file not
  found" up front.

## [1.1.3] - 2026-05-19

Adhesion catches tall narrow detach risk regardless of material,
accounts for thermal stress from layer cooling, and admits when
it's uncertain.  Prints remember which goal they came from, so
monitoring, reprints, and version saves don't need to be told twice.

### Added

- **Adhesion model knows when geometry alone is risky.** A 2×2×250
  mm tower in any material now reads as "marginal" regardless of
  the force ratio — extreme aspect ratios (> 50) concentrate peel
  stress at the base in ways the static formula doesn't capture.
- **Aspect-ratio-aware peel force.** Peel grows nonlinearly with
  height-to-base ratio above ~10:1. Tall vases / pen holders stay
  secure; tall narrow towers in any material get flagged.
- **Thermal-stress contribution to peel** (free tier sees it for
  every material at a conservative default; Pro tier tunes it per
  material). ABS, ASA, Nylon, PP, and PEEK now generate larger
  effective peel stress on tall prints because they actually do —
  cooling layers cyclically pull at the base. PLA / PETG see
  proportionally less because they actually shrink less.
- **Adhesion verdicts admit when they're uncertain.** Every
  `AdhesionForceEstimate` now carries a `model_confidence` field:
  `"high"` for clear extremes, `"approximate"` for the messy middle
  where the static model is less reliable. Lets agents soften
  wording on uncertain verdicts.
- **Heads-up when the slicer will bridge instead of support.** For
  short-span undersides (tabletop tops, U-shapes, square bridges)
  the slicer usually crosses the gap with a bridge rather than
  building a support tower. Kiln now flags those so you can force
  supports if you want a smoother bottom surface.
- **Smart bridging.** Kiln no longer tells you to add supports
  when your printer can simply bridge across a small horizontal
  gap on its own. One clear verdict with the reason.
- **Kiln Pro tells you how many grams of filament your supports
  will use.** A number you can plan a spool around. Matches what
  your slicer will actually print — sticky filaments like PETG and
  TPU get bumped up because they take more to peel cleanly.
- **Overhang limits know your filament.** Bendy filaments like
  TPU get flagged at steeper angles; stiff ones like PLA get more
  leeway. (Free tier still uses the universal 45° rule of thumb;
  Kiln Pro tunes it per material.)
- **Support estimates always match the verdict.** If Kiln says
  you need supports, it now always tells you roughly how much
  filament those supports will take. No more "needs supports"
  paired with a 0-gram estimate.
- **Arched and pillared ceilings appear in the bridging report.**
  Curved doorway tops, colonnaded ceilings, rows of windows above
  pillars — each unsupported span shows up with its actual length,
  so bridge warnings fire.
- **Wall thickness probed directly from the mesh.** Stable across
  tessellation densities, single-body or multi-body.
- **Lattices and scaffolds: each bar measured separately.** A
  1.5 mm lattice bar reads at 1.5 mm regardless of joint geometry.
- **Helical-surface filter on thin-wall probes.** Threaded rods and
  springs report at their structural thickness.
- **Pro+ measures your rod's actual diameter.** Threaded rods,
  springs, and knurled grips come back with two concrete numbers
  — the rod's structural cross-section and the depth of its
  surface features — when you have Kiln Pro. Replaces the
  free-tier "this reading might be measuring surface texture"
  qualifier with hard numbers.
- **Cavity-width check** for engraved grooves, slot cuts, and
  debossed text — narrow cuts below the slicer's extrusion floor
  now show up with their actual widths.
- **Slicer support style on `analyze_printability`** —
  `slicer_style="grid"|"snug"|"organic"|"tree"`. Kiln Pro
  estimates support grams that style actually extrudes.
- **Saved goals are listable.** The CLI and any MCP client can ask
  Kiln for the list of saved goals, so agents and the web app can
  show an inbox view.
- **Free-tier design intelligence finds what you ask for.** Material
  recommendations, design template discovery, printer profile
  lookups, BOSL2-using SCAD generators, and Bambu A1 print prep now
  have their catalogs on disk.
- **Release gate** asserts data files ship in the wheel before any
  PyPI publish.

### Changed

- **Bridge length uses the longer XY axis of the bridge
  footprint** so warnings fire regardless of which direction the
  slicer chooses to bridge.
- **Adhesion docstring honesty** — the `risk_level` field is now
  documented as a best-effort approximation, especially in the
  middle range. For high-aspect-ratio prints in warp-prone
  materials, treat `secure` as `plausible` rather than `verified`.
- **Thin-wall reports return `0.0` when no thin walls are found.**
  Was `nozzle_diameter`. Read `thin_wall_count > 0` before treating
  `min_wall_thickness_mm` as a measurement.
- **`PrintabilityReport.triangle_count`** — the mesh's triangle
  count is now on the report so callers can tell coarse meshes
  apart from detailed ones.
- **Pro thresholds scale with your nozzle.** Kiln Pro's per-material
  wall and hole floors now match the nozzle you're actually
  printing with. A 0.6 mm nozzle gets 0.6 mm-appropriate thresholds
  instead of always using the 0.4 mm baseline.
- **Cavity check scales with part dimensions** so large interior
  cavities register.
- **New `PrintabilityReport` shape signals:** `dimensions_mm`,
  `connected_components`, `component_size_uniformity`. Kiln Pro
  reads these to recognize lattices and multi-part topologies.
- **Prints remember which goal they came from.** Monitoring a print,
  reprinting it, saving a new version, or iterating on a design all
  pick up the saved goal automatically.  Agents no longer need to
  restate the goal at every step.
- `get_design_brief` is renamed `analyze_design_requirements`. The
  old name is removed. This only affects callers that asked for the
  functional analysis directly; the user-facing entry point for new
  designs (`design_session`) is unchanged.

### Fixed

- Walls angled exactly 45° now get checked properly. A rounding
  quirk was letting them sneak past the overhang check.
- When you tell Kiln what filament you're using, design audits
  now use that filament's actual overhang limit instead of
  falling back to the generic 45° rule.
- Support estimates no longer come back as zero on T-shapes,
  mushrooms, tabletops, umbrellas, and other shapes where the
  overhang sits above a narrower base.
- Support percentage no longer reports above 100% on rare
  thin-disc-on-tall-post geometries.
- **Hole detection works on tilted parts.** No more straightening
  required. Parts coming out of slicer auto-orient or any other
  tilt source (typically 15–30° off) used to silently produce zero
  hole warnings; now the detector recovers each hole's axis from
  its own geometry, so 3°, 30°, 45° — any tilt — surfaces the
  same warnings a flat-laid part would.
- **Chamfered hole entries no longer hide the underlying hole.**
  Holes with countersunk or 45° chamfered mouths used to read as
  "non-circular feature" once the chamfer ate enough of the
  cluster's normal budget; the detector now tries every principal
  direction of the cluster as a candidate axis and keeps just the
  cylinder wall for the radius fit. Sparse-mesh cylinders (6-8
  segments — common in low-poly props) also stay together where
  the previous version shattered them on a floating-point boundary.
- **Hex pockets are distinguishable from drilled holes.** Each
  detected hole carries a new `facet_segments` field: smoothly
  tessellated round bores report 20-48 facets, hex nut traps
  report 6, octagonal pockets 8. Callers (printability
  recommendations, kiln-pro overlays) can use the count to suppress
  hole-diameter warnings on intentional polygonal pockets without
  the detector having to guess intent from the mesh.
- **Sub-floor pocket recommendations now match what you can
  actually do about them.** A 0.5 mm round bore below the FDM
  hole-printing floor gets the existing "enlarge in CAD or drill
  after printing" recommendation; a 0.5 mm hex pocket gets a
  different one that explicitly says drilling won't help and points
  at the inscribed-dimension issue. Polygonal pockets used to get
  the round-bore advice, which was misleading.

### Internal

- New CI guard asserts the `kiln-pro` package is NOT installed in
  the public Kiln CI environment — prevents future tier-coupling
  leak regressions where public tests silently depend on the Pro
  overlay.

### Compatibility

- Released as `kiln3d` 1.1.3 — pair with `kiln-pro` 1.1.3 for the
  Pro+ experience.

## [1.1.2] - 2026-05-16

Hole-aware printability, automatic previews on the tools that change a
mesh, audits that pick up where inspection left off, and recovery
events on the bus so agents can wait on a recovered print instead of
polling.

### Added

- **Printability analysis detects holes.** Each hole's position, size,
  depth, and orientation is reported; undersized holes get flagged.
- `detect_holes(file_path)` is exposed standalone in
  `kiln.generation.validation`.
- `analyze_printability` gets two new arguments: `printer_id` (forwards
  to Pro+ per-printer tuning when available) and
  `include_hole_detection` (default True; perf opt-out).
- **Automatic inspection previews** on `preflight_check`,
  `start_print_recovery`, and `complete_print_recovery`.
- **Tools that change a mesh show you what they did.** Saving or
  rolling back a design version, applying a decoration, composing
  parts, rotating a model, importing a STEP file, and ~20 other mesh
  operations attach a small preview to their response.
- **Audits skip work when the inspection already happened.** If an
  earlier tool already inspected the mesh, the audit reuses those
  findings instead of re-running them.
- **Audits confirm the design matches what you asked for.** When the
  design started from a saved design-goal questionnaire (Pro),
  the audit ends with a plain-English summary or names the goals that
  weren't met.
- **Audits surface concrete remediation candidates.** On Kiln Pro, the
  audit lists the specific fixes available for each warning. Pass
  `apply_remedies=True` to let the overlay dispatch them; the default
  surfaces the options without mutating.
- **Recovery is on the event bus.** Three new event types —
  `RECOVERY_NEEDED`, `RECOVERY_STARTED`, `RECOVERY_COMPLETED` — let
  agents wait on recovery instead of polling. `watch_print_status`
  wakes on `recovery.completed` by default.

### Changed

- **Every Kiln event names who triggered it.** Each event records its
  origin — system, agent, or user — and carries that across threads.

### Fixed

- Hole detection handles rotated or simplified meshes.
- `reslice_with_overrides` and `slice_and_print` now include the
  `calibration_used` block on the response when a calibrated profile
  applies, matching `recommend_settings`'s shape.

## [1.1.1] - 2026-05-13

Vision event-name split, MCP-client config self-heal, an event-aware
`watch_print_status` subscribe mode, and a few quality fixes.

### Added

- `watch_print_status` `block_until_event` subscribe mode — agents can
  await a specific event instead of polling.
- `kiln health`: detects MCP-client config drift and self-heals stale
  `kiln` command paths in Claude Desktop / Claude Code / Codex configs.
- `kiln install-mcp`: richer installer promoted from kiln-pro.
- `kiln serve`: orphan-host watchdog exits cleanly when the MCP host
  goes away.

### Changed

- Vision events: `VISION_CHECK` split into `VISION_FRAME_CAPTURED` and
  `VISION_AGENT_INSPECTION`.  Subscribers should migrate; the original
  name is removed.
- The split is by **actor**, not payload size: `VISION_FRAME_CAPTURED`
  marks a system auto-capture (background watcher / first-layer
  monitor) and carries the snapshot bytes plus raw signals;
  `VISION_AGENT_INSPECTION` marks an explicit agent
  `monitor_print_vision` call and carries thin metadata.  Subscribers
  can now distinguish routine monitoring from deliberate inspection.
- Pro-tool manifest registers `get_design_pull_request` so agents
  discover it without a kiln-pro install.

### Fixed

- `print_health_monitor`: silent stall-event publish — stall warnings
  were observable internally but never reached subscribers.
- Bambu adapter: `can_snapshot=True` when the printer has a working
  camera URL, regardless of ffmpeg availability.
- `kiln health`: tolerates invalid TOML escapes in third-party MCP
  client configs.

## [1.1.0] - 2026-05-06

Design templates rebuild + automatic Pro+ calibration on every slice
tool + safer pre-print validation gate + a self-service tier diagnostic.

### Added

- **`check_my_tier` MCP tool** — answers "what plan am I on, and why" for
  agents handling user questions about tier / subscription / paywall
  confusion. Walks the live tier-resolution chain (env var → license
  file → OAuth session → cached entitlement → free fallback) and
  returns a plain-English summary the agent can paste straight to the
  user. Free-tier safe — no license needed to call.
- **First-time calibration overlay notice** — the first time Kiln
  applies your slicer calibration to a slice (Pro tier), the response
  carries a `first_time_notice` block that the agent can surface to
  the user ("Kiln just used your OrcaSlicer profile…"). After that,
  silent overlay with the audit trail in every slice response.
  Removable marker at `~/.kiln/calibration_overlay_first_use.seen` if
  you want to re-trigger the notice.
- **Pro+ continuous calibration learning** — when Kiln Pro is
  installed, per-printer offsets refine from every observation (not
  just one-time profile imports).  Freshness tracking surfaces when
  your printer drifts; full provenance answers "why is this dimension
  what it is?".
- **`tablet_stand` template** — dedicated for tablets / iPads, with
  wider base depth, taller back panel, and slot sized for keyboard
  cases. Phone stands stay focused on phones.
- **Pre-print validation gate runs by default** — `slice_and_print`,
  `run_quick_print`, and `run_reslice_and_print` now validate the
  mesh against printer + material constraints before slicing.
- **Per-machine calibration overlay applied uniformly** — Pro+
  calibration now applies to `slice_and_print` (previously only
  `slice_model` and `reslice_with_overrides`).
- **`kiln events tail` / `kiln events summary`** — read-only CLI
  access to the local event log.
- **Craftcloud order safety gates** — fulfillment orders now require
  an explicit preview-confirmation token AND a shipping-address
  confirmation token before placing.  Two new endpoints
  (`POST /api/fulfillment/preview-confirm`,
  `POST /api/fulfillment/shipping-confirm`) issue the tokens after
  the user has actually seen the rendered preview and reviewed the
  shipping address.  Prevents "agent placed an order I didn't
  expect," surprise shipping mismatches, and stale-quote charges.
- **Local shipping profiles store** — named shipping addresses now
  persist locally at `~/.kiln/shipping_profiles.json` (override via
  `KILN_SHIPPING_PROFILES_PATH`) and can be referenced by name when
  issuing a shipping-confirm token.  No more re-entering the same
  address every time you order a part for the same workshop or
  customer.
- **Food-safety material overlays** — material-recommendation,
  slice, and food-contact tool paths now consult a structured
  food-safety catalog.  PETG / PETG-HF surface as food-safe; PLA
  returns a `conditional` verdict with constraints (water-only,
  hand-wash, replace 6-12 months); ABS, TPU, ASA, and CF blends
  hard-refuse with `MATERIAL_NOT_FOOD_SAFE`.  Affects
  `generate_pet_bowl`, material recommendations, and slice metadata.
- **Anonymous unique-device counting** — heartbeat telemetry
  (already on in v1.0) now includes a one-way-hashed device
  fingerprint so we can count unique installs without identifying
  anyone.  No reversal possible; no PII collected.
- **Free-tier `recommend_settings` calibration overlay** — free
  users now get per-printer calibration data folded into setting
  recommendations when a calibrated profile is present (Pro+ writes,
  free reads).  Smaller-than-Pro continuous-learning, but real.

### Changed

- **3 design-template MCP tools renamed** — `find_design_templates`,
  `list_design_templates_catalog`, `get_design_template_info`. The
  prior `_design_patterns_` names are removed in this release (see
  Removed below).
- **Design template count: 17 → 18** — dropped 3 (cable management
  clip schema break, GoPro mount single-vendor, wall mount bracket
  folded into cantilever bracket), then split phone stand into
  `phone_stand` + `tablet_stand`.
- **`design_patterns.json` → `design_templates.json`** — file name
  matches the user-facing concept.  Old Python imports
  (`DesignPattern`, `pattern_id`, `get_design_pattern`, etc.) are
  removed in this release (see Removed below).
- **WHITEPAPER + LITEPAPER** — refreshed to match current capability
  + count.
- **Site nav** — Install hoisted to the top of the Product menu;
  install copy refreshed.

### Fixed

- **Free-tier safety-floor fallback** — when the engineering overlay
  isn't loaded, design intelligence returns conservative safety-floor
  recommendations instead of an error.
- **PTFE temperature clamp now actually fires** — the safety guard
  capping PTFE-lined hotends at 240°C had a silent failure path
  (`PrinterIntel` dataclass treated as a dict) that meant Ender 3,
  Ender 5, and any other PTFE-lined hotend was getting the profile's
  raw 260°C ceiling instead of the PTFE-safe 240°C.  Now clamps
  correctly; set `KILN_OVERRIDE_PTFE_LIMIT=1` to disable for
  user-installed all-metal conversions.
- **Decoration engine fixes** — face detection, deboss math,
  decoration hierarchy, and fit calculations all hardened in
  this release.  Affects users who hit edge-case decoration
  bugs in v1.0.
- **Documentation counts** refreshed across README, white paper,
  litepaper, FAQ, and the marketing site.

### Removed

- MCP tools `find_design_patterns`, `list_design_patterns_catalog`,
  `get_design_pattern_info` — replaced by the `_design_templates_`
  variants in this release.  No back-compat aliases.
- Python imports `DesignPattern`, `pattern_id`, `kb.patterns`,
  `get_design_pattern`, `list_design_patterns`,
  `find_patterns_for_use_case` — all renamed to `DesignTemplate`,
  `template_id`, `kb.templates`, `get_design_template`,
  `list_design_templates`, `find_templates_for_use_case`.

## [1.0.0] - 2026-04-29

All changes vs v0.5.0:

### Added

- **Stable Public Package** — Kiln now ships as `kiln3d 1.0.0` on PyPI with production/stable package metadata.
- **MCP Registry Release** — Kiln now publishes 1.0.0 metadata for MCP-compatible clients.
- **Expanded MCP Surface** — Kiln now exposes 763 total MCP capabilities for local agent-operated manufacturing workflows.
- **Expanded CLI Surface** — The `kiln` CLI now exposes 215 commands across setup, design, slicing, printing, monitoring, recovery, and release workflows.
- **MCP Installer Command** — Added `kiln install-mcp` for easier setup in MCP clients.
- **Account Pairing Flow** — Added local sign-in, pairing, and account-status commands for connecting a machine to a Kiln account.
- **Tool Discovery Metadata** — Updated capability metadata so agents can find the right tool faster.
- **Git for 3D Foundations** — Added design versioning, ancestry, aliases, rollback, mesh-only imports, and release-signing foundations.
- **STEP Import** — Added STEP file import support with multi-backend handling.
- **Design Recipe System** — Added recipe/provenance tracking so generated and modified objects can be edited later.
- **Surface Decoration Tools** — Added workflows for text, image, SVG, photo, and reusable surface decorations.
- **Photo Emboss Pipeline** — Added image-to-surface and emboss/deboss tooling for relief-style printed surfaces.
- **Surface Intelligence** — Added face detection and placement helpers for decorating printable models.
- **Multicolor 3MF Composer** — Added 3MF composition for multicolor and multi-material print outputs.
- **Colored 3MF Preview** — Added per-face color rendering so multicolor files can be previewed before printing.
- **Universal Model Visualizer** — Added adaptive model preview rendering for flat, tall, colored, and Bambu-wrapped 3MF files.
- **Mesh Tooling Expansion** — Added repair, split, merge, mirror, hollow, simplify, compare, orient, and printability tools.
- **Pre-Generation Estimator** — Added time, filament, and cost estimates before a model is generated.
- **Post-Generation Validation** — Added validation gates for AI-generated meshes before print.
- **OpenSCAD Verification** — Added static checks for common parametric model mistakes before printing.
- **Generation Feedback Loop** — Failed print outcomes can feed back into future generation constraints.
- **Material Intelligence Expansion** — Expanded material data, brand filament profiles, compatibility rules, troubleshooting, and post-processing guidance.
- **Filament Resolver** — Added smarter brand/material matching across slicing, recommendations, and print planning.
- **Multi-Material System Support** — Added support metadata for multi-material printer workflows.
- **Creality Moonraker Support** — Added Creality local Moonraker adapter support where local Moonraker is reachable.
- **Bambu AMS Auto-Routing** — Added AMS-aware material routing and safer tray handling.
- **Multi-Color Copies** — Added workflows for printing multiple copies in different AMS colors.
- **MakerWorld Adapter** — Added MakerWorld marketplace support alongside existing marketplace integrations.
- **Assembly Manual Metadata** — Added public-side metadata for assembly manual workflows.
- **Print Health Monitor** — Reworked monitoring around a canonical health monitor with predictive risk and failure signals.
- **Print Watchdog** — Added watchdog support for safer monitoring and recovery decisions.
- **Incident Recorder** — Added structured incident recording for safety-critical printer events.
- **Failure Vocabulary** — Added unified failure type and severity normalization.
- **Printer Model Resolver** — Added explicit printer model resolution instead of unsafe guessing.
- **Safety Gap Warnings** — Added warnings when Kiln cannot fully verify printer-specific safety limits.
- **G-Code Interception Rules** — Added default interception rules for safer printer command handling.
- **Preview Confirmation Gate** — Added confirmation tokens so print starts can be tied to a verified preview.

### Printer & Slicing Improvements

- **Bambu 3MF Reliability** — Improved Bambu 3MF wrapping, thumbnails, object printing, and startup handling.
- **Bambu MQTT Compatibility** — Fixed Bambu connection behavior with newer MQTT client versions.
- **Bambu A-Series Handling** — Improved A-series state parsing, file transfer behavior, and print-start polling.
- **AMS Ambiguity Handling** — Kiln now fails safer when AMS tray state is ambiguous.
- **Build Volume Resolution** — Printer build volumes now resolve consistently across tool paths.
- **Slicer Profiles Expansion** — Expanded bundled slicer profile metadata and printer compatibility data.
- **3MF Extraction** — Added extraction support for models embedded inside `.3mf` and `.gcode.3mf` files.
- **Prusa/Bambu Time Estimates** — Improved print time estimation from slicer output.
- **SCAD Color Preview Fixes** — OpenSCAD previews now preserve expected colors.
- **Adaptive Preview Angles** — Model previews choose better angles for different part shapes.

### Fixed

- **Config Validation** — `printer_model` validation now preserves case-sensitive model IDs.
- **Registry Recovery** — Printer registry access is more reliable across CLI, MCP, and plugin paths.
- **Monitor Import Fixes** — Fixed missing health-check imports and retired stale monitor paths.
- **Tool Name Cleanup** — Removed stale suffixes and repaired naming collisions.
- **Auth Command Cleanup** — Clarified sign-in, pairing, linking, and identity behavior.
- **Preview Token Binding** — Preview confirmation now binds more reliably to the intended file.
- **Path Traversal Fixes** — Hardened marketplace downloads, snapshots, and file destinations.
- **Shell Injection Fix** — Hardened preview notification command handling.
- **Bambu Credential Masking** — Bambu access codes are masked in sensitive output paths.
- **Public Install Cleanup** — Removed install paths that could fail for normal public package users.

### Security & Safety

- **License Change** — Kiln moved to AGPL-3.0-or-later with commercial licensing available.
- **Release Signing Workflow** — Added signed-release workflow foundations.
- **SBOM Workflow** — Added software bill of materials generation.
- **SLSA Attestation Workflow** — Added provenance/attestation workflow for release artifacts.
- **PyPI Publishing Hardening** — Fixed release workflow issues for trusted publishing.
- **Printer Crash Prevention** — Added stronger safety checks around upload, startup, and command execution.
- **Tier Boundary Clarity** — Free, Pro, Business, and Enterprise capabilities are clearer across CLI, MCP metadata, and account-aware workflows.

## [0.4.2] - 2026-03-18

### Added
- SDCP V3 HTTP push upload for Elegoo Centauri Carbon with V2 fallback — thanks [@bobbyhiddn](https://github.com/bobbyhiddn)
- Multi-color multi-copy printing (each copy a different AMS color)
- Parametric design template library (10 OpenSCAD modules: gears, threads, containers, lattice, etc.)
- Three-layer generation system (parametric library, compile-fix loop, visual verification)
- MCP session greeting — agents see full capability map on connect without discovery
- did-you-mean CLI suggestions for mistyped commands
- 3MF cost estimation from Bambu/OrcaSlicer slice metadata
- Improved upon Image-to-3D support via Gemini Deep Think provider
- Fulfillment materials command and smart material resolver

### Fixed
- Heatup false-positive temp drift alerts during cold starts (reached-target gating)
- Elegoo CLI adapter missing from `_make_adapter()` (silently fell through to Moonraker)
- Elegoo WebSocket deadlock (`Lock` to `RLock`)
- Elegoo SDCP V3 `CurrentStatus` returned as list instead of int
- `mcp.instructions` read-only property crash on FastMCP 1.9+
- Phantom MCP tool references in agent guidance
- External thread compile time 2min to 17sec
- Stale skill manifest test

### Dependencies
- certifi 2026.1.4 to 2026.2.25
- rich 14.3.2 to 14.3.3
- pydantic-settings 2.12.0 to 2.13.1
- GitHub Actions: upload-artifact v4 to v7, download-artifact v4 to v8

### Added
- 25 mesh analysis and transformation MCP tools (pure Python, no external mesh libraries):
  - Geometry analysis: `analyze_mesh_geometry`, `mesh_quality_scorecard`, `analyze_non_manifold_edges`, `diagnose_mesh`
  - Repair: `repair_mesh`, `repair_mesh_advanced` (hole closing), `remove_mesh_floating_regions`
  - Transformations: `mirror_mesh_model`, `hollow_mesh_model`, `center_model_on_bed`, `scale_mesh_to_fit`, `optimize_print_orientation`, `simplify_mesh_model`
  - Composition: `compose_models`, `merge_mesh_files`, `split_mesh_by_component`
  - Estimation: `estimate_material_cost`, `estimate_support_material`, `estimate_mesh_print_time`, `predict_print_failure`
  - Comparison: `compare_mesh_versions` (with Hausdorff distance)
  - Readiness: `check_print_readiness` (single-call gate with optional auto-repair)
  - Export/Import: `export_model_3mf`, `extract_model_from_3mf` (3MF/gcode.3mf → STL)
- Parametric design templates with `list_design_templates`, `generate_from_template`, `generate_template_variations`
- Design advisor tool for recommending generation approach (template, OpenSCAD, or AI)
- Closed-loop design iteration: `iterate_design` (generate → validate → improve → regenerate)
- Print cost estimation in `monitor_print` and `preflight_check` (9 materials, USD estimates)
- Cross-printer learning feedback loop with auto-outcome recording from scheduler
- `recommend_settings` MCP tool for history-based print setting recommendations
- Outcome-aware preflight warnings (low success rate advisories)
- `get_started` MCP onboarding tool for AI agents
- `safety_status` MCP dashboard tool
- `confirm_action` two-step confirmation gate for destructive operations
- `safety_audit` MCP tool for querying the safety audit log
- Smart printer routing based on historical success rates
- Tier-aware agent error messages with suggested alternative tools
- Agent onboarding improvements (`get_started` tool, session recovery hints)
- Fly.io deployment support (`deploy.sh`, `Dockerfile.api`, GitHub Actions workflow)
- Circle setup script (`scripts/circle_setup.py`) for one-time entity secret and wallet provisioning
- Health check endpoint (`/api/health`) on REST API
- Donation info endpoint on REST API

### Changed
- `generate_and_print` and `download_and_upload` no longer auto-start prints (upload only, explicit start required)
- Auto-print toggles for marketplace and generated models (env var opt-in, default OFF)
- Docs: clarified product boundary to reflect existing intent (orchestration layer, partner integrations; no strategy change)
- Partner/provider naming is now canonical for remote integration surfaces (`partner` CLI group and provider-oriented MCP tools)
- Legacy `network_*` MCP tools and `kiln network ...` CLI remain as compatibility aliases (deprecated in `v0.2.0`, removal target `v0.4.0`)
- Fulfillment responses now include explicit provider ownership metadata (`provider_name`, `provider_order_id`, `provider_terms_url`, `support_owner=provider`, `merchant_of_record=provider`)
- Billing language standardized to "orchestration fee" in user-facing docs/CLI copy
- SKILL.md reorganized: quick start moved to top, fulfillment section added, JSON response examples
- Enriched `kiln status --json` with `printer_name` and `printer_type` fields
- Improved config validation errors with actionable quick-fix suggestions
- Bambu MQTT timeout error now includes troubleshooting checklist
- Rewrote Circle payment provider for W3S Programmable Wallets API (replaced deprecated Transfers API)
- Circle payments now use RSA-OAEP entity secret encryption for secure wallet operations

### Fixed
- CI failures: OpenSCAD macOS fallback test on Linux, flaky uptime test tolerance
- Bambu A1/A1 Mini uppercase state parsing
- Bambu A-series implicit FTPS on port 990
- Print start confirmation polling for Bambu printers
- YAML parse errors now surfaced instead of silently returning empty config

### Dependencies
- Added `cryptography>=41.0` to payments optional dependencies

### Security
- Safety audit log records all guarded/confirm/emergency tool executions
- Emergency cooldown escalation (circuit breaker) for repeated blocked actions
- Unified temperature limit resolution via safety profiles (single source of truth)
- Pause/resume rate limiting to prevent mechanical wear
- Dry-run mode for `send_gcode`
- G-code auto-detect printer profile from slicer comments

## [0.1.0] - 2026-02-10

### Added
- OctoPrint REST adapter (full printer control)
- Moonraker REST adapter for Klipper-based printers
- Bambu Lab MQTT adapter for X1C, P1S, A1 over LAN
- Prusa Link REST adapter for MK4, XL, Mini+
- MCP server with 79+ tools for AI agent printer control
- CLI with 47+ commands and `--json` output on every command
- Fleet management: multi-printer registry, fleet status
- Priority job queue with background dispatch and auto-retry with exponential backoff
- Mandatory preflight checks before print jobs
- G-code safety validation with per-printer limits (28 printer safety profiles)
- Bundled slicer profiles for 14 printer models (PrusaSlicer/OrcaSlicer)
- Printer intelligence database (firmware quirks, material compatibility, failure modes)
- Pre-validated pipelines: quick_print, calibrate, benchmark
- Slicer integration (PrusaSlicer, OrcaSlicer) with auto-detection
- Model marketplace adapters: MyMiniFactory, Cults3D, Thingiverse (deprecated — acquired by MyMiniFactory, Feb 2026)
- Fulfillment service adapters: Craftcloud
- Text-to-model generation via Meshy AI (cloud) and OpenSCAD (local)
- Mesh validation pipeline (STL/OBJ parsing, manifold check, dimension limits)
- Print cost estimation from G-code analysis
- Material and spool tracking with mismatch warnings
- Bed leveling trigger system with configurable policies
- OTA firmware updates for OctoPrint and Moonraker
- Webcam snapshot capture and MJPEG stream proxy
- Print history and agent memory persistence (SQLite)
- Cross-printer learning database with outcome tracking
- Closed-loop vision monitoring (snapshot + print phase hints)
- Print failure analysis and post-print quality validation
- Await print completion polling tool
- Local vs. fulfillment cost comparison tool
- Webhooks with HMAC-SHA256 signing
- Event bus with pub/sub
- Cloud sync for printer configs and job history
- Plugin system with entry-point discovery
- API key authentication with scope-based access
- Billing/fee tracking for fulfillment orders (5% platform fee, first 3 free/month)
- Multi-model agent support via OpenRouter (any OpenAI-compatible LLM)
- REST API wrapper (FastAPI) exposing all MCP tools as HTTP endpoints
- Tool tiers: essential (15), standard (43), full (101+)
- Network printer discovery via mDNS and HTTP probing
- Device type generalization (FDM, SLA, CNC, Laser forward-compatible)
- Resumable marketplace downloads with HTTP Range headers
- One-line install script for Linux/macOS

### Security
- Temperature range enforcement per printer and material
- Path traversal prevention on snapshots, slicer output, and Bambu file operations
- Agent tool result sanitization (injection pattern stripping, truncation)
- REST API hardening: parameter filtering, rate limiting, CORS lockdown, body size limits
- Payment address validation (Ethereum, Solana)
- Plugin loading gated by allow-list
- OpenSCAD input validation (size limit, dangerous function blocking)
- File upload validation (existence, size, empty file rejection)
- G-code batch size limits (100 commands max)
