-- ============================================================================
-- schema_master.sql  —  DDL for the PERMANENT master.db
-- ============================================================================
-- WHERE THIS FITS IN THE WHOLE APPLICATION
-- ----------------------------------------------------------------------------
-- master.db is the durable heart of the system (spec Sections 5 & 13). It
-- holds EVERYTHING that must survive a cycle reset:
--   * Group A (identity/participation, minus the token): students, offering
--   * Group C (configuration): category, template, template_version, question,
--     scale, scale_option, cycle, plus the academic_year settings switch.
-- The per-cycle answer tables (token, response, answer) live in a SEPARATE
-- file (schema_cycle.sql) so answers can never be joined to identities.
--
-- This script is idempotent: every table uses CREATE TABLE IF NOT EXISTS, so
-- re-running init_db.py will not clobber existing data.
-- ============================================================================


-- ############################################################################
-- GROUP C — CONFIGURATION (the editable content: categories, questions, scales)
-- ############################################################################

-- --------------------------------------------------------------------------
-- category — the four feedback categories (spec Section 3). Extensible: the
-- admin can add a fifth (e.g. "Internship") later, hence its own table rather
-- than a hard-coded enum.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS category (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    NOT NULL UNIQUE,   -- 'THEORY','LAB','SKILL','AE' — the internal key
    name        TEXT    NOT NULL,          -- human label, e.g. 'Theory'
    form_title  TEXT,                      -- the Google-Form title this maps to
    report_key  TEXT                       -- 'T','L','SL','AE' — which report template
);

-- --------------------------------------------------------------------------
-- scale — a reusable answer scale (spec Section 5, Group C). One scale is
-- shared by many questions. This is the "kind" of answer widget; the actual
-- options + weights live in scale_option.
--   e.g. 'AGREE5' (the 5-point agree scale), 'SYLLABUS' (100/80/60),
--        'AE_TRAINING', 'AE_MATERIAL', 'AE_KNOWLEDGE', 'AE_OVERALL',
--        'POST_ASSESS' (Theory post-assessment), 'OPEN' (free text).
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scale (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    NOT NULL UNIQUE,   -- stable key referenced by seed + code
    name        TEXT    NOT NULL,          -- description for the admin UI
    is_free_text INTEGER NOT NULL DEFAULT 0 -- 1 = open comment (no options/weights)
);

-- --------------------------------------------------------------------------
-- scale_option — the options of a scale, each with its DISPLAY position and
-- its SCORING WEIGHT (spec Section 10). This is where the approved numbers
-- live: 10/8/6/4/1 for the agree scale, 10/8/6/4/2 for AE Training, etc.
--
-- IMPORTANT NUANCE captured here: on the printed form the agree options are
-- shown in the order SA, A, MA, D, SD (display_order), but the WEIGHTS are
-- SA=10, MA=8, A=6, D=4, SD=1 — i.e. "Moderately Agree" (8) outranks "Agree"
-- (6). Keeping display_order and weight as separate columns reproduces the
-- form's layout AND the approved scoring, without either distorting the other.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scale_option (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    scale_id      INTEGER NOT NULL,        -- which scale this option belongs to
    label         TEXT    NOT NULL,        -- verbatim option text, e.g. 'Strongly Agree'
    weight        REAL,                    -- scoring weight; NULL for syllabus fractions/open
    fraction      REAL,                    -- for syllabus scale: 1.0/0.8/0.6 (spec 10.1)
    display_order INTEGER NOT NULL,        -- left-to-right position on the form
    FOREIGN KEY (scale_id) REFERENCES scale(id)
);

-- --------------------------------------------------------------------------
-- template — a questionnaire belonging to a category (spec Section 5). It is
-- VERSIONED: the template is the stable identity ("Theory feedback form"),
-- while each edit produces a new template_version snapshot.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS template (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL,          -- the category this questionnaire serves
    name        TEXT    NOT NULL,          -- e.g. 'Theory Feedback (Intermediate)'
    FOREIGN KEY (category_id) REFERENCES category(id)
);

