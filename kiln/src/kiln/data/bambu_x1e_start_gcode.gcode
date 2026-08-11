M73 P0 R22
M201 X20000 Y20000 Z500 E5000
M203 X500 Y500 Z20 E30
M204 P20000 R5000 T20000
M205 X9.00 Y9.00 Z3.00 E2.50
M106 S0
M106 P2 S0
; FEATURE: Custom
;===== machine: X1E-0.4 =========================
;===== date: 20251031 =====================
;===== turn on the HB fan =================
M104 S75 ;set extruder temp to turn on the HB fan and prevent filament oozing from nozzle
;===== reset machine status =================
G91
M17 Z0.4 ; lower the z-motor current
G380 S2 Z30 F300 ; G380 is same as G38; lower the hotbed , to prevent the nozzle is below the hotbed
G380 S2 Z-25 F300 ;
G1 Z5 F300;
G90
M17 X1.2 Y1.2 Z0.75 ; reset motor current to default
M960 S5 P1 ; turn on logo lamp
G90
M220 S100 ;Reset Feedrate
M221 S100 ;Reset Flowrate
M73.2   R1.0 ;Reset left time magnitude
M1002 set_gcode_claim_speed_level : 5
M221 X0 Y0 Z0 ; turn off soft endstop to prevent protential logic problem
G29.1 Z0 ; clear z-trim value first
M204 S10000 ; init ACC set to 10m/s^2

;==== if Chamber Cooling is necessary ====


M1002 gcode_claim_action : 29
G28
G90
G1 X60 F12000
G1 Y245
G1 Y265 F3000
G1 Z75
M140 S0 ; stop heatbed from heating
M106 P2 S255 ; open auxiliary fan for cooling
M106 P3 S255 ; open chamber fan for cooling
M191 S0 ; wait for chamber temp
M106 P3 S0 ; reset chamber fan cmd
M106 P2 S0; reset auxiliary fan cmd


;===== heatbed preheat ====================
M1002 gcode_claim_action:54
M140 S55 ;set bed temp
M190 S55 ;wait for bed temp


;=========register first layer scan=====
M977 S1 P60


;=============turn on fans to prevent PLA jamming=================

    
    M106 P3 S255
    ;Prevent PLA from jamming
    M142 P1 R35 S40

M106 P2 S100 ; turn on big fan ,to cool down toolhead

;===== prepare print temperature and material ==========
M104 S220 ;set extruder temp
G91
G0 Z10 F1200
G90
G28 X
M975 S1 ; turn on
G1 X60 F12000
G1 Y245
G1 Y265 F3000
M620 M
M620 S0A   ; switch material if AMS exist
    M109 S220
    G1 X120 F12000

    G1 X20 Y50 F12000
    G1 Y-3
    T0
    G1 X54 F12000
    G1 Y265
    M400
M621 S0A
M620.1 E F299.339 T240

M412 S1 ; ===turn on filament runout detection===

M109 S290 ;set nozzle to common flush temp
M106 P1 S0
G92 E0
M73 P1 R22
G1 E50 F200
M400
M104 S220
G92 E0
M73 P3 R21
G1 E50 F200
M400
M106 P1 S255
G92 E0
M73 P22 R17
G1 E5 F300
M109 S200 ; drop nozzle temp, make filament shink a bit
G92 E0
G1 E-0.5 F300

M73 P23 R17
G1 X70 F9000
M73 P24 R16
G1 X76 F15000
G1 X65 F15000
G1 X76 F15000
G1 X65 F15000; shake to put down garbage
G1 X80 F6000
G1 X95 F15000
G1 X80 F15000
M73 P25 R16
G1 X165 F15000; wipe and shake
M400
M106 P1 S0

;===== set chamber temperature ==========


;===== prepare print temperature and material end =====


;===== wipe nozzle ===============================
M1002 gcode_claim_action : 14
M975 S1
M106 S255
G1 X65 Y230 F18000
G1 Y264 F6000
M109 S200
G1 X100 F18000 ; first wipe mouth

G0 X135 Y253 F20000  ; move to exposed steel surface edge
G28 Z P0 T300; home z with low precision,permit 300deg temperature
G29.2 S0 ; turn off ABL
G0 Z5 F20000

