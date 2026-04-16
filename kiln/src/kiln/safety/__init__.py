"""Safety subsystem — backstop validators for G-code and print jobs.

This package houses the last-line-of-defense safety layers that sit
between the slicer output and the printer motors.  It complements the
mesh-level (``kiln.printers.bed_fit``) and 3MF-level pre-send gates by
providing per-command interception rules that can reject dangerous
moves even when upstream validation is bypassed or produces bad data.
"""
