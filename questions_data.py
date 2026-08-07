# ============================================================================
# questions_data.py  —  COMPATIBILITY SHIM (kept only to avoid stale imports)
# ============================================================================
# The canonical, single source of truth for the approved categories, scales,
# weights and VERBATIM question sets is `seed_data.py`. This module simply
# re-exports those same structures so any older code that did
# `from questions_data import ...` keeps working. New code should import from
# seed_data directly.
# ============================================================================

from seed_data import SCALES, CATEGORIES, TEMPLATES  # re-export (single source)

__all__ = ["SCALES", "CATEGORIES", "TEMPLATES"]
