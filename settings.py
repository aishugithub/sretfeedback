# ============================================================================
# settings.py  —  tiny key/value store for COLLEGE-WIDE app settings (master.db)
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# A few switches govern the WHOLE system, not one cycle and not one leader — the
# first of them (Sept 2026) is the "ATR mode" master switch the Dean asked for:
# turn the whole ATR / faculty-report-disclosure flow ON or OFF from an admin
# page, and be able to flip it back later.
#
# There was no place for a global switch before: `cycle` rows are per-cycle,
# `app_user.atr_email_enabled` is per-leader, and `config.py` / `.env` are set
# on the server (not from the browser). So this module adds a tiny, generic
# key/value table `app_setting` in master.db and the two helpers everything else
# uses. Keeping it generic means the NEXT global switch is just another key — no
# new table, no new migration.
#
# WHO READS/WRITES IT:
#   * admin/routes.py  — the toggle POST writes atr_enabled here.
#   * app/__init__.py  — a context processor exposes atr_enabled() to EVERY
#                        template (base nav, HOD dashboard, distribute page).
#   * atr/routes.py    — guards the ATR action routes with atr_enabled().
#   * distribution.py  — skips ATR creation when atr_enabled() is False.
#
# ANONYMITY: master.db only (Group C config). Never opens a per-cycle answer DB.
#
# DEFENSIVE-BY-DESIGN: every read is wrapped so that if the `app_setting` table
# does not exist yet (i.e. the code was deployed but migrate_app_settings.py has
# not been run), the helpers return the DEFAULT instead of crashing. Combined
# with atr_enabled()'s default of True, this means: deploying the code changes
# NOTHING until you (a) run the migration and (b) actually switch ATR off — a
# safe, no-surprises rollout.
# ----------------------------------------------------------------------------

import sqlite3
import db   # the one master.db opener


# The setting key for the ATR master switch, named once so no caller hard-codes
# the string. "1" (or missing) = ATR ON (default, current behaviour); "0" = OFF.
ATR_ENABLED_KEY = "atr_enabled"


def _ensure_table(conn):
    """Create app_setting if it is not there yet. Cheap (CREATE TABLE IF NOT
    EXISTS) and lets set() work even on a DB that predates the migration."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS app_setting ("
        "  key   TEXT PRIMARY KEY,"
        "  value TEXT NOT NULL"
        ")")


def get(key, default=None):
    """Read one setting's raw string value, or `default` if it is unset OR the
    table does not exist yet (see DEFENSIVE-BY-DESIGN above)."""
    conn = db.get_master()
    try:
        row = conn.execute(
            "SELECT value FROM app_setting WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
    except sqlite3.OperationalError:
        # 'no such table: app_setting' — migration not run yet. Behave as unset.
        return default
    finally:
        conn.close()


def set(key, value):
    """Create-or-update one setting (UPSERT). Stores the value as a string."""
    conn = db.get_master()
    try:
        _ensure_table(conn)
        conn.execute(
            "INSERT INTO app_setting (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)))
        conn.commit()
    finally:
        conn.close()


def get_bool(key, default):
    """Read a setting as a boolean. Anything in the truthy set counts as True; a
    stored "0"/"false"/"no"/"off" (or unset -> `default`) counts as False."""
    raw = get(key, None)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


# ----------------------------------------------------------------------------
# atr_enabled() — THE one call the rest of the app uses to ask "is the ATR /
# faculty-report-disclosure flow turned on this cycle?"  Defaults to True, so
# until an admin explicitly switches it off the system behaves exactly as before.
# ----------------------------------------------------------------------------
def atr_enabled():
    return get_bool(ATR_ENABLED_KEY, True)
