# ============================================================================
# restore_open_question.py  —  one-time maintenance: BRING BACK the free-text
# (OPEN) comment box on every feedback template.
# ============================================================================
# WHY THIS EXISTS
#   The Dean asked (Sept 2026) for the student free-text comment box to return.
#   It had earlier been removed by `remove_open_question.py`, which simply
#   DELETED the open-comment question row from each template's latest version.
#   Crucially, that removal touched ONLY the data (the question rows) — none of
#   the machinery that renders, stores, scores or prints a free-text answer was
#   taken out. So restoring the box is the exact inverse operation: re-INSERT one
#   OPEN question into each template's latest version, and the whole pipeline
#   lights up again on its own.
#
# HOW THE PIECES FIT (why re-inserting ONE row is enough):
#   * services.questions_for_version() already returns free-text questions with
#     is_free_text=True, so the student form (student_course_form.html) renders a
#     <textarea> for them again automatically.
#   * student/routes.course_submit() already treats free-text as OPTIONAL (it is
#     skipped by the "every rating is mandatory" validator) and stores whatever
#     the student typed into the anonymous `answer.value` column.
#   * scoring.compute_scores() already collects every free-text answer verbatim
#     into result["open_comments"] and NEVER scores it (a free-text question has
#     no weighted options), so the approved /10 numbers are completely unchanged.
#   * report_export renders that open_comments block on the report PDF/Excel.
#   In short: the scoring engine and reports are untouched; only the question
#   comes back.
#
# WHAT IT DOES, for the LATEST version of each template (the one used going
# forward — see services.current_template_version_id, which always picks the
# highest version_no):
#   1. If that version ALREADY has a free-text question, do nothing (idempotent).
#   2. Otherwise INSERT one OPEN question:
#        - scale   = the existing 'OPEN' scale (is_free_text=1),
#        - section = 'Open' (matches the original),
#        - text    = the verbatim original prompt (restored from the pre-removal
#                    backup master.db.prev-openq-20260807-123607),
#        - display_order = max(existing)+1, so it sits LAST on the form, exactly
#                    where it used to (Theory 13, Lab 12, Skill 14, AE 8).
#
# It does NOT change is_locked and does NOT touch older versions (past cycles'
# frozen forms stay exactly as students answered them). Running it twice is safe.
#
# ANONYMITY: master.db only; it never opens a per-cycle answer file.
#
# TIMING NOTE (important): run this BEFORE students start submitting for the
# current cycle. A response is tied to the template version it answered; students
# who submit before the question is restored simply won't have been shown it
# (their submissions remain valid — the comment is optional). So restore first,
# then open/announce the cycle.
#
# USAGE (from the app folder):  python restore_open_question.py
#   Run it once wherever the live master.db is (locally, or on the
#   PythonAnywhere Bash console), then Reload / restart so the running app sees
#   the change. If you edit a downloaded copy, re-upload master.db afterwards.
# ============================================================================

import db   # the one master.db opener (keeps WAL + the anonymity two-file split)


# The verbatim prompt the box used to show, restored exactly (do not paraphrase —
# keeping it identical means past and future cycles read the same question).
OPEN_QUESTION_TEXT = (
    "Are any specific things about this course that could be improved to "
    "better support student learning?"
)
OPEN_SECTION = "Open"          # the section label the original used
OPEN_SCALE_CODE = "OPEN"       # the free-text scale (scale.is_free_text = 1)


def _latest_version_id(conn, template_id):
    """The highest-numbered version of a template — the one the student form and
    the admin preview both use going forward (services.current_template_version_id
    selects with the same ORDER BY version_no DESC)."""
    row = conn.execute(
        "SELECT id FROM template_version WHERE template_id = ? "
        "ORDER BY version_no DESC LIMIT 1", (template_id,)).fetchone()
    return row["id"] if row else None


def _open_scale_id(conn):
    """Resolve the id of the shared free-text 'OPEN' scale. It is seeded once and
    survived the earlier removal (only the question rows were deleted, not the
    scale), so this should always find it — but we fail loudly if it is missing
    rather than silently inserting a question with no scale."""
    row = conn.execute(
        "SELECT id FROM scale WHERE code = ? OR is_free_text = 1 "
        "ORDER BY (code = ?) DESC LIMIT 1",
        (OPEN_SCALE_CODE, OPEN_SCALE_CODE)).fetchone()
    if row is None:
        raise RuntimeError(
            "No free-text 'OPEN' scale found in master.db. Re-seed the scales "
            "(seed_data.py) before restoring the open question.")
    return row["id"]


def restore_open_questions(conn):
    """Add the OPEN question back to every template's latest version (unless it
    is already present). Returns a per-template summary for the printout."""
    summary = []
    open_scale_id = _open_scale_id(conn)

    templates = conn.execute(
        "SELECT t.id, t.name, c.name AS category "
        "FROM template t LEFT JOIN category c ON c.id = t.category_id "
        "ORDER BY t.id").fetchall()

    for t in templates:
        vid = _latest_version_id(conn, t["id"])
        if vid is None:
            summary.append((t["category"] or t["name"], None, "no version"))
            continue

        # Is a free-text question already on this version? If so, this template is
        # done — that is what makes the script safe to re-run.
        existing = conn.execute(
            "SELECT COUNT(*) AS n FROM question q JOIN scale s ON s.id = q.scale_id "
            "WHERE q.template_version_id = ? AND s.is_free_text = 1", (vid,)
        ).fetchone()["n"]
        if existing:
            summary.append((t["category"] or t["name"], vid, "already present"))
            continue

        # Place it LAST: one past the current maximum display_order. COALESCE
        # handles the (impossible here) case of a version with zero questions.
        next_order = conn.execute(
            "SELECT COALESCE(MAX(display_order), 0) + 1 AS nxt "
            "FROM question WHERE template_version_id = ?", (vid,)
        ).fetchone()["nxt"]

        conn.execute(
            "INSERT INTO question (template_version_id, section, text, scale_id, "
            "                      display_order) VALUES (?, ?, ?, ?, ?)",
            (vid, OPEN_SECTION, OPEN_QUESTION_TEXT, open_scale_id, next_order))

        summary.append((t["category"] or t["name"], vid, f"added at #{next_order}"))

    return summary


def main():
    print("=" * 70)
    print("Restoring the free-text (open) comment box to every template…")
    print("=" * 70)
    conn = db.get_master()
    try:
        summary = restore_open_questions(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    for category, vid, note in summary:
        vlabel = f"version #{vid}" if vid is not None else "—"
        print(f"  {category:<24} latest {vlabel:<12}: {note}")
    print("-" * 70)
    print("Done. Reload on PythonAnywhere (or restart python run.py) so the")
    print("running app rebuilds the student form with the comment box.")
    print("REMINDER: run this BEFORE students begin submitting this cycle.")
    conn.close()


if __name__ == "__main__":
    main()
