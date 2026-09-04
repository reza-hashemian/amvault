"""TOTP (RFC 6238) code generation.

This exists so the vault can replace Google Authenticator for services that
require a 2FA code.  Note the tradeoff: keeping the TOTP seed next to the
password means one compromised vault yields both factors.  For low-stakes
accounts that convenience is usually worth it; for a bank or a root account,
keep the seed on a separate device.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time
from urllib.parse import parse_qs, unquote, urlparse

DEFAULT_DIGITS = 6
DEFAULT_PERIOD = 30
DEFAULT_ALGORITHM = "SHA1"

_ALGOS = {
    "SHA1": hashlib.sha1,
    "SHA256": hashlib.sha256,
    "SHA512": hashlib.sha512,
}


class TotpError(ValueError):
    pass


def normalize_secret(secret: str) -> bytes:
    """Decode a base32 TOTP seed as services usually print it.

    Authenticator seeds are commonly shown in spaced, lower-case, unpadded
    base32, none of which b32decode accepts as-is.
    """
    cleaned = secret.replace(" ", "").replace("-", "").strip().upper()
    if not cleaned:
        raise TotpError("TOTP secret is empty.")
    padding = "=" * (-len(cleaned) % 8)
    try:
        return base64.b32decode(cleaned + padding, casefold=True)
    except Exception as exc:
        raise TotpError("TOTP secret is not valid base32.") from exc


def generate(
    secret: str,
    at: float | None = None,
    digits: int = DEFAULT_DIGITS,
    period: int = DEFAULT_PERIOD,
    algorithm: str = DEFAULT_ALGORITHM,
) -> str:
    key = normalize_secret(secret)
    algo = _ALGOS.get(algorithm.upper())
    if algo is None:
        raise TotpError(f"Unsupported TOTP algorithm: {algorithm}")

    counter = int((at if at is not None else time.time()) // period)
    digest = hmac.new(key, struct.pack(">Q", counter), algo).digest()

    # Dynamic truncation, RFC 4226 section 5.4.
    offset = digest[-1] & 0x0F
    code_int = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code_int % (10**digits)).zfill(digits)


def seconds_remaining(at: float | None = None, period: int = DEFAULT_PERIOD) -> int:
    now = at if at is not None else time.time()
    return int(period - (now % period))


def parse_uri(uri: str) -> dict:
    """Parse an otpauth:// URI, the format behind QR codes.

    Lets a user paste the whole URI a service gives them instead of picking
    the seed out of it by hand.
    """
    parsed = urlparse(uri)
    if parsed.scheme != "otpauth":
        raise TotpError("Not an otpauth:// URI.")
    if parsed.netloc.lower() != "totp":
        raise TotpError("Only TOTP (not HOTP) URIs are supported.")

    params = parse_qs(parsed.query)
    secret = params.get("secret", [""])[0]
    if not secret:
        raise TotpError("otpauth URI has no secret parameter.")

    label = unquote(parsed.path.lstrip("/"))
    issuer = params.get("issuer", [""])[0]
    if not issuer and ":" in label:
        issuer = label.split(":", 1)[0]

    return {
        "secret": secret,
        "label": label,
        "issuer": issuer,
        "digits": int(params.get("digits", [DEFAULT_DIGITS])[0]),
        "period": int(params.get("period", [DEFAULT_PERIOD])[0]),
        "algorithm": params.get("algorithm", [DEFAULT_ALGORITHM])[0].upper(),
    }
