# ============================================================================
# consolidation.py  —  Elective "delivery" grouping (the one-report-per-class rule)
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# The feedback system stores ONE `offering` row per (cycle, programme code,
# course, faculty, section). That is exactly right for a CORE course: each
# programme/section is a distinct class and deserves its own report.
#
# But an ELECTIVE (a "basket" subject) is different. One elective — say
# "Data Storage and Analytics" taught by Mr. Manoj Kumaran — is offered to
# students drawn from SEVERAL programme codes (E01, E02, E03 …) who all sit in
# ONE physical class. The allocation still records it as one offering row PER
# programme (because every allocation line carries a programme code), so the same
# real class shows up as three offering rows with three offering_ids. Left
# unconsolidated, every downstream step (scoring, GOOD/POOR banding, the faculty
# report, the ATR) fires once PER programme row — three thin reports and three
# ATRs for what is really ONE class and ONE teacher.
#
# THIS MODULE introduces the single idea that fixes that everywhere: a DELIVERY.
#
#   * A "delivery" is the real teaching unit a single feedback report / band /
#     ATR should cover.
#   * For CORE / LAB / PROJECT offerings, a delivery is just that one offering
#     (behaviour is completely unchanged — one report per offering, as before).
#   * For an ELECTIVE offering, a delivery is the SET of offering rows that share
#     the same (cycle, course identity, faculty, elective basket, section) but
#     differ only by programme code. Those rows are the one class, pooled.
#
# The ANCHOR of a delivery is the smallest offering_id in the set. It is a stable,
# deterministic representative: the single band row, the single ATR row, and the
# report filename all key off the anchor, while the report CONTENT pools every
# member. "Smallest id" never changes for a fixed set of rows, so re-running
# classification or re-opening a report always picks the same anchor.
#
# WHY A SEPARATE MODULE (not a helper buried in one consumer): the exact same
# grouping rule is needed by classification.py (band once per delivery),
# distribution.py (email one report/ATR per delivery), admin/reports.py and
# atr/routes.py (download one consolidated report), and audit_report.py (audit one
# row per delivery). Encoding the rule ONCE here means the definition of "the same
# class" can never drift between those screens — change it in one place and every
# consumer agrees.
#
# THE PROFESSOR'S SAFETY GUARDRAIL (Aug 2026): "be careful not to combine classes
# taken to different sections — the same course taught to different sections of the
# same programme is NOT an elective." And its refinement: an elective itself can
# span sections — E01 sec A + sec B, E02 sec NA, E03 sec A + sec B can ALL be the
# one elective class. Two things reconcile these:
#   1. course_type gates everything — only rows explicitly marked ELECTIVE are ever
#      pooled; a CORE / LAB / PROJECT course is ALWAYS its own delivery, so a core
#      course split across sections is never touched. This alone is the guardrail.
#   2. Precisely because course_type already protects core courses, both the
#      programme code AND the section are LEFT OUT of the elective key. A single
#      elective class draws students from several programmes and several of their
#      home sections, so those offering rows must pool into ONE delivery. The only
#      things that separate two elective deliveries are a different course, a
#      different teacher, or a different basket.
#
# NO DATABASE CHANGES: this module only READS the `offering` columns that already
# exist (course_type, elective_basket, faculty_id, course_code, course_name,
# dept_code, section) plus the per-cycle `response` table to know which offerings
# actually collected feedback. It never alters a schema, a stored answer, or a
# programme's data — consolidation is purely a read-time regrouping.
# ----------------------------------------------------------------------------

from collections import OrderedDict


# The one course_type string that triggers pooling. Kept as a named constant so
# the allocation importer, the schema and this module all spell it identically —
# a typo would become a silent "no elective ever merges", so we centralise it.
COURSE_TYPE_ELECTIVE = "ELECTIVE"

# The exact set of offering columns every grouping decision needs. We always
# SELECT this list (never `SELECT *`) so the code reads deterministically and a
# future column rename is a one-line change here. `id` first because it is the
# anchor candidate.
_OFFERING_COLS = ("id, cycle_code, dept_code, course_code, course_name, "
                  "course_type, elective_basket, faculty_id, faculty, section")


