# Kiln Demo GIF
# Generated with VHS (https://github.com/charmbracelet/vhs)

Output demo/kiln-demo.gif
Output demo/kiln-demo.webm

Set Shell bash
Set FontSize 15
Set Width 880
Set Height 520
Set Padding 16
Set Theme "Catppuccin Mocha"
Set TypingSpeed 35ms
Set CursorBlink false

# Set up alias silently
Hide
Type "alias kiln='./demo/fake-kiln'"
Enter
Type "clear"
Enter
Sleep 500ms
Show

Sleep 500ms

# Discover printers on the network
Type "kiln discover"
Enter
Sleep 3500ms

# Check printer status
Type "kiln status"
Enter
Sleep 3s

# Slice a model
Type "kiln slice benchy.stl"
Enter
Sleep 3500ms

# Safety preflight
Type "kiln preflight"
Enter
Sleep 3s

# Start printing
Type "kiln print benchy_0.2mm_PLA_ender3.gcode"
Enter
Sleep 3s

# Fleet overview
Type "kiln fleet status"
Enter
Sleep 4s
