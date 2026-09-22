M73 P0 R19
M201 X20000 Y20000 Z500 E30000
M203 X1000 Y1000 Z20 E30
M204 P20000 R30000 T20000
M205 X9.00 Y9.00 Z3.00 E2.50
M106 S0
M106 P2 S0
; FEATURE: Custom
;M1002 set_flag extrude_cali_flag=1
;M1002 set_flag g29_before_print_flag=1
;M1002 set_flag auto_cali_toolhead_offset_flag=1
;M1002 set_flag build_plate_detect_flag=1

;======== X2D start gcode==========
;===== 2026/06/05 =====

  M140 S55 ; heat heatbed first
  M993 A0 B0 C0 ; nozzle cam detection not allowed.
  M400
  ;M73 P99

;=====printer start sound ===================
M17
M400 S1
M1006 S1
M1006 A53 B9 L50 C53 D9 M50 E53 F9 N50
M1006 A56 B9 L50 C56 D9 M50 E56 F9 N50
M1006 A61 B9 L50 C61 D9 M50 E61 F9 N50
M1006 A53 B9 L50 C53 D9 M50 E53 F9 N50
M1006 A56 B9 L50 C56 D9 M50 E56 F9 N50
M1006 A61 B18 L50 C61 D18 M50 E61 F18 N50
M1006 W
;=====printer start sound ===================

  M1012.1 T1100
  M620 M ;enable remap
  M622.1 S0
  G383.4

;===== avoid end stop =================
  G91
  G380 S2 Z22 F1200
  G380 S2 Z-12 F1200
  G90
;===== avoid end stop =================

;===== reset machine status =================
  M204 S10000
  M630 S0 P1
  G90
  M17 D ; reset motor current to default
  M960 S5 P1 ; turn on logo lamp
  M220 S100 ;Reset Feedrate
  M1002 set_gcode_claim_speed_level: 5
  M221 S100 ;Reset Flowrate
  M73.2   R1.0 ;Reset left time magnitude
  G29.1 Z0 ; clear z-trim value first
  M983.1 M1
  M982.2 S1 ; turn on cog noise reduction
;===== reset machine status =================

;==== set airduct mode ====

M145 P0 ; set airduct mode to cooling mode for cooling
M106 P2 S255 ; turn on auxiliary fan for cooling
M106 P10 S255 ; turn on auxiliary fan for cooling
M106 P3 S127 ; turn on chamber fan for cooling
;M140 S0 ; stop heatbed from heating
M1002 gcode_claim_action : 29
M191 S0 ; wait for chamber temp
M106 P2 S102 ; turn on auxiliary fan
M106 P10 S102 ; turn on chamber fan
M142 P6 R30 S40 U0.6 V0.8 ; set PLA/TPU/PETG exhaust chamber autocooling

;==== set airduct mode ====

;===== start to heat heatbed & hotend==========
  M1002 gcode_claim_action : 2
  M1002 set_filament_type:PLA

  ;===== set chamber temperature ==========
  
;===== set chamber temperature ==========

  G29.2 S0 ; avoid invalid abl data

;===== first homing start =====
  M1002 gcode_claim_action : 13
  G28 X T300 R
  G150.1 F8000 ; wipe mouth to avoid filament stick to heatbed
  G150.3
  M972 S24 P0
  M1002 gcode_claim_action : 74 ; Heatbed surface foreign object detection
  M972 S26 P0 C0
  G90
  M83
  G1 Y128 F30000
  G1 X128
  G28 Z P0 T400
  M400
;===== first homign end =====

;===== detection start =====
  M1002 gcode_claim_action : 11

      M104 S0 T0
      M104 S0 T1
      M562 P1 E0 B1
      M562 P2 E0 B1
      M18 E
      M400 P200
      M1028 S1
      M972 S19 P0   ;heatbed detection
      M972 S31 P0   ;toolhead camera dirt detection
      M1002 gcode_claim_action : 73 ; Build plate alignment detection
      M972 S34 P0   ;print plate deviation detection
      M1028 S0
      M562 P1 E1 B1
      M562 P2 E1 B1
      M17 D

  ;M400
  M104 S220 T1 ; rise temp in advance

  
;===== detection end =====

;===== prepare print temperature and material ==========
  M104 S180 A ; rise temp in advance
  M400
  M211 X0 Y0 Z0 ;turn off soft endstop
  M975 S1 ; turn on input shaping

  G29.2 S0 ; avoid invalid abl data
  G150.3

M620.10 A0 F299.339 H0.4 T240 P220 S1
M620.10 A1 F299.339 H0.4 T240 P220 S1


 M620.11 P0 L0 I0 B-1 E0
 M620.11 K0 I0 B-1 R0

  M620 S0A H-1 B   ; switch material if AMS exist
  M620.22 I0 P1    ; enable remote extruder runout auto purge.
  M1002 gcode_claim_action : 4
  M1002 set_filament_type:UNKNOWN
  M400
  T0 H-1
  M400
  M628 S0
  M629
  M400
  M1002 set_filament_type:PLA
  M621 S0A B
  M104 S220
  M400
  M106 P1 S0
  M400
  G29.2 S1
;===== prepare print temperature and material ==========

