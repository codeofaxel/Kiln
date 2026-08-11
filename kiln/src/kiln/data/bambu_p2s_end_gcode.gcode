; Bambu Lab P2S end G-code.
;
; Source: BambuStudio 02.06.00.51 vendor profile bundle, file
;   profiles/BBL/machine/"Bambu Lab P2S 0.4 nozzle template machine_end_gcode.json".
; BambuStudio is licensed AGPL-3.0 and Kiln is licensed AGPL-3.0, so
; redistributing this sequence here is license-compatible.
;
; The bundle ships end G-code at the 0.4 nozzle only -- unlike start G-code,
; there is no per-nozzle end variant, so this one file covers every nozzle.
;
; Copied verbatim, placeholders included, so it stays diffable against the
; vendor bundle on a refresh.  Every brace expression and conditional below is
; resolved at build time by kiln.printers.bambu_3mf._resolve_end_gcode, which
; refuses to emit a template it could not fully resolve.
;======== P2S end gcode ==========
;===== 2026/04/01 =====
M400 ; wait for buffer to clear
G92 E0 ; zero the extruder

G90
G1 Z{max_layer_z + 0.4} F900 ; lower z a little
M1002 judge_flag timelapse_record_flag
M622 J1
    G150.3
    M400 ; wait all motion done
    M991 S0 P-1 ;end smooth timelapse at safe pos
    M400 S5 ;wait for last picture to be taken
M623  ;end of "timelapse_record_flag

G90
G1 Z{max_layer_z + 10} F900 ; lower z a little

M140 S0 ; turn off bed
M106 S0 ; turn off fan
M106 P2 S0 ; turn off remote part cooling fan
M106 P3 S0 ; turn off chamber cooling fan
M106 P10 S0 ; turn off left aux fan

; pull back filament to AMS
M620 S65535
T65535
G150.1 F8000
M621 S65535

G150.3
M104 S0 ; turn off hotend
M400 ; wait all motion done
M17 S
M17 Z0.4 ; lower z motor current to reduce impact if there is something in the bottom
{if (80.0 - max_layer_z/2) > 0}
    {if (max_layer_z + 80.0 - max_layer_z/2) < 256}
        G1 Z{max_layer_z + 80.0 - max_layer_z/2} F600
        G1 Z{max_layer_z + 78.0 - max_layer_z/2}
    {else}
        G1 Z256 F600
        G1 Z256
    {endif}
{else}
    {if (max_layer_z + 4.0) < 256}
        G1 Z{max_layer_z + 4.0} F600
        G1 Z{max_layer_z + 2.0}
    {else}
        G1 Z256 F600
        G1 Z256
    {endif}
{endif}
M400 P100
M17 R ; restore z current


M220 S100  ; Reset feedrate magnitude
M201.2 K1.0 ; Reset acc magnitude
M73.2 R1.0 ;Reset left time magnitude
M1002 set_gcode_claim_speed_level : 0

M1015.3 S0 ;disable clog detect
M1015.4 S0 K0 ;disable air printing detect

;=====printer finish air purification=========
M622.1 S0
M1002 judge_flag print_finish_air_filt_flag

M622 J1
M1002 gcode_claim_action : 66
M145 P1
M106 P2 S255
M400 S180
M106 P2 S0
M623

M622 J2
M1002 gcode_claim_action : 66
M145 P0
M106 P3 S255
M400 S180
M106 P3 S0
M623
;=====printer finish air purification=========

;=====printer finish  sound=========
M17
M400 S1
M1006 S1
M1006 A53 B10 L50 C53 D10 M50 E53 F10 N50 
M1006 A57 B10 L50 C57 D10 M50 E57 F10 N50 
M1006 A0 B15 L0 C0 D15 M0 E0 F15 N0 
M1006 A53 B10 L50 C53 D10 M50 E53 F10 N50 
M1006 A57 B10 L50 C57 D10 M50 E57 F10 N50 
M1006 A0 B15 L0 C0 D15 M0 E0 F15 N0 
M1006 A48 B10 L50 C48 D10 M50 E48 F10 N50 
M1006 A0 B15 L0 C0 D15 M0 E0 F15 N0 
M1006 A60 B10 L50 C60 D10 M50 E60 F10 N50 
M1006 W
;=====printer finish  sound=========
M400
M18
