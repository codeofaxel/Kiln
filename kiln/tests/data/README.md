# Test data: BambuStudio's own output, kept so the checks run without it

Byte-for-byte copies. Nothing here is shipped to a printer.

- `bambu_a1_end_template_20231229.gcode` — Bambu's A1 end-G-code *template*
  stamped `;===== date: 20231229`: the one Kiln's hardware-proven
  `kiln/src/kiln/data/bambu_a1_end_gcode.gcode` was captured from. Copied from
  OrcaSlicer 2.3.2's copy of Bambu's profiles
  (`profiles/BBL/machine/Bambu Lab A1 0.4 nozzle.json`, key
  `machine_end_gcode`); BambuStudio shipped the same stamp through at least
  02.06.00.51. Expanding it at `max_layer_z=65` reproduces the proven file's
  middle exactly, which is how the end-template expander is checked.
- `bambu_h2c_end_bambustudio_z20.gcode` — the end block BambuStudio 02.08.02.61
  itself wrote for the H2C, slicing a 20 mm cube in Generic PLA at 220C on a
  textured plate (the slice the H2C's start capture came from). Kiln's
  expansion of the H2C end template must reproduce it line for line.

BambuStudio and OrcaSlicer are AGPL-3.0, as is Kiln.
