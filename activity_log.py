# ============================================================================
# activity_log.py  —  Version 2.0 · Module 3 : the system-wide ACTIVITY LOG
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# The professor's question was simple and important: "what are we doing on the
# system, WHO is doing it, and WHEN?" — so that at any point we can look back and
# understand our own system. Version 2.0 already shipped a NARROW `admin_log`
# table, but it only records IAM events (create/disable a leader login, reset a
# password). It does NOT capture the day-to-day operational actions: opening a
# cycle, generating tokens, uploading an allocation, exporting a report, a leader
# logging in, a faculty member filing an ATR. THIS module is that missing, wider
# audit trail.
#
# THE DESIGN IN ONE PICTURE
# ----------------------------------------------------------------------------
#   Every state-changing HTTP request (a form POST, a delete, a report download)
#        │
#        ▼
#   install()'s after_request hook  ──►  resolves WHO (session) + WHAT (a friendly
#                                        label for the endpoint) + WHEN (UTC now)
#                                        + WHERE (path + IP) + HTTP status
#        │
#        ▼
#   record()  ──►  one row in master.db · activity_log   (best-effort, never
#                  allowed to break the real action it is describing)
#        │
#        ▼
#   /admin/activity viewer  ◄── recent()  reads the rows back for the professor.
#
# WHY THIS SHAPE (and how it fits the rest of the codebase):
#   * ONE automatic hook is the backbone, so we capture EVERYTHING that changes
#     state without having to remember to add a log line to every route. Routes
#     may OPTIONALLY enrich their entry with note(...) (a single harmless line)
#     when a human-readable detail like a count is worth keeping.
#   * It writes to master.db (the PERMANENT store), NOT the per-cycle db, so the
#     log survives the confidential archive-and-reset of a cycle (RUN-AND-HOST §6).
#   * It reuses db.get_master() — the ONE place that opens master.db with WAL +
#     foreign-key enforcement — so the logger inherits the exact same connection
#     rules as everything else; no bespoke sqlite handling here.
#
# THE ANONYMITY GUARDRAIL (spec Section 5 — the whole point of the two-file split)
# ----------------------------------------------------------------------------
#   The student feedback flow (/f/<token>, the `student` blueprint) is DELIBERATELY
#   EXCLUDED from this log. We never record who submitted, when they submitted, or
#   from which IP — because correlating submission timing/IP with a token would
#   chip away at the guarantee that an answer can never be traced to a student.
#   The activity log is an ADMIN/OPERATIONS trail only: it watches the people who
#   RUN the system (admin, leaders, faculty filing ATRs), never the students who
#   answer it. This exclusion lives in _should_log() and is commented there too.
# ----------------------------------------------------------------------------

import sys                      # to print best-effort logging failures to stderr
import sqlite3                  # only for the exception type when swallowing errors

from flask import request, session, g   # request/session give us WHERE + WHO; g carries per-request enrichment

import db                       # db.get_master() — the single master.db opener (WAL + FK on)


# ----------------------------------------------------------------------------
# ACTOR TYPES — the four kinds of "who" the system recognises.
# Kept as named constants so the viewer, the routes and this module all agree on
# the exact strings stored in activity_log.actor_type.
# ----------------------------------------------------------------------------
ACTOR_ADMIN   = "ADMIN"    # the professor operating the app locally (no login screen)
ACTOR_LEADER  = "LEADER"   # a HOD / Vice Dean / Dean logged in via the leader password flow
ACTOR_FACULTY = "FACULTY"  # a faculty member arriving on their one-time ATR magic link
ACTOR_SYSTEM  = "SYSTEM"   # the app itself (scheduled/automatic work, e.g. a mailer batch)


