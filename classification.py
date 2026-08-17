# ============================================================================
# classification.py  —  Version 2.0 · §6 : the GOOD/POOR banding step
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# Version 1.0's scoring engine (scoring.py) turns raw anonymous answers into a
# single /10 overall score per offering, and is FROZEN — proven exact by
# verify_scoring.py and never touched by anything in Version 2.0. This module is
# the NEXT step, and a deliberately SEPARATE one (Design §6):
#
#         scoring.py            classification.py            (Module 2)
#     raw answers ─► /10 score ─► GOOD / POOR band ─► ATR workflow trigger
#
# Banding is kept OUT of scoring on purpose. A faculty member may dispute *why*
# they were asked to file an ATR, so the rule that flags them must be a
# transparent, auditable threshold — never buried inside the numeric engine, and
# never ML. Splitting it here means the score stays a pure fact and the verdict
# stays a pure, testable policy on top of that fact.
#
# TWO PARTS:
#   1. band(...)          — a PURE function: numbers in, a verdict out. No DB, no
#                           I/O, so it is trivially unit-testable (and it is, in
#                           verify_v2_module1.py). This is the whole §6 rule.
#   2. classify_cycle(...) — the RUNNER: reads a cycle's already-scored offerings,
#                           calls band() on each, and records the verdict in the
#                           offering_classification table. band == 'POOR' is the
#                           single trigger Module 2 keys the ATR workflow off.
#
# ANONYMITY BOUNDARY (unchanged, Design §3.2): a band attaches to an OFFERING
# (a course + faculty), never to a student or a response row. We read aggregate
# scores only. The two-file split is untouched — we ask scoring.py for the number
# and it, not us, is the only thing that opens the per-cycle answer file.
# ----------------------------------------------------------------------------

import scoring        # the frozen Version 1.0 engine — we consume its output, never edit it
import consolidation  # Aug 2026: groups an elective's per-programme offerings into ONE delivery


# Verdict constants — used everywhere instead of bare strings so a typo becomes
# an ImportError, not a silently-miswritten band. NULL/None is the deliberate
# third outcome: "scored, but too few responses to judge fairly" (the guard).
BAND_GOOD = "GOOD"
BAND_POOR = "POOR"


# ----------------------------------------------------------------------------
# band(overall_score, n_responses, threshold_overall, threshold_section,
#      section_scores, min_responses)  ->  'GOOD' | 'POOR' | None
# ----------------------------------------------------------------------------
# THE §6 RULE, verbatim, as a pure function:
#
#     GUARD : only band when  n_responses >= min_responses
#             (too few responses → return None : do NOT force an ATR on noise)
#     POOR  : overall_score < threshold_overall
#             OR (threshold_section is set AND any section < threshold_section)
#     GOOD  : otherwise
#
# PARAMETERS
#   overall_score     the /10 overall from scoring.compute_scores()['overall'].
#   n_responses       how many forms were submitted for this offering.
#   threshold_overall the cycle's overall cut (e.g. 8.0 for CA1) — REQUIRED.
#   threshold_section the cycle's OPTIONAL critical-section cut. None = the whole
#                     section rule is OFF for this cycle (the common case now,
#                     since "which sections are critical" is left open in §14.1;
#                     when a value is given we apply it to EVERY section, i.e.
#                     "no section may fall below this floor").
#   section_scores    an iterable of the per-section /10 scores. Accepts either
#                     plain numbers (e.g. [8.4, 7.9, 9.0]) OR the dicts scoring.py
#                     emits (each {'key','title','score',...}); we normalise both,
#                     so the runner can hand us result['section_scores'] directly.
#   min_responses     the cycle's tiny-sample guard (e.g. 10).
#
# RETURNS the string 'GOOD' or 'POOR', or None when the guard trips. Returning a
# distinct None (rather than defaulting to GOOD) matters: "we couldn't judge"
# and "we judged it fine" are different facts the dashboards and Module 2 must
# tell apart.
# ----------------------------------------------------------------------------
def band(overall_score, n_responses, threshold_overall, threshold_section,
         section_scores, min_responses):
    # ----- GUARD first: not enough responses → no verdict (Design §6) --------
    # Compare with a safe default of 0 for min_responses if somehow None, so a
    # mis-seeded cycle can never crash the whole classification run here.
    guard = min_responses if min_responses is not None else 0
    if n_responses is None or n_responses < guard:
        return None

    # ----- Normalise section_scores to a list of plain floats ----------------
    # The runner passes scoring.py's list of section dicts; a unit test may pass
    # bare numbers. Accept both so band() is convenient AND self-contained.
    section_values = []
    for s in (section_scores or []):
        if isinstance(s, dict):
            v = s.get("score")
        else:
            v = s
        if v is not None:
            section_values.append(v)

    # ----- POOR test 1: overall below the cycle's overall threshold ----------
    if overall_score is not None and overall_score < threshold_overall:
        return BAND_POOR

    # ----- POOR test 2 (optional): ANY section below the critical floor -------
    # Only runs when the cycle actually set threshold_section (else it stays off,
    # exactly as §6 describes — "optional, per cycle").
    if threshold_section is not None:
        for v in section_values:
            if v < threshold_section:
                return BAND_POOR

    # ----- Neither POOR test tripped, and the guard passed → GOOD ------------
    return BAND_GOOD


