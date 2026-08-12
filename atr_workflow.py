# ============================================================================
# atr_workflow.py  —  Version 2.0 · Module 2 · §8 : the ATR state machine
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# A POOR band (from Module 1's classification.py) is the single trigger that an
# Action-Taken-Report (ATR) is EXPECTED for an offering. From there the ATR
# climbs a multi-level endorsement chain — faculty files it, the HOD endorses or
# returns it, then the Vice Dean, then the Dean — and any level may RETURN it
# ONE level down for revision. This module is the heart of that flow, split into
# two cleanly separated layers so the rule is testable in isolation:
#
#   1. transition(state, action, actor_role)  — a PURE function. Given the ATR's
#      current state, an action, and WHO is trying it, it returns the next state,
#      or raises IllegalTransition. No DB, no I/O, no side effects — this is the
#      §8.2 transition table encoded EXACTLY, and it is what the unit tests
#      hammer (every legal move, every illegal move, the return-one-level-down
#      rule). Because it is pure, "is the workflow correct?" is answerable
#      without a database at all.
#
#   2. apply_transition(cycle, atr_id, action, actor_role, actor_user_id, ...) —
#      the SERVICE layer. It reads the ATR's current state from the per-cycle DB,
#      calls the pure transition() to get the next state, writes the new state
#      back, AND — every single time, without exception — appends an atr_event
#      audit row. State never moves without a matching audit line, which is what
#      makes the endorsement chain non-repudiable (spec §8.2, §11).
#
# WHY THE TWO LAYERS: the promise "a Vice Dean returns to the HOD, never straight
# to the faculty" (spec §14.3) must be impossible to get wrong. Encoding it once,
# in a pure function, and routing every state change through it, means no route
# handler can invent an illegal shortcut. The routes (app/atr) are thin wrappers
# over apply_transition(); they hold no workflow logic of their own.
#
# ANONYMITY (spec §3.2, unchanged): an ATR is about an OFFERING. Nothing here
# reads a response, a token, or a student — it only ever touches the atr /
# atr_event tables (identity-free) in the per-cycle file.
# ----------------------------------------------------------------------------

# Role constants are shared with rbac.py (Module 1) so there is ONE spelling of
# each role across the whole system — a typo becomes an ImportError, never a
# silently-mismatched string. FACULTY is new here: faculty have no app_user row
# (they act via magic links, §5.2), so their "role" is this sentinel.
import rbac

ROLE_FACULTY = "FACULTY"
ROLE_HOD = rbac.ROLE_HOD              # 'HOD'
ROLE_VICE_DEAN = rbac.ROLE_VICE_DEAN  # 'VICE_DEAN'
ROLE_DEAN = rbac.ROLE_DEAN            # 'DEAN'


# ----------------------------------------------------------------------------
# STATES (spec §8.1). Named constants so the FSM, the DB defaults, the routes
# and the tests all agree on the exact strings.
#   EXPECTED     : offering banded POOR, ATR not yet filed (owner: faculty).
#   DRAFT        : optional save-before-submit (owner: faculty).
#   PENDING_HOD  : filed, awaiting the HOD's endorse/return.
#   PENDING_VD   : HOD-endorsed, awaiting the Vice Dean.
#   PENDING_DEAN : VD-endorsed, awaiting the Dean.
#   CLOSED       : Dean-endorsed → the ATR is complete (terminal).
# ----------------------------------------------------------------------------
STATE_EXPECTED = "EXPECTED"
STATE_DRAFT = "DRAFT"
STATE_PENDING_HOD = "PENDING_HOD"
STATE_PENDING_VD = "PENDING_VD"
STATE_PENDING_DEAN = "PENDING_DEAN"
STATE_CLOSED = "CLOSED"

ALL_STATES = frozenset({
    STATE_EXPECTED, STATE_DRAFT, STATE_PENDING_HOD,
    STATE_PENDING_VD, STATE_PENDING_DEAN, STATE_CLOSED,
})