-- --------------------------------------------------------------------------
-- template_version — a FROZEN snapshot of a template's questions (spec
-- Section 5/7). A submitted response is tied to the exact version it answered,
-- so editing questions later never corrupts past data, and CA1 wording stays
-- attached to CA1 data even after CA3 tweaks.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS template_version (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id  INTEGER NOT NULL,
    version_no   INTEGER NOT NULL,         -- 1, 2, 3 ... increments on each edit
    is_locked    INTEGER NOT NULL DEFAULT 0, -- 1 once responses start arriving
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (template_id) REFERENCES template(id),
    UNIQUE (template_id, version_no)        -- no duplicate version numbers
);

-- --------------------------------------------------------------------------
-- question — one row per question line shown on a form (spec Section 5/9).
-- A "matrix" question on the Google Form (e.g. "Faculty Teaching" with 5 rows)
-- is stored as 5 question rows sharing the same `section` label, because each
-- row is independently averaged in the scoring (spec Section 10).
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS question (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    template_version_id INTEGER NOT NULL,   -- which frozen snapshot this belongs to
    section             TEXT,               -- 'Syllabus','Faculty Teaching', etc.
    text                TEXT NOT NULL,      -- VERBATIM question text from the form
    scale_id            INTEGER NOT NULL,   -- which answer scale this question uses
    display_order       INTEGER NOT NULL,   -- order within the form
    FOREIGN KEY (template_version_id) REFERENCES template_version(id),
    FOREIGN KEY (scale_id) REFERENCES scale(id)
);


-- ############################################################################
-- GROUP A — IDENTITY & PARTICIPATION (who exists, what is offered)
-- The `token` table (the third Group A member) lives in the per-cycle DB,
-- because participation is per-cycle and must be archived/reset with it.
-- ############################################################################

-- --------------------------------------------------------------------------
-- students — the PER-CYCLE student roster (spec v3 §2, §6, §7.1).
--
-- ARCHITECTURAL REVERSAL (v3): the old design stored an immutable `batch_year`
-- and DERIVED the current year of study by a formula. v3 §2.1 DELETES that
-- inference outright. The governing principle is now "the system knows nothing
-- it was not told": every cycle begins by uploading a roster that STATES each
-- class's year of study, and the system operates on precisely and only what
-- that roster names. No graduation arithmetic, no programme durations, no
-- promotion — a wrong roster fails loudly ("3rd year B.Sc got nothing") instead
-- of a wrong formula failing silently (M.Sc students quietly excluded).
--
-- CONSEQUENCE — students are SCOPED PER CYCLE. Each cycle's roster defines its
-- own student set, so the table is keyed by (reg_no, cycle_code). There is no
-- permanent, drifting student master to maintain; re-uploading next semester's
-- roster is ~30-40 rows and a very good trade (§2.1).
--
-- This table is on the IDENTITY side (Group A) and is NEVER joined to answers.
-- `year_of_study` is a STORED fact taken from the roster, never computed.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS students (
    reg_no         TEXT NOT NULL,            -- university registration number, e.g. E0225014
    cycle_code     TEXT NOT NULL,            -- which cycle's roster this row belongs to (scope)
    name           TEXT,                     -- OPTIONAL — spec §7.1: names are not required anywhere
    email          TEXT,                     -- COMPUTED as <reg_no>@sriher.edu.in (spec §4)
    dept_code      TEXT NOT NULL,            -- programme code E01…E81 (validated against programme master)
    programme      TEXT,                     -- display label 'B.Tech'/'B.Sc'/'M.Sc' (from programme master)
    year_of_study  INTEGER NOT NULL,         -- 1..4 — SUPPLIED by the roster, NEVER derived (spec §4, §7.1)
    section        TEXT,                     -- 'A'/'B' or 'NA' when the class is not divided
    -- Exclusion / withdrawal handling (spec §7.7). status='active' normally;
    -- 'excluded' means the student left mid-cycle (TC/withdrawn) and must be
    -- removed from expected counts, have their email cancelled and token killed,
    -- while any feedback they already gave is RETAINED (anonymity means it could
    -- not be identified and removed anyway). Exclusion is reversible.
    status         TEXT NOT NULL DEFAULT 'active', -- 'active' / 'excluded'
    excl_reason    TEXT,                     -- 'TC' / 'withdrawn' / 'long absence' / 'data error'
    excl_effective TEXT,                     -- date the student effectively left
    excl_by        TEXT,                     -- admin username who excluded (audit trail)
    excl_at        TEXT,                     -- timestamp the exclusion was recorded (audit trail)
    PRIMARY KEY (reg_no, cycle_code)         -- a student may appear in many cycles' rosters, once each
);

