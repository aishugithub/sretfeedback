# ============================================================================
# emailer.py  —  One-email-per-student sending, with a safe dev/test mode
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# The admin, after generating tokens for a batch, needs to actually deliver each
# student their single private link (spec Sections 6.6 & 12). This module is the
# only place that talks to SMTP, so the rest of the app just calls send_batch()
# and never worries about mail transport.
#
# THREE MODES, chosen automatically from configuration (precedence order):
#   * GMAIL-API mode (v1.5, PREFERRED) — if Gmail OAuth2 credentials are present
#     (FEEDBACK_GMAIL_* env vars) we send via the Gmail REST API over HTTPS as
#     feedback@sret.edu.in. No password needed (App Passwords are disabled on our
#     Workspace domain), works on PythonAnywhere's free tier, and the mail is
#     DKIM-signed by Workspace so it survives spam checks at sriher.edu.in. All
#     the Gmail-specific logic lives in the sibling module gmail_api.py.
#   * SMTP mode  — if institutional SMTP settings are present instead, we open an
#     SMTP connection and send genuine email (needs a login password/app-password).
#   * DEV/TEST mode (the default when neither is configured) — we DO NOT send
#     anything over the network. Instead each message is written as a .eml file
#     into app/outbox/. This lets the whole flow be demonstrated on a laptop with
#     no mail server: you can open the .eml files, copy the link, and test the
#     student form. The spec explicitly asks for this safe test path (task brief).
#
# PRECEDENCE: gmail-api > smtp > dev-outbox. The first one whose credentials are
# fully configured wins; the others are transparent fallbacks.
#
# PLACEHOLDERS (spec Section 11/12): the per-cycle email body is editable and may
# contain {student_name}, {cycle_name} and {link}. render_body() fills them in
# per student. "What you see in the editor is exactly what is sent."
# ----------------------------------------------------------------------------

import os                       # filesystem paths for the dev outbox
import smtplib                  # standard-library SMTP client (real mode)
from email.message import EmailMessage  # builds a correctly-formatted email
from datetime import datetime   # timestamped, unique .eml filenames


# ----------------------------------------------------------------------------
# SMTP configuration is read from ENVIRONMENT VARIABLES so no secret is ever
# committed to the codebase. If FEEDBACK_SMTP_HOST is unset, we are in dev mode.
#   FEEDBACK_SMTP_HOST   e.g. "smtp.gmail.com" or the college relay
#   FEEDBACK_SMTP_PORT   e.g. "587" (STARTTLS) — defaults to 587
#   FEEDBACK_SMTP_USER   login user (also the default From address)
#   FEEDBACK_SMTP_PASS   password / app-password
#   FEEDBACK_SMTP_FROM   optional explicit From address (defaults to USER)
# ----------------------------------------------------------------------------
def smtp_settings():
    host = os.environ.get("FEEDBACK_SMTP_HOST", "").strip()
    return {
        "host": host,
        "port": int(os.environ.get("FEEDBACK_SMTP_PORT", "587")),
        "user": os.environ.get("FEEDBACK_SMTP_USER", "").strip(),
        "password": os.environ.get("FEEDBACK_SMTP_PASS", ""),
        "from_addr": os.environ.get("FEEDBACK_SMTP_FROM",
                                    os.environ.get("FEEDBACK_SMTP_USER", "feedback@sret.local")).strip(),
        "enabled": bool(host),   # real mode iff a host is configured
    }


# ----------------------------------------------------------------------------
# active_mode() — the SINGLE source of truth for "what will happen if I send now",
# honouring the real precedence gmail-api > smtp > dev-outbox. Every pre-send
# banner (Tokens, Participation, Results) MUST use this instead of only checking
# SMTP — otherwise it wrongly reports "DEV mode" while Gmail-API sending is in
# fact live (the exact bug this fixes). Returns a small dict the templates read:
#   mode     : 'gmail-api' | 'smtp' | 'dev-outbox'
#   live     : True when real mail leaves the server (gmail-api OR smtp)
#   from_addr: the address mail is sent AS
#   host     : a human label of the transport (for the reassurance line)
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# THREE-LEVEL TESTING MODEL (v2.1). A cycle carries a `test_level` (0-3) that
# controls, PER AUDIENCE, whether a message goes to a real recipient or is
# redirected to a test inbox — and whether reports are watermarked. This is the
# single place the routing rule lives, so the mailer, the distribution job and
# the pre-send banners all agree.
#
#   Level 0 PRODUCTION : students, faculty, leaders all REAL; reports clean.
#   Level 1 (safest)   : students -> student test inbox (Config.TEST_REDIRECT_EMAIL)
#                        faculty/leaders -> staff test inbox (Config.FACULTY_ALIAS_EMAIL)
#   Level 2            : students -> student test inbox; faculty/leaders REAL.
#   Level 3            : students, faculty, leaders all REAL; reports WATERMARKED.
# ----------------------------------------------------------------------------
TEST_LEVEL_PRODUCTION = 0