# ----------------------------------------------------------------------------
# ACTIONS (spec §8.2). SUBMIT/ENDORSE/RETURN drive the state machine below.
# REMIND is deliberately NOT a state-changing action — it is an audit-only event
# (the HOD nudging a faculty who has not filed yet), so it is handled by the
# reminder flow (notifications.py / record_reminder here) and is intentionally
# absent from the transition table. Feeding REMIND to transition() is therefore
# an IllegalTransition, by design.
# ----------------------------------------------------------------------------
ACTION_SUBMIT = "SUBMIT"
ACTION_ENDORSE = "ENDORSE"
ACTION_RETURN = "RETURN"
ACTION_REMIND = "REMIND"


# ----------------------------------------------------------------------------
# IllegalTransition — raised by transition() when a (state, action, role) triple
# is not in the §8.2 table. A distinct exception type (not ValueError) lets the
# routes catch exactly this and turn it into a clean "you can't do that here"
# response, while the tests assert precisely on it.
# ----------------------------------------------------------------------------
class IllegalTransition(Exception):
    pass


# ----------------------------------------------------------------------------
# THE §8.2 TRANSITION TABLE, verbatim.
# ----------------------------------------------------------------------------
# Keyed by (from_state, action) -> (required_actor_role, to_state). Encoding it
# as data (not nested if/elif) makes it a direct, auditable transcription of the
# design's table, and makes "list every legal move" a one-liner for the tests.
#
#   | From         | Action  | Actor      | To            |
#   | EXPECTED     | SUBMIT  | Faculty    | PENDING_HOD   |
#   | DRAFT        | SUBMIT  | Faculty    | PENDING_HOD   |
#   | PENDING_HOD  | ENDORSE | HOD        | PENDING_VD    |
#   | PENDING_HOD  | RETURN  | HOD        | EXPECTED      |  (back to faculty)
#   | PENDING_VD   | ENDORSE | Vice Dean  | PENDING_DEAN  |
#   | PENDING_VD   | RETURN  | Vice Dean  | PENDING_HOD   |  (ONE level down)
#   | PENDING_DEAN | ENDORSE | Dean       | CLOSED        |
#   | PENDING_DEAN | RETURN  | Dean       | PENDING_VD    |  (ONE level down)
#
# RETURN-ONE-LEVEL-DOWN is the exact rule (spec §14.3): a Dean returns to the
# Vice Dean, a Vice Dean returns to the HOD, and ONLY the HOD's own return
# reaches the faculty (EXPECTED). There is deliberately NO Dean→faculty and NO
# VD→faculty entry in this table, so those shortcuts are unreachable — an
# IllegalTransition, not a special case to remember.
# ----------------------------------------------------------------------------
_TRANSITIONS = {
    (STATE_EXPECTED,     ACTION_SUBMIT):  (ROLE_FACULTY,   STATE_PENDING_HOD),
    (STATE_DRAFT,        ACTION_SUBMIT):  (ROLE_FACULTY,   STATE_PENDING_HOD),

    (STATE_PENDING_HOD,  ACTION_ENDORSE): (ROLE_HOD,       STATE_PENDING_VD),
    (STATE_PENDING_HOD,  ACTION_RETURN):  (ROLE_HOD,       STATE_EXPECTED),

    (STATE_PENDING_VD,   ACTION_ENDORSE): (ROLE_VICE_DEAN, STATE_PENDING_DEAN),
    (STATE_PENDING_VD,   ACTION_RETURN):  (ROLE_VICE_DEAN, STATE_PENDING_HOD),

    (STATE_PENDING_DEAN, ACTION_ENDORSE): (ROLE_DEAN,      STATE_CLOSED),
    (STATE_PENDING_DEAN, ACTION_RETURN):  (ROLE_DEAN,      STATE_PENDING_VD),
}


# ----------------------------------------------------------------------------
# STATE_OWNER — who must act next in each state (spec §8, §9). Derived from the
# state and mirrored onto atr.current_owner_role so a dashboard can filter "my
# queue" cheaply. A terminal/closed ATR has no owner (None). This is a pure
# lookup with no bearing on the FSM legality — it is display/routing convenience.
# ----------------------------------------------------------------------------
STATE_OWNER = {
    STATE_EXPECTED:     ROLE_FACULTY,
    STATE_DRAFT:        ROLE_FACULTY,
    STATE_PENDING_HOD:  ROLE_HOD,
    STATE_PENDING_VD:   ROLE_VICE_DEAN,
    STATE_PENDING_DEAN: ROLE_DEAN,
    STATE_CLOSED:       None,
}