G1 X60 Y265
G92 E0
G1 E-0.5 F300 ; retrack more
G1 X100 F5000; second wipe mouth
G1 X70 F15000
G1 X100 F5000
G1 X70 F15000
G1 X100 F5000
G1 X70 F15000
G1 X100 F5000
G1 X70 F15000
G1 X90 F5000
G0 X128 Y261 Z-1.5 F20000  ; move to exposed steel surface and stop the nozzle
M104 S140 ; set temp down to heatbed acceptable
M106 S255 ; turn on fan (G28 has turn off fan)

M221 S; push soft endstop status
M221 Z0 ;turn off Z axis endstop
G0 Z0.5 F20000
G0 X125 Y259.5 Z-1.01
G0 X131 F211
G0 X124
G0 Z0.5 F20000
G0 X125 Y262.5
G0 Z-1.01
G0 X131 F211
G0 X124
G0 Z0.5 F20000
G0 X125 Y260.0
G0 Z-1.01
G0 X131 F211
G0 X124
G0 Z0.5 F20000
G0 X125 Y262.0
G0 Z-1.01
G0 X131 F211
G0 X124
G0 Z0.5 F20000
G0 X125 Y260.5
G0 Z-1.01
G0 X131 F211
G0 X124
G0 Z0.5 F20000
G0 X125 Y261.5
G0 Z-1.01
G0 X131 F211
G0 X124
G0 Z0.5 F20000
G0 X125 Y261.0
G0 Z-1.01
G0 X131 F211
G0 X124
G0 X128
G2 I0.5 J0 F300
G2 I0.5 J0 F300
G2 I0.5 J0 F300
G2 I0.5 J0 F300

M109 S140 ; wait nozzle temp down to heatbed acceptable
G2 I0.5 J0 F3000
G2 I0.5 J0 F3000
G2 I0.5 J0 F3000
G2 I0.5 J0 F3000

M221 R; pop softend status
G1 Z10 F1200
M400
G1 Z10
G1 F30000
G1 X128 Y128
G29.2 S1 ; turn on ABL
;G28 ; home again after hard wipe mouth
M106 S0 ; turn off fan , too noisy
;===== wipe nozzle end ================================

;===== check scanner clarity ===========================
G1 X128 Y128 F24000
G28 Z P0
M972 S5 P0
M73 P26 R16
G1 X230 Y15 F24000
;===== check scanner clarity end =======================

;===== bed leveling ==================================
M1002 judge_flag g29_before_print_flag
M622 J1

    M1002 gcode_claim_action : 1
    G29 A X118 Y118 I20 J20
    M400
    M500 ; save cali data

M623
;===== bed leveling end ================================

;===== home after wipe mouth============================
M1002 judge_flag g29_before_print_flag
M622 J0

    M1002 gcode_claim_action : 13
    G28

M623
;===== home after wipe mouth end =======================

M975 S1 ; turn on vibration supression

;=============turn on fans to prevent PLA jamming=================

    
    M106 P3 S255
    ;Prevent PLA from jamming
    M142 P1 R35 S40

M106 P2 S100 ; turn on big fan ,to cool down toolhead

M104 S220 ; set extrude temp earlier, to reduce wait time

;===== mech mode fast check============================
G1 X128 Y128 Z10 F20000
M400 P200
M970.3 Q1 A7 B30 C80  H15 K0
M974 Q1 S2 P0

G1 X128 Y128 Z10 F20000
M400 P200
M970.3 Q0 A7 B30 C90 Q0 H15 K0
M974 Q0 S2 P0

M975 S1
G1 F30000
G1 X230 Y15
G28 X ; re-home XY
;===== mech mode fast check============================


;start heatbed  scan====================================
M976 S2 P1
G90
G1 X128 Y128 F20000
M976 S3 P2  ;register void printing detection


;===== noozle load line ===============================
M975 S1
G90
M83
T1000
G1 X18.0 Y1.0 Z0.8 F18000;Move to start position
M109 S220
G1 Z0.2
G0 E2 F300
G0 X240 E15 F4800
G0 Y11 E0.700 F1200
G0 X239.5
G0 E0.2
G0 Y1.5 E0.700
G0 X231 E0.700 F4800
M400