# ----------------------------------------------------------------------------
# ENDPOINT_LABELS — turn Flask's machine endpoint name into a PLAIN-ENGLISH verb.
# ----------------------------------------------------------------------------
# Flask sets request.endpoint to "<blueprint>.<function>" (e.g. "admin.cycles_toggle").
# Mapping those to friendly labels here means the viewer reads like a diary —
# "Opened / closed a cycle" instead of "POST /admin/cycles/3/toggle" — WITHOUT us
# having to edit a single route handler. Any endpoint not in this map still gets
# logged; it just falls back to a generic "METHOD /path" label (see _label_for).
# Add a line here whenever a new meaningful action is worth naming.
ENDPOINT_LABELS = {
    # ---- Cycles: the heart of a collection session -------------------------
    "admin.cycles_new":          "Created a cycle",
    "admin.cycles_toggle":       "Opened / closed a cycle",
    "admin.cycles_email":        "Edited a cycle's email text",
    "admin.tokens_generate":     "Generated student tokens (batch)",
    # ---- Results / distribution (post-close workflow) ----------------------
    "admin.distribute_thresholds":"Set the POOR level + re-classified",
    "admin.distribute_classify":  "Ran scoring / classification",
    "admin.distribute_send":      "Sent results to teachers",
    # ---- Data in: rosters, allocations, enrollment -------------------------
    "admin.allocation_upload":   "Uploaded a course allocation",
    "admin.enrollment_upload":   "Uploaded an enrollment file",
    "admin.students_upload":     "Uploaded the student roster",
    "admin.students_exclude":    "Excluded a student from a cycle",
    "admin.students_include":    "Re-included a student",
    "admin.students_seed_demo":  "Seeded demo students",
    "admin.roster_edit":         "Edited an offering",
    # ---- Configuration: categories, templates, scales ----------------------
    "admin.categories_new":      "Created a category",
    "admin.categories_edit":     "Edited a category",
    "admin.question_add":        "Added a question",
    "admin.question_edit":       "Edited a question",
    "admin.question_delete":     "Deleted a question",
    "admin.question_move":       "Reordered a question",
    "admin.template_new_version":"Started a new template version",
    "admin.scale_option_weight": "Changed a rating-scale weight",
    # ---- Readiness ---------------------------------------------------------
    "admin.readiness_dismiss":   "Dismissed a readiness flag",
    "admin.readiness_export":    "Exported the readiness check",
    # ---- Reports out -------------------------------------------------------
    "admin.report_one":          "Downloaded a report",
    "admin.report_bulk":         "Downloaded a bulk report batch",
    # ---- Leader (HOD/VD/Dean) authentication + ATR workflow ---------------
    "atr.leader_login":          "Leader logged in",
    "atr.leader_logout":         "Leader logged out",
    "atr.leader_set_password":   "Leader set / reset a password",
    "atr.atr_file_submit":       "Faculty filed an ATR",
    "atr.atr_act":               "Leader endorsed / returned an ATR",
    "atr.atr_remind":            "Leader sent an ATR reminder",
}

# SENSITIVE_GET — endpoints where even a GET is an ACTION worth recording (a
# report download reveals data, so "who downloaded what" matters; a logout changes
# session state), whereas an ordinary GET is just a page view and would only add
# noise. Everything else that is a GET is skipped by _should_log(); these are the
# exceptions.
SENSITIVE_GET = {
    "admin.report_one",       # single report download
    "admin.report_bulk",      # batch report download
    "admin.readiness_export", # readiness-check export
    "admin.cycles_audit_report",  # signed audit-report generation + download (v2.1)
    "atr.leader_logout",      # session end (GET, but state-changing)
    "admin.admin_logout",     # admin session end (v2.1)
}

# HTTP methods that CHANGE state. Any request using one of these is logged (unless
# it belongs to the excluded student flow). GET/HEAD/OPTIONS are read-only and are
# skipped except for the SENSITIVE_GET set above.
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