# ----------------------------------------------------------------------------
# _reason(band_value, overall_score, n_responses, threshold_overall,
#         threshold_section, section_values)  ->  str
# ----------------------------------------------------------------------------
# Build the short human-readable justification we store alongside each verdict
# (offering_classification.reason). This is the audit sentence a faculty member
# is owed under §6 — "why was I flagged?" — and it also makes the test output
# and any future dashboard self-explaining. Pure string work, no side effects.
# ----------------------------------------------------------------------------
def _reason(band_value, overall_score, n_responses, threshold_overall,
            threshold_section, section_values):
    if band_value is None:
        return ("insufficient responses (n=%s < min_responses)" % n_responses)
    if band_value == BAND_POOR:
        # Say precisely which test tripped, overall first (it is checked first).
        if overall_score is not None and overall_score < threshold_overall:
            return ("overall %.2f < threshold_overall %.2f"
                    % (overall_score, threshold_overall))
        # Otherwise it was a section falling below the critical floor.
        low = [v for v in section_values
               if threshold_section is not None and v < threshold_section]
        return ("section score(s) %s < threshold_section %.2f"
                % (", ".join("%.2f" % v for v in low), threshold_section))
    # GOOD
    return ("overall %.2f >= threshold_overall %.2f (n=%s)"
            % (overall_score, threshold_overall, n_responses))


# ----------------------------------------------------------------------------
# record_band(master, offering_id, cycle_code, band_value, overall_score,
#             n_responses, threshold_overall, threshold_section, min_responses,
#             reason)
# ----------------------------------------------------------------------------
# UPSERT one verdict into offering_classification. The table's UNIQUE(offering_id,
# cycle_code) key means re-running the classifier for a cycle overwrites the
# previous verdict for each offering instead of piling up duplicates — so the
# professor can tweak a cycle's thresholds and re-run freely (the app's
# "editable, re-runnable" style). Uses ON CONFLICT ... DO UPDATE, the SQLite
# upsert, keyed on that unique pair. Commit is left to the caller so the whole
# cycle's classification lands as one transaction.
# ----------------------------------------------------------------------------
def record_band(master, offering_id, cycle_code, band_value, overall_score,
                n_responses, threshold_overall, threshold_section,
                min_responses, reason):
    master.execute(
        """
        INSERT INTO offering_classification
            (offering_id, cycle_code, band, overall_score, n_responses,
             threshold_overall, threshold_section, min_responses, reason,
             classified_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(offering_id, cycle_code) DO UPDATE SET
            band              = excluded.band,
            overall_score     = excluded.overall_score,
            n_responses       = excluded.n_responses,
            threshold_overall = excluded.threshold_overall,
            threshold_section = excluded.threshold_section,
            min_responses     = excluded.min_responses,
            reason            = excluded.reason,
            classified_at     = excluded.classified_at
        """,
        (offering_id, cycle_code, band_value, overall_score, n_responses,
         threshold_overall, threshold_section, min_responses, reason),
    )


# ----------------------------------------------------------------------------
# _offering_ids_with_responses(cycle) -> list[int]
# ----------------------------------------------------------------------------
# The offerings we can actually judge are exactly those with at least one
# submitted response in this cycle (mirrors admin/reports.py's own selection).
# An offering with zero responses is trivially "insufficient" and simply gets no
# row here — it is not scored, and n=0 < min_responses would band it None anyway.
# Reads Group B (the per-cycle answer file) only, so the anonymity boundary holds.
# ----------------------------------------------------------------------------
def _offering_ids_with_responses(cycle):
    rows = cycle.execute(
        "SELECT DISTINCT offering_id FROM response ORDER BY offering_id"
    ).fetchall()
    return [r["offering_id"] for r in rows]


