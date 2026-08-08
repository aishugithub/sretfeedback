# ============================================================================
# signing.py  —  v2.1 : app-embedded digital signing of the audit PDF (pyHanko)
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# The end-of-cycle audit report (audit_report.py) must be a TAMPER-EVIDENT soft
# copy that can be stored for audits. The professor chose the "app-embedded
# signature" model: the SYSTEM seals the finished PDF itself, using its own
# self-managed certificate, the moment the report is generated — no USB token and
# no manual step. Any later edit to even one byte invalidates the signature, which
# is exactly the tamper-evidence an audit trail needs.
#
# HOW IT WORKS:
#   * ensure_keypair() creates, ONCE, a self-signed X.509 certificate + RSA key for
#     the institution ("SRET Automated Feedback System") and stores them in data/
#     (git-ignored, alongside master.db). Re-runs are no-ops.
#   * sign_pdf_bytes() embeds a PKCS#7/CMS digital signature into the PDF using
#     pyHanko. The signature records WHO (the institutional cert), WHY (reason) and
#     WHEN (signing time), and seals the whole file.
#
# WHAT A VERIFIER SEES: opening the signed PDF in Adobe Reader shows "Signed and
# all signatures valid" for integrity, with the signer identity "SRET Automated
# Feedback System". Because the certificate is SELF-signed (not issued by a public
# CA), Adobe will add "the signer's identity is unknown" until someone adds this
# certificate to their Trusted Certificates — a one-time trust step. Integrity
# (was-it-tampered) is proven regardless; identity-trust is the one-time add.
# See the user manual's "Verifying the audit signature" section.
# ----------------------------------------------------------------------------

import os
import io
import datetime

from config import Config


# Where the institutional signing material lives (git-ignored data/ dir, like the
# databases). The key never leaves the server; treat it like master.db.
_KEY_PATH = os.path.join(Config.DATA_DIR, "afs_signing_key.pem")
_CERT_PATH = os.path.join(Config.DATA_DIR, "afs_signing_cert.pem")

# The identity that appears as the signer on every audit PDF.
_ORG_NAME = "Sri Ramachandra Faculty of Engineering and Technology"
_COMMON_NAME = "SRET Automated Feedback System"


def ensure_keypair():
    """Create the self-signed signing certificate + key ONCE, and return their
    paths. Idempotent: if both files already exist, it does nothing and returns
    them. Uses the `cryptography` library (a pyHanko dependency, so no new install).
    """
    if os.path.exists(_KEY_PATH) and os.path.exists(_CERT_PATH):
        return _KEY_PATH, _CERT_PATH

    # Imported lazily so importing this module never forces the crypto stack unless
    # a signature is actually needed.
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography import x509
    from cryptography.x509.oid import NameOID

    os.makedirs(Config.DATA_DIR, exist_ok=True)

    # A 2048-bit RSA key is the sensible, widely-compatible choice for PDF signing.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    name = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, _ORG_NAME),
        x509.NameAttribute(NameOID.COMMON_NAME, _COMMON_NAME),
    ])
    now = datetime.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)                       # self-signed: subject == issuer
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))   # ~10 years
        .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                       critical=True)
        # A document-signing certificate: digital signature + non-repudiation.
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=True,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=False, crl_sign=False,
                encipher_only=False, decipher_only=False),
            critical=True)
        .sign(key, hashes.SHA256())
    )

    # Write the private key UNENCRYPTED into the protected data/ dir (same trust
    # boundary as master.db). pyHanko's SimpleSigner.load then needs no passphrase.
    with open(_KEY_PATH, "wb") as fh:
        fh.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()))
    with open(_CERT_PATH, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    # Keep the key file readable only by the owner where the OS supports it.
    try:
        os.chmod(_KEY_PATH, 0o600)
    except OSError:
        pass
    return _KEY_PATH, _CERT_PATH


def _timestamper():
    """Build an RFC-3161 HTTP timestamper from the configured TSA URL, or return
    None if no TSA is configured. Kept separate so the signing path is simple and
    the TSA is easy to swap per deployment (env FEEDBACK_TSA_URL)."""
    url = (Config.TSA_URL or "").strip()
    if not url:
        return None
    from pyhanko.sign.timestamps import HTTPTimeStamper
    return HTTPTimeStamper(url=url)


def sign_pdf_bytes(pdf_bytes, reason="End-of-cycle feedback audit record",
                   location="SRET", signer_name=None):
    """Return (signed_pdf_bytes, timestamp_status) — a NEW PDF sealing `pdf_bytes`
    with an embedded digital signature (institutional self-signed cert, created on
    first use). If a TSA is configured (Config.TSA_URL) we ALSO embed an RFC-3161
    trusted timestamp; if that TSA call fails (unreachable / blocked, e.g. on the
    PythonAnywhere free tier) we FALL BACK to signing WITHOUT a timestamp so audit
    generation never breaks. `timestamp_status` is one of:
        'timestamped'  — signed AND trusted-timestamped,
        'no-tsa'       — signed; no TSA was configured,
        'tsa-failed: <error>' — signed; the TSA was configured but unreachable.
    """
    from pyhanko.sign import signers
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter

    key_path, cert_path = ensure_keypair()
    signer = signers.SimpleSigner.load(key_path, cert_path)
    meta = signers.PdfSignatureMetadata(
        field_name="AFSSignature",
        reason=reason,
        location=location,
        name=(signer_name or _COMMON_NAME),
    )

    tsa = _timestamper()

    def _sign(timestamper):
        # Fresh writer per attempt — a BytesIO can't be re-consumed after a failure.
        writer = IncrementalPdfFileWriter(io.BytesIO(pdf_bytes))
        out = signers.sign_pdf(writer, meta, signer=signer, timestamper=timestamper)
        out.seek(0)
        return out.read()

    if tsa is not None:
        try:
            return _sign(tsa), "timestamped"
        except Exception as e:
            # TSA unreachable/blocked/erroring — degrade gracefully to a plain
            # signature rather than failing the whole audit report.
            return _sign(None), "tsa-failed: %s" % e
    return _sign(None), "no-tsa"
