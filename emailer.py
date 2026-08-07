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
def send_batch(base_dir, subject, messages, is_test=False):
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

    # ---- TEST-MODE HARD REDIRECT (spec §9.1) --------------------------------
    # When the cycle is a test cycle, EVERY outbound message is rewritten to the
    # single designated test address, right here in the mailer. This is enforced
    # in code, not by a config file or by fake addresses in a spreadsheet — so
    # one un-edited cell can never leak a real send. The original intended
    # recipient is preserved in the body for the tester's reference, and the
    # subject is tagged. Applies to BOTH smtp and dev-outbox modes.
    if is_test:
        from config import Config
        redirect_to = Config.TEST_REDIRECT_EMAIL
        subject = f"[TEST] {subject}"
        rewritten = []
        for m in messages:
            rewritten.append({
                "to": redirect_to,
                "body": (f"*** TEST CYCLE — original recipient: {m.get('to','?')} ***\n\n"
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
        from_addr = gcfg["from_addr"]           # feedback@sret.edu.in
        for m in messages:
            try:
                send_via_gmail_api(gcfg, from_addr, m["to"], subject, m["body"],
                                   attachments=m.get("attachments"))
                summary["count"] += 1
            except Exception as e:
                summary["errors"].append(f"{m.get('to', '?')}: {e}")
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
