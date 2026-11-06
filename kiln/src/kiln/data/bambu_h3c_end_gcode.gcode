; Bambu Lab H3C end G-code.
;
; Source: Bambu Lab's own end sequence for this machine, from the vendor
; profile bundle its slicer ships.
; BambuStudio is licensed AGPL-3.0 and Kiln is licensed AGPL-3.0, so
; redistributing this sequence here is license-compatible.
;
; The bundle ships end G-code at the 0.4 nozzle only -- unlike start G-code,
; there is no per-nozzle end variant, so this one file covers every nozzle.
;
; Copied verbatim, placeholders included, so it stays diffable against the
; vendor bundle on a refresh.  Every brace expression and conditional below is
; resolved at build time by kiln.printers.bambu_3mf._resolve_end_gcode, which
; refuses to emit a template it could not fully resolve.  The retraction it
; reads besides the height is _MODEL_END_VALUES["bambu_h3c"].
;===== H3C endgcode;=====
;===== 20261028 ;=====

M1003 S0
M73 P100 R0

M400 ; wait for buffer to clear
G92 E0 ; zero the extruder
G1 E-{retraction_length[current_extruder]} F1800 ; retract
M211 Z1

G90
G1 Z{max_layer_z + 0.4} F900 ; lower z a little
M1002 judge_flag timelapse_record_flag
M622 J1
    G150.3
    M400 ; wait all motion done
    M991 S0 P-1 ;end smooth timelapse at safe pos
    M400 S5 ;wait for last picture to be taken
M623  ;end of "timelapse_record_flag

M220 S100  ; Reset feedrate magnitude
M204.2 K1.0 ; Reset acc magnitude
M17 S
M17 Z0.65 ; lower z motor current to reduce impact if there is something in the bottom
M204.3 S ; save xyz acc 
M204 Z300
G90
{if (max_layer_z + 10) < 327}
G1 Z{max_layer_z + 10} F900 ; lower z a little
{else}
G1 Z327 F900
{endif}
M400 P100
M204.4 S ; restore xyz acc
M17 R ; restore z current

M141 S0 ; turn off chamber heating
M140 S0 ; turn off bed
M106 S0 ; turn off fan
M106 P2 S0 ; turn off remote part cooling fan
M106 P3 S0 ; turn off chamber cooling fan
M106 P15 S0 ; turn off chamber1 cooling fan

; pull back filament to AMS
M620 S65535
T65535
M621 S65535

G150.1
M650.1 P
G90
G1 Y316.89 F60000
G1 X255.72 F60000
G1 Y336.89 F9000
G1 X255.72 F3000
M650 J

M104 S0 T0; turn off hotend
M104 S0 T1; turn off hotend

M400 ; wait all motion done
M17 S
M17 Z0.65 ; lower z motor current to reduce impact if there is something in the bottom
M204.3 S ; save xyz acc 
M204 Z300
G90
{if (max_layer_z + 155) < 327}
G1 Z{max_layer_z + 155} F900
{elsif (max_layer_z + 10) < 327}
G1 Z327 F900
{endif}
M400 P100
M204.4 S ; restore xyz acc
M17 R ; restore z current

M650.4 U
M73.2   R1.0 ;Reset left time magnitude
M1002 set_gcode_claim_speed_level : 5

; temp
M104 S0 G

G34 K0 ;break hall sensor lost step detect off
G34 M4 ;break hall sensor info print
M650.8 C1 ; Enable hotend detachment detection 

M1015.3 S0 ;disable clog detect

M1015.4 L10
M17 E1.8
M1015.5 S1

;=====printer finish air purification=========
M622.1 S0
M1002 judge_flag print_finish_air_filt_flag

M622 J1
M1002 gcode_claim_action : 66
M145 P1
M106 P3 S178 B
M400 S180
M106 P3 S0 B
M623

M622 J2
M1002 gcode_claim_action : 66
M145 P0
M106 P3 S178 B
M106 P15 S178 B
M400 S180
M106 P3 S0 B
M106 P15 S0 B
M623
;=====printer finish air purification=========

;=====printer finish  sound=========
M17
M400 S1
M1006 S1
M1006 A53 B10 L99 C53 D10 M99 E53 F10 N99 
M1006 A57 B10 L99 C57 D10 M99 E57 F10 N99 
M1006 A0 B15 L0 C0 D15 M0 E0 F15 N0 
M1006 A53 B10 L99 C53 D10 M99 E53 F10 N99 
M1006 A57 B10 L99 C57 D10 M99 E57 F10 N99 
M1006 A0 B15 L0 C0 D15 M0 E0 F15 N0 
M1006 A48 B10 L99 C48 D10 M99 E48 F10 N99 
M1006 A0 B15 L0 C0 D15 M0 E0 F15 N0 
M1006 A60 B10 L99 C60 D10 M99 E60 F10 N99
M1006 W
;=====printer finish  sound=========
M400
M18