-- --------------------------------------------------------------------------
-- roster_range — the SOURCE rows of the roster upload (spec §7.1). The admin
-- uploads ~30-40 rows, each naming a contiguous register-number range for one
-- (programme, year, section); the importer EXPANDS every range into individual
-- `students` rows for the cycle. Keeping the ranges lets us show a compact
-- reconciliation ("11 programmes · 4 year-groups · 1,847 students"), re-expand
-- on demand, and diff against the previous cycle (spec §2.2, §8.5).
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS roster_range (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_code     TEXT    NOT NULL,         -- which cycle this range belongs to (scope)
    programme_code TEXT    NOT NULL,         -- E01…E81 — must exist in programme master
    year_of_study  INTEGER NOT NULL,         -- 1..4 — supplied, never computed
    section        TEXT    NOT NULL DEFAULT 'NA', -- 'A'/'B' or 'NA' if the class is not divided
    reg_no_start   TEXT    NOT NULL,         -- full register number, e.g. E0225001
    reg_no_end     TEXT    NOT NULL,         -- full register number, e.g. E0225030
    expected_count INTEGER,                  -- admin's stated size; cross-checked against the range
    UNIQUE (cycle_code, programme_code, year_of_study, section, reg_no_start)
);

-- --------------------------------------------------------------------------
-- offering — one TEACHING ASSIGNMENT = one feedback target (spec v3 §5, §6).
--
-- THE ATOMIC UNIT of the whole system (spec §6): one faculty + one course + one
-- section. Feedback records, reports, participation rows and dashboard rows all
-- key off this single unit. A course taught by three faculty is three rows; an
-- elective basket of five options is five rows, each with its own faculty and
-- its own system-generated id.
--
-- In v3 an offering is created by the RIGID allocation upload (spec §7.2), one
-- flat row per assignment — NOT by the old matrix auto-converter, which guessed
-- at merged elective cells. The importer still guarantees idempotency via
-- ux_offering_natural below (re-import = 0 new rows).
--
-- NOTE ON COMPATIBILITY: the primary key `id` is preserved and is exactly what
-- the per-cycle response.offering_id points at, so the verified scoring engine
-- and report exporter keep working unchanged across this migration.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS offering (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_code     TEXT    NOT NULL,         -- which cycle's allocation this belongs to (scope)
    academic_year  TEXT    NOT NULL,         -- 'AY 2026-27' (kept for report headers + filenames)
    semester       TEXT,                     -- 'ODD'/'EVEN' (spec §7.2) — free-form, editable
    programme      TEXT,                     -- display label 'B.Tech'/'B.Sc'/'M.Sc'
    dept_code      TEXT    NOT NULL,         -- programme code E01…E81 (must exist in programme master)
    year_of_study  INTEGER NOT NULL,         -- 1..4 — must match a roster row (cross-check §7.2)
    section        TEXT    NOT NULL DEFAULT 'NA', -- 'A'/'B' or 'NA' if the class is not divided
    course_code    TEXT    NOT NULL,         -- e.g. 'CS4001'
    course_name    TEXT,                     -- e.g. 'Cloud Computing'
    course_type    TEXT    NOT NULL DEFAULT 'CORE', -- CORE / ELECTIVE / LAB / PROJECT (spec §7.2, §7.4)
    elective_basket TEXT,                    -- 'Elective-I' — required iff course_type=ELECTIVE
    faculty         TEXT,                    -- faculty display name, e.g. 'Dr. Priya R'
    faculty_email   TEXT,                    -- institutional email (validated at upload §7.2)
    faculty_id      TEXT,                    -- stable key across cycles, e.g. 'SRET1234'
    role            TEXT NOT NULL DEFAULT 'Primary', -- 'Primary' / 'Co-teacher'
    expected_students INTEGER,               -- optional; blank for electives (derived from enrollment)
    category_id     INTEGER,                 -- FK to category; auto-detected from code, editable
    source_sheet    TEXT,                    -- audit trail if produced by the migration helper
    source_cell     TEXT,                    -- audit trail if produced by the migration helper
    FOREIGN KEY (category_id) REFERENCES category(id)
);