;===== for Textured PEI Plate , lower the nozzle as the nozzle was touching topmost of the texture when homing ==
;curr_bed_type=Textured PEI Plate

G29.1 Z-0.04 ; for Textured PEI Plate


;===== draw extrinsic para cali paint =================
M1002 judge_flag extrude_cali_flag
M622 J1

    M1002 gcode_claim_action : 8

    T1000

    G0 F1200.0 X231 Y12   Z0.2 E0.577
    G0 F1200.0 X226 Y12   Z0.2 E0.275
    G0 F1200.0 X226 Y1.5  Z0.2 E0.577
    G0 F1200.0 X220 Y1.5  Z0.2 E0.330
    G0 F1200.0 X220 Y8    Z0.2 E0.358
    G0 F1200.0 X210 Y8    Z0.2 E0.549
    G0 F1200.0 X210 Y1.5  Z0.2 E0.357

    G0 X48.0 E11.9 F4800
    G0 X48.0 Y12 E0.772 F1200.0
    G0 X45.0 E0.22 F1200.0
    G0 X35.0 Y6.0 E0.86 F1200.0

    ;=========== extruder cali extrusion ==================
    T1000
    M83
    
        
            M204 S5000
        
    
    G0 X35.000 Y6.000 Z0.300 F30000 E0
    G1 F1500.000 E0.800
    M106 S0 ; turn off fan
    G0 X185.000 E9.35441 F4800
    G0 X187 Z0
    G1 F1500.000 E-0.800
    G0 Z1
    G0 X180 Z0.3 F18000

    M900 L1000.0 M1.0
    M900 K0.040
    G0 X45.000 F30000
    G0 Y8.000 F30000
    G1 F1500.000 E0.800
    G1 X65.000 E1.24726 F1200
    G1 X70.000 E0.31181 F1200
    G1 X75.000 E0.31181 F4800
    G1 X80.000 E0.31181 F1200
    G1 X85.000 E0.31181 F4800
    G1 X90.000 E0.31181 F1200
M73 P27 R16
    G1 X95.000 E0.31181 F4800
    G1 X100.000 E0.31181 F1200
    G1 X105.000 E0.31181 F4800
    G1 X110.000 E0.31181 F1200
    G1 X115.000 E0.31181 F4800
    G1 X120.000 E0.31181 F1200
    G1 X125.000 E0.31181 F4800
    G1 X130.000 E0.31181 F1200
    G1 X135.000 E0.31181 F4800
    G1 X140.000 E0.31181 F1200
    G1 X145.000 E0.31181 F4800
    G1 X150.000 E0.31181 F1200
    G1 X155.000 E0.31181 F4800
    G1 X160.000 E0.31181 F1200
    G1 X165.000 E0.31181 F4800
    G1 X170.000 E0.31181 F1200
M73 P28 R16
    G1 X175.000 E0.31181 F4800
    G1 X180.000 E0.31181 F4800
    G1 F1500.000 E-0.800
    G1 X183 Z0.15 F30000
    G1 X185
    G1 Z1.0
    G0 Y6.000 F30000 ; move y to clear pos
    G1 Z0.3
    M400

    G0 X45.000 F30000
    M900 K0.020
    G0 X45.000 F30000
    G0 Y10.000 F30000
    G1 F1500.000 E0.800
    G1 X65.000 E1.24726 F1200
    G1 X70.000 E0.31181 F1200
    G1 X75.000 E0.31181 F4800
    G1 X80.000 E0.31181 F1200
    G1 X85.000 E0.31181 F4800
    G1 X90.000 E0.31181 F1200
    G1 X95.000 E0.31181 F4800
    G1 X100.000 E0.31181 F1200
    G1 X105.000 E0.31181 F4800
    G1 X110.000 E0.31181 F1200
    G1 X115.000 E0.31181 F4800
    G1 X120.000 E0.31181 F1200
    G1 X125.000 E0.31181 F4800
    G1 X130.000 E0.31181 F1200
