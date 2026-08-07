# ============================================================================
# seed.py  —  COMPATIBILITY SHIM (kept only to avoid stale imports)
# ============================================================================
# The seeding logic now lives in `seed_data.py` (function `seed(conn)`), the
# single source of truth for populating master.db's configuration tables. This
# shim re-exports it under the historical name seed_all(conn). New code should
# call seed_data.seed(conn) directly.
# ============================================================================

from seed_data import seed as seed_all  # re-export under the historical name

__all__ = ["seed_all"]