-- --------------------------------------------------------------------------
-- faculty — the FACULTY MASTER (v2.0 Module 5): one row per teacher, the single
-- source of truth for their contact details and home department. The `offering`
-- table references a faculty by emp_no (via its faculty_id column); email/phone/
-- department are looked up here by join at send time, so a teacher's email is
-- changed in ONE place, never across many allocation rows.
--   * emp_no is the stable key (matches offering.faculty_id); TEXT to allow both
--     real staff numbers and any legacy placeholder, and to keep leading zeros.
--   * home_dept_code is NULLABLE — until set, ATR routing falls back to the
--     course offering's own department (see notifications.py).
-- Populated by seed_faculty.py from app/data/faculty_roster.tsv, then maintained
-- on the admin "Manage Faculty" page.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS faculty (
    emp_no         TEXT PRIMARY KEY,         -- employee/staff number: the stable key
    name           TEXT,                     -- canonical display name
    email          TEXT,                     -- institutional email (managed here)
    phone          TEXT,                     -- contact number (optional)
    home_dept_code TEXT,                     -- department -> HOD who endorses; NULLABLE
    status         TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'inactive'
    created_by     TEXT,                     -- audit: who/what created the row
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (home_dept_code) REFERENCES department(code)
);
CREATE INDEX IF NOT EXISTS idx_faculty_email     ON faculty(email);
CREATE INDEX IF NOT EXISTS idx_faculty_home_dept ON faculty(home_dept_code);

-- --------------------------------------------------------------------------
-- enrollment — students attached to an ELECTIVE teaching assignment (spec §7.3).
-- ELECTIVES ONLY. Core/lab/project audiences resolve straight from the roster by
-- (programme, year, section); an elective cannot, because one course code may be
-- taught by two faculty to two different student sets. The allocation upload has
-- already created two offering rows (Dr. A, Dr. B); enrollment attaches each
-- register number to exactly one of them via offering_id. The only membership
-- test is "is this reg_no in this cycle's roster" — no year or programme rule,
-- so M.Sc and mixed-year electives work unchanged (spec §7.3).
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS enrollment (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_code   TEXT    NOT NULL,           -- scope
    offering_id  INTEGER NOT NULL,           -- which elective teaching assignment (offering.id)
    reg_no       TEXT    NOT NULL,           -- the enrolled student (must be in the cycle roster)
    UNIQUE (cycle_code, offering_id, reg_no), -- a student enrolls in a given offering at most once
    FOREIGN KEY (offering_id) REFERENCES offering(id)
);

-- --------------------------------------------------------------------------
-- ux_offering_natural — the idempotency guard for the allocation importer.
--
-- CRITICAL SQLite NUANCE: a plain `UNIQUE (…, section, …, faculty)` table
-- constraint does NOT dedupe here, because SQLite treats every NULL as DISTINCT
-- inside a unique index. `section` (single-column depts) and `faculty` (cells
-- with no name) are frequently NULL, so those rows would slip past INSERT OR
-- IGNORE and duplicate on every re-import. We instead build the unique index on
-- COALESCE(col,'') expressions, which collapse NULL and '' to one value so the
-- natural key compares as a human would expect. `course_name` is included so two
-- genuinely different no-code electives (same dept/year, blank code) remain two
-- rows rather than colliding into one.
--
-- Result: re-running init_db.py, or clicking "Re-import" in the admin page,
-- leaves the roster unchanged (0 new rows) — the true idempotency the spec's
-- "editable roster is the working source of truth" (§4.1) depends on.
-- --------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS ux_offering_natural ON offering (
    cycle_code,
    academic_year,
    dept_code,
    year_of_study,
    COALESCE(section, ''),
    course_code,
    COALESCE(course_name, ''),
    COALESCE(faculty, ''),
    COALESCE(faculty_id, '')
);


-- ############################################################################
-- GROUP C (cont.) — CYCLE + ACADEMIC-YEAR SETTINGS
-- ############################################################################