# ----------------------------------------------------------------------------
# transition(state, action, actor_role) -> next_state   (the pure §8.2 rule)
# ----------------------------------------------------------------------------
# Look the (state, action) pair up in the table. If it is absent, the move is
# illegal from this state at all → IllegalTransition. If it is present but the
# actor's role is not the one the table requires (e.g. a Dean trying to endorse a
# PENDING_HOD, or a faculty trying to endorse), that too is illegal → the actor
# is not entitled to make this move. Otherwise return the next state. Roles are
# compared case-insensitively so a hand-set 'hod' still matches ROLE_HOD.
#
# This one function is the entire safety guarantee: because every state change in
# the app goes through it (via apply_transition), no illegal or out-of-turn move
# can ever be persisted.
# ----------------------------------------------------------------------------
def transition(state, action, actor_role):
    key = (state, action)
    if key not in _TRANSITIONS:
        raise IllegalTransition(
            "no %s transition from state %r" % (action, state))

    required_role, to_state = _TRANSITIONS[key]
    got = (actor_role or "").strip().upper()
    if got != required_role:
        raise IllegalTransition(
            "%s from %s requires role %s, not %r"
            % (action, state, required_role, actor_role))
    return to_state


# ----------------------------------------------------------------------------
# legal_actions(state, actor_role) -> list[str]
# ----------------------------------------------------------------------------
# Convenience for the routes/templates: which actions may THIS actor take on an
# ATR in THIS state right now? (e.g. an HOD looking at a PENDING_HOD ATR should
# see ENDORSE + RETURN buttons; the same HOD looking at a PENDING_VD ATR should
# see none.) Derived straight from the table so it can never drift from the rule.
# ----------------------------------------------------------------------------
def legal_actions(state, actor_role):
    got = (actor_role or "").strip().upper()
    out = []
    for (frm, act), (req_role, _to) in _TRANSITIONS.items():
        if frm == state and req_role == got:
            out.append(act)
    return sorted(out)


# ############################################################################
# SERVICE LAYER — applies a transition AND writes the audit row, atomically.
# ############################################################################

# ----------------------------------------------------------------------------
# _now() — a single timestamp helper so every row this module writes uses the
# same format SQLite's datetime('now') produces (UTC, 'YYYY-MM-DD HH:MM:SS').
# Kept explicit (rather than relying only on column defaults) so the state row's
# updated_at and its matching event's `at` are written from ONE clock.
# ----------------------------------------------------------------------------
def _now(conn):
    return conn.execute("SELECT datetime('now') AS t").fetchone()["t"]


# ----------------------------------------------------------------------------
# _record_event(cycle, atr_id, actor_user_id, action, comment, at)
# ----------------------------------------------------------------------------
# Append ONE audit row to atr_event. actor_user_id is stored as TEXT so both an
# integer app_user.id and the 'FACULTY' sentinel fit the one column; we coerce
# an int id to str here so callers can pass either. This is the ONLY place ATR
# events are written, so the audit trail can never be bypassed.
# ----------------------------------------------------------------------------
def _record_event(cycle, atr_id, actor_user_id, action, comment, at):
    actor = None if actor_user_id is None else str(actor_user_id)
    cycle.execute(
        "INSERT INTO atr_event (atr_id, actor_user_id, action, comment, at) "
        "VALUES (?, ?, ?, ?, ?)",
        (atr_id, actor, action, comment, at),
    )


# ----------------------------------------------------------------------------
# get_atr(cycle, atr_id) -> Row | None — read one ATR's current row. A thin
# helper so callers never hand-write the SELECT (and so a future column rename
# touches one line).
# ----------------------------------------------------------------------------
def get_atr(cycle, atr_id):
    return cycle.execute("SELECT * FROM atr WHERE id = ?", (atr_id,)).fetchone()


# ----------------------------------------------------------------------------
# get_atr_for_offering(cycle, offering_id) -> Row | None — the ATR for a given
# offering in this cycle (there is at most one, by the UNIQUE(offering_id)).
# ----------------------------------------------------------------------------
def get_atr_for_offering(cycle, offering_id):
    return cycle.execute(
        "SELECT * FROM atr WHERE offering_id = ?", (offering_id,)).fetchone()


