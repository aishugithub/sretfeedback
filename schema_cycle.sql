-- ============================================================================
-- schema_cycle.sql  —  DDL for a PER-CYCLE database (cycle_<AY>_<CA>.db)
-- ============================================================================
-- WHERE THIS FITS IN THE WHOLE APPLICATION
-- ----------------------------------------------------------------------------
-- One of these files exists per feedback cycle, e.g. cycle_2026-27_CA1.db. It
-- holds ONLY the transient participation + answer data, and is archived (moved
-- aside) when the cycle closes (spec Section 13). It contains exactly the two
-- halves of the anonymity design that must never meet:
--
--   Group A (identity/participation):  token
--   Group B (anonymous answers):       response, answer
--
-- The critical rule (spec Section 5): these tables share NO key. `token` knows
-- WHO participated and WHICH of their courses are done, but stores nothing
-- about their actual answers. `response`/`answer` store the answers with NO
-- student id, NO token, NO roll number. Because they cannot be joined, a filled
-- form can never be traced back to a person — even by the admin with full DB
-- access. The `offering_id` in `response` refers to a row in master.db, but
-- points only to the COURSE, never to a student.
-- ============================================================================


-- ############################################################################
-- GROUP A — PARTICIPATION (one token per student per cycle)
-- ############################################################################

-- --------------------------------------------------------------------------
-- token — one row per student per cycle (spec Section 5/8). A single long
-- random token is emailed to the student; the link (.../f/<token>) reveals all
-- their courses. `progress_json` records which offerings they have completed
-- (so they can pause/resume), and `completed_all` + `completed_at` flag when
-- they are finished. THIS TABLE KNOWS WHO PARTICIPATED — AND NOTHING ELSE.
--
-- Note: reg_no is stored here (identity side) purely so the admin can send
-- reminders to non-submitters. It is NEVER written into Group B, so it can
-- never link a person to an answer.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS token (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    token         TEXT NOT NULL UNIQUE,     -- long random string used in the link
    reg_no        TEXT NOT NULL,            -- which student (identity side only)
    progress_json TEXT NOT NULL DEFAULT '{}',-- {offering_id: "done"} map for resume
    completed_all INTEGER NOT NULL DEFAULT 0,-- 1 once every course is submitted
    completed_at  TEXT,                     -- timestamp of completion
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (reg_no)                          -- one token per student per cycle
);


-- ############################################################################
-- GROUP B — ANONYMOUS ANSWERS (no identity anywhere in here)
-- ############################################################################

-- --------------------------------------------------------------------------
-- response — one row per submitted form (spec Section 5). It records only:
--   * offering_id           — which COURSE the feedback is about (a master.db id)
--   * template_version_id   — the exact question snapshot answered (a master.db id)
--   * submitted_at          — when it was submitted
-- There is deliberately NO student id, NO token, NO roll number here. When a
-- student submits, the app writes this row (Group B) AND separately ticks the
-- course off in their token.progress_json (Group A). The two writes share no
-- key, which is exactly what makes the content anonymous yet trackable.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS response (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    offering_id         INTEGER NOT NULL,   -- master.db offering.id (course only)
    template_version_id INTEGER NOT NULL,   -- master.db template_version.id
    submitted_at        TEXT NOT NULL DEFAULT (datetime('now')),
    -- quality_flag (spec §11.1): 1 = this submission looked "straight-lined"
    -- (all rating answers identical AND completed implausibly fast). It is a
    -- SIGNAL for the admin, not a block, and carries NO identity — it stays on
    -- the anonymous Group B side. Scoring ignores it entirely.
    quality_flag        INTEGER NOT NULL DEFAULT 0
);

-- --------------------------------------------------------------------------
-- answer — one row per question answered within a response (spec Section 5).
-- `value` holds the chosen option label (e.g. 'Strongly Agree', '80%',
-- 'Excellent') or the free-text comment. Scoring (Night 3) maps `value` back to
-- a weight via master.db's scale_option table at report time — the weight is
-- NOT duplicated here, so a later correction to a weight re-scores cleanly.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS answer (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    response_id INTEGER NOT NULL,           -- which response this belongs to
    question_id INTEGER NOT NULL,           -- master.db question.id that was answered
    value       TEXT,                       -- chosen option label OR free-text comment
    FOREIGN KEY (response_id) REFERENCES response(id)
);


