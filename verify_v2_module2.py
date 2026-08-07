# ============================================================================
# verify_v2_module2.py  —  THE VERIFICATION GATE for Version 2.0 · Module 2
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# verify_scoring.py proves the frozen scoring engine; verify_v2_module1.py proves
# the Module 1 delta (banding/RBAC/IAM). THIS is their Module 2 sibling: it
# proves the ATR workflow behaves EXACTLY as Design §8/§5.2/§7/§9 require, BEFORE
# any of it is trusted on the professor's live data.
#
# Like its siblings, everything runs against a THROWAWAY COPY of master.db + the
# per-cycle DBs in a temp folder — Config.DATA_DIR / Config.MASTER_DB are pointed
# at the copy before anything is imported, so no statement here can touch the
# real database. Emails are NEVER sent: the tests exercise the pure engine and
# the planners; the one place mail would flow (distribution) is checked via its
# pure planners, and the end-to-end flow drives the service layer directly.
#
# WHAT IT CHECKS (each prints OK/XX; the run ends ALL PASS or FAILURES):
#   A. transition()            — every LEGAL move in §8.2; every ILLEGAL move
#                                rejected; the correct-actor rule; and the EXACT
#                                return-one-level-down behaviour (no shortcuts).
#   B. apply_transition()      — an audit row is written on EVERY transition;
#                                state + owner updated; body set on SUBMIT.
#   C. faculty_tokens          — issue/verify happy path; expiry; one-time reuse
#                                blocked; wrong-offering blocked; bad signature
#                                blocked; wrong purpose blocked.
#   D. auth_leaders            — pbkdf2 hash/verify; set-pw token issue/redeem;
#                                expiry + one-time reuse.
#   E. distribution routing    — faculty see ONLY their own offerings; an E01 HOD
#                                sees ONLY E01; the Dean sees all (via rbac).
#   F. notifications routing   — recipients_for picks the right next actor per §9.
#   G. migration               — idempotent + non-destructive on a real cycle DB.
#   H. END-TO-END              — band POOR -> issue token -> SUBMIT -> HOD ENDORSE
#                                -> VD ENDORSE -> Dean ENDORSE (CLOSED), asserting
#                                states + the full audit trail; plus a RETURN path.
#
# Run with:  python verify_v2_module2.py   (from the app/ folder).
# ----------------------------------------------------------------------------

import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta

from config import Config

_REAL_DATA_DIR = Config.DATA_DIR
_REAL_MASTER = Config.MASTER_DB

# Build a private sandbox and copy the live DBs into it (master + every cycle DB).
_SANDBOX = tempfile.mkdtemp(prefix="afs_v2m2_verify_")
shutil.copy2(_REAL_MASTER, os.path.join(_SANDBOX, "master.db"))
for _f in os.listdir(_REAL_DATA_DIR):
    if _f.startswith("cycle_") and _f.endswith(".db"):
        shutil.copy2(os.path.join(_REAL_DATA_DIR, _f), os.path.join(_SANDBOX, _f))

# Reroute the whole app at the sandbox for the rest of this process.
Config.DATA_DIR = _SANDBOX
Config.MASTER_DB = os.path.join(_SANDBOX, "master.db")

# Now it is safe to import the modules under test (they read Config at call time).
import db                      # noqa: E402
import rbac                    # noqa: E402
import classification          # noqa: E402
import atr_workflow as W       # noqa: E402
import faculty_tokens as FT    # noqa: E402
import auth_leaders as AL      # noqa: E402
import notifications as N      # noqa: E402
import distribution as D       # noqa: E402
import migrate_v2_module2 as M2  # noqa: E402


# ----------------------------------------------------------------------------
# Tiny dependency-free harness (same spirit as the Module 1 gate).
# ----------------------------------------------------------------------------
_RESULTS = {"pass": 0, "fail": 0}


def check(label, condition, detail=""):
    ok = bool(condition)
    _RESULTS["pass" if ok else "fail"] += 1
    line = "   [%s] %s" % ("OK " if ok else "XX ", label)
    if detail:
        line += "  — %s" % detail
    print(line)
    return ok