# ----------------------------------------------------------------------------
# ensure_expected_atr(cycle, offering_id, cycle_code) -> atr_id
# ----------------------------------------------------------------------------
# Make sure an ATR row exists for a POOR offering, in the initial EXPECTED state,
# and return its id. Called by the distribution job when it flags a POOR subject
# (so the offering is on the HOD's "ATR expected" list immediately) and again
# when the faculty opens their magic link. Idempotent: the UNIQUE(offering_id)
# means a second call just returns the existing row's id, never a duplicate.
# Does NOT commit — the caller owns the transaction.
# ----------------------------------------------------------------------------
def ensure_expected_atr(cycle, offering_id, cycle_code):
    existing = get_atr_for_offering(cycle, offering_id)
    if existing is not None:
        return existing["id"]
    at = _now(cycle)
    cur = cycle.execute(
        "INSERT INTO atr (offering_id, cycle_code, state, current_owner_role, "
        "                 body, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, NULL, ?, ?)",
        (offering_id, cycle_code, STATE_EXPECTED, STATE_OWNER[STATE_EXPECTED],
         at, at),
    )
    return cur.lastrowid


# ----------------------------------------------------------------------------
# ensure_hod_filed_atr(cycle, offering_id, cycle_code) -> atr_id
# ----------------------------------------------------------------------------
# EXTERNAL-FACULTY variant of ensure_expected_atr (the professor's rule, Aug 2026).
# WHY THIS EXISTS: a course taught by the "External" placeholder faculty has NO
# real person behind it — no email, no unique staff id, no login — so the normal
# flow (EXPECTED, owner FACULTY, faculty files via a magic link) can never
# complete: there is nobody to click "File ATR". Instead, for these courses the
# HOD of the External department (EXT — Dr Arul Chezhian) writes the action note
# himself and submits it upward to the Vice Dean.
#
# We realise that by creating the ATR ALREADY in PENDING_HOD state (owner = HOD),
# skipping the EXPECTED/faculty stage entirely. From there the HOD's ordinary
# ENDORSE (carrying his note as the ATR `body`) moves it to PENDING_VD — exactly
# "HOD writes a note and submits to Vice Dean". No new FSM transition is needed:
# PENDING_HOD --ENDORSE(HOD)--> PENDING_VD already exists in the §8.2 table, so
# the whole safety guarantee (legal-move + correct-actor + audit row) is reused
# untouched. Like ensure_expected_atr, this writes NO audit event on creation
# (creation is not a transition); the first event will be the HOD's ENDORSE.
# Idempotent via UNIQUE(offering_id): a second call returns the existing id.
# Does NOT commit — the caller owns the transaction.
# ----------------------------------------------------------------------------
def ensure_hod_filed_atr(cycle, offering_id, cycle_code):
    existing = get_atr_for_offering(cycle, offering_id)
    if existing is not None:
        return existing["id"]                       # never duplicate an ATR row
    at = _now(cycle)                                 # one clock for created/updated
    cur = cycle.execute(
        "INSERT INTO atr (offering_id, cycle_code, state, current_owner_role, "
        "                 body, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, NULL, ?, ?)",
        # State starts at PENDING_HOD (owner HOD) — NOT EXPECTED — so it lands
        # directly in the HOD's action queue with no faculty step in between.
        (offering_id, cycle_code, STATE_PENDING_HOD, STATE_OWNER[STATE_PENDING_HOD],
         at, at),
    )
    return cur.lastrowid


