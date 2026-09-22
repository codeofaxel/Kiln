# Test data: the maker's own slicer output, kept so the checks run without it

Byte-for-byte copies. Nothing here is shipped to a printer.

- `bambu_a1_end_template_20231229.gcode` — Bambu's A1 end-G-code *template*
  stamped `;===== date: 20231229`: the version Kiln's hardware-proven
  `kiln/src/kiln/data/bambu_a1_end_gcode.gcode` was sliced with. Expanding it
  at `max_layer_z=65` reproduces the proven file's middle exactly, which is how
  the end-template expander is checked.
- `bambu_h2c_end_bambustudio_z20.gcode` — the end block the maker's own slicer
  wrote for the H2C on a 20 mm cube. Kiln's expansion of the H2C end template
  must reproduce it line for line.

BambuStudio and OrcaSlicer are AGPL-3.0, as is Kiln.