# ----------------------------------------------------------------------------
# _actor_from_session() -> (actor_type, actor_id, actor_label)
# ----------------------------------------------------------------------------
# Resolve WHO is acting from the Flask session. The leader login flow stores
# session["leader_id"] / ["leader_email"] (see atr/routes.py), so a present
# leader_id means a logged-in HOD/VD/Dean. With no leader session we are on the
# admin console — the professor running the app locally — for which there is no
# login by design (RUN-AND-HOST: the admin is the person at the laptop). Faculty
# acting via a magic link carry no session, so their routes announce themselves
# explicitly through note(actor_type=FACULTY, actor_label=<their email>); that
# override (read in _after_request) wins over this default.
def _actor_from_session():
    leader_id = session.get("leader_id")
    if leader_id:
        # A logged-in leader: label them by the email we stored at login time.
        return ACTOR_LEADER, leader_id, session.get("leader_email", "leader")
    # A logged-in ADMIN (v2.1): the console is now login-gated with two named admin
    # accounts for accountability, so attribute the action to the SPECIFIC admin
    # (their email + app_user id) rather than a generic "admin-console" — this is
    # exactly what makes the two operators individually accountable in the audit log.
    admin_id = session.get("admin_id")
    if admin_id:
        return ACTOR_ADMIN, admin_id, session.get("admin_email", "admin")
    # No session at all → a system/bootstrap action (e.g. a CLI script).
    return ACTOR_ADMIN, None, "admin-console"


# ----------------------------------------------------------------------------
# note(**overrides) — let a route ENRICH the entry the hook is about to write.
# ----------------------------------------------------------------------------
# The automatic hook knows the generic facts (who/what/when/where) but not the
# domain specifics — e.g. "37 new tokens for E01 · Year 2". A route can stash
# those on flask.g with a single harmless line, and _after_request folds them in:
#
#     activity_log.note(detail="37 new tokens, 3 already had one",
#                       cycle_code=c["code"], target_type="cycle", target_id=cycle_id)
#
# It is OPTIONAL everywhere: omit it and the request is still logged, just without
# the extra colour. Accepted keys mirror the activity_log columns: detail,
# cycle_code, target_type, target_id, action, actor_type, actor_id, actor_label.
# We store them in a dict on g so a request can call note() more than once (later
# keys win) and the hook reads the merged result.
def note(**overrides):
    # g is request-scoped; getattr guard means note() is safe even if called very
    # early. We keep a private dict rather than setting many g attributes so the
    # hook has ONE place to look.
    current = getattr(g, "_activity_note", None) or {}
    current.update({k: v for k, v in overrides.items() if v is not None})
    g._activity_note = current


# ----------------------------------------------------------------------------
# _label_for(endpoint, method, path) -> str
# ----------------------------------------------------------------------------
# Choose the human-readable action text. Prefer the curated ENDPOINT_LABELS map;
# fall back to a compact "METHOD /path" so an un-named endpoint is still legible
# and never silently unlabelled.
def _label_for(endpoint, method, path):
    if endpoint and endpoint in ENDPOINT_LABELS:
        return ENDPOINT_LABELS[endpoint]
    return "%s %s" % (method, path)


# ----------------------------------------------------------------------------
# _cycle_hint() -> str | None
# ----------------------------------------------------------------------------
# Best-effort guess at which cycle a request concerns, so the viewer can filter by
# cycle. We look (in order) at a ?cycle= query arg, a "cycle" form field, or a
# "cycle_code" form field — the three ways the admin screens carry cycle context.
# Returns None when the request is not about a particular cycle. This is a
# convenience only; a route that knows better can override via note(cycle_code=..).
def _cycle_hint():
    hint = (request.args.get("cycle")
            or request.form.get("cycle")
            or request.form.get("cycle_code"))
    hint = (hint or "").strip()
    return hint or None


# ----------------------------------------------------------------------------
# _should_log(response) -> bool
# ----------------------------------------------------------------------------
# The gate that decides whether a finished request earns a log row. The rules,
# in order:
#   1. ANONYMITY FIRST — never log the student feedback flow. request.blueprint is
#      "student" for every /f/<token> view; we bail immediately so no submission
#      timing/IP is ever recorded (see the module header's anonymity note).
#   2. Skip static files (request.endpoint == 'static') — pure noise.
#   3. Log every mutating method (POST/PUT/PATCH/DELETE).
#   4. Also log the few SENSITIVE_GET endpoints (report downloads).
#   5. Skip everything else (ordinary GET page views).
def _should_log(response):
    # (1) The anonymity guardrail — the single most important line in this file.
    if request.blueprint == "student":
        return False
    # (2) Static assets carry no operational meaning.
    if request.endpoint == "static" or request.endpoint is None:
        return False
    # (3) Any state change is logged.
    if request.method in _MUTATING_METHODS:
        return True
    # (4) A GET that is really a data export is logged; other GETs are not.
    if request.endpoint in SENSITIVE_GET:
        return True
    # (5) Ordinary read-only page view — not logged.
    return False


