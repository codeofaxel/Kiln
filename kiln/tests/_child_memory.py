"""Memory measurement code inserted into isolated test children."""

# Linux's ru_maxrss can include the forking pytest worker's earlier peak.
# VmHWM belongs to the child process whose work the test is measuring.
CHILD_PEAK_MB_FUNCTION = (
    "def _peak_mb():\n"
    "    import resource, sys\n"
    "    try:\n"
    "        with open('/proc/self/status') as fh:\n"
    "            for line in fh:\n"
    "                if line.startswith('VmHWM:'):\n"
    "                    return int(line.split()[1]) / 1024\n"
    "    except OSError:\n"
    "        pass\n"
    "    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss\n"
    "    return peak / (1024 * 1024) if sys.platform == 'darwin' else peak / 1024\n"
)