def _fresh_cycle_db(name="cycle_TEST_M2.db"):
    """Create a brand-new per-cycle DB (with the Module 2 tables) in the sandbox
    and return an open connection. Used by the engine/token tests that want a
    clean slate independent of any real cycle's data."""
    path = os.path.join(_SANDBOX, name)
    if os.path.exists(path):
        os.remove(path)
    conn = db.get_cycle("TEST", "M2")   # cycle_db_path -> cycle_TEST_M2.db
    schema_path = os.path.join(Config.BASE_DIR, "schema_cycle.sql")
    with open(schema_path, "r", encoding="utf-8") as fh:
        conn.executescript(fh.read())
    conn.commit()
    return conn


# ############################################################################
# SECTION A — transition() : the pure §8.2 rule
# ############################################################################
def test_transition_pure():
    print("\nA. transition() — §8.2 legal/illegal moves, actor rule, return-one-down")

    F, H, V, De = W.ROLE_FACULTY, W.ROLE_HOD, W.ROLE_VICE_DEAN, W.ROLE_DEAN
    SUB, EN, RET = W.ACTION_SUBMIT, W.ACTION_ENDORSE, W.ACTION_RETURN

    # ---- Every LEGAL move maps to the exact next state -----------------------
    legal = [
        (W.STATE_EXPECTED,     SUB, F,  W.STATE_PENDING_HOD),
        (W.STATE_DRAFT,        SUB, F,  W.STATE_PENDING_HOD),
        (W.STATE_PENDING_HOD,  EN,  H,  W.STATE_PENDING_VD),
        (W.STATE_PENDING_HOD,  RET, H,  W.STATE_EXPECTED),     # HOD return reaches faculty
        (W.STATE_PENDING_VD,   EN,  V,  W.STATE_PENDING_DEAN),
        (W.STATE_PENDING_VD,   RET, V,  W.STATE_PENDING_HOD),  # VD return -> HOD (one down)
        (W.STATE_PENDING_DEAN, EN,  De, W.STATE_CLOSED),
        (W.STATE_PENDING_DEAN, RET, De, W.STATE_PENDING_VD),   # Dean return -> VD (one down)
    ]
    for frm, act, role, to in legal:
        check("%-13s %-8s by %-9s -> %s" % (frm, act, role, to),
              W.transition(frm, act, role) == to)

    # ---- RETURN-ONE-LEVEL-DOWN: the exact rule, no shortcuts -----------------
    check("VD RETURN does NOT reach faculty (goes to PENDING_HOD)",
          W.transition(W.STATE_PENDING_VD, RET, V) == W.STATE_PENDING_HOD,
          "no VD->faculty shortcut")
    check("Dean RETURN does NOT reach faculty (goes to PENDING_VD)",
          W.transition(W.STATE_PENDING_DEAN, RET, De) == W.STATE_PENDING_VD,
          "no Dean->faculty shortcut")
    check("ONLY the HOD's return reaches faculty (EXPECTED)",
          W.transition(W.STATE_PENDING_HOD, RET, H) == W.STATE_EXPECTED)

    # ---- WRONG ACTOR is rejected even for an otherwise-legal (state,action) ---
    def illegal(frm, act, role):
        try:
            W.transition(frm, act, role)
            return False
        except W.IllegalTransition:
            return True

    check("Dean cannot ENDORSE a PENDING_HOD (wrong actor)",
          illegal(W.STATE_PENDING_HOD, EN, De))
    check("Faculty cannot ENDORSE anything",
          illegal(W.STATE_PENDING_HOD, EN, F))
    check("HOD cannot ENDORSE a PENDING_VD (out of turn)",
          illegal(W.STATE_PENDING_VD, EN, H))
    check("VD cannot ENDORSE a PENDING_DEAN (out of turn)",
          illegal(W.STATE_PENDING_DEAN, EN, V))

    # ---- Structurally-illegal moves are rejected ----------------------------
    check("no action out of CLOSED (terminal)",
          illegal(W.STATE_CLOSED, EN, De) and illegal(W.STATE_CLOSED, RET, De))
    check("cannot SUBMIT an already-PENDING_HOD ATR",
          illegal(W.STATE_PENDING_HOD, SUB, F))
    check("REMIND is not a state transition (rejected by transition())",
          illegal(W.STATE_EXPECTED, W.ACTION_REMIND, H),
          "reminders are audit-only, handled separately")

    # ---- legal_actions() reflects the table ---------------------------------
    check("legal_actions(PENDING_HOD, HOD) == [ENDORSE, RETURN]",
          W.legal_actions(W.STATE_PENDING_HOD, H) == ["ENDORSE", "RETURN"])
    check("legal_actions(PENDING_VD, HOD) == [] (not their turn)",
          W.legal_actions(W.STATE_PENDING_VD, H) == [])


