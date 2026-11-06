M73 P0 R21
M201 X25000 Y25000 Z1000 E5000
M203 X1400 Y1400 Z35 E30
M204 P25000 R5000 T25000
M205 X9.00 Y9.00 Z3.00 E2.50
M106 S0
M106 P2 S0
M106 P10 S0
; FEATURE: Custom
;===== machine: H3C start ======
;===== date: 20261102 =====================
M104 S0 G
M630 S0 P0
M630 S1 P0

M400

;=====printer start sound ===================
M17
M400 S1
M1006 S1
M1006 A53 B9 L50 C53 D9 M99 E53 F9 N99 
M1006 A56 B9 L90 C56 D9 M99 E56 F9 N99 
M1006 A61 B9 L99 C61 D9 M99 E61 F9 N99 
M1006 A53 B9 L99 C53 D9 M99 E53 F9 N99 
M1006 A56 B9 L99 C56 D9 M99 E56 F9 N99 
M1006 A61 B18 L60 C61 D18 M80 E61 F18 N80
M1006 W
;=====printer start sound ===================

;===== reset machine status =================
M204 S10000
M630 S0 P0;close shell detect
M630 S1 P0;close cutter detect

G90
M17 D ; reset motor current to default
G90
M220 S100 ;Reset Feedrate
M221 S100 ;Reset Flowrate
M73.2   R1.0 ;Reset left time magnitude
G29.1 Z0 ; clear z-trim value first
M983.1 M1 ; Set nonlinear extrusion compensation
M901 D4 ; Set the advance amount for the E-axis
M481 S0 ; turn off cutter pos comp
G28.140 D0; reset pre-extrude z pos
M9714 A0
;===== reset machine status =================

M620.29 P ; print nozzle info
M620 M ; enable remap
M620 T0
M650.5 P1
M650.5 W L20 F600
M650.8 C1 ; Enable hotend detachment detection
M650.11 E0


    M620.35 S1


;===== avoid end stop =================
G91
G380 S2 Z27 F1200
G380 S2 Z-12 F1200
G90
;===== avoid end stop =================

;===== first XY & holder homing start =====
M1002 gcode_claim_action : 13
M650.4 H F
G28 X T300
;===== first XY & holder homing end =====

;===== break hall sensor selfcheck start =====
G34 M4		;printf brake hall sensor info
G34 M6 F84000 D1	;break hall sensor selfcheck
G34 K1		;turn on sensor_lost_step_detect sub switch
G34 M4		;printf brake hall sensor info

M1001 S46 P12	;TH E1M cog_cali_info print
;===== break hall sensor selfcheck end =====

M400
;====== cog noise reduction=================
M982.2 S1 ; turn on cog noise reduction

;===== first Z homing start =====
M1002 gcode_claim_action : 74 ; Heatbed surface foreign object detection

M650.1 S0

G150.1 F8000 ; wipe mouth to avoid filament stick to heatbed
G150.3 F8000
M400 P200

M972 S24 P0 T30000 ; liveview camera detect
M972 S26 P0 C0 ; Heatbed surface foreign object detection
M972 S41 P0

M1009 Q1 L1
G90
G150.1
G1 X165 Y160 F30000
G28 Z P0 T250 U1
M1009 Q1 L0

G90

;===== first Z homing end =====
M1002 judge_flag build_plate_detect_flag
M622 S1
    ;M1002 gcode_claim_action : 11 ; Indentifying build plate type
    G1 X165 Y160 F30000
    M972 S19 P0 C0    ; heatbed presence detection
M623

;==== set airduct mode ====


    M145 P0 ; set airduct mode to cooling mode for cooling
    M106 P2 S178 ; turn on auxiliary fan for cooling
    M106 P10 S178 ; turn on auxiliary fan1 for cooling
    M106 P3 S178 ; turn on chamber fan for cooling
    M106 P15 S178 ; turn off chamber1 fan
    M140 S0 ; stop heatbed from heating

    M1002 gcode_claim_action : 29
    M191 S0 ; wait for chamber temp
    M106 P2 S0 ; turn off auxiliary fan
    M106 P10 S0 ; turn off auxiliary fan1
    
        
            M142 P1 R30 S38 T40 U0.3 V0.75 W1.0 O45; set PLA/TPU ND0.4 chamber autocooling
            M142 P6 R30 S38 T41 U0.3 V0.6 W1.0    ; set PLA/TPU ND0.4 chamber1 autocooling
        
    