-- --------------------------------------------------------------------------
-- cycle — CA1 (Intermediate) or CA3 (End-of-Course) (spec Section 5/7). Holds
-- the label, the editable email text, and the open/close state. Two cycles per
-- semester reuse the same templates.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cycle (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT NOT NULL,            -- 'CA1' / 'CA3' / 'testCA1' (spec §9)
    label         TEXT NOT NULL,            -- 'CA1 – Intermediate'
    academic_year TEXT NOT NULL,            -- 'AY 2026-27' (which AY this cycle belongs to)
    semester      TEXT,                     -- 'ODD'/'EVEN' — the semester this cycle covers
    email_body    TEXT,                     -- editable email text with {placeholders}
    is_open       INTEGER NOT NULL DEFAULT 0, -- 1 = accepting submissions (legacy flag, kept)
    -- v3 lifecycle (spec §9): DRAFT while uploads/readiness in progress, OPEN once
    -- the Readiness Check passes and invitations go out, CLOSED after the window,
    -- ARCHIVED once the per-cycle DB is moved aside.
    status        TEXT NOT NULL DEFAULT 'DRAFT', -- DRAFT / OPEN / CLOSED / ARCHIVED
    -- is_test (spec §9.1) — THE critical safety flag. When 1, ALL outbound email
    -- is hard-redirected to a test address inside the mailer (not by config), and
    -- generated PDFs carry a 'TEST DATA' watermark; test cycles are excluded from
    -- every real analytic and can be deleted wholesale.
    is_test       INTEGER NOT NULL DEFAULT 0,
    -- readiness_state (spec §8.6) — cached result of the Readiness Check:
    -- 'UNKNOWN' (not yet run), 'NOT_READY' (errors outstanding), 'READY' (gate open).
    -- The "Open cycle" action is disabled unless this reads 'READY'.
    readiness_state TEXT NOT NULL DEFAULT 'UNKNOWN',
    -- VERSION 2.0 · §6 — PER-CYCLE CLASSIFICATION THRESHOLDS. The GOOD/POOR band
    -- is defined PER CYCLE, not globally, and lives here on the cycle row (which
    -- already holds the cycle's label, email text and open/close state) so CA1
    -- can band on "< 8.0" while the next cycle uses "< 8.5" with zero code change,
    -- and each cycle keeps its own definition on the record. The app scores on
    -- /10, so these are /10 numbers. classification.py (never scoring.py) reads
    -- them. Editable up to the moment banding runs, matching the app's existing
    -- "editable, no re-import" style.
    threshold_overall REAL NOT NULL DEFAULT 8.0, -- overall < this ⇒ POOR (e.g. CA1 8.0)
    threshold_section REAL,                       -- optional: any critical section < this ⇒ POOR (NULL = off)
    min_responses     INTEGER NOT NULL DEFAULT 10,-- only band when n_responses ≥ this (tiny-sample guard)
    -- VERSION 2.0 · §7 — the editable greeting/preamble that tops each faculty
    -- RESULT-DISTRIBUTION email (distinct from email_body, which is the STUDENT
    -- invitation text). Kept per-cycle so wording can vary; NULL = use the default.
    dist_intro    TEXT,
    UNIQUE (academic_year, code)
);

-- --------------------------------------------------------------------------
-- readiness_dismissal — an audit record of a Readiness-Check error that the
-- admin explicitly dismissed with a written reason (spec §8.3). Dismissal is
-- never silent: the offending assignment, the reason and who/when are recorded
-- against the cycle so the gate can be re-opened deliberately, not by accident.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS readiness_dismissal (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_code    TEXT NOT NULL,             -- which cycle
    offering_id   INTEGER,                   -- the flagged teaching assignment (NULL for reverse-check items)
    check_key     TEXT NOT NULL,             -- stable id of the flagged item (e.g. 'zero_students:42')
    reason        TEXT NOT NULL,             -- the admin's written justification
    dismissed_by  TEXT,                      -- audit trail
    dismissed_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (cycle_code, check_key)
);

-- --------------------------------------------------------------------------
-- academic_year — a LABEL row only (spec v3 §2.1). In the old design this drove
-- year-of-study derivation via `start_year`; v3 deleted that inference, so this
-- table no longer computes anything. It survives purely to (a) name cycles and
-- build per-cycle DB filenames, and (b) stamp report headers. `start_year` is
-- retained as a nullable convenience column but NOTHING derives student years
-- from it any more — the roster states the year (spec §4, §7.1).
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS academic_year (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ay_label    TEXT NOT NULL UNIQUE,       -- 'AY 2026-27'
    start_year  INTEGER,                    -- vestigial label only — no derivation (v3 §2.1)
    current_sem TEXT NOT NULL DEFAULT 'odd',-- 'odd'/'even' — labels CA cycles & report semester
    is_active   INTEGER NOT NULL DEFAULT 0  -- 1 = the one active academic year (for defaults)
);

-- --------------------------------------------------------------------------
-- programme — the PROGRAMME MASTER (spec v3 §3). Display + validation only:
-- render programme names on reports and confirm an uploaded programme_code is
-- real. DELIBERATELY NO duration column and NO graduation arithmetic (§2.1) —
-- durations were removed with the derivation model. Stored as data (not a
-- hard-coded enum) so a new programme is added without a code change.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS programme (
    code   TEXT PRIMARY KEY,                -- E01…E81
    name   TEXT NOT NULL,                   -- full programme name for report headers
    level  TEXT NOT NULL                    -- 'B.Tech' / 'B.Sc' / 'M.Sc' — display label only
);


-- ############################################################################
-- VERSION 2.0 · MODULE 1 — ORG TREE · IAM · CLASSIFICATION
-- ############################################################################
-- WHERE THIS FITS IN THE WHOLE APPLICATION
-- ----------------------------------------------------------------------------
-- Everything below is the Version 2.0 DELTA on top of the proven Version 1.0
-- schema (Design §0). It is ADDITIVE ONLY: no Version 1.0 table above is
-- modified, no scoring/report/anonymity structure is touched. It adds:
--   * the ORG TREE  (department leaders + app_user leader logins),         §3/§4
--   * IAM plumbing  (set_pw_token one-time links, admin_log audit trail),  §17
--   * the CLASSIFICATION record (offering_classification) that stores the
--     GOOD/POOR band computed AFTER scoring — the single ATR trigger.      §6
--
-- These CREATE TABLE IF NOT EXISTS statements make a FRESH install (init_db.py)
-- come up already carrying the Version 2.0 shape. For an EXISTING master.db
-- (the professor's live DB) the parallel, idempotent `migrate_v2_module1.py`
-- applies the same additions with guarded `ALTER TABLE ... ADD COLUMN` — SQLite
-- has no "ADD COLUMN IF NOT EXISTS", so that conditional logic must live in
-- Python, not here. Keep the two in lock-step when either changes.
-- ############################################################################

-- --------------------------------------------------------------------------
-- department — Version 1.0 created this as a bare (code, name) legend inside
-- init_db.py. Version 2.0 promotes it to the BACKBONE of access control
-- (Design §3.1/§4): each E-code now also names its HOD login and its Vice Dean
-- login. We KEEP `code` as the primary key (it is what every offering.dept_code
-- already points at, and what Version 1.0's report legend joins on) and simply
-- add two nullable foreign keys to app_user. Defining the full table here means
-- a fresh install gets the leader columns from the start; init_db.py's own
-- `CREATE TABLE IF NOT EXISTS department (code, name)` then becomes a harmless
-- no-op that only inserts the reference names.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS department (
    code              TEXT PRIMARY KEY,     -- E01…E81 — the stable dept key (unchanged from v1.0)
    name              TEXT NOT NULL,        -- short label, e.g. 'CSE-AIML' (unchanged from v1.0)
    hod_user_id       INTEGER,              -- FK -> app_user.id : this dept's HOD login (nullable until seeded)
    vice_dean_user_id INTEGER,              -- FK -> app_user.id : the Vice Dean over this dept (nullable)
    FOREIGN KEY (hod_user_id)       REFERENCES app_user(id),
    FOREIGN KEY (vice_dean_user_id) REFERENCES app_user(id)
);

-- --------------------------------------------------------------------------
-- app_user — the ~13 LEADER LOGINS (Design §2, §3.1, §17). Students and faculty
-- never appear here — students use anonymous magic links and faculty are reached
-- purely through their offering.faculty_email, so this table holds ONLY the
-- password-login roles: one HOD per department, the Vice Dean(s), the Dean.
--
-- SCOPE lives in `scope_dept_ids` as a simple CSV of department codes, or the
-- sentinel 'ALL' for whole-college roles (Vice Dean / Dean). rbac.py is the ONE
-- place that interprets this column, so "E01 HOD can never see E02" (Design §4)
-- is enforced in a single testable choke-point rather than per screen.
--
-- PASSWORDS: the admin never sets or sees one (Design §17.2). `pw_hash` starts
-- EMPTY; the user sets it later by clicking an emailed one-time set_pw_token
-- link, and it is then stored ONLY as a pbkdf2_hmac hash (stdlib, no bcrypt
-- dependency). An empty pw_hash therefore means "account created, password not
-- yet set" — a normal, expected state right after seeding.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_user (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    email          TEXT NOT NULL UNIQUE,     -- login id, an @sriher.edu.in address
    name           TEXT,                     -- display name for the accounts screen (optional)
    role           TEXT NOT NULL,            -- 'HOD' | 'VICE_DEAN' | 'DEAN'
    scope_dept_ids TEXT NOT NULL DEFAULT '', -- CSV of E-codes, or 'ALL' for whole college
    pw_hash        TEXT NOT NULL DEFAULT '', -- pbkdf2_hmac hash; '' until the user sets a password
    status         TEXT NOT NULL DEFAULT 'active', -- 'active' | 'disabled'
    last_login_at  TEXT,                     -- stamped by the login route (Module 2/D4)
    created_by     TEXT,                     -- admin email who created the row (audit)
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- --------------------------------------------------------------------------
-- set_pw_token — the one-time "set / reset your password" email link (Design
-- §17.2/§17.3). Mirrors the app's existing student-token pattern: a random,
-- single-purpose, expiring `jti` bound to one user. On click the login flow
-- (Module 2) verifies the jti exists, is unexpired and unused, lets the user
-- choose a password (hashed into app_user.pw_hash), then stamps `used_at` so the
-- link can never be replayed. Nothing here stores a password — only the grant to
-- set one.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS set_pw_token (
    jti        TEXT PRIMARY KEY,             -- unguessable token id (secrets.token_urlsafe)
    user_id    INTEGER NOT NULL,             -- FK -> app_user.id : whose password this sets
    purpose    TEXT NOT NULL,                -- 'SET' (first-time) | 'RESET' (forgot password)
    expires_at TEXT NOT NULL,                -- ISO timestamp; link is dead after this
    used_at    TEXT,                         -- set once redeemed → one-time use
    FOREIGN KEY (user_id) REFERENCES app_user(id)
);

-- --------------------------------------------------------------------------
-- admin_log — the audit trail for every sensitive Admin/IAM action (Design
-- §17.3): creating or disabling a leader, resending a set-password link, editing
-- a scope, and starting/ending a read-only "View as" impersonation session.
-- Because the admin can act on accounts but never learns a password, this log is
-- what makes those powers accountable (and also protects the admin).
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS admin_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_user_id  INTEGER,                  -- who performed the action (app_user.id or NULL for system)
    action         TEXT NOT NULL,            -- CREATE_USER|DISABLE|RESEND_LINK|IMPERSONATE_START|IMPERSONATE_END|EDIT_SCOPE|SEED
    target_user_id INTEGER,                  -- which account it affected (nullable)
    at             TEXT NOT NULL DEFAULT (datetime('now'))
);

-- --------------------------------------------------------------------------
-- offering_classification — the RESULT of the banding step (Design §6). Scoring
-- (scoring.py) is untouched and stays purely numeric; classification.py runs
-- AFTER it and writes ONE row here per (offering, cycle) recording the GOOD/POOR
-- verdict together with the exact thresholds and response count it was judged
-- against — a transparent, auditable trail so a faculty member can be told
-- precisely WHY an ATR was requested (Design §6: "a transparent rule, not ML").
--
-- band = 'POOR' is the SINGLE trigger that Module 2 keys the whole ATR workflow
-- off: it both puts the ATR button in that faculty's email and marks the offering
-- "ATR expected" so HODs can chase missing ones. band = NULL records the
-- deliberate "insufficient responses (n < min_responses)" outcome — scored but
-- not banded, so tiny-sample noise never forces an ATR. The UNIQUE(offering,
-- cycle) key makes re-running the classifier an idempotent upsert, matching the
-- app's "editable, re-runnable" style.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS offering_classification (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    offering_id       INTEGER NOT NULL,      -- FK -> offering.id : the course+faculty judged
    cycle_code        TEXT    NOT NULL,      -- which cycle's scoring this band belongs to (scope)
    band              TEXT,                  -- 'GOOD' | 'POOR' | NULL (insufficient responses)
    overall_score     REAL,                  -- the /10 overall the verdict used (audit)
    n_responses       INTEGER,               -- responses counted (the min_responses guard input)
    threshold_overall REAL,                  -- the cycle's overall cut applied (audit)
    threshold_section REAL,                  -- the cycle's optional critical-section cut (nullable)
    min_responses     INTEGER,               -- the cycle's minimum-sample guard applied (audit)
    reason            TEXT,                  -- human-readable justification string (audit, §6)
    classified_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (offering_id, cycle_code),        -- one verdict per offering per cycle → re-run = upsert
    FOREIGN KEY (offering_id) REFERENCES offering(id)
);

-- --------------------------------------------------------------------------
-- activity_log — the SYSTEM-WIDE operations audit trail (Version 2.0 · Module 3).
-- Where admin_log (above) records only IAM account events, THIS table answers the
-- broader question "what are we doing on the system, who is doing it, and when?":
-- every state-changing action — opening/closing a cycle, generating tokens,
-- uploading an allocation, exporting a report, a leader logging in, a faculty
-- member filing an ATR — lands here as one row, written automatically by the
-- after_request hook in activity_log.py.
--
-- ANONYMITY: the student feedback flow (/f/<token>) is DELIBERATELY never written
-- here (activity_log._should_log excludes the `student` blueprint), so no answer,
-- submission time or student IP is ever recorded. This is an admin/operations
-- trail about the people who RUN the system, never the students who answer it.
-- It lives in master.db (permanent), so it survives a cycle's archive-and-reset.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS activity_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL DEFAULT (datetime('now')),  -- WHEN (UTC, matches the rest of the app)
    actor_type  TEXT NOT NULL,        -- WHO (kind): 'ADMIN' | 'LEADER' | 'FACULTY' | 'SYSTEM'
    actor_id    INTEGER,              -- WHO (id):   app_user.id when a leader; NULL for admin/system/faculty
    actor_label TEXT,                 -- WHO (human): email / 'admin-console' — what the viewer shows
    action      TEXT NOT NULL,        -- WHAT: a friendly verb, e.g. 'Opened / closed a cycle'
    endpoint    TEXT,                 -- WHAT (machine): Flask endpoint, e.g. 'admin.cycles_toggle'
    method      TEXT,                 -- HTTP method (POST/GET/…)
    path        TEXT,                 -- WHERE: the request path, e.g. '/admin/cycles/3/toggle'
    status      INTEGER,              -- HTTP status the action returned (200/302/403/…)
    cycle_code  TEXT,                 -- which cycle the action concerned, when known
    target_type TEXT,                 -- optional object kind ('cycle' | 'offering' | 'user' | …)
    target_id   TEXT,                 -- optional object id (text: some targets are codes, some ints)
    detail      TEXT,                 -- optional human note, e.g. '37 new tokens, 3 already had one'
    ip          TEXT                  -- WHERE-from: requester IP (LAN address / X-Forwarded-For)
);
-- Indexes for the viewer's common filters (newest-first paging, by-actor, by-action).
CREATE INDEX IF NOT EXISTS ix_activity_at     ON activity_log (at);
CREATE INDEX IF NOT EXISTS ix_activity_actor  ON activity_log (actor_label);
CREATE INDEX IF NOT EXISTS ix_activity_action ON activity_log (action);
CREATE INDEX IF NOT EXISTS ix_activity_cycle  ON activity_log (cycle_code);