-- ############################################################################
-- VERSION 2.0 · MODULE 2 — ATR WORKFLOW (spec §3.1, §8) — CYCLE-SCOPED
-- ############################################################################
-- WHERE THIS FITS IN THE WHOLE APPLICATION
-- ----------------------------------------------------------------------------
-- Module 1 added the leader logins + the GOOD/POOR banding step (in master.db).
-- Module 2 adds the Action-Taken-Report (ATR) workflow that a POOR band triggers.
-- These three tables are deliberately CYCLE-SCOPED (they live in this per-cycle
-- file, not master.db) for the same reason responses are: an ATR belongs to CA1
-- or CA3 specifically, and must archive WITH that cycle when it closes (spec
-- §3.1 — "ATR and its events are cycle-scoped ... they archive with the cycle
-- exactly like responses do today").
--
-- CRITICAL ANONYMITY RULE (spec §3.2, UNCHANGED): an ATR attaches to an
-- OFFERING (a course + faculty), NEVER to a student or a response row. There is
-- no student id, token, or reg_no anywhere in these tables. Faculty and leaders
-- act on AGGREGATE scores only; the two-file split is untouched. The offering_id
-- below points at a master.db offering (the COURSE), exactly as `response` does.
-- ############################################################################


-- --------------------------------------------------------------------------
-- atr — one Action-Taken-Report per (offering, cycle) that was banded POOR
-- (spec §3.1, §8.1). It is the live state of one ATR as it climbs the
-- endorsement chain. `state` is the finite-state-machine state (EXPECTED,
-- DRAFT, PENDING_HOD, PENDING_VD, PENDING_DEAN, CLOSED — see atr_workflow.py,
-- which is the ONLY module allowed to change it). `current_owner_role` is a
-- convenience mirror of "who must act next" derived from `state`, so a
-- dashboard can filter "my queue" with a single column instead of re-deriving
-- it. `body` is the faculty's written action plan (free text). The
-- UNIQUE(offering_id) guarantees exactly one ATR per offering in this cycle —
-- re-filing edits the same row, never creates a second.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS atr (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    offering_id        INTEGER NOT NULL,     -- master.db offering.id (the COURSE+faculty, never a student)
    cycle_code         TEXT    NOT NULL,     -- which cycle this ATR belongs to (redundant-but-explicit scope)
    state              TEXT    NOT NULL DEFAULT 'EXPECTED', -- FSM state; only atr_workflow.py writes it
    current_owner_role TEXT,                 -- 'FACULTY'|'HOD'|'VICE_DEAN'|'DEAN'|NULL — who must act next (derived)
    body               TEXT,                 -- the faculty's action-taken narrative (free text)
    created_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    -- NOTE: offering_id points at a master.db offering row, but there is NO SQL
    -- FOREIGN KEY here — `offering` lives in the OTHER file (the two-file split),
    -- so a cross-file FK is impossible by design. This mirrors the `response`
    -- table above, which references offering_id the same way with no FK. The
    -- link is enforced in application code, never across the anonymity boundary.
    UNIQUE (offering_id)                     -- one ATR per offering per cycle (this file IS one cycle)
);


-- --------------------------------------------------------------------------
-- atr_event — the FULL AUDIT TRAIL (spec §3.1, §8.2). One row is written for
-- EVERY action taken on an ATR — SUBMIT / ENDORSE / RETURN / REMIND — by the
-- service layer in atr_workflow.py, which never changes a state without also
-- appending here. This is what makes the endorsement chain non-repudiable:
-- every hand-off is stamped with who did it, when, and any comment (e.g. a
-- Vice Dean's reason when returning one level down). `actor_user_id` is the
-- app_user.id of the leader who acted, OR the literal sentinel 'FACULTY' when
-- the actor is a faculty member acting through a magic link (faculty have no
-- app_user row — see §5.2). It is stored as TEXT so both an integer id and the
-- 'FACULTY' sentinel fit the one column.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS atr_event (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    atr_id        INTEGER NOT NULL,          -- which ATR this event belongs to
    actor_user_id TEXT,                      -- app_user.id (as text) OR 'FACULTY' sentinel
    action        TEXT NOT NULL,             -- 'SUBMIT' | 'ENDORSE' | 'RETURN' | 'REMIND'
    comment       TEXT,                      -- optional note / return-reason (audit)
    at            TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (atr_id) REFERENCES atr(id)
);


-- --------------------------------------------------------------------------
-- faculty_token — the faculty MAGIC LINK (spec §3.1, §5.2). Faculty never log
-- in with a password; instead the distribution email carries a signed, single-
-- purpose, expiring, ONE-TIME token bound to exactly ONE offering. This mirrors
-- the student `token` table above (the app's existing magic-link pattern) and
-- the Module 1 `set_pw_token` table: an unguessable random `jti` IS the secret
-- (there is no separate signature to forge — an attacker cannot produce a jti
-- that exists in this table), verified by DB lookup in faculty_tokens.py.
--
--   purpose = 'ATR_FILE'  → powers the faculty "File ATR" button (a SUBMIT).
--   purpose = 'VIEW'      → a read-only link to view an offering's own report.
--
-- `used_at` enforces one-time use for state-changing links (set on redemption);
-- `expires_at` makes the link short-lived. The HOD "Send reminder" button
-- simply issues a FRESH ATR_FILE token, so a stale/expired link is replaced.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS faculty_token (
    jti           TEXT PRIMARY KEY,          -- unguessable token id (secrets.token_urlsafe) = the secret
    offering_id   INTEGER NOT NULL,          -- the ONE offering this link may act on (narrow scope)
    faculty_email TEXT,                      -- who it was issued to (audit; offering.faculty_email at issue)
    purpose       TEXT NOT NULL,             -- 'ATR_FILE' (SUBMIT) | 'VIEW' (read-only report)
    expires_at    TEXT NOT NULL,             -- ISO timestamp; link is dead after this
    used_at       TEXT,                      -- set once redeemed → one-time use for ATR_FILE
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