M142.1 P8 Q1;enable ac autocooling
;==== set airduct mode ====

;====== preheat heated bed in advance =========
M1002 gcode_claim_action : 2
M140 S55
;====== set chamber temperature ============


M650.6 ; Nozzle Pick-and-Place Inspection
M650.1 S0

M400

M1002 judge_flag build_plate_detect_flag
M622 S1
    G1 X165 Y160 F30000
    M106 P1 S0
    M18 E
    M400 P200
    M1028 S1

    M972 S31 P0 T5000 ; toolhead camera dirty detection
    M972 S34 P0 T5000 ; heatbed plate offset detection
    M972 S30 P0 T30000 ; birdeye camera foolproof
    M1028 S0
    M17 D
M623

;===== detection start =====
M1002 gcode_claim_action : 72 ; Hotend Type Detection
M651 S0 ; nozzle type detection
M650.1 S0

M104 S140 A L2; rise temp in advance
M104 I0 S140

G150.3
G90
G1 Z3 F1200
M400
M650.4 H F
G1 Z10 F1200
M400
G150.3


;===== detection end =====

M400

;===== get hotend map start=====
M620 T3
;===== get hotend map end=====

;===== prepare print temperature and material ==========
M211 X0 Y0 Z0 ;turn off soft endstop
M975 S1 ; turn on input shaping

G29.2 S0 ; avoid invalid abl data


M620.10 A0 F299.341 H0.4 T240 P220 S1
M620.10 A1 F299.341 H0.4 T240 P220 S1 V4.98902


M620.11 P0 I0 E0


M620.11 K0 I0 R0



M650.11 E1
M650.11 A57 B52


M650.8 C2
M620 S0A   ; switch material if AMS exist
M1002 gcode_claim_action : 4
M1002 set_filament_type:UNKNOWN
M1002 set_next_filament_id:GFL99
M400
T0
M400
M628 S0
M629

M400
M1002 set_filament_type:PLA
M621 S0A
M650.8 C1

M104 S220
M400
M106 P1 S0

G29.2 S1
;===== prepare print temperature and material ==========

M1002 set_filament_type:PLA
;===== auto extrude cali start =========================
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
M650.11 E0
G150.1
M400

G387 Y295 J1 F60000
M400
G387 Y265 J1 F18000
M400

;===== wipe nozzle start =====
M1002 gcode_claim_action : 14
G150 T220 U1


M106 S255 ; turn on fan to cool the nozzle
M109 S140
M400 S5
G91
G1 Z5 F1200
G90
M106 S0 ; turn off fan
;===== wipe nozzle end =====

G387 Y295 J1 F60000
M400
G387 Y265 J1 F18000
M400

;===== mech mode sweep start =====
M1002 gcode_claim_action : 3
G90
G0 X165 Y160 Z5 F10000
M400 P200

M970.3 Q0 A8 K0 O1
M970.2 Q0 K1 W87 Z0.10
M970.3 Q1 A5 K0 O1
M970.2 Q1 K1 W81 Z0.12
M970.2 Q2 K0 W36 Z0.01
M970.2 Q2 K1 W76 Z0.10
M974 S2 P0

M975 S1
M400
;===== mech mode sweep end =====


;===== pre-check for toolhead cali ====================
M1002 judge_flag auto_cali_toolhead_offset_flag
M622 J2
    G37.3;
M623

;===== extra wait for thermal deformation =============
G90
G1 X165 Y160 F20000
M190 S55

M1002 set_flag auto_detect_heatbed_type_flag=1
M1002 judge_flag auto_detect_heatbed_type_flag

    M1002 judge_flag bed_heat_stable_wait_flag
    M622 J1
        M1002 gcode_claim_action : 82
        
            M1030 S120 ; Waiting for temperature to equalize
            SYNC R0 T120
            M1030 C
        
    M623


