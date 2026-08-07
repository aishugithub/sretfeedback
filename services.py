# ============================================================================
# services.py  —  Shared "domain logic" used by BOTH the admin pages and the
#                 student feedback flow (and, later, the report engine)
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# Night 1 gave us the tables. Night 2 introduces real *behaviour* that several
# different routes need to agree on, exactly:
#
#   * "Which COURSES must this student give feedback on?"         -> courses_for_student
#     (year of study is now a STORED roster fact — no derivation, spec v3 §2.1)
#   * "Which frozen QUESTION SNAPSHOT does a category use now?"   -> current_template_version_id
#   * "Have any responses arrived yet for this cycle?" (the lock) -> cycle_has_responses
#   * "Make a fresh unguessable token."                          -> new_token
#
# If each route re-implemented these, they would eventually disagree (e.g. the
# admin's participation view would count different courses than the student form
# actually shows). Centralising them here guarantees the admin's "pending vs
# submitted" math and the student's course list are computed by the SAME code.
#
# IMPORTANT ANONYMITY BOUNDARY: functions here that take a `master` connection
# only ever touch master.db (identity + config). Functions that take a `cycle`
# connection only touch the per-cycle file. Nothing in this module opens both at
# once, preserving the "two writes share no key" rule (spec Section 5).
# ----------------------------------------------------------------------------

import secrets   # cryptographically-strong random tokens (never `random`, which
                 # is predictable and would let someone guess another student's link)

from config import Config  # programme master, email domain — the reference data


# ----------------------------------------------------------------------------
# email_for(reg_no)  (spec v3 §4)
# ----------------------------------------------------------------------------
# THE ONLY derivation the system performs: a student's email is their register
# number concatenated with the institutional domain, e.g.
#     E0225014  ->  E0225014@sriher.edu.in
# Pure string work, no assumptions attached. Kept here so the roster importer,
# the reminder sender and any admin view all produce the identical address.
# ----------------------------------------------------------------------------
def email_for(reg_no):
    return f"{reg_no.strip()}@{Config.STUDENT_EMAIL_DOMAIN}"


# ----------------------------------------------------------------------------
# programme_meta(reg_no) -> (programme_code, level_or_None)  (spec v3 §3, §4)
# ----------------------------------------------------------------------------
# The register number encodes the programme in its 2nd-3rd characters
# ("E[PP]YYRRR"): E02... -> programme code E02. We look that code up in the
# PROGRAMMES master purely for DISPLAY and VALIDATION — never to infer a year.
# Returns (code, level) or (code, None) if the code is not a known programme.
# ----------------------------------------------------------------------------
def programme_meta(reg_no):
    code = "E" + reg_no.strip()[1:3]          # 'E0225014' -> 'E02'
    entry = Config.PROGRAMMES.get(code)
    level = entry[1] if entry else None
    return code, level


# ----------------------------------------------------------------------------
# ARCHITECTURAL NOTE (v3): derive_year_of_study() and student_is_eligible() were
# DELETED in the v3 migration. The old design computed a student's year from an
# immutable batch_year; spec §2.1 removed that inference entirely. Year of study
# is now a STORED fact on the per-cycle roster (students.year_of_study), and a
# student is "eligible" simply by being present in this cycle's roster with
# status='active'. There is no graduation arithmetic left in the system.
# ----------------------------------------------------------------------------


# ----------------------------------------------------------------------------
# student_is_eligible(student_row)  (spec v3 §7.7)
# ----------------------------------------------------------------------------
# Under the roster model, presence in the cycle roster already means "in scope".
# The only thing that removes a student is an EXCLUSION (TC / withdrawal), which
# flips status to 'excluded'. Returns (bool, reason) so the admin UI can explain
# why someone was skipped when generating tokens.
# ----------------------------------------------------------------------------
def student_is_eligible(student_row):
    if student_row["status"] != "active":
        return False, f"status={student_row['status']}"
    return True, ""