# ----------------------------------------------------------------------------
# record(action, *, actor_type, actor_id, actor_label, endpoint, method, path,
#        status, cycle_code, target_type, target_id, detail, ip, conn=None)
# ----------------------------------------------------------------------------
# THE single writer: insert one row into master.db · activity_log. It is
# deliberately BEST-EFFORT — an audit trail must never be the thing that crashes a
# real operation, so any error (disk full, locked db, table missing before the
# migration) is caught and merely printed to stderr; the caller's action proceeds.
#
# It opens its OWN short-lived master connection and commits immediately, so the
# log entry is durable even if the surrounding request later errors out (we WANT a
# record of an attempt, not just of successes). Passing conn= lets a caller who
# needs the log write to share a transaction with their action instead; unused by
# the hook, provided for completeness/tests.
#
# Callable OUTSIDE a request too (SYSTEM events, tests): every field is an explicit
# argument, so nothing here reads request/session — that resolution happens in the
# hook, before calling record().
def record(action, *, actor_type=ACTOR_SYSTEM, actor_id=None, actor_label=None,
           endpoint=None, method=None, path=None, status=None,
           cycle_code=None, target_type=None, target_id=None,
           detail=None, ip=None, conn=None):
    owns_conn = conn is None          # if WE opened it, WE close it in finally
    try:
        if conn is None:
            conn = db.get_master()
        conn.execute(
            """
            INSERT INTO activity_log
                (actor_type, actor_id, actor_label, action, endpoint, method,
                 path, status, cycle_code, target_type, target_id, detail, ip)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (actor_type, actor_id, actor_label, action, endpoint, method,
             path, status,
             cycle_code,
             target_type,
             None if target_id is None else str(target_id),  # ids stored as text: some targets are codes, some ints
             detail, ip),
        )
        conn.commit()
    except sqlite3.Error as exc:
        # Never propagate: logging failure must not sink the user's real action.
        print("activity_log: could not record '%s' — %s" % (action, exc),
              file=sys.stderr)
    finally:
        if owns_conn and conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


# ----------------------------------------------------------------------------
# _client_ip() -> str | None
# ----------------------------------------------------------------------------
# The requester's IP for the WHERE-from column. On the college LAN this is the
# device's 192.168.x.x address. If the app ever sits behind a proxy, the
# X-Forwarded-For header holds the real client; we prefer its first hop, else fall
# back to remote_addr.
def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        # "client, proxy1, proxy2" → the left-most entry is the original client.
        return fwd.split(",")[0].strip()
    return request.remote_addr


# ----------------------------------------------------------------------------
# _after_request(response) -> response
# ----------------------------------------------------------------------------
# The Flask hook installed by install(). It runs AFTER every request's view
# function, so it can see the final HTTP status. It assembles the who/what/when/
# where, folds in any note() enrichment the route left on g, writes one row, and
# then MUST return the response object unchanged (Flask contract).
def _after_request(response):
    try:
        if not _should_log(response):
            return response

        # WHO (session default, possibly overridden by a route's note()).
        actor_type, actor_id, actor_label = _actor_from_session()

        # WHAT/WHERE from the request itself.
        endpoint = request.endpoint
        method = request.method
        path = request.path
        action = _label_for(endpoint, method, path)
        cycle_code = _cycle_hint()

        # Fold in optional per-request enrichment set via note().
        extra = getattr(g, "_activity_note", None) or {}
        actor_type   = extra.get("actor_type", actor_type)
        actor_id     = extra.get("actor_id", actor_id)
        actor_label  = extra.get("actor_label", actor_label)
        action       = extra.get("action", action)
        cycle_code   = extra.get("cycle_code", cycle_code)
        target_type  = extra.get("target_type")
        target_id    = extra.get("target_id")
        detail       = extra.get("detail")

        record(action,
               actor_type=actor_type, actor_id=actor_id, actor_label=actor_label,
               endpoint=endpoint, method=method, path=path,
               status=response.status_code,
               cycle_code=cycle_code, target_type=target_type,
               target_id=target_id, detail=detail, ip=_client_ip())
    except Exception as exc:            # noqa: BLE001 — the hook must be bullet-proof
        # A logging bug must never turn a good response into a 500. Swallow +note.
        print("activity_log: after_request hook error — %s" % exc, file=sys.stderr)
    return response


# ----------------------------------------------------------------------------
# install(app) — wire the logger into a Flask app.
# ----------------------------------------------------------------------------
# Called once from the application factory (create_app in app/__init__.py). It
# registers the after_request hook that IS the automatic backbone. One line at the
# factory, and from then on every operational action is captured. Kept as a
# function (rather than an import side-effect) so tests can build an app without
# the hook, and so the wiring is explicit and greppable.
def install(app):
    app.after_request(_after_request)
    return app


# ----------------------------------------------------------------------------
# recent(conn, *, actor=None, action=None, cycle=None, day=None, limit=300)
# ----------------------------------------------------------------------------
# The read side, used by the /admin/activity viewer. Returns the most recent log
# rows (newest first), optionally narrowed by actor label, action text, cycle code
# or a single UTC day (YYYY-MM-DD). Every filter is PARAMETERISED (no string
# interpolation of user input → no SQL-injection surface), matching the rest of the
# app's query style. `limit` caps the page so the view stays fast even after months
# of activity.
def recent(conn, *, actor=None, action=None, cycle=None, day=None, limit=300):
    clauses, params = [], []
    if actor:
        # substring match so "cse" finds "hod.cse@..." etc.
        clauses.append("actor_label LIKE ?"); params.append("%%%s%%" % actor)
    if action:
        clauses.append("action LIKE ?"); params.append("%%%s%%" % action)
    if cycle:
        clauses.append("cycle_code = ?"); params.append(cycle)
    if day:
        # activity_log.at is 'YYYY-MM-DD HH:MM:SS' UTC; match the date prefix.
        clauses.append("substr(at,1,10) = ?"); params.append(day)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = ("SELECT * FROM activity_log %s ORDER BY id DESC LIMIT ?" % where)
    params.append(int(limit))
    return conn.execute(sql, params).fetchall()


# ----------------------------------------------------------------------------
# distinct_actions(conn) / distinct_actors(conn) — populate the viewer's filter
# dropdowns with only values that actually occur, so the professor filters by real
# choices rather than typing guesses.
# ----------------------------------------------------------------------------
def distinct_actions(conn):
    return [r["action"] for r in conn.execute(
        "SELECT DISTINCT action FROM activity_log ORDER BY action").fetchall()]


def distinct_actors(conn):
    return [r["actor_label"] for r in conn.execute(
        "SELECT DISTINCT actor_label FROM activity_log "
        "WHERE actor_label IS NOT NULL ORDER BY actor_label").fetchall()]


# ----------------------------------------------------------------------------
# summary(conn) -> dict
# ----------------------------------------------------------------------------
# The numbers behind the viewer's "at a glance" header strip. Returns a small dict
# the template turns into metric cards, so the professor sees the shape of activity
# without scrolling: how much happened in total and TODAY (UTC), a breakdown by
# actor type, and the single most recent action's timestamp. Cheap COUNT queries,
# backed by the ix_activity_* indexes.
def summary(conn):
    total = conn.execute("SELECT COUNT(*) n FROM activity_log").fetchone()["n"]
    # datetime('now') is UTC, matching how rows are stamped, so "today" lines up
    # with the at-column dates the viewer shows.
    today = conn.execute(
        "SELECT COUNT(*) n FROM activity_log WHERE substr(at,1,10)=substr(datetime('now'),1,10)"
    ).fetchone()["n"]
    # Count per actor type (ADMIN/LEADER/FACULTY/SYSTEM) so the header shows who is
    # driving the activity. Returned as a plain dict keyed by type.
    by_type = {r["actor_type"]: r["n"] for r in conn.execute(
        "SELECT actor_type, COUNT(*) n FROM activity_log GROUP BY actor_type").fetchall()}
    last = conn.execute("SELECT at FROM activity_log ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "total": total,
        "today": today,
        "by_type": by_type,
        "last_at": last["at"] if last else None,
    }
