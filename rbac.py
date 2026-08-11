# ============================================================================
# rbac.py  —  Version 2.0 · §4 : the ONE access-control choke-point
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# Version 2.0 adds leader logins (HODs, Vice Dean, Dean). The hard promise of
# §4 is that "an E01 HOD can never see E02". The safe way to keep a promise like
# that is to have exactly ONE place that decides what a leader may see, and route
# every dashboard, ATR view and report download through it — so scope is derived
# from data (app_user.scope_dept_ids), never hand-coded per screen where one
# forgotten WHERE clause would leak a department.
#
# This module is that single choke-point. It is deliberately PURE and DB-light:
#   * in_scope(user, dept_code)      — a pure predicate (no DB) : the whole rule.
#   * scope_clause(user)             — turns a user's scope into a SQL fragment.
#   * visible_offerings(master,user) — the ONLY sanctioned way to list offerings
#                                      a leader may see; every read goes through it.
#
# SCOPE MODEL (Design §4; extended in v2.2 §18). Each app_user carries
# `scope_dept_ids`:
#     HOD        -> one OR MORE E-codes, e.g. 'E01' or 'E01,E05' (v2.2: the same
#                   person may head several program codes). Never the 'ALL'
#                   sentinel — only VD/Dean are college-wide.
#     Vice Dean  -> a subset of E-codes, or the sentinel 'ALL'
#     Dean       -> 'ALL' (whole college)
# The parsing below (allowed_dept_codes) has ALWAYS read this as a CSV set, so a
# multi-department HOD needed no logic change here — only this note and the
# relaxed validation in admin/users.py._compute_scope().
# The sentinel string 'ALL' (or the DEAN role) means "no restriction". Anything
# else is read as a comma-separated allow-list of department codes.
#
# FACULTY never come through here: their ATR magic links carry their own narrow,
# one-offering scope (§5.2), so they bypass this path entirely. This module is
# only about the ~13 password-login leaders.
# ----------------------------------------------------------------------------


# The sentinel that means "whole college / unrestricted" when it appears in
# app_user.scope_dept_ids. Kept as a named constant so the seed, the tests and
# this module all agree on the exact string.
SCOPE_ALL = "ALL"

# The role that is unconditionally college-wide regardless of what its
# scope_dept_ids says (belt-and-braces: a Dean is always 'ALL').
ROLE_DEAN = "DEAN"
ROLE_VICE_DEAN = "VICE_DEAN"
ROLE_HOD = "HOD"
# v2.1 — the ADMIN role: the two operators who run the console (login-gated). An
# admin is NOT part of the endorsement org tree (never an endorser), so the
# distribution roll-ups query only HOD/VICE_DEAN/DEAN and skip ADMIN rows. Admins
# live in the same app_user table purely to reuse the password + set-password-link
# machinery; their access is gated by the admin blueprint, not by dept scope.
ROLE_ADMIN = "ADMIN"

# v2.0 · Module 5 — the special home_dept_code value meaning "this faculty has NO
# HOD; only the Vice Dean/Dean oversee them" (external/visiting staff). It is NOT
# a real department, so no HOD's scope ever contains it; the Vice Dean's college-
# wide scope does. A faculty whose home_dept_code is NULL is "UNASSIGNED" — also
# invisible to every HOD, but shown to the admin/Dean as a to-do to assign a
# department (the professor's rule: everyone gets a department except externals).
DEPT_EXTERNAL = "EXTERNAL"


# ----------------------------------------------------------------------------
# effective_dept(master, offering_row) -> str | None
# ----------------------------------------------------------------------------
# The department that decides WHO endorses/sees an offering's ATR. Under the
# Faculty Master (Module 5) this is the FACULTY's home department (which HOD owns
# the person), NOT the course's own department — resolved via
# offering.faculty_id -> faculty.home_dept_code. Returns:
#     a real dept code   -> that department's HOD (and its VD/Dean) endorse
#     'EXTERNAL'          -> no HOD; Vice Dean/Dean only
#     None                -> UNASSIGNED (faculty not in master, or dept not set)
# Guarded so a DB that predates the faculty table still works (returns None).
# ----------------------------------------------------------------------------
def effective_dept(master, offering_row):
    try:
        emp = offering_row["faculty_id"]
    except (KeyError, IndexError, TypeError):
        emp = None
    if not emp:
        return None
    try:
        row = master.execute(
            "SELECT home_dept_code FROM faculty WHERE emp_no = ?", (emp,)).fetchone()
    except Exception:
        return None            # no faculty table yet
    return row["home_dept_code"] if row else None


# ----------------------------------------------------------------------------
# _user_get(user, key, default) — read a field from an app_user that may be a
# sqlite3.Row, a plain dict, or any mapping. Rows raise on a missing key, so we
# guard, letting tests pass a bare {'role':..., 'scope_dept_ids':...} dict while
# production passes a real DB row — both work through the same code.
# ----------------------------------------------------------------------------
def _user_get(user, key, default=None):
    try:
        value = user[key]
    except (IndexError, KeyError, TypeError):
        # Fall back to attribute access for object-style users, else the default.
        value = getattr(user, key, default)
    return value if value is not None else default