# ----------------------------------------------------------------------------
# _course_identity(row) -> str
# ----------------------------------------------------------------------------
# The "which course is this" part of the delivery key. Normally the course_code
# (upper-cased) is the identity. But the schema explicitly allows blank-code
# electives (some no-code electives are distinguished only by their NAME). So when
# course_code is blank we fall back to the course NAME, prefixed 'name:' so a code
# and a name can never accidentally collide. This keeps two genuinely different
# no-code electives apart while still merging the programme rows of one of them.
# ----------------------------------------------------------------------------
def _course_identity(row):
    code = (row["course_code"] or "").strip().upper()
    if code:
        return code
    return "name:" + (row["course_name"] or "").strip().upper()


# ----------------------------------------------------------------------------
# _faculty_identity(row) -> str
# ----------------------------------------------------------------------------
# The "which teacher" part of the key. Prefer the faculty_id (emp-no) because it
# is the stable identifier; fall back to the (upper-cased) faculty NAME when the
# id is blank (can happen in test data). Two different teachers of the same
# elective therefore land in DIFFERENT deliveries — each gets their own report and
# their own ATR, which is exactly right for a team-taught basket (two-faculty
# electives are two rows by design).
# ----------------------------------------------------------------------------
def _faculty_identity(row):
    fid = str(row["faculty_id"] or "").strip().upper()
    if fid:
        return "id:" + fid
    return "nm:" + (row["faculty"] or "").strip().upper()


# ----------------------------------------------------------------------------
# delivery_key(row) -> tuple   (the heart of the whole module)
# ----------------------------------------------------------------------------
# Map one offering row to the key of the DELIVERY it belongs to. Two offering rows
# belong to the same delivery iff they produce the same key.
#
#   * A non-elective row keys to ("SINGLE", offering_id) — a key unique to that ONE
#     row, so it is always its own delivery (never merged with anything). This is
#     what preserves today's exact behaviour for CORE/LAB/PROJECT and, crucially,
#     for two sections of a core course.
#   * An elective row keys to ("ELECTIVE", cycle, course, faculty, basket) —
#     deliberately WITHOUT the programme code (dept_code) OR the section, so ALL
#     the rows of one shared elective class collapse together even when its
#     students come from several programmes AND several of their home sections
#     (E01-A, E01-B, E02-NA, E03-A, E03-B → one delivery). Core courses are safe
#     because they take the SINGLE branch below, not this one.
#
# Returning a tuple (an immutable, hashable value) lets callers use it directly as
# a dict key when grouping.
# ----------------------------------------------------------------------------
def delivery_key(row):
    ctype = (row["course_type"] or "").strip().upper()
    if ctype == COURSE_TYPE_ELECTIVE:
        return (
            "ELECTIVE",
            (row["cycle_code"] or ""),        # never merge across cycles
            _course_identity(row),            # same course code (or name)
            _faculty_identity(row),           # same teacher
            (row["elective_basket"] or "").strip().upper(),  # same basket
            # NB: programme code AND section are intentionally omitted — one
            # elective class spans both, so they must not split the delivery.
        )
    # Non-elective: unique per offering ⇒ always a delivery of one.
    return ("SINGLE", row["id"])


# ----------------------------------------------------------------------------
# _anchor(oids) -> int  — the delivery's stable representative id (smallest).
# ----------------------------------------------------------------------------
def _anchor(oids):
    return min(oids)


