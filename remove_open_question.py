# ============================================================================
# remove_open_question.py  —  one-time maintenance: drop the free-text (OPEN)
# question from every feedback template, and unlock the templates.
# ============================================================================
# WHY THIS EXISTS
#   The professor decided the free-text "Are there any specific things…" question
#   should not appear on ANY feedback form. Normally you would edit a template
#   through Admin -> Templates, but a template version LOCKS the moment the first
#   response references it (a data-integrity guard, see admin/config_routes.py),
#   and several versions had already locked from TEST/DEMO cycles. This script
#   performs the edit directly and safely, because all existing responses are
#   throwaway test data.
#
# WHAT IT DOES, for the LATEST version of each template (the one used going
# forward):
#   1. deletes every question whose answer scale is free-text (scale.is_free_text
#      = 1) — i.e. the open comment box;
#   2. sets that version's is_locked = 0, so it is fully editable again in the UI.
#
# It does NOT touch older versions (old test responses keep their original form),
# and it is idempotent — running it twice is harmless (nothing left to delete).
#
# ANONYMITY: master.db only; never opens a per-cycle answer file.
#
# USAGE (from the app folder):  python remove_open_question.py
#   Run it once locally, and once on the server (PythonAnywhere Bash console),
#   OR run it locally and re-upload master.db. Restart / Reload afterwards.
# ============================================================================

import db   # the one master.db opener


def _latest_version_id(conn, template_id):
    row = conn.execute(
        "SELECT id FROM template_version WHERE template_id = ? "
        "ORDER BY version_no DESC LIMIT 1", (template_id,)).fetchone()
    return row["id"] if row else None


def remove_open_questions(conn):
    summary = []
    templates = conn.execute(
        "SELECT t.id, t.name, c.name AS category "
        "FROM template t LEFT JOIN category c ON c.id = t.category_id "
        "ORDER BY t.id").fetchall()

    for t in templates:
        vid = _latest_version_id(conn, t["id"])
        if vid is None:
            continue

        # Which free-text questions are in this version? (Usually exactly one.)
        open_qs = conn.execute(
            "SELECT q.id FROM question q JOIN scale s ON s.id = q.scale_id "
            "WHERE q.template_version_id = ? AND s.is_free_text = 1", (vid,)
        ).fetchall()

        # Delete them.
        for q in open_qs:
            conn.execute("DELETE FROM question WHERE id = ?", (q["id"],))

        # Unlock the version so it stays editable in the admin UI.
        conn.execute(
            "UPDATE template_version SET is_locked = 0 WHERE id = ?", (vid,))

        summary.append((t["category"] or t["name"], vid, len(open_qs)))

    return summary


def main():
    print("=" * 66)
    print("Removing the free-text (open) question from every template…")
    print("=" * 66)
    conn = db.get_master()
    try:
        summary = remove_open_questions(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    for category, vid, n in summary:
        print(f"  {category:<22} latest version #{vid}: removed {n} open question(s), unlocked")
    print("-" * 66)
    print("Done. Restart the local server (python run.py) or Reload on PythonAnywhere.")
    conn.close()


if __name__ == "__main__":
    main()