# ----------------------------------------------------------------------------
# apply_transition(cycle, atr_id, action, actor_role, actor_user_id=None,
#                  comment=None, body=None) -> new_state
# ----------------------------------------------------------------------------
# THE public state-change entry point. In ONE transaction it:
#   1. reads the ATR's current state (404-safe: raises if the id is unknown);
#   2. asks the PURE transition() for the next state — which enforces both the
#      legal-move rule AND the correct-actor rule, raising IllegalTransition
#      otherwise (so an out-of-turn or shortcut move is rejected before any
#      write happens);
#   3. optionally updates the ATR body (a faculty SUBMIT carries the action-plan
#      text; leaders leave it unchanged);
#   4. writes the new state + derived current_owner_role + updated_at; and
#   5. appends the matching atr_event audit row.
# Steps 4 and 5 happen together or not at all, so state and audit never diverge.
# Does NOT commit — the caller (route or test) wraps it, so a whole multi-step
# flow can be one atomic unit if desired. Returns the new state.
# ----------------------------------------------------------------------------
def apply_transition(cycle, atr_id, action, actor_role,
                     actor_user_id=None, comment=None, body=None):
    row = get_atr(cycle, atr_id)
    if row is None:
        raise IllegalTransition("no ATR with id %r" % (atr_id,))

    current_state = row["state"]

    # The pure rule decides the next state (and enforces role + legality). If it
    # raises, we have written nothing — the ATR is left exactly as it was.
    next_state = transition(current_state, action, actor_role)

    at = _now(cycle)

    # Build the UPDATE. We always set state, owner and updated_at; we set body
    # only when the caller supplied one (a faculty SUBMIT), so a leader endorsing
    # can never accidentally blank the faculty's narrative.
    if body is not None:
        cycle.execute(
            "UPDATE atr SET state = ?, current_owner_role = ?, body = ?, "
            "               updated_at = ? WHERE id = ?",
            (next_state, STATE_OWNER.get(next_state), body, at, atr_id),
        )
    else:
        cycle.execute(
            "UPDATE atr SET state = ?, current_owner_role = ?, "
            "               updated_at = ? WHERE id = ?",
            (next_state, STATE_OWNER.get(next_state), at, atr_id),
        )

    # Every transition writes exactly one audit row — no exceptions (spec §8.2).
    _record_event(cycle, atr_id, actor_user_id, action, comment, at)

    return next_state


# ----------------------------------------------------------------------------
# record_reminder(cycle, atr_id, actor_user_id, comment=None) -> None
# ----------------------------------------------------------------------------
# The audit side of the HOD "Send reminder" action (spec §9). A reminder is NOT
# a state change — the ATR stays EXPECTED — so it does not go through
# apply_transition/transition (which would reject REMIND by design). Instead it
# writes a single atr_event(REMIND) row so the fact that the HOD chased the
# faculty, and when, is on the permanent record. The email itself is sent by
# notifications.py; this only logs it. Does NOT commit.
# ----------------------------------------------------------------------------
def record_reminder(cycle, atr_id, actor_user_id, comment=None):
    at = _now(cycle)
    _record_event(cycle, atr_id, actor_user_id, ACTION_REMIND, comment, at)


# ----------------------------------------------------------------------------
# cycle_atr_summary(cycle) -> dict — a small tally of this cycle's ATRs by state.
# ----------------------------------------------------------------------------
# Used by (a) the "Endorse all" flow and (b) the "is the whole cycle finished?"
# check that flips a cycle to RECORDED once the Dean has closed the last ATR.
#   total       : how many ATR rows exist for the cycle,
#   closed      : how many are CLOSED (Dean-endorsed, terminal),
#   all_closed  : True iff there is at least one ATR and EVERY one is CLOSED,
#   counts      : the full state -> count map (for a dashboard breakdown).
# Reads only the per-cycle atr table (identity-free); commits nothing.
# ----------------------------------------------------------------------------
def cycle_atr_summary(cycle):
    rows = cycle.execute(
        "SELECT state, COUNT(*) AS n FROM atr GROUP BY state").fetchall()
    counts = {r["state"]: r["n"] for r in rows}
    total = sum(counts.values())
    closed = counts.get(STATE_CLOSED, 0)
    return {"total": total, "closed": closed,
            "all_closed": (total > 0 and closed == total), "counts": counts}


# ----------------------------------------------------------------------------
# events_for(cycle, atr_id) -> list[Row] — the full, ordered audit trail for one
# ATR, for a review screen or an export. Oldest-first so it reads as a timeline.
# ----------------------------------------------------------------------------
def events_for(cycle, atr_id):
    return cycle.execute(
        "SELECT * FROM atr_event WHERE atr_id = ? ORDER BY id ASC",
        (atr_id,),
    ).fetchall()