# ----------------------------------------------------------------------------
# group_rows(rows) -> OrderedDict  anchor_id -> group-dict
# ----------------------------------------------------------------------------
# The pure grouping step: take a list of offering rows (already fetched) and fold
# them into deliveries. No DB access here, so it is trivially unit-testable with
# plain dict "rows". Each group-dict describes one delivery:
#
#   {
#     "anchor":      int,            # smallest offering_id (the representative)
#     "oids":        [int, ...],     # ALL member offering_ids, sorted ascending
#     "rows":        [row, ...],     # the member offering rows (same order as oids)
#     "is_elective": bool,           # True when this is a pooled elective delivery
#     "dept_codes":  [str, ...],     # every programme code in the delivery, sorted
#     "basket":      str | None,     # the elective basket label (electives only)
#     "course_code": str,            # anchor row's course code (for labels)
#     "course_name": str,            # anchor row's course name (for labels)
#     "faculty":     str,            # anchor row's faculty name (for labels)
#   }
#
# The returned OrderedDict is keyed by anchor id and ordered by anchor id, so the
# grouping is deterministic across runs (important for stable report ordering and
# for tests).
# ----------------------------------------------------------------------------
def group_rows(rows):
    # First pass: bucket rows by their delivery key.
    buckets = {}
    for r in rows:
        buckets.setdefault(delivery_key(r), []).append(r)

    # Second pass: turn each bucket into a rich group-dict keyed by its anchor.
    groups = {}
    for _key, members in buckets.items():
        oids = sorted(m["id"] for m in members)
        anchor = _anchor(oids)
        # Keep the member rows in the same ascending-id order as `oids` so
        # rows[i] corresponds to oids[i] for any caller that zips them.
        members_sorted = sorted(members, key=lambda m: m["id"])
        # The anchor row supplies the human labels (course/faculty). It is the
        # first element because members_sorted is ascending by id.
        anchor_row = members_sorted[0]
        is_elective = (len(members_sorted) > 1) or (
            (anchor_row["course_type"] or "").strip().upper() == COURSE_TYPE_ELECTIVE)
        # Every programme code represented in this delivery, de-duplicated and
        # sorted, for headers like "E01, E02, E03".
        dept_codes = sorted({(m["dept_code"] or "").strip()
                             for m in members_sorted if (m["dept_code"] or "").strip()})
        groups[anchor] = {
            "anchor": anchor,
            "oids": oids,
            "rows": members_sorted,
            "is_elective": is_elective,
            "dept_codes": dept_codes,
            "basket": (anchor_row["elective_basket"] or None),
            "course_code": anchor_row["course_code"] or "",
            "course_name": anchor_row["course_name"] or "",
            "faculty": anchor_row["faculty"] or "",
        }

    # Return ordered by anchor id for deterministic iteration everywhere.
    return OrderedDict(sorted(groups.items(), key=lambda kv: kv[0]))


# ----------------------------------------------------------------------------
# _fetch_offerings(master, cycle_code, oids=None) -> list[Row]
# ----------------------------------------------------------------------------
# Read the offering rows we need to group. When `oids` is given we fetch exactly
# those (chunked to stay under SQLite's parameter limit); when it is None we fetch
# every offering in the cycle. Reads master.db only — offering identities live
# there — so the anonymity boundary (answers live in the per-cycle file) is never
# crossed by grouping.
# ----------------------------------------------------------------------------
def _fetch_offerings(master, cycle_code, oids=None):
    if oids is None:
        return master.execute(
            "SELECT %s FROM offering WHERE cycle_code = ?" % _OFFERING_COLS,
            (cycle_code,)).fetchall()

    ids = sorted(set(int(o) for o in oids))
    if not ids:
        return []
    out = []
    # Chunk the IN(...) list well under SQLite's 999-variable ceiling.
    CHUNK = 400
    for i in range(0, len(ids), CHUNK):
        chunk = ids[i:i + CHUNK]
        placeholders = ",".join("?" * len(chunk))
        rows = master.execute(
            "SELECT %s FROM offering WHERE id IN (%s)" % (_OFFERING_COLS, placeholders),
            chunk).fetchall()
        out.extend(rows)
    return out


# ----------------------------------------------------------------------------
# responded_offering_ids(cycle_db) -> list[int]
# ----------------------------------------------------------------------------
# The offering_ids that actually collected at least one response in this cycle,
# read from the per-cycle answer file. This is the universe every delivery is
# defined over — so classification, the reports list and distribution all group
# over the SAME set of offerings and therefore always agree on membership AND on
# which id is the anchor. (An elective sibling with zero responses simply is not a
# member; if it later gets responses and re-runs, the group re-forms consistently.)
# ----------------------------------------------------------------------------
def responded_offering_ids(cycle_db):
    rows = cycle_db.execute(
        "SELECT DISTINCT offering_id FROM response").fetchall()
    return [r["offering_id"] for r in rows]


