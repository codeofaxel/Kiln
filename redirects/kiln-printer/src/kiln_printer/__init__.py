"""
kiln-printer is a redirect package. The correct package is kiln3d.

    pip install kiln3d

kiln3d is already installed as a dependency of this package.
Visit https://kiln3d.com for documentation.
"""

import warnings as _warnings

_warnings.warn(
    "You installed 'kiln-printer'. The correct package name is 'kiln3d'. "
    "It's already installed as a dependency — just use 'import kiln' or run 'kiln' from the CLI. "
    "You can uninstall this redirect: pip uninstall kiln-printer",
    UserWarning,
    stacklevel=2,
)