M73 P28 R15
    G1 X135.000 E0.31181 F4800
    G1 X140.000 E0.31181 F1200
    G1 X145.000 E0.31181 F4800
    G1 X150.000 E0.31181 F1200
    G1 X155.000 E0.31181 F4800
    G1 X160.000 E0.31181 F1200
    G1 X165.000 E0.31181 F4800
    G1 X170.000 E0.31181 F1200
    G1 X175.000 E0.31181 F4800
    G1 X180.000 E0.31181 F4800
    G1 F1500.000 E-0.800
    G1 X183 Z0.15 F30000
    G1 X185
M73 P29 R15
    G1 Z1.0
    G0 Y6.000 F30000 ; move y to clear pos
    G1 Z0.3
    M400

    G0 X45.000 F30000
    M900 K0.000
    G0 X45.000 F30000
    G0 Y12.000 F30000
    G1 F1500.000 E0.800
    G1 X65.000 E1.24726 F1200
    G1 X70.000 E0.31181 F1200
    G1 X75.000 E0.31181 F4800
    G1 X80.000 E0.31181 F1200
    G1 X85.000 E0.31181 F4800
    G1 X90.000 E0.31181 F1200
    G1 X95.000 E0.31181 F4800
    G1 X100.000 E0.31181 F1200
    G1 X105.000 E0.31181 F4800
    G1 X110.000 E0.31181 F1200
    G1 X115.000 E0.31181 F4800
    G1 X120.000 E0.31181 F1200
    G1 X125.000 E0.31181 F4800
    G1 X130.000 E0.31181 F1200
    G1 X135.000 E0.31181 F4800
    G1 X140.000 E0.31181 F1200
    G1 X145.000 E0.31181 F4800
    G1 X150.000 E0.31181 F1200
    G1 X155.000 E0.31181 F4800
    G1 X160.000 E0.31181 F1200
    G1 X165.000 E0.31181 F4800
    G1 X170.000 E0.31181 F1200
    G1 X175.000 E0.31181 F4800
    G1 X180.000 E0.31181 F4800
    G1 F1500.000 E-0.800
    G1 X183 Z0.15 F30000
    G1 X185
    G1 Z1.0
    G0 Y6.000 F30000 ; move y to clear pos
    G1 Z0.3

    G0 X45.000 F30000 ; move to start point

M623 ; end of "draw extrinsic para cali paint"


M1002 judge_flag extrude_cali_flag
M622 J0
    G0 X231 Y1.5 F30000
    G0 X18 E14.3 F4800
M623

M104 S140


;=========== laser and rgb calibration ===========
M400
M18 E
M500 R

M973 S3 P14

G1 X120 Y1.0 Z0.3 F18000.0;Move to first extrude line pos
T1100
G1 X235.0 Y1.0 Z0.3 F18000.0;Move to first extrude line pos
M400 P100
M960 S1 P1
M400 P100
M973 S6 P0; use auto exposure for horizontal laser by xcam
M960 S0 P0

G1 X240.0 Y6.0 Z0.3 F18000.0;Move to vertical extrude line pos
M960 S2 P1
M400 P100
M973 S6 P1; use auto exposure for vertical laser by xcam
M960 S0 P0