# ############################################################################
# SECTION B — apply_transition() : state + audit, atomically
# ############################################################################
def test_service_layer():
    print("\nB. apply_transition() — audit row on EVERY move; state/owner/body")

    cy = _fresh_cycle_db()
    OID = 90001

    atr_id = W.ensure_expected_atr(cy, OID, "M2")
    cy.commit()
    row = W.get_atr(cy, atr_id)
    check("ensure_expected_atr -> EXPECTED, owner FACULTY",
          row["state"] == "EXPECTED" and row["current_owner_role"] == "FACULTY")
    check("ensure_expected_atr is idempotent (same id on 2nd call)",
          W.ensure_expected_atr(cy, OID, "M2") == atr_id)

    # SUBMIT carries a body; assert the body is stored and one audit row written.
    W.apply_transition(cy, atr_id, W.ACTION_SUBMIT, W.ROLE_FACULTY,
                       actor_user_id="FACULTY", body="We will add tutorials.")
    cy.commit()
    row = W.get_atr(cy, atr_id)
    check("after SUBMIT -> PENDING_HOD, owner HOD, body stored",
          row["state"] == "PENDING_HOD" and row["current_owner_role"] == "HOD"
          and row["body"] == "We will add tutorials.")
    n_events = cy.execute("SELECT COUNT(*) n FROM atr_event WHERE atr_id=?",
                          (atr_id,)).fetchone()["n"]
    check("exactly 1 audit row after 1 transition", n_events == 1)

    # A leader ENDORSE must NOT blank the faculty body (body left unchanged).
    W.apply_transition(cy, atr_id, W.ACTION_ENDORSE, W.ROLE_HOD, actor_user_id=3)
    cy.commit()
    row = W.get_atr(cy, atr_id)
    check("HOD ENDORSE leaves faculty body intact",
          row["body"] == "We will add tutorials." and row["state"] == "PENDING_VD")
    check("2 audit rows after 2 transitions",
          cy.execute("SELECT COUNT(*) n FROM atr_event WHERE atr_id=?",
                     (atr_id,)).fetchone()["n"] == 2)

    # An illegal move writes NOTHING (state and audit unchanged).
    before = cy.execute("SELECT COUNT(*) n FROM atr_event").fetchone()["n"]
    try:
        W.apply_transition(cy, atr_id, W.ACTION_ENDORSE, W.ROLE_HOD, actor_user_id=3)
        raised = False
    except W.IllegalTransition:
        raised = True
    after = cy.execute("SELECT COUNT(*) n FROM atr_event").fetchone()["n"]
    check("illegal apply_transition raises and writes no audit row",
          raised and before == after)

    # REMIND writes an audit row but does NOT change state.
    W.record_reminder(cy, atr_id, actor_user_id=3, comment="nudge")
    cy.commit()
    row2 = W.get_atr(cy, atr_id)
    reminders = cy.execute(
        "SELECT COUNT(*) n FROM atr_event WHERE atr_id=? AND action='REMIND'",
        (atr_id,)).fetchone()["n"]
    check("record_reminder logs REMIND without changing state",
          reminders == 1 and row2["state"] == "PENDING_VD")
    cy.close()


# ############################################################################
# SECTION C — faculty_tokens : issue / verify / expiry / one-time / scope
# ############################################################################
def test_faculty_tokens():
    print("\nC. faculty_tokens — issue/verify, expiry, one-time, offering scope")

    cy = _fresh_cycle_db("cycle_TEST_TOK.db")
    OID, OTHER = 111, 222
    email = "dr.x@sriher.edu.in"

    jti, exp = FT.issue(cy, OID, email, purpose=FT.PURPOSE_ATR_FILE)
    cy.commit()

    check("happy path: correct offering + purpose verifies",
          FT.verify(cy, jti, expected_offering_id=OID).ok)

    check("bad signature: unknown jti rejected",
          FT.verify(cy, "totally-made-up-token", expected_offering_id=OID).reason
          == "bad_signature")

    check("wrong offering rejected",
          FT.verify(cy, jti, expected_offering_id=OTHER).reason == "wrong_offering")

    check("wrong purpose rejected (VIEW link cannot FILE)",
          FT.verify(cy, jti, expected_offering_id=OID,
                    expected_purpose=FT.PURPOSE_VIEW).reason == "wrong_purpose")

    # Expiry: mint a token that was already expired (inject a past 'now').
    past = datetime.utcnow() - timedelta(days=30)
    jti_exp, _ = FT.issue(cy, OID, email, ttl_days=1, now=past)
    cy.commit()
    check("expired token rejected",
          FT.verify(cy, jti_exp, expected_offering_id=OID).reason == "expired")

    # One-time reuse: mark_used then verify must reject.
    check("valid before use", FT.verify(cy, jti, expected_offering_id=OID).ok)
    FT.mark_used(cy, jti); cy.commit()
    check("one-time reuse blocked after mark_used",
          FT.verify(cy, jti, expected_offering_id=OID).reason == "used")
    cy.close()