# ----------------------------------------------------------------------------
# allowed_dept_codes(user) -> set[str] | None
# ----------------------------------------------------------------------------
# Resolve a user's scope to a CONCRETE set of department codes, or None meaning
# "unrestricted / whole college". This is the heart of the model, and everything
# else (the predicate, the SQL clause) is expressed in terms of it.
#
#   * DEAN role                      -> None (always all)
#   * scope_dept_ids == 'ALL'        -> None (all)
#   * scope_dept_ids == 'E01,E03'    -> {'E01','E03'}
#   * empty / missing scope          -> empty set (sees NOTHING — a fail-CLOSED
#                                        default, the safe direction for RBAC)
#
# Parsing is forgiving of whitespace and case ('e01' -> 'E01', 'all' -> ALL) so
# a hand-typed scope in the admin form still behaves.
# ----------------------------------------------------------------------------
def allowed_dept_codes(user):
    role = (_user_get(user, "role", "") or "").strip().upper()
    if role == ROLE_DEAN:
        return None  # a Dean is unconditionally college-wide

    raw = (_user_get(user, "scope_dept_ids", "") or "").strip()
    if not raw:
        # Fail CLOSED: a leader with no scope set sees nothing, never everything.
        return set()

    # Split the CSV, normalise each token, drop blanks.
    tokens = [t.strip().upper() for t in raw.split(",") if t.strip()]
    if SCOPE_ALL in tokens:
        return None  # the 'ALL' sentinel anywhere in the list ⇒ unrestricted
    return set(tokens)


# ----------------------------------------------------------------------------
# in_scope(user, dept_code) -> bool   (the pure §4 predicate)
# ----------------------------------------------------------------------------
# "May this user see rows belonging to department `dept_code`?" No DB, no I/O —
# just set membership against allowed_dept_codes(). This is the single sentence
# the whole promise reduces to, and the one the unit tests hammer:
#     in_scope(E01_HOD, 'E02')  ==  False      (the headline guarantee)
#     in_scope(vice_dean, any) subset of its ticked depts
#     in_scope(dean, any)       ==  True
# ----------------------------------------------------------------------------
def in_scope(user, dept_code):
    allowed = allowed_dept_codes(user)
    if allowed is None:
        return True  # unrestricted (Dean, or Vice Dean scoped 'ALL')
    return (dept_code or "").strip().upper() in allowed


# ----------------------------------------------------------------------------
# scope_clause(user) -> (sql_fragment, params)
# ----------------------------------------------------------------------------
# Express the same scope as a parameterised SQL fragment to append to a WHERE on
# the `offering` table (aliased `o`). Returning params separately keeps every
# query parameterised (no string-interpolated dept codes → no injection surface,
# consistent with the rest of the app).
#
#   unrestricted -> ("1=1", [])                          # matches every row
#   {E01,E03}    -> ("o.dept_code IN (?,?)", ['E01','E03'])
#   empty scope  -> ("1=0", [])                          # matches NO row (fail closed)
#
# Because visible_offerings() below is the only sanctioned reader, callers get
# this filtering for free and can never forget it.
# ----------------------------------------------------------------------------
def scope_clause(user):
    allowed = allowed_dept_codes(user)
    if allowed is None:
        return "1=1", []                 # whole college
    if not allowed:
        return "1=0", []                 # nothing (fail closed)
    placeholders = ",".join("?" * len(allowed))
    # sorted() gives a stable, testable parameter order.
    return "o.dept_code IN (%s)" % placeholders, sorted(allowed)


# ----------------------------------------------------------------------------
# visible_offerings(master, user, cycle_code=None) -> list[Row]
# ----------------------------------------------------------------------------
# THE sanctioned read. Return every offering `user` is allowed to see, optionally
# narrowed to one cycle. Built by composing the scope_clause() fragment onto a
# normal offering query, so the §4 filter is applied in exactly one place for
# every dashboard, ATR list and report picker that lists offerings for a leader.
#
# It reads master.db only (offering identities live there); it never opens a
# per-cycle answer file, so the anonymity boundary is untouched. Joining the
# category name is a convenience for the dashboards, matching how reports.py
# already shapes offering rows.
# ----------------------------------------------------------------------------
def visible_offerings(master, user, cycle_code=None):
    # v2.0 · Module 5: scope now follows the FACULTY's home department (who the
    # HOD owns), not the course's own department. We LEFT JOIN the faculty master
    # and filter on faculty.home_dept_code, so:
    #   * an E01 HOD sees offerings whose FACULTY belongs to E01;
    #   * an EXTERNAL faculty (home_dept_code='EXTERNAL') or an UNASSIGNED one
    #     (home_dept_code IS NULL, or faculty not in the master) matches NO HOD's
    #     dept list, so it is hidden from every HOD — and shown only to the Vice
    #     Dean/Dean, whose 'ALL' scope matches everything (1=1).
    # We reuse allowed_dept_codes() but express the clause on f.home_dept_code.
    allowed = allowed_dept_codes(user)
    if allowed is None:
        clause, params = "1=1", []                 # whole college (VD/Dean)
    elif not allowed:
        clause, params = "1=0", []                 # fail closed
    else:
        placeholders = ",".join("?" * len(allowed))
        clause = "f.home_dept_code IN (%s)" % placeholders
        params = sorted(allowed)

    sql = (
        "SELECT o.*, c.name AS category_name, c.report_key AS report_key, "
        "       f.home_dept_code AS eff_dept "
        "FROM offering o "
        "LEFT JOIN category c ON c.id = o.category_id "
        "LEFT JOIN faculty  f ON f.emp_no = o.faculty_id "
        "WHERE " + clause
    )
    # Optional cycle narrowing, still fully parameterised.
    if cycle_code is not None:
        sql += " AND o.cycle_code = ?"
        params = list(params) + [cycle_code]

    sql += " ORDER BY o.dept_code, o.year_of_study, o.course_code"
    return master.execute(sql, params).fetchall()