def test_level_of(cycle_row):
    """Read a cycle's test_level, tolerating rows that predate the column (a test
    fixture, or a DB not yet migrated): fall back to the legacy is_test flag
    (1 -> level 1, else level 0). Always returns a plain int 0-3."""
    lvl = None
    try:
        lvl = cycle_row["test_level"]
    except (KeyError, IndexError, TypeError):
        lvl = None
    if lvl is None:
        try:
            lvl = 1 if cycle_row["is_test"] else 0
        except (KeyError, IndexError, TypeError):
            lvl = 0
    try:
        return int(lvl)
    except (TypeError, ValueError):
        return 0


def is_watermarked(cycle_row):
    """Reports carry the 'TEST DATA' watermark at every non-production level
    (1, 2 and 3); only a promoted, level-0 production cycle prints clean copies."""
    return test_level_of(cycle_row) != TEST_LEVEL_PRODUCTION


def redirect_target(test_level, audience):
    """WHERE a message of the given audience is really delivered at this level, or
    None to send it to its real recipient. `audience` is 'student', 'faculty' or
    'leader' (faculty and leaders route identically — both are institutional staff).
    This is the whole §9 routing table expressed once, in code:

        level 0  -> None for everyone (production)
        level 1  -> students to the student inbox, staff to the staff inbox
        level 2  -> students to the student inbox, staff REAL (None)
        level 3  -> None for everyone (all real, but reports still watermarked)
    """
    from config import Config
    is_student = (audience or "student").lower() == "student"
    try:
        level = int(test_level)
    except (TypeError, ValueError):
        level = 0
    if level <= TEST_LEVEL_PRODUCTION:          # 0 production: all real
        return None
    if level == 1:
        return Config.TEST_REDIRECT_EMAIL if is_student else Config.FACULTY_ALIAS_EMAIL
    if level == 2:
        return Config.TEST_REDIRECT_EMAIL if is_student else None
    return None                                  # level 3: everyone real


def active_mode():
    from gmail_api import gmail_settings   # imported here to avoid an import cycle
    g = gmail_settings()
    s = smtp_settings()
    if g["enabled"]:
        return {"mode": "gmail-api", "live": True,
                "from_addr": g["from_addr"], "host": "Gmail API"}
    if s["enabled"]:
        return {"mode": "smtp", "live": True,
                "from_addr": s["from_addr"], "host": s["host"]}
    return {"mode": "dev-outbox", "live": False,
            "from_addr": s["from_addr"], "host": s["host"]}


# ----------------------------------------------------------------------------
# render_body(template_text, student_name, cycle_name, link)
# ----------------------------------------------------------------------------
# Fill the three supported placeholders. We use str.replace (not str.format) on
# purpose: the admin's free text may contain stray "{" or "}" characters that
# would crash str.format; replace is forgiving and only touches the exact tokens.
# ----------------------------------------------------------------------------
def render_body(template_text, student_name, cycle_name, link):
    text = template_text or ""
    text = text.replace("{student_name}", student_name or "student")
    text = text.replace("{cycle_name}", cycle_name or "")
    text = text.replace("{link}", link or "")
    return text