;===== auto extrude cali start =========================
  M975 S1
  M1002 judge_flag extrude_cali_flag
  M622 J0
    M983.3 F5 A0.4 ; cali dynamic extrusion compensation
  M623

  M622 J1
    M1002 set_filament_type:PLA
    M1002 gcode_claim_action : 8
    M109 S220
    G90
    M83
    M983.3 F5 A0.4 ; cali dynamic extrusion compensation
    M400
    M106 P1 S255
    M400 S5
    M106 P1 S0
    G150.3
  M623

  M622 J2
    M1002 set_filament_type:PLA
    M1002 gcode_claim_action : 8
    M109 S220
    G90
    M83
    M983.3 F5 A0.4 ; cali dynamic extrusion compensation
    M400
    M106 P1 S255
    M400 S5
    M106 P1 S0
    G150.3
  M623
;===== auto extrude cali end =========================

  

  
    M83
    G1 E-3 F1800
    M400 P500
  
  G150.2
  G150.1 F8000
  G150.2
  G150.1 F8000

  G91
  G1 Y-16 F12000 ; move away from the trash bin
  G90
  M400

  M104 S140 A

;===== wipe right nozzle start =====
  M1002 gcode_claim_action : 14
  G150 T220
  M400
;===== wipe left nozzle end =====


  M109 S140 A

  M106 S0 ; turn off fan , too noisy
  G91
M73 P2 R19
  G1 Z5 F1200
  G90
  M400
  G150.1



;===== z ofst cali start =====
  M190 S55; ensure bed temp
  G383 O0 M1 T140
  M400
;===== z ofst cali end =====
G90
M83
G0 Y200 F18000

;===== bed leveling ==================================
  M1002 gcode_claim_action : 54
  M190 S55; ensure bed temp
  M109 S140 A
  M106 S0 ; turn off fan , too noisy
  M1002 judge_flag g29_before_print_flag
  M622 J1
    M1002 gcode_claim_action : 1
    
      G29 A1 X128.25 Y118 I20 J20 R
    
    M400
  M623

  M622 J2
    M1002 gcode_claim_action : 1
    
      G29 A2 X128.25 Y118 I20 J20 R
    
    M400
  M623

  M622 J0
    G28 R
  M623
  G29.2 S1
;===== bed leveling end ================================

; cali eddy z pos
;G383.13 T1 C1

M104 S220 A
;===== mech mode sweep start =====
  M1002 gcode_claim_action : 3
  G90
M73 P3 R19
  G1 X128 Y128 F20000
M73 P25 R14
  G1 Z5 F1200
  M400 P200
  M970.3 Q1 A5 K0 O1
  M974 Q1 S2 P0
  M970.3 Q0 A7 K0 O1
  M970.2 Q0 W73 K1 Z0.01
  M974 Q0 S2 P0
  M975 S1
  M400
;===== mech mode sweep end =====

M104 S220 A
G150.3

;===== xy ofst cali start =====
M1002 judge_flag auto_cali_toolhead_offset_flag

M622 J0
    M1012.5 N1 R1
M623

M622 J1
    M1002 gcode_claim_action : 39
    M141 S0
    M620.17 T0 S220 L0
    M620.17 T1 S220 L0
    M620 D-1
    G383 O1 T220 L0
    M141 S0
M623

M622 J2
    M1002 gcode_claim_action : 39
    M141 S0
    M620.17 T0 S220 L0
    M620.17 T1 S220 L0
    M620 D-1
    G383.3 T220 L0
    M141 S0
M623
;===== xy ofst cali end =====

  M104 S220 A

 G150.3 ; move to garbage can to wait for temp

;===== wait temperature reaching the reference value =======
  M140 S55
  M190 S55

  ;========turn off light and fans =============
  M960 S1 P0 ; turn off laser
  M960 S2 P0 ; turn off laser
  M106 S0 ; turn off cooling fan

;===== wait temperature reaching the reference value =======

  M1002 gcode_claim_action : 255
  M400
  M975 S1 ; turn on mech mode supression
  M983.4 S0 ; turn off deformation compensation

;============switch again==================
  M211 X0 Y0 Z0 ;turn off soft endstop
  G91
  G1 Z6 F1200
  G90
  M1002 set_filament_type:PLA
  M620 S0A H-1 B
  M620.22 I0 P1    ; enable remote extruder runout auto purge.
  M400
  T0 H-1
  M400
  M628 S0
  M629
  M400
  M621 S0A B
;============switch again==================

;===== for Textured PEI Plate , lower the nozzle as the nozzle was touching topmost of the texture when homing ==
  
    
      G29.1 Z0.002 ; for Textured PEI Plate
    
  

;===== nozzle load line ===============================
M1002 gcode_claim_action : 51
  G29.2 S1 ; ensure z comp turn on
  G90
  M83
  M400 P50
  M500 D1
  M400 S3
  M109 S220
  G0 X100 Y0 F24000
  M400
  ;G130 O0 X100 Y-0.4 Z0.6 F2.49449 L40 E20 D5
  G130 O0 X100 Y-0.2 Z0.6 F2.49449 L40 E12 D4
G90
  G90
  M83
  G1 Z1
  M400
;===== noozle load line end ===========================
M1002 gcode_claim_action : 0
  G29.99

;M993 A1 B1 C1 ; nozzle cam detection allowed.

M620.6 I0 H-1 W1 ;enable ams air printing detect



M1015.3 S0;disable tpu clog detect



M1015.4 S1 K1 H0.4 ;enable E air printing detect


; MACHINE_START_GCODE_END
; filament start gcode
;VT0 H-1
G90
G21
M83 ; use relative distances for extrusion
M981 S1 P20000 ;open spaghetti detector
