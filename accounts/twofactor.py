"""Two-factor sign-in: RFC 6238 time-based one-time passwords (authenticator apps) and single-use recovery codes."""
import base64
import hashlib
import hmac
import io
import secrets
import struct
import time
from urllib.parse import quote

STEP = 30
DIGITS = 6
WINDOW = 1  # accept the previous and next 30-second code too (clock drift)


def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret):
    return base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)


def code_at(secret, step):
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** DIGITS).zfill(DIGITS)


def current_step(now=None):
    return int((now if now is not None else time.time()) // STEP)


def match_step(secret, code, last_step=0, now=None):
    """The time step a code belongs to, or None. Codes at or before last_step are rejected (no replay)."""
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(code) != DIGITS or not secret:
        return None
    base = current_step(now)
    for step in range(base - WINDOW, base + WINDOW + 1):
        if step > last_step and hmac.compare_digest(code_at(secret, step), code):
            return step
    return None


def otpauth_uri(secret, account, issuer="Paperlane"):
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={STEP}"


def qr_svg(data):
    """An inline SVG QR code (no external service sees the secret)."""
    import segno

    buf = io.BytesIO()
    segno.make(data, error="m").save(buf, kind="svg", scale=5, border=2, xmldecl=False, svgns=True, dark="#171521",
                                     light="#ffffff")
    return buf.getvalue().decode()


def _hash(code):
    return hashlib.sha256(code.replace("-", "").strip().lower().encode()).hexdigest()


def new_recovery_codes(n=10):
    """Returns (plain codes to show once, hashes to store)."""
    plain = [f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(n)]
    return plain, [_hash(c) for c in plain]


def use_recovery_code(profile, code):
    """Consume a recovery code; True if it was valid."""
    h = _hash(code or "")
    codes = list(profile.recovery_codes or [])
    for stored in codes:
        if hmac.compare_digest(stored, h):
            codes.remove(stored)
            profile.recovery_codes = codes
            profile.save(update_fields=["recovery_codes"])
            return True
    return False


def verify(profile, code):
    """Check an authenticator code or a recovery code for a profile with two-factor on. Returns 'totp', 'recovery' or None."""
    step = match_step(profile.totp_secret, code, profile.totp_last_step)
    if step is not None:
        profile.totp_last_step = step
        profile.save(update_fields=["totp_last_step"])
        return "totp"
    if "-" in str(code or "") or len(str(code or "").strip()) == 12:
        if use_recovery_code(profile, code):
            return "recovery"
    return None