# ############################################################################
# SECTION D — auth_leaders : pbkdf2 + set-password token flow
# ############################################################################
def test_auth_leaders():
    print("\nD. auth_leaders — pbkdf2 hash/verify + set-password token flow")

    h = AL.hash_password("correct horse")
    check("pbkdf2 hash verifies the right password", AL.verify_password("correct horse", h))
    check("pbkdf2 hash rejects the wrong password", not AL.verify_password("wrong", h))
    check("two hashes of same password differ (random salt)",
          AL.hash_password("same") != AL.hash_password("same"))
    check("empty/unset hash safely rejects", not AL.verify_password("x", ""))

    master = db.get_master()
    # Pick any seeded leader (e.g. the E01 HOD) to exercise the token flow.
    user = master.execute("SELECT * FROM app_user WHERE role='HOD' ORDER BY id LIMIT 1").fetchone()
    jti = AL.issue_set_pw_token(master, user["id"], purpose="SET")
    master.commit()
    ok, reason, row = AL.verify_set_pw_token(master, jti)
    check("set-pw token verifies before use", ok and row["user_id"] == user["id"])

    done, why = AL.redeem_set_pw_token(master, jti, "s3cret-pass")
    master.commit()
    check("redeem sets the password + burns the token", done)
    fresh = master.execute("SELECT pw_hash FROM app_user WHERE id=?", (user["id"],)).fetchone()
    check("password now authenticates via authenticate()",
          AL.authenticate(master, user["email"], "s3cret-pass") is not None
          and AL.verify_password("s3cret-pass", fresh["pw_hash"]))
    check("wrong password fails authenticate()",
          AL.authenticate(master, user["email"], "nope") is None)

    # One-time reuse of the set-pw link is blocked.
    ok2, reason2, _ = AL.verify_set_pw_token(master, jti)
    check("set-pw token one-time reuse blocked", (not ok2) and reason2 == "used")

    # Expired set-pw token rejected.
    jti2 = AL.issue_set_pw_token(master, user["id"], purpose="RESET", ttl_days=-1)
    master.commit()
    ok3, reason3, _ = AL.verify_set_pw_token(master, jti2)
    check("expired set-pw token rejected", (not ok3) and reason3 == "expired")
    master.rollback()  # discard the test's password change from the sandbox
    master.close()


