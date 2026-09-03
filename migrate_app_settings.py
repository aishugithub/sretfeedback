# ============================================================================
# migrate_app_settings.py  —  idempotent migration: add the app_setting table
# ============================================================================
# WHY THIS EXISTS
#   settings.py introduces a college-wide key/value store (app_setting) in
#   master.db — the home of the Dean's "ATR mode" master switch (Sept 2026).
#   A fresh DB gets the table from schema_master.sql, but the LIVE server DB
#   already exists, so it needs this one-time ALTER-equivalent to gain the table
#   without touching any existing data. This mirrors the project's other
#   migrate_*.py scripts (e.g. migrate_atr_email_flag.py).
#
# WHAT IT DOES (safe to run twice):
#   1. CREATE TABLE IF NOT EXISTS app_setting(key, value).
#   2. Seed the default 'atr_enabled' = '1' ONLY if the key is not already set,
#      so re-running never overwrites a choice you made in the admin UI.
#
# It changes NO other table and NO answer data. master.db only (anonymity split
# untouched).
#
# USAGE:  python migrate_app_settings.py     (locally, or on PythonAnywhere Bash)
#   Then Reload so the running app sees the table. Deploying the code before
#   running this is harmless: settings.py treats a missing table as "unset" and
#   falls back to ATR = ON (the current behaviour).
# ============================================================================

import db
import settings


def main():
    conn = db.get_master()
    try:
        # 1. Ensure the table exists (reuses settings._ensure_table so the schema
        #    is defined in exactly one place).
        settings._ensure_table(conn)

        # 2. Seed a default value only when the key is absent. We check first so a
        #    later re-run cannot clobber an admin's OFF setting back to ON.
        row = conn.execute(
            "SELECT value FROM app_setting WHERE key = ?",
            (settings.ATR_ENABLED_KEY,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO app_setting (key, value) VALUES (?, '1')",
                (settings.ATR_ENABLED_KEY,))
            seeded = "seeded atr_enabled = 1 (ON, default)"
        else:
            seeded = f"atr_enabled already set to {row['value']!r} — left as-is"

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print("app_setting table ready. " + seeded)
    print("Reload on PythonAnywhere (or restart run.py) to pick it up.")


if __name__ == "__main__":
    main()
