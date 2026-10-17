"""The plan this install runs on: the one name every gate imports.

With kiln-pro installed, this name IS kiln-pro's licensing module, exactly
as before this file existed (kiln-pro used to register it itself).  Without
it, which is every ``pip install kiln3d``, the plan comes from the Kiln
account this machine is signed in to (:mod:`kiln.account_plan`): a
subscriber who runs ``kiln signin`` is on their plan here, and everyone
else is on Free.

Names only kiln-pro defines (``get_license_manager`` and the rest) raise
``ImportError`` on a plain install, as they always have; callers that need
them already handle that.
"""

from __future__ import annotations

import logging
import sys

_pro_licensing = None
try:
    from kiln_pro.enterprise import licensing as _pro_licensing
except ImportError:
    # No kiln-pro: the account's plan, below.
    _pro_licensing = None
except Exception:  # noqa: BLE001 — a broken kiln-pro must not break Kiln
    logging.getLogger(__name__).warning(
        "kiln-pro is installed but its licensing module failed to load; "
        "using the signed-in account's plan",
        exc_info=True,
    )
    _pro_licensing = None

if _pro_licensing is not None:
    sys.modules[__name__] = _pro_licensing
else:
    from kiln.account_plan import (  # noqa: F401 — re-exported
        BUSINESS_TIER_MAX_PRINTERS,
        FREE_TIER_MAX_PRINTERS,
        PRO_TIER_MAX_PRINTERS,
        LicenseTier,
        check_tier,
        get_tier,
        max_printers_for_tier,
        refresh_plan,
        requires_tier,
    )
