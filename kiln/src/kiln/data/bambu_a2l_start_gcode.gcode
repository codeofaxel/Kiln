M73 P0 R22
M201 X6000 Y6000 Z1500 E5000
M203 X500 Y500 Z30 E30
M204 P12000 R5000 T12000
M205 X9.00 Y9.00 Z3.00 E3.00
M106 S0
; FEATURE: Custom
;M1002 set_flag extrude_cali_flag=1
;M1002 set_flag g29_before_print_flag=1
;M1002 set_flag build_plate_detect_flag=1
;M1002 set_flag bed_heat_stable_wait_flag=1

;======== A2L start gcode==========
;===== 2026/05/26 =====
T1000 O0
M1002 gcode_claim_action : 2

    M140 S55 ; heat heatbed first


M993 A0 B0 C0 ; nozzle cam detection not allowed.
M400

;=====printer start sound ===================
M17
M400 S1
M1006 S1
M1006 A53 B9 L30 C53 D9 M30 E53 F9 N30
M1006 A56 B9 L30 C56 D9 M30 E56 F9 N30
M1006 A61 B9 L30 C61 D9 M30 E61 F9 N30
M1006 A53 B9 L30 C53 D9 M30 E53 F9 N30
M1006 A56 B9 L30 C56 D9 M30 E56 F9 N30
M1006 A61 B18 L30 C61 D18 M30 E61 F18 N30
M1006 W
;=====printer start sound ===================

  M620 M ;enable remap
  G389

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
  M221 S100 ;Reset Flowrate
  M73.2   R1.0 ;Reset left time magnitude
  G29.1 Z0 ; clear z-trim value first
  M983.1 M1
  M982.2 S1 ; turn on cog noise reduction
  M983.4 S0
;===== reset machine status =================
;Set the filament gear warning temperature

    
        M142 P1 O60; set PLA/PLACF/PLAAERO/PVA/TPU gear warning temperature when start
    

;===== start to heat heatbed & hotend==========
  M1002 set_filament_type:PLA
  M104 S140 A

  G29.2 S0 ; avoid invalid abl data

;===== first homing start =====
  M1002 gcode_claim_action : 13
  M105
  G28 X Z P0 T300 W
  G150.3
  G1 Z1.3 F1200
  G150.1 F16000 ; wipe mouth to avoid filament stick to heatbed
  G90
  M400
;===== first homing end =====


;===== detection start =====
;===== build_plate_detect_flag start =====
M1002 judge_flag build_plate_detect_flag
M622 S1
  G91
  G1 Z5 F1200
  G90
  G0 X15 F30000
  G0 Y319 F3000
  G91
  G1 Z-5 F1200
  G28 Z P0 T140
  G1 F1200
  G39.4
  G90
  G1 Z5 F1200
M623
;===== build_plate_detect_flag end =====
;===== detection end =====


;===== hotend hotbed pre-heat start =====
  M104 S140 A ; rise nozzle temp in advance

  G90
  G1 Y220 F3000 ; Put away the heated bed to prevent collisions

  
      M190 S55
  
;===== hotend hotbed pre-heat end =====


;===== prepare print temperature and material ==========
  M400
  M211 X0 Y0 Z0 ;turn off soft endstop
  M975 S1 ; turn on input shaping

  G29.2 S0 ; avoid invalid abl data
  G150.3

M620.10 A0 F299.339 H0.4 T240 P220 S1
M620.10 A1 F299.339 H0.4 T240 P220 S1


 M620.11 P0 L0 I0 E0
 M620.11 K0 I0 R0

  M620 S0A   ; switch material if AMS exist
  M1002 gcode_claim_action : 4
  M1002 set_filament_type:UNKNOWN
  M400
  T0
  M400
  M628 S0
  M629
  M400
  M1002 set_filament_type:PLA
  M621 S0A
  M104 S220
  M400
  M106 P1 S0
  M400
  G29.2 S1

  G390.7 M6 G6 C3

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
  M623

  M622 J2
    M1002 set_filament_type:PLA
    M1002 gcode_claim_action : 8
    M109 S220
    G90
    M83
    M983.3 F5 A0.4 ; cali dynamic extrusion compensation
    M400
  M623
