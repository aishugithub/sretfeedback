# ============================================================================
# sign_audit.py  —  v2.1 : sign + trusted-timestamp an audit PDF ON THE LAPTOP
# ============================================================================
# WHY THIS EXISTS
# ----------------------------------------------------------------------------
# The server (PythonAnywhere) builds the audit report's CONTENT, but the FINAL
# sealing is best done here on the laptop, because:
#   * the private signing key then NEVER sits on the internet-facing server
#     (a real security improvement — the key lives only on your machine), and
#   * the laptop has OPEN internet, so the RFC-3161 trusted-timestamp call to a
#     Timestamp Authority (TSA) just works — no PythonAnywhere whitelisting or paid
#     tier needed.
#
# THE FLOW:
#   1. In the web app, on the Cycles page, click "Sign on laptop" — this downloads
#      the file  Audit_<AY>_<CODE>_<time>_UNSIGNED.pdf.
#   2. On the laptop, in the app/ folder, run:
#          python sign_audit.py  Audit_..._UNSIGNED.pdf
#      This signs it with the institutional certificate (created here on first use)
#      and, if a TSA is configured, adds a trusted timestamp. It writes
#          Audit_..._signed.pdf
#   3. Store that signed PDF in the Dean's / Vice-Dean's Drive as the audit copy.
#
# TSA: reads FEEDBACK_TSA_URL from app/.env (e.g. https://freetsa.org/tsr), or pass
# --tsa <url> to override, or --no-tsa to sign without a timestamp. If a TSA is set
# but unreachable, it still signs and tells you the timestamp was skipped.
#
# USAGE:
#   python sign_audit.py <input.pdf> [output.pdf] [--tsa <url>] [--no-tsa]
# ----------------------------------------------------------------------------

import os
import sys
import hashlib


# ----------------------------------------------------------------------------
# _load_dotenv(path) — the SAME tiny .env loader the web app uses, so running this
# script picks up FEEDBACK_TSA_URL (and any other FEEDBACK_* vars) from app/.env
# WITHOUT needing Flask or the app to be running. Must run BEFORE importing config
# (config.py reads the environment at import time).
# ----------------------------------------------------------------------------
def _load_dotenv(path):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            if k and k not in os.environ:
                os.environ[k] = v


def _default_output(inp):
    """Turn 'Audit_..._UNSIGNED.pdf' -> 'Audit_..._signed.pdf' (or append _signed)."""
    stem = inp[:-4] if inp.lower().endswith(".pdf") else inp
    if stem.endswith("_UNSIGNED"):
        stem = stem[:-len("_UNSIGNED")]
    return stem + "_signed.pdf"


def main(argv):
    # ---- parse a tiny argv (no argparse dependency) ----
    args = [a for a in argv]
    tsa_override = None
    no_tsa = False
    if "--no-tsa" in args:
        no_tsa = True
        args.remove("--no-tsa")
    if "--tsa" in args:
        i = args.index("--tsa")
        try:
            tsa_override = args[i + 1]
            del args[i:i + 2]
        except IndexError:
            print("--tsa needs a URL"); return
    if not args:
        print(__doc__)
        return
    inp = args[0]
    out = args[1] if len(args) > 1 else _default_output(inp)
    if not os.path.exists(inp):
        print("No such file: %s" % inp); return

    # Load app/.env (this file lives in app/, so .env is right next to it) BEFORE
    # importing config/signing so the TSA URL is available.
    _load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    if tsa_override is not None:
        os.environ["FEEDBACK_TSA_URL"] = tsa_override
    if no_tsa:
        os.environ["FEEDBACK_TSA_URL"] = ""

    import signing  # imports config, which now sees the env we just set

    data = open(inp, "rb").read()
    fingerprint = hashlib.sha256(data).hexdigest()
    print("Input : %s" % inp)
    print("SHA-256 of input content: %s" % fingerprint)

    signed, status = signing.sign_pdf_bytes(
        data, reason="End-of-cycle feedback audit record", location="SRET")
    with open(out, "wb") as fh:
        fh.write(signed)

    print("Output: %s" % out)
    if status == "timestamped":
        print("Result: signed AND trusted-timestamped ✔")
    elif status == "no-tsa":
        print("Result: signed ✔  (no TSA configured — set FEEDBACK_TSA_URL or pass "
              "--tsa <url> to also add a trusted timestamp)")
    else:
        print("Result: signed ✔  but the timestamp was skipped (%s). The signature "
              "and fingerprint are still valid; retry with a reachable TSA to add "
              "the timestamp." % status)
    print("\nVerify in Adobe Reader: it will show 'signed and unchanged'. The signer "
          "is the college's own certificate, so do a one-time 'Add to Trusted "
          "Certificates' to also show the identity as trusted.")


if __name__ == "__main__":
    main(sys.argv[1:])