;===== xy ofst cali start =====
M1002 judge_flag auto_cali_toolhead_offset_flag
M622 J1
    M190 D55
    M1002 gcode_claim_action : 39
    M650.8 C2
    G37 O2 M2 T140 U140 H180 Q1
    M650.8 C1
M623

M622 J2
    M190 D55
    M1002 gcode_claim_action : 39
    M650.8 C2
    G37 J1 O2 M2 T140 U140 H180 Q1
    M650.8 C1
M623

G37.6
;===== xy ofst cali end =====

M400

;===== bed leveling ==================================
M190 S55; ensure bed temp
M109 S140
M650.1 A7 ; retake nozzle
M106 S0 ; turn off fan , too noisy

G91
M73 P1 R21
G1 Z5 F1200
G90
M73 P2 R21
G1 X165 Y160 F30000

M1002 judge_flag g29_before_print_flag
M622 J1
    M1002 gcode_claim_action : 1
    G29.20 A3
    G29 A1 U1 O X155 Y150 I20 J20 R
    M400
M623

M622 J2
    M1002 gcode_claim_action : 1
    
        G29.20 A4
        G29 A2 U1 O X155 Y150 I20 J20 R
    
    M400
M623

M622 J0
    G28 X R
    G150.1
    G1 X165 Y160 F18000
    G28 Z U1
M623

;===== bed leveling end ================================
G90
G1 Z5 F1200
M73 P31 R14
G1 X270 Y-0.5 F60000
G28.140 S0 ; cali pre-extrude z pos
G1 Z5 F1200

M400

G150.3 ; move to garbage can to wait for temp
M1026

M400

;============switch again==================
M620 O1
M211 X0 Y0 Z0 ;turn off soft endstop
G91
G1 Z6 F1200
G90
M1002 set_filament_type:PLA

M650.11 E1
M650.11 A57 B52

M650.8 C2
M620 S0A
M400
T0
M400
M628 S0
M629
M400
M621 S0A
M650.8 C1
;============switch again==================

M400

;===== wait temperature reaching the reference value =======
M104 S0 A; rise prepare temp in advance
M104 S220 ; rise to print tmpr

M140 S55
M190 S55

    ;========turn off light and fans =============
    M960 S1 P0 ; turn off laser
    M960 S2 P0 ; turn off laser
    M106 S0 ; turn off fan
    M106 P2 S0 ; turn off auxiliary fan
    M106 P10 S0 ; turn off auxiliary fan1

    ;============set motor current==================
    M400 S1

;===== wait temperature reaching the reference value =======

;===== for Textured PEI Plate , lower the nozzle as the nozzle was touching topmost of the texture when homing ==
    
        
            
                
                    G29.1 Z-0.02 ; for Textured PEI Plate
                
            
        
    

G150.1
G150.3

M975 S1 ; turn on mech mode supression
G29.2 S1 ; turn on pos comp

G90
G1 Z5 F1200


M1015.4 L10
M1015.5 S1

M620.6 I0 H-1 W1 ;enable ams air printing detect

G387 Y295 J1 F60000
G387 Y265 J1 F18000

;===== nozzle load line ===============================
M1002 gcode_claim_action : 51
    G29.2 S1 ; ensure z comp turn on
    G90
    M83
    G1 Z5 F1200
    G1 X270 Y-0.5 F60000
    G28.14 R0
    G29.2 S0
    G91
    G1 Z0.8 F1200
    G90
    G1 X250 F60000
    
    ;========== record data ==========
    M1026
    M1012.8
    M1012.9
    M400 P50
    G29.9
    M400 P50
    G37.2 R
    G37.5
    ;========== record data ==========
    
    M400 P50
    M500 D1
    M400 S3
    M109 S220
    M83


    G1 E5 F299.341
    G1 X290 E10 F299.341
    G91
    G3 Z0.4 I1.217 J0 P1 F60000
    G90
    M83
    G29.2 S1 ; ensure z comp turn on
;===== noozle load line end ===========================

M1002 gcode_claim_action : 0

M400
M211 Z1
G29.99
; MACHINE_START_GCODE_END
; filament start gcode
;VT0 H-1
G90
G21
M83 ; use relative distances for extrusion
M981 S1 P20000 ;open spaghetti detector