# ----------------------------------------------------------------------------
# classify_cycle(master, cycle, cycle_row, discussed_late_weight=None)
# ----------------------------------------------------------------------------
# THE RUNNER. For one cycle:
#   1. read the cycle's classification thresholds off cycle_row (Design §6 —
#      they live on the cycle row: threshold_overall / _section / min_responses);
#   2. for every offering that has responses, ask the FROZEN scoring engine for
#      its overall + section scores (scoring.score_offering — unchanged);
#   3. call the pure band() with those numbers + the cycle's thresholds;
#   4. upsert the verdict (with its audit reason) into offering_classification.
#
# It does NOT commit — the caller wraps the run in one transaction so a cycle is
# classified all-or-nothing. Returns a summary dict {good, poor, insufficient,
# skipped, total} so the admin screen / a scheduled job can report the outcome.
#
# `discussed_late_weight` is the one configurable scoring input (scoring.py owns
# it); we read it once via scoring.get_discussed_late_weight() when not supplied,
# so banding uses the exact same score the reports show.
# ----------------------------------------------------------------------------
def classify_cycle(master, cycle, cycle_row, discussed_late_weight=None):
    cycle_code = cycle_row["code"]

    # Thresholds for THIS cycle. Use dict-style access with fallbacks so the
    # runner still works if handed a pre-migration cycle row in a test.
    threshold_overall = _row_get(cycle_row, "threshold_overall", 7.5)
    threshold_section = _row_get(cycle_row, "threshold_section", None)
    min_responses = _row_get(cycle_row, "min_responses", 0)

    # Resolve the configurable scoring weight once (single source of truth).
    if discussed_late_weight is None:
        discussed_late_weight = scoring.get_discussed_late_weight(master)

    summary = {"good": 0, "poor": 0, "insufficient": 0, "skipped": 0, "total": 0}

    # ---- DELIVERY-AWARE banding (Aug 2026 elective-consolidation change) ------
    # We no longer band each offering row on its own. Instead we group the cycle's
    # responded offerings into DELIVERIES (consolidation.py): a CORE/LAB/PROJECT
    # offering is its own delivery (unchanged), while an ELECTIVE's several
    # per-programme rows collapse into ONE delivery whose responses are pooled. We
    # then band each delivery ONCE, from the pooled score, and record that single
    # verdict against the delivery's ANCHOR offering (its smallest offering_id).
    # This is what makes "one class → one band → one ATR" true for a shared
    # elective; a POOR band on the anchor is the single ATR trigger for the whole
    # delivery. The non-anchor member offerings deliberately get NO classification
    # row — they are represented by the anchor everywhere downstream.
    groups = consolidation.deliveries_with_responses(master, cycle, cycle_code)

    # AUTHORITATIVE RE-GENERATION: wipe this cycle's existing verdicts first, so no
    # stale per-programme rows survive from an earlier (pre-consolidation) run and
    # get double-counted by the dashboards. classify_cycle is the sole writer of
    # offering_classification and is re-runnable by design, so clearing the cycle's
    # rows before rewriting is safe — it is derived data, never student feedback.
    # (We do NOT commit here; the caller wraps the whole run in one transaction.)
    master.execute(
        "DELETE FROM offering_classification WHERE cycle_code = ?", (cycle_code,))

    for anchor_id, grp in groups.items():
        summary["total"] += 1

        # Score the WHOLE delivery as one pooled report (electives pooled across
        # programmes; a singleton delivery scores exactly as before). None means
        # uncategorised/template-less — skip, exactly as the report engine does.
        result = scoring.score_offering_group(
            master, cycle, grp["oids"], discussed_late_weight)
        if result is None:
            summary["skipped"] += 1
            continue

        overall_score = result["overall"]
        # Pooled count of submitted forms across the delivery — the correct
        # denominator for the tiny-sample guard on a shared elective class.
        n_responses = result["n_responses"]
        section_scores = result["section_scores"]   # list of {key,title,score,...}

        # The pure §6 verdict, unchanged — just fed pooled numbers now.
        band_value = band(overall_score, n_responses, threshold_overall,
                          threshold_section, section_scores, min_responses)

        # Normalise section scores to numbers for the audit reason string.
        section_values = [s["score"] for s in section_scores
                          if s.get("score") is not None]
        reason = _reason(band_value, overall_score, n_responses,
                         threshold_overall, threshold_section, section_values)

        # ONE verdict row, keyed to the delivery's anchor offering_id.
        record_band(master, anchor_id, cycle_code, band_value, overall_score,
                    n_responses, threshold_overall, threshold_section,
                    min_responses, reason)

        # Tally for the summary the caller reports.
        if band_value == BAND_POOR:
            summary["poor"] += 1
        elif band_value == BAND_GOOD:
            summary["good"] += 1
        else:
            summary["insufficient"] += 1

    return summary


# ----------------------------------------------------------------------------
# _row_get(row, key, default) — read a column from a sqlite3.Row (or dict) that
# MIGHT not have that column (e.g. a pre-migration cycle row in a test). A plain
# row["missing"] raises; this makes the runner defensive without hiding real
# data errors — it only substitutes the documented default when the column is
# genuinely absent.
# ----------------------------------------------------------------------------
def _row_get(row, key, default):
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return value if value is not None else default