# ----------------------------------------------------------------------------
# ATTACHMENTS (v3.3) — the result-distribution job (distribution.py) needs to
# send each teacher their report PDF(s). Until now every email was body-only;
# these two helpers add file attachments to an EmailMessage in ONE shared place,
# so the dev-outbox, SMTP and Gmail-API paths all attach identically.
#
# An "attachment" is simply a (filename, data_bytes) pair. We guess the MIME type
# from the extension so a PDF opens as a PDF (not a generic download). Anything we
# don't recognise falls back to a safe generic binary type.
# ----------------------------------------------------------------------------
def _guess_mime(filename):
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    return {
        "pdf":  ("application", "pdf"),
        "xlsx": ("application",
                 "vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        "zip":  ("application", "zip"),
    }.get(ext, ("application", "octet-stream"))


def add_attachments(msg, attachments):
    """Attach each (filename, data_bytes) pair to an EmailMessage. `attachments`
    may be None/empty (a normal body-only email), in which case this is a no-op —
    so every existing caller keeps working unchanged."""
    for att in (attachments or []):
        filename, data = att[0], att[1]
        maintype, subtype = _guess_mime(filename)
        msg.add_attachment(data, maintype=maintype, subtype=subtype,
                           filename=filename)


# ----------------------------------------------------------------------------
# _outbox_dir(base_dir) — ensure app/outbox exists and return its path. This is
# where dev-mode .eml files land.
# ----------------------------------------------------------------------------
def _outbox_dir(base_dir):
    path = os.path.join(base_dir, "outbox")
    os.makedirs(path, exist_ok=True)
    return path


# ----------------------------------------------------------------------------
# send_batch(base_dir, subject, messages) — deliver a list of messages.
#
#   messages : list of dicts, each { "to": email, "body": rendered_text,
#              "attachments": [(filename, data_bytes), ...]  # optional }
#
# Returns a summary dict: how many were "sent" (real) or "written" (dev), the
# mode used, the outbox path (dev mode), and a list of per-message errors. The
# admin route surfaces this so the professor sees exactly what happened.
#
# We build ONE EmailMessage per student (spec: "one email per student"), so a
# recipient never sees another student's address, and each carries only that
# student's link.
# ----------------------------------------------------------------------------
def send_batch(base_dir, subject, messages, is_test=False,
               test_level=None, audience="student"):
    # THREE-LEVEL ROUTING (v2.1). `test_level` (0-3) + `audience`
    # ('student'/'faculty'/'leader') decide whether this batch is delivered to
    # real recipients or hard-redirected to a test inbox. For backward
    # compatibility, a caller that still passes only the old `is_test` bool is
    # mapped to level 1 (redirect) or 0 (production) — so nothing that hasn't been
    # updated yet can accidentally start sending live.
    if test_level is None:
        test_level = 1 if is_test else 0

    # Detect BOTH transports up front so we can pick the mode by precedence.
    # gmail_settings() lives in gmail_api.py; imported flat (like `from config
    # import Config` below) because the app runs with app/ on the import path.
    from gmail_api import gmail_settings, send_via_gmail_api
    gcfg = gmail_settings()          # Gmail-API creds (preferred transport)
    cfg = smtp_settings()            # SMTP creds (secondary transport)

    # Choose the mode label for the summary using the same precedence the code
    # below follows: gmail-api first, then smtp, then the dev-outbox fallback.
    if gcfg["enabled"]:
        mode = "gmail-api"
    elif cfg["enabled"]:
        mode = "smtp"
    else:
        mode = "dev-outbox"
    summary = {"mode": mode,
               "count": 0, "errors": [], "outbox": None, "redirected": False}

    # ---- TEST-LEVEL HARD REDIRECT (§9, three-level model) -------------------
    # Ask the single routing table (redirect_target) where THIS batch's audience
    # goes at THIS level. If it returns an address, EVERY outbound message is
    # rewritten to that test inbox, right here in the mailer — enforced in code,
    # not by a config file or by fake spreadsheet addresses, so one un-edited cell
    # can never leak a real send. If it returns None, mail flows to the real
    # recipients (level 0 for everyone; level 2 for staff; level 3 for everyone).
    # The original intended recipient is preserved in the body and the subject is
    # tagged. Applies identically to gmail-api, smtp and dev-outbox modes.
    redirect_to = redirect_target(test_level, audience)
    if redirect_to:
        subject = f"[TEST L{test_level}] {subject}"
        rewritten = []
        for m in messages:
            rewritten.append({
                "to": redirect_to,
                "body": (f"*** TEST (level {test_level}, {audience}) — "
                         f"original recipient: {m.get('to','?')} ***\n\n"
                         + (m.get("body") or "")),
                # Preserve any attachments through the redirect so a test send still
                # demonstrates the real report PDFs (just to the safe test address).
                "attachments": m.get("attachments"),
            })
        messages = rewritten
        summary["redirected"] = True

    # ---- GMAIL-API MODE (v1.5, preferred) ----------------------------------
    # If OAuth2 credentials are configured we send each student their private
    # link through the Gmail API as feedback@sret.edu.in. One HTTPS call per
    # student (the access token is fetched once and cached inside gmail_api),
    # mirroring the "one email per student" rule the SMTP path also follows. A
    # failure on one recipient is recorded and the batch continues.
    if gcfg["enabled"]:
        import time as _time                      # local: throttle + progress only
        # THROTTLE for large one-shot blasts (e.g. ~1,400 students). Gmail enforces
        # a short-term per-user SEND-RATE cap (separate from the 2,000/day quota); a
        # tight no-delay loop can trip "Rate limit exceeded". A small pause between
        # sends keeps us comfortably under it. Tunable via FEEDBACK_SEND_DELAY_SEC
        # (seconds, default 0.25 ≈ 4/sec ≈ ~1,400 mails in ~6 min). Combined with
        # the retry/backoff in gmail_api, a full-cohort send goes out without drops.
        try:
            delay = float(os.environ.get("FEEDBACK_SEND_DELAY_SEC", "0.25"))
        except ValueError:
            delay = 0.25
        from_addr = gcfg["from_addr"]           # feedback@sret.edu.in
        total = len(messages)
        for idx, m in enumerate(messages, start=1):
            try:
                send_via_gmail_api(gcfg, from_addr, m["to"], subject, m["body"],
                                   attachments=m.get("attachments"))
                summary["count"] += 1
            except Exception as e:
                summary["errors"].append(f"{m.get('to', '?')}: {e}")
            # Progress line every 100 sends (visible when run from a console).
            if idx % 100 == 0 or idx == total:
                print(f"[emailer] sent {idx}/{total} "
                      f"(ok={summary['count']}, failed={len(summary['errors'])})",
                      flush=True)
            if delay > 0 and idx < total:
                _time.sleep(delay)              # gentle pacing under the rate cap
        return summary

    if cfg["enabled"]:
        # ---- REAL MODE ---------------------------------------------------
        # Open a single SMTP connection and reuse it for the whole batch (far
        # faster than reconnecting per message, and gentler on the relay).
        try:
            server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=30)
            server.starttls()                       # upgrade to an encrypted channel
            if cfg["user"]:
                server.login(cfg["user"], cfg["password"])
        except Exception as e:
            # If we cannot even connect, fail the whole batch cleanly with a
            # clear message rather than half-sending.
            summary["errors"].append(f"SMTP connect/login failed: {e}")
            return summary

        for m in messages:
            try:
                msg = EmailMessage()
                msg["Subject"] = subject
                msg["From"] = cfg["from_addr"]
                msg["To"] = m["to"]
                msg.set_content(m["body"])
                add_attachments(msg, m.get("attachments"))   # report PDFs, if any
                server.send_message(msg)
                summary["count"] += 1
            except Exception as e:
                summary["errors"].append(f"{m['to']}: {e}")
        try:
            server.quit()
        except Exception:
            pass
        return summary

    # ---- DEV / TEST MODE (no SMTP configured) ----------------------------
    # Write each message to app/outbox/<timestamp>_<email>.eml. The file is a
    # real, openable email; the professor can double-click it to read the link.
    outbox = _outbox_dir(base_dir)
    summary["outbox"] = outbox
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for i, m in enumerate(messages, start=1):
        try:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = cfg["from_addr"]
            msg["To"] = m["to"]
            msg.set_content(m["body"])
            add_attachments(msg, m.get("attachments"))   # report PDFs, if any
            # Sanitise the email into a safe filename.
            safe = m["to"].replace("@", "_at_").replace("/", "_")
            fname = f"{stamp}_{i:04d}_{safe}.eml"
            with open(os.path.join(outbox, fname), "wb") as fh:
                fh.write(bytes(msg))
            summary["count"] += 1
        except Exception as e:
            summary["errors"].append(f"{m.get('to','?')}: {e}")
    return summary