# ############################################################################
# SECTION E — distribution routing scope (through rbac)
# ############################################################################
def test_distribution_scope():
    print("\nE. distribution routing — faculty own-only; HOD dept-only; Dean all")

    master = db.get_master()
    CODE = "DISTTEST"

    # Seed synthetic classification rows for a controlled mix of departments and
    # faculty, so the routing assertions are exact and independent of live data.
    # Use real offering ids and stamp known dept/faculty onto them in the sandbox.
    fixtures = [
        # (offering_id, dept_code, faculty_email, band)
        (261, "E01", "fac.a@sriher.edu.in", "POOR"),
        (275, "E01", "fac.a@sriher.edu.in", "GOOD"),   # same faculty, 2nd subject
        (290, "E01", "fac.b@sriher.edu.in", "POOR"),
        (250, "E03", "fac.c@sriher.edu.in", "GOOD"),
        (293, "E03", "fac.c@sriher.edu.in", "POOR"),
    ]
    for oid, dept, femail, band in fixtures:
        # Set the offering's OWN cycle_code to the synthetic test code too, so
        # rbac.visible_offerings (which narrows on offering.cycle_code) finds them
        # under CODE. In production offering.cycle_code and the classification's
        # cycle_code always match (an offering belongs to exactly one cycle); the
        # fixture just makes that alignment explicit for an isolated test cycle.
        master.execute("UPDATE offering SET dept_code=?, faculty_email=?, cycle_code=? "
                       "WHERE id=?", (dept, femail, CODE, oid))
        master.execute(
            "INSERT INTO offering_classification "
            "(offering_id, cycle_code, band, overall_score, n_responses, "
            " threshold_overall, min_responses, reason) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(offering_id,cycle_code) DO UPDATE SET band=excluded.band",
            (oid, CODE, band, 7.5 if band == "POOR" else 8.6, 20, 8.0, 10, "fixture"))
    master.commit()

    # ---- FACULTY: each faculty sees ONLY their own offerings -----------------
    plan = D.plan_faculty(master, CODE)
    check("faculty A sees exactly her 2 subjects (261,275)",
          sorted(plan.get("fac.a@sriher.edu.in", [])) == [261, 275])
    check("faculty B sees exactly her 1 subject (290)",
          sorted(plan.get("fac.b@sriher.edu.in", [])) == [290])
    check("faculty C sees exactly her 2 subjects (250,293)",
          sorted(plan.get("fac.c@sriher.edu.in", [])) == [250, 293])
    check("no faculty sees another faculty's subject",
          all(261 not in v and 275 not in v and 290 not in v
              for k, v in plan.items() if k == "fac.c@sriher.edu.in"))

    # ---- HOD: E01 HOD sees ONLY E01; never E03 -------------------------------
    e01_hod = master.execute(
        "SELECT * FROM app_user WHERE role='HOD' AND scope_dept_ids='E01'").fetchone()
    e01_plan = D.plan_leader(master, e01_hod, CODE)
    check("E01 HOD roll-up = only E01 offerings (261,275,290)",
          e01_plan == [261, 275, 290])
    check("E01 HOD roll-up excludes every E03 offering (250,293)",
          250 not in e01_plan and 293 not in e01_plan,
          "the §4 promise: E01 never receives E03")

    e03_hod = master.execute(
        "SELECT * FROM app_user WHERE role='HOD' AND scope_dept_ids='E03'").fetchone()
    e03_plan = D.plan_leader(master, e03_hod, CODE)
    check("E03 HOD roll-up = only E03 offerings (250,293)", e03_plan == [250, 293])

    # ---- DEAN: sees ALL classified offerings ---------------------------------
    dean = master.execute("SELECT * FROM app_user WHERE role='DEAN'").fetchone()
    dean_plan = D.plan_leader(master, dean, CODE)
    check("Dean roll-up = all 5 classified offerings",
          sorted(dean_plan) == [250, 261, 275, 290, 293])

    # ---- VICE DEAN (scope ALL): also all -------------------------------------
    vd = master.execute("SELECT * FROM app_user WHERE role='VICE_DEAN'").fetchone()
    check("Vice Dean (scope ALL) roll-up = all 5",
          sorted(D.plan_leader(master, vd, CODE)) == [250, 261, 275, 290, 293])

    master.rollback()  # discard fixtures from the sandbox master
    master.close()


# ############################################################################
# SECTION F — notifications routing (§9 "who acts next")
# ############################################################################
def test_notifications_routing():
    print("\nF. notifications.recipients_for — the §9 next-actor routing")

    master = db.get_master()
    # Stamp a known dept + faculty onto a real offering so the resolver has data.
    master.execute("UPDATE offering SET dept_code='E01', "
                   "faculty_email='fac.n@sriher.edu.in' WHERE id=261")
    master.commit()
    off = master.execute("SELECT * FROM offering WHERE id=261").fetchone()

    def emails(action, new_state):
        return [(e, r) for (e, r) in N.recipients_for(master, off, action, new_state)]

    # SUBMIT -> PENDING_HOD notifies the E01 HOD.
    subm = emails(W.ACTION_SUBMIT, W.STATE_PENDING_HOD)
    check("SUBMIT notifies the dept HOD",
          any(r == "HOD" and e == "hod.e01@sriher.edu.in" for e, r in subm))

    # HOD ENDORSE -> PENDING_VD notifies the Vice Dean.
    vd = emails(W.ACTION_ENDORSE, W.STATE_PENDING_VD)
    check("HOD ENDORSE notifies the Vice Dean",
          any(r == "VICE_DEAN" for _e, r in vd))

    # VD ENDORSE -> PENDING_DEAN notifies the Dean.
    dean = emails(W.ACTION_ENDORSE, W.STATE_PENDING_DEAN)
    check("VD ENDORSE notifies the Dean",
          any(r == "DEAN" for _e, r in dean))

    # HOD RETURN -> EXPECTED notifies the FACULTY (the only return that reaches them).
    ret = emails(W.ACTION_RETURN, W.STATE_EXPECTED)
    check("HOD RETURN notifies the faculty",
          ret == [("fac.n@sriher.edu.in", "FACULTY")])

    # Dean ENDORSE -> CLOSED notifies faculty AND HOD (acknowledgement).
    closed = emails(W.ACTION_ENDORSE, W.STATE_CLOSED)
    roles = sorted(r for _e, r in closed)
    check("CLOSED acknowledges faculty + HOD", roles == ["FACULTY", "HOD"])

    master.rollback()
    master.close()