;===== auto extrude cali end =========================


  
    M83
    G1 E-3 F1800
    M400 P500
  
  G0 Z1.3 F1200
  G150.2
  G150.1 F16000

  G91
  G1 X20 F12000 ; move away from the trash bin
  G90
  M400


;===== wipe nozzle start =====
  M1002 gcode_claim_action : 14
  G150 T220
  M400
  M109 S140 A
  M106 P1 S255
  G91
  G1 Z1.3 F1200
  G90
  M400 S1
  ;======== enhance brush nozzle start =====
  G150.1 F16000
  G91
  G1 Y5   F5000
M73 P1 R22
  G1 X-20 F16000
  G1 Y-10 F5000
  G1 X-20 F16000
  G1 Y10  F5000
  G1 X20  F16000
  G1 Y-10 F5000
  G1 X20  F16000
  G1 Y5   F5000
  G90
  G150.1 F16000
  G150.3
  ;======== enhance brush nozzle end =====
  M106 P1 S0
;===== wipe nozzle end =====

;===== mech mode sweep start =====
  M1002 gcode_claim_action : 3
  G90
  G1 Z5 F1200
  G1 X165 Y160 F20000
  M400 P200
  M970.3 Q1 A5 K0 O3
  M970.3 Q1 B1
  M970.2 Q1 K1 W52 Z0.1 B30 ;
  M970.3 Q0 A10 K0 O1
  M970.3 Q0 B1
  M970.2 Q0 K1 W40 Z0.1 B20 ;
  M974 Q0 S2 P0
  M974 Q1 S2 P0
  M975 S1 R1 M1
  M400
  G1 X155 F3000
  G150.3
;===== mech mode sweep end =====


;===== bed leveling ==================================

  
    M190 S55 ; ensure bed temp
  
  M109 S140 A


  
    M1002 judge_flag bed_heat_stable_wait_flag
    M622 J1
      
        M1002 gcode_claim_action : 54
        G29.30 X155 Y150 I20 J20
      
    M623
  
  SYNC R0 T120 ; Adjust estimated time

  M106 S0 ; turn off fan , too noisy
  M1002 judge_flag g29_before_print_flag
  M622 J1
    M1002 gcode_claim_action : 1
    
      
        G29 A1 X155 Y150 I20 J20 O1 R
      
    
    M400
  M623

  M622 J2
    M1002 gcode_claim_action : 1
    
      
        G29 A2 X155 Y150 I20 J20 O1 R
      
    
    M400
  M623

  M622 J0
    G28 R
  M623
  G29.2 S1
;===== bed leveling end ================================

  M985.1 U0 E2
  M985.1 U1 E2

  M104 S220 A
  G150.3 ; move to garbage can to wait for temp

;===== wait temperature reaching the reference value =======
  
    M190 S55 ; ensure bed temp
  

  ;========turn off light and fans =============
  M960 S1 P0 ; turn off laser
  M960 S2 P0 ; turn off laser
  M106 S0 ; turn off cooling fan

;===== wait temperature reaching the reference value =======

  M1002 gcode_claim_action : 255
  M400
  M975 S1 ; turn on mech mode supression

;===== for Textured PEI Plate , lower the nozzle as the nozzle was touching topmost of the texture when homing ==
  
    
      G29.1 Z-0.04 ; for Textured PEI Plate
    
  

;===== nozzle load line ===============================
M1002 gcode_claim_action : 51
  G29.2 S1 ; ensure z comp turn on
  G90
  M83
  M400 P50
  M500 D1
  M400 S3
  M109 S220
  G0 X145 Y0 F24000
  M400
  G130 O0 X145 Y-0.2 Z0.8 F2.49449 L40 E20 D4
  G90
  M83
  G1 Z0.2
  M400
;===== nozzle load line end ===========================
M1007 S1 C1;turn on mass estimation && clear
M1002 gcode_claim_action : 0
  G29.99


M1015.3 S0;disable tpu clog detect



M1015.4 S1 K1 H0.4 ;enable E air printing detect


M620.6 I0 W1 ;enable ams air printing detect

M1010 Q0 B0.005 S0.01
M1010 Q1 B0.002 S0.01
M1010.1 S1
; MACHINE_START_GCODE_END
;VT0 H-1
G90
G21
M83 ; use relative distances for extrusion
M981 S1 P20000 ;open spaghetti detector