# ----------------------------------------------------------------------------
# courses_for_student(master, student_row, cycle_code)  (spec v3 §7.3, §7.4)
# ----------------------------------------------------------------------------
# THE HEART OF THE STUDENT FLOW. Given one roster student, return every offering
# (teaching assignment) they must give feedback on this cycle. A single link then
# reveals exactly these courses.
#
# There are TWO ways an offering reaches a student, matching spec §7.4:
#
#   1. CORE / LAB / PROJECT — resolved from the ROSTER by matching
#      (dept_code, year_of_study, section). A 'NA' section on the offering means
#      the class is not divided, so it reaches every student in that year/dept;
#      a concrete section ('A'/'B') reaches only that section. (Labs resolve
#      exactly like core courses — one teacher per lab, no enrollment upload.)
#
#   2. ELECTIVE — resolved from the ENROLLMENT upload only. A student sees an
#      elective offering iff their reg_no is attached to that specific
#      offering_id in `enrollment` (which faculty's group they are in). Electives
#      are NEVER matched by year/section, so M.Sc and mixed-year electives work.
#
# The UNION of the two keeps a single ordered list with no duplicates.
# ----------------------------------------------------------------------------
def courses_for_student(master, student_row, cycle_code):
    rows = master.execute(
        """
        SELECT o.*, c.name AS category_name, c.code AS category_code
        FROM offering o
        LEFT JOIN category c ON c.id = o.category_id
        WHERE o.cycle_code = ?
          AND o.course_type != 'ELECTIVE'
          AND o.dept_code     = ?
          AND o.year_of_study = ?
          AND (o.section = 'NA' OR o.section IS NULL OR o.section = ?)

        UNION

        SELECT o.*, c.name AS category_name, c.code AS category_code
        FROM offering o
        LEFT JOIN category c ON c.id = o.category_id
        JOIN enrollment e ON e.offering_id = o.id AND e.cycle_code = o.cycle_code
        WHERE o.cycle_code = ?
          AND o.course_type = 'ELECTIVE'
          AND e.reg_no = ?

        ORDER BY course_code
        """,
        (cycle_code, student_row["dept_code"], student_row["year_of_study"],
         student_row["section"],
         cycle_code, student_row["reg_no"]),
    ).fetchall()
    return rows


# ----------------------------------------------------------------------------
# current_template_version_id(master, category_id)  (spec Sections 5 & 6.3)
# ----------------------------------------------------------------------------
# A category has ONE template, but that template is VERSIONED. Editing questions
# after a cycle has started creates a NEW version (the old one stays frozen with
# the responses that used it). "The current form" for a category is therefore
# the HIGHEST version_no of its template. The student form renders this version;
# a submitted response is stamped with whichever version it answered.
# Returns None if the category has no template yet (a freshly-added category).
# ----------------------------------------------------------------------------
def current_template_version_id(master, category_id):
    row = master.execute(
        """
        SELECT tv.id
        FROM template t
        JOIN template_version tv ON tv.template_id = t.id
        WHERE t.category_id = ?
        ORDER BY tv.version_no DESC
        LIMIT 1
        """,
        (category_id,),
    ).fetchone()
    return row["id"] if row else None


# ----------------------------------------------------------------------------
# questions_for_version(master, template_version_id)  (spec Section 9)
# ----------------------------------------------------------------------------
# Load, in display order, every question of a frozen template version together
# with its scale options (so the student form can render the exact radio choices,
# and the admin preview can show them). We return a list of dicts, each:
#   { id, section, text, scale_code, is_free_text, options:[{label,...}, ...] }
# Options are ordered by display_order so the on-screen layout matches the
# approved paper form (recall SA, A, MA, D, SD ordering even though MA outscores A).
# ----------------------------------------------------------------------------
def questions_for_version(master, template_version_id):
    qrows = master.execute(
        """
        SELECT q.id, q.section, q.text, q.display_order,
               s.id AS scale_id, s.code AS scale_code, s.is_free_text
        FROM question q
        JOIN scale s ON s.id = q.scale_id
        WHERE q.template_version_id = ?
        ORDER BY q.display_order
        """,
        (template_version_id,),
    ).fetchall()

    questions = []
    for q in qrows:
        # For a normal (non-free-text) scale, pull its options in display order.
        options = []
        if not q["is_free_text"]:
            options = master.execute(
                "SELECT label, weight, fraction, display_order "
                "FROM scale_option WHERE scale_id = ? ORDER BY display_order",
                (q["scale_id"],),
            ).fetchall()
        questions.append({
            "id": q["id"],
            "section": q["section"],
            "text": q["text"],
            "scale_code": q["scale_code"],
            "is_free_text": bool(q["is_free_text"]),
            "options": options,
        })
    return questions


# ----------------------------------------------------------------------------
# cycle_has_responses(cycle)  (spec Section 6.3 — the data-integrity lock)
# ----------------------------------------------------------------------------
# Editing questions/templates is FREE before students start answering, but must
# LOCK once even a single response has arrived, so a live cycle's data can never
# be corrupted by a mid-flight question change. The single source of truth for
# "has answering started?" is simply: does the cycle's Group B `response` table
# hold any rows? This takes the per-cycle connection (Group B lives there).
# ----------------------------------------------------------------------------
def cycle_has_responses(cycle):
    n = cycle.execute("SELECT COUNT(*) AS n FROM response").fetchone()["n"]
    return n > 0


# ----------------------------------------------------------------------------
# new_token()  (spec Sections 5 & 8)
# ----------------------------------------------------------------------------
# A URL-safe, unguessable, ~43-character random string used as the student's
# private link (.../f/<token>). secrets.token_urlsafe uses the OS CSPRNG, so
# tokens cannot be predicted or enumerated to reach someone else's form.
# ----------------------------------------------------------------------------
def new_token():
    return secrets.token_urlsafe(32)