# ----------------------------------------------------------------------------
# deliveries(master, cycle_code, oids) -> OrderedDict anchor_id -> group-dict
# ----------------------------------------------------------------------------
# The DB-backed grouping entry point most callers use: fetch the offering rows for
# `oids` and group them. Pass the set of offerings you care about (typically the
# responded set, or a leader's visible set); you get back the deliveries those
# offerings form. Non-elective ids come back as singleton deliveries, so a caller
# can treat every returned group uniformly.
# ----------------------------------------------------------------------------
def deliveries(master, cycle_code, oids):
    rows = _fetch_offerings(master, cycle_code, oids)
    return group_rows(rows)


# ----------------------------------------------------------------------------
# deliveries_with_responses(master, cycle_db, cycle_code) -> OrderedDict
# ----------------------------------------------------------------------------
# Convenience wrapper: the deliveries formed by exactly the offerings that
# collected responses this cycle. This is what classification, the admin reports
# list and the distribution job iterate — one entry per real class that has
# feedback, electives already pooled.
# ----------------------------------------------------------------------------
def deliveries_with_responses(master, cycle_db, cycle_code):
    return deliveries(master, cycle_code, responded_offering_ids(cycle_db))


# ----------------------------------------------------------------------------
# members_of(master, cycle_db, cycle_code, offering_id) -> list[int]
# ----------------------------------------------------------------------------
# Given ANY offering_id (an anchor, or a non-anchor member such as an old
# bookmarked E02 link), return every offering_id in its delivery — i.e. the ids to
# pool for one consolidated report. Defined over the SAME responded universe as
# everything else so the delivery (and its anchor) match classification exactly.
#
# If the offering has no responses (so it is not in any responded delivery) we
# fall back to grouping it among its own siblings from the offering table, so a
# direct hit on a zero-response elective row still resolves to its class rather
# than erroring. If even that finds nothing, we return just [offering_id] so a
# caller can always score SOMETHING.
# ----------------------------------------------------------------------------
def members_of(master, cycle_db, cycle_code, offering_id):
    offering_id = int(offering_id)

    # Primary path: the responded deliveries (authoritative, anchor-consistent).
    for grp in deliveries_with_responses(master, cycle_db, cycle_code).values():
        if offering_id in grp["oids"]:
            return list(grp["oids"])

    # Fallback: group this offering with its siblings from the full offering table
    # (covers a zero-response offering opened directly). We fetch the target row,
    # then every offering in the cycle sharing its delivery key.
    target = master.execute(
        "SELECT %s FROM offering WHERE id = ?" % _OFFERING_COLS,
        (offering_id,)).fetchone()
    if target is None:
        return [offering_id]
    key = delivery_key(target)
    all_rows = _fetch_offerings(master, cycle_code, oids=None)
    siblings = sorted(r["id"] for r in all_rows if delivery_key(r) == key)
    return siblings or [offering_id]


# ----------------------------------------------------------------------------
# dept_codes_str(group_or_rows) -> str
# ----------------------------------------------------------------------------
# A small label helper: turn a delivery's programme codes into "E01, E02, E03".
# Accepts either a group-dict (uses its precomputed 'dept_codes') or a raw list of
# offering rows. Used by the report header, the roll-up lines and the audit table
# so a consolidated elective shows every programme it served rather than just the
# anchor's one code.
# ----------------------------------------------------------------------------
def dept_codes_str(group_or_rows):
    if isinstance(group_or_rows, dict) and "dept_codes" in group_or_rows:
        codes = group_or_rows["dept_codes"]
    else:
        codes = sorted({(r["dept_code"] or "").strip()
                       for r in group_or_rows if (r["dept_code"] or "").strip()})
    return ", ".join(codes)