;=========== handeye calibration ======================
M1002 judge_flag extrude_cali_flag
M622 J1

    M973 S3 P1 ; camera start stream
    M400 P500
    M973 S1
    G0 F6000 X228.500 Y4.750 Z0.000
    M960 S0 P1
    M973 S1
    M400 P800
    M971 S6 P0
    M973 S2 P0
    M400 P500
    G0 Z0.000 F12000
    M960 S0 P0
    M960 S1 P1
    G0 X215.00 Y4.750
    M400 P200
    M971 S5 P1
    M973 S2 P1
    M400 P500
    M960 S0 P0
    M960 S2 P1
    G0 X228.5 Y6.75
    M400 P200
    M971 S5 P3
    G0 Z0.500 F12000
    M960 S0 P0
    M960 S2 P1
    G0 X228.5 Y6.75
    M400 P200
    M971 S5 P4
    M973 S2 P0
    M400 P500
    M960 S0 P0
    M960 S1 P1
    G0 X215.00 Y4.750
    M400 P500
    M971 S5 P2
    M963 S1
    M400 P1500
    M964
    T1100
    G1 Z3 F3000

    M400
    M500 ; save cali data

    M104 S220 ; rise nozzle temp now ,to reduce temp waiting time.

    T1100
    M400 P400
    M960 S0 P0
    G0 F30000.000 Y10.000 X65.000 Z0.000
    M400 P400
    M960 S1 P1
    M400 P50

    M969 S1 N3 A2000
    G0 F360.000 X181.000 Z0.000
    M980.3 A70.000 B74.8733 C5.000 D299.493 E5.000 F175.000 H1.000 I0.000 J0.020 K0.040
    M400 P100
    G0 F20000
    G0 Z1 ; rise nozzle up
    T1000 ; change to nozzle space
    G0 X45.000 Y4.000 F30000 ; move to test line pos
    M969 S0 ; turn off scanning
    M960 S0 P0


    G1 Z2 F20000
    T1000
    G0 X45.000 Y4.000 F30000 E0
    M109 S220
    G0 Z0.3
    G1 F1500.000 E3.600
    G1 X65.000 E1.24726 F1200
    G1 X70.000 E0.31181 F1200
    G1 X75.000 E0.31181 F4800
    G1 X80.000 E0.31181 F1200
    G1 X85.000 E0.31181 F4800
    G1 X90.000 E0.31181 F1200
    G1 X95.000 E0.31181 F4800
    G1 X100.000 E0.31181 F1200
    G1 X105.000 E0.31181 F4800
    G1 X110.000 E0.31181 F1200
    G1 X115.000 E0.31181 F4800
    G1 X120.000 E0.31181 F1200
    G1 X125.000 E0.31181 F4800
    G1 X130.000 E0.31181 F1200
    G1 X135.000 E0.31181 F4800

    ; see if extrude cali success, if not ,use default value
    M1002 judge_last_extrude_cali_success
    M622 J0
        M400
        M900 K0.02 M0.099831
    M623

    G1 X140.000 E0.31181 F1200
    G1 X145.000 E0.31181 F4800
    G1 X150.000 E0.31181 F1200
    G1 X155.000 E0.31181 F4800
    G1 X160.000 E0.31181 F1200
M73 P30 R15
    G1 X165.000 E0.31181 F4800
    G1 X170.000 E0.31181 F1200
    G1 X175.000 E0.31181 F4800
    G1 X180.000 E0.31181 F1200
    G1 X185.000 E0.31181 F4800
    G1 X190.000 E0.31181 F1200
    G1 X195.000 E0.31181 F4800
    G1 X200.000 E0.31181 F1200
    G1 X205.000 E0.31181 F4800
    G1 X210.000 E0.31181 F1200
    G1 X215.000 E0.31181 F4800
    G1 X220.000 E0.31181 F1200
    G1 X225.000 E0.31181 F4800
    M973 S4

M623

;===== wait chamber temperature reaching the reference value =======


;========turn off light and wait extrude temperature =============
M1002 gcode_claim_action : 0
M973 S4 ; turn off scanner
M400 ; wait all motion done before implement the emprical L parameters
;M900 L500.0 ; Empirical parameters
M109 S220
M960 S1 P0 ; turn off laser
M960 S2 P0 ; turn off laser
M106 S0 ; turn off fan
M106 P2 S0 ; turn off big fan
M106 P3 S0 ; turn off chamber fan

M975 S1 ; turn on mech mode supression
G90
M83
T1000
;===== purge line to wipe the nozzle ============================
G1 E-0.8 F1800
G1 X18.0 Y2.5 Z0.8 F18000.0;Move to start position
G1 E0.8 F1800
M109 S220
G1 Z0.2
G0 X239 E15 F4800
G0 Y12 E0.7 F1200
; MACHINE_START_GCODE_END
; filament start gcode
M106 P3 S150


;VT0 H-1
G90
G21
M83 ; use relative distances for extrusion
M981 S1 P20000 ;open spaghetti detector
