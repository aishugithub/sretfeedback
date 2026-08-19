# ============================================================================
# db.py  —  SQLite connection helpers (WAL mode) and the anonymity guardrails
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# This is the ONLY place in the codebase that opens SQLite connections. Every
# other module (the seed script, the allocation importer, the admin blueprint,
# and — later — the scoring engine and student form) asks db.py for a
# connection instead of calling sqlite3 directly. Centralising it means:
#   * WAL mode and foreign-key enforcement are applied CONSISTENTLY, and
#   * the "two-file split" (master.db vs per-cycle db) lives in one spot.
#
# THE ANONYMITY DESIGN (spec Section 5) — why there are two databases:
#   master.db      -> Group A (identity) + Group C (config): students,
#                     offerings, categories, templates, questions, scales.
#   cycle_*.db     -> Group A token table + Group B (anonymous answers):
#                     token, response, answer.
# Group A (who participated) and Group B (the answers) live in SEPARATE files
# and share NO key, so a response can never be joined back to a student — even
# with full disk access. This module deliberately offers no helper that opens
# both files in one connection, reinforcing that they are never JOINed.
# ----------------------------------------------------------------------------

import os        # to check/create the data directory
import sqlite3   # Python's built-in SQLite driver — no external dependency

from config import Config  # our central paths + reference data


# ----------------------------------------------------------------------------
# _configure(conn) — apply the PRAGMAs we want on EVERY connection.
# Pulled into a helper so master.db and cycle DBs are treated identically.
# ----------------------------------------------------------------------------
def _configure(conn: sqlite3.Connection) -> sqlite3.Connection:
    # row_factory = sqlite3.Row makes query results behave like dicts
    # (row["column_name"]) as well as tuples — far more readable in the
    # admin views and scoring code than positional row[0], row[1], ...
    conn.row_factory = sqlite3.Row

    # PRAGMA journal_mode=WAL (Write-Ahead Logging): the headline requirement
    # from the spec. WAL lets MANY students submit concurrently (readers do
    # not block the writer, the writer does not block readers), which is what
    # a "batch of 500 students on the LAN" needs (spec Section 8).
    conn.execute("PRAGMA journal_mode=WAL;")

    # PRAGMA foreign_keys=ON: SQLite ships with FK enforcement OFF by default.
    # We turn it on so, e.g., an `answer` cannot point at a non-existent
    # `response`, and an `offering` cannot reference a missing `category`.
    conn.execute("PRAGMA foreign_keys=ON;")

    # PRAGMA synchronous=NORMAL: the safe, standard companion to WAL. Durable
    # across application crashes; a good speed/safety balance for a laptop host.
    conn.execute("PRAGMA synchronous=NORMAL;")

    # PRAGMA busy_timeout=15000 (15s) — the CONCURRENCY fix. WAL lets readers and
    # the writer coexist, but SQLite still permits only ONE writer at a time. When
    # a whole class hits /submit together, two submissions can reach for the write
    # lock at the same instant. Without a busy timeout the loser raises
    # "database is locked" almost immediately (and the student sees an error);
    # with it, that connection simply WAITS up to 15s for the lock to free — which
    # in practice is a few hundred milliseconds — and then proceeds. This is the
    # single most important line for surviving a lab-sized burst on a server with
    # few web workers (e.g. PythonAnywhere), turning hard lock errors into short,
    # invisible waits. Applied on EVERY connection (master and cycle) via _configure.
    conn.execute("PRAGMA busy_timeout=15000;")

    return conn


# ----------------------------------------------------------------------------
# _ensure_data_dir() — make sure the folder that will hold the .db files exists
# before we try to create a database inside it. Safe to call repeatedly.
# ----------------------------------------------------------------------------
def _ensure_data_dir() -> None:
    os.makedirs(Config.DATA_DIR, exist_ok=True)


# ----------------------------------------------------------------------------
# get_master() — open (creating if needed) the PERMANENT master.db.
# Used by: seed script, allocation importer, admin roster pages, student-list
# lookups. NEVER holds answer data.
# ----------------------------------------------------------------------------
def get_master() -> sqlite3.Connection:
    _ensure_data_dir()
    conn = sqlite3.connect(Config.MASTER_DB)
    return _configure(conn)


# ----------------------------------------------------------------------------
# cycle_db_path(ay_label, cycle_code) — build the on-disk filename for a
# per-cycle database, e.g. ay_label="AY 2026-27", cycle_code="CA1" ->
# ".../data/cycle_2026-27_CA1.db". We strip the "AY " prefix and spaces so the
# filename is clean and shell-friendly.
# ----------------------------------------------------------------------------
def cycle_db_path(ay_label: str, cycle_code: str) -> str:
    # "AY 2026-27" -> "2026-27": drop a leading "AY", then strip whitespace.
    ay_clean = ay_label.replace("AY", "").strip().replace(" ", "")
    filename = f"cycle_{ay_clean}_{cycle_code}.db"
    return os.path.join(Config.DATA_DIR, filename)


# ----------------------------------------------------------------------------
# get_cycle(ay_label, cycle_code) — open (creating if needed) the per-cycle
# database that holds tokens + responses + answers. Used by the student form
# (Night 2) and the scoring engine (Night 3). Deliberately a SEPARATE
# connection from master.db so the two are never joined in one query.
# ----------------------------------------------------------------------------
def get_cycle(ay_label: str, cycle_code: str) -> sqlite3.Connection:
    _ensure_data_dir()
    conn = sqlite3.connect(cycle_db_path(ay_label, cycle_code))
    return _configure(conn)


# ----------------------------------------------------------------------------
# run_script(conn, sql_text) — execute a multi-statement SQL script (our
# schema files are one big string of CREATE TABLE statements). executescript()
# runs them all in one call and commits.
# ----------------------------------------------------------------------------
def run_script(conn: sqlite3.Connection, sql_text: str) -> None:
    conn.executescript(sql_text)
    conn.commit()
