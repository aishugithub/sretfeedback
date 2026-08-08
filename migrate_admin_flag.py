# ============================================================================
# migrate_admin_flag.py  —  add app_user.is_admin (v2.1)
# ============================================================================
# Adds the is_admin flag that decouples "may use the admin console" from the
# org-tree `role` (see schema_master.sql / admin/auth.py). Guarded + idempotent.
# Run once on an existing master.db before seed_admins.py; run on the SERVER too.
#
# USAGE:  python migrate_admin_flag.py
# ----------------------------------------------------------------------------

import db


def main():
    conn = db.get_master()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(app_user)")}
        if "is_admin" not in cols:
            print("app_user — adding column is_admin (INTEGER NOT NULL DEFAULT 0)")
            conn.execute(
                "ALTER TABLE app_user ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        else:
            print("app_user — is_admin already present (skip)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