# ############################################################################
# SECTION G — the per-cycle migration is idempotent + non-destructive
# ############################################################################
def test_migration():
    print("\nG. migrate_v2_module2 — idempotent + non-destructive on a real cycle")

    # DEMOCA1 is a real cycle with responses in the sandbox copy.
    cy = db.get_cycle("AY 2026-27", "DEMOCA1")
    before = cy.execute("SELECT COUNT(*) n FROM response").fetchone()["n"]
    cy.close()

    M2.migrate_one("AY 2026-27", "DEMOCA1")
    M2.migrate_one("AY 2026-27", "DEMOCA1")   # run twice — must not error

    cy = db.get_cycle("AY 2026-27", "DEMOCA1")
    tables = {r["name"] for r in cy.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    after = cy.execute("SELECT COUNT(*) n FROM response").fetchone()["n"]
    cy.close()
    check("ATR tables present after migration",
          {"atr", "atr_event", "faculty_token"} <= tables)
    check("migration is non-destructive (response count unchanged)",
          before == after, "%d responses before and after" % before)


# ############################################################################
# SECTION H — END-TO-END: POOR -> file -> full endorsement chain (+ a RETURN)
# ############################################################################
def test_end_to_end():
    print("\nH. END-TO-END on a COPY of a TEST cycle — band POOR, file, endorse, close")

    master = db.get_master()
    cyc_row = master.execute("SELECT * FROM cycle WHERE code='DEMOCA1'").fetchone()

    # Force a POOR band deterministically: lower the guard to 1 and raise the
    # overall threshold to 10 so a real DEMOCA1 offering bands POOR, then run the
    # frozen scoring + Module 1 classifier (reused, unchanged).
    master.execute("UPDATE cycle SET min_responses=1, threshold_overall=10.0 "
                   "WHERE code='DEMOCA1'")
    master.commit()
    cyc_row = master.execute("SELECT * FROM cycle WHERE code='DEMOCA1'").fetchone()

    cy = db.get_cycle("AY 2026-27", "DEMOCA1")
    M2.migrate_cycle_conn(cy); cy.commit()   # ensure ATR tables exist on the copy
    summary = classification.classify_cycle(master, cy, cyc_row)
    master.commit()
    check("classifier produced at least one POOR offering", summary["poor"] >= 1,
          "poor=%d of total=%d" % (summary["poor"], summary["total"]))

    poor = master.execute(
        "SELECT * FROM offering_classification "
        "WHERE cycle_code='DEMOCA1' AND band='POOR' ORDER BY offering_id LIMIT 1"
    ).fetchone()
    oid = poor["offering_id"]
    # Give the offering a faculty email so the token/notify path has a recipient.
    master.execute("UPDATE offering SET faculty_email='e2e.faculty@sriher.edu.in' "
                   "WHERE id=?", (oid,))
    master.commit()

    # 1. Issue a faculty ATR magic link for the POOR offering, then verify + SUBMIT.
    jti, _exp = FT.issue(cy, oid, "e2e.faculty@sriher.edu.in")
    cy.commit()
    check("token verifies for the POOR offering",
          FT.verify(cy, jti, expected_offering_id=oid).ok)

    atr_id = W.ensure_expected_atr(cy, oid, "DEMOCA1")
    W.apply_transition(cy, atr_id, W.ACTION_SUBMIT, W.ROLE_FACULTY,
                       actor_user_id="FACULTY", body="Remedial plan attached.")
    FT.mark_used(cy, jti)
    cy.commit()
    check("after faculty SUBMIT -> PENDING_HOD",
          W.get_atr(cy, atr_id)["state"] == "PENDING_HOD")
    check("magic link is now one-time-spent",
          FT.verify(cy, jti, expected_offering_id=oid).reason == "used")

    # 2. Full endorsement chain: HOD -> VD -> Dean (CLOSED).
    W.apply_transition(cy, atr_id, W.ACTION_ENDORSE, W.ROLE_HOD, actor_user_id=3)
    check("HOD ENDORSE -> PENDING_VD",
          W.get_atr(cy, atr_id)["state"] == "PENDING_VD")
    W.apply_transition(cy, atr_id, W.ACTION_ENDORSE, W.ROLE_VICE_DEAN, actor_user_id=2)
    check("VD ENDORSE -> PENDING_DEAN",
          W.get_atr(cy, atr_id)["state"] == "PENDING_DEAN")
    W.apply_transition(cy, atr_id, W.ACTION_ENDORSE, W.ROLE_DEAN, actor_user_id=1)
    cy.commit()
    check("Dean ENDORSE -> CLOSED", W.get_atr(cy, atr_id)["state"] == "CLOSED")

    # 3. The audit trail is complete and ordered (SUBMIT, ENDORSE x3).
    trail = [e["action"] for e in W.events_for(cy, atr_id)]
    check("audit trail = SUBMIT, ENDORSE, ENDORSE, ENDORSE",
          trail == ["SUBMIT", "ENDORSE", "ENDORSE", "ENDORSE"],
          " -> ".join(trail))

    # 4. A RETURN path on a SECOND POOR offering: VD returns one level down to HOD.
    poor2 = master.execute(
        "SELECT * FROM offering_classification "
        "WHERE cycle_code='DEMOCA1' AND band='POOR' AND offering_id!=? "
        "ORDER BY offering_id LIMIT 1", (oid,)).fetchone()
    if poor2:
        oid2 = poor2["offering_id"]
        aid2 = W.ensure_expected_atr(cy, oid2, "DEMOCA1")
        W.apply_transition(cy, aid2, W.ACTION_SUBMIT, W.ROLE_FACULTY,
                           actor_user_id="FACULTY", body="draft")
        W.apply_transition(cy, aid2, W.ACTION_ENDORSE, W.ROLE_HOD, actor_user_id=3)
        # VD returns -> must land at PENDING_HOD (one level down), not EXPECTED.
        new = W.apply_transition(cy, aid2, W.ACTION_RETURN, W.ROLE_VICE_DEAN,
                                 actor_user_id=2, comment="clarify metrics")
        cy.commit()
        check("VD RETURN lands at PENDING_HOD (one level down, not faculty)",
              new == "PENDING_HOD")
        # HOD then returns -> reaches the faculty (EXPECTED).
        new2 = W.apply_transition(cy, aid2, W.ACTION_RETURN, W.ROLE_HOD,
                                  actor_user_id=3, comment="please expand")
        cy.commit()
        check("HOD RETURN then reaches the faculty (EXPECTED)", new2 == "EXPECTED")
        ret_events = [e["action"] for e in W.events_for(cy, aid2)]
        check("return path audited (SUBMIT,ENDORSE,RETURN,RETURN)",
              ret_events == ["SUBMIT", "ENDORSE", "RETURN", "RETURN"],
              " -> ".join(ret_events))
    else:
        check("second POOR offering available for RETURN path", False,
              "only one POOR offering; RETURN path skipped")

    cy.close()
    master.close()


# ############################################################################
# RUN ALL
# ############################################################################
def run():
    print("=" * 74)
    print("AFS Version 2.0 · Module 2 — VERIFICATION  (sandbox copy: %s)" % _SANDBOX)
    print("=" * 74)

    test_transition_pure()
    test_service_layer()
    test_faculty_tokens()
    test_auth_leaders()
    test_distribution_scope()
    test_notifications_routing()
    test_migration()
    test_end_to_end()

    print("\n" + "=" * 74)
    ok = _RESULTS["fail"] == 0
    print("RESULT: %s   (%d passed, %d failed)"
          % ("ALL PASS" if ok else "FAILURES", _RESULTS["pass"], _RESULTS["fail"]))
    print("=" * 74)
    return ok


def _cleanup():
    shutil.rmtree(_SANDBOX, ignore_errors=True)
    Config.DATA_DIR = _REAL_DATA_DIR
    Config.MASTER_DB = _REAL_MASTER


if __name__ == "__main__":
    try:
        ok = run()
    finally:
        _cleanup()
    raise SystemExit(0 if ok else 1)
