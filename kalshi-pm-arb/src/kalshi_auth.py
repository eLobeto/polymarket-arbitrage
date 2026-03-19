"""kalshi_auth.py — RSA request signing for Kalshi API (RSA-PSS + SHA-256)."""
import base64
import time
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as _padding
from config import KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_PATH


def _load_key():
    """Load RSA private key from file path specified in KALSHI_PRIVATE_KEY_PATH."""
    key_path = KALSHI_PRIVATE_KEY_PATH
    if not key_path:
        raise ValueError("KALSHI_PRIVATE_KEY_PATH env var not set")
    with open(key_path, "rb") as f:
        pem_data = f.read()
    return serialization.load_pem_private_key(pem_data, password=None)


def signed_headers(method: str, path: str) -> dict:
    """
    Return signed headers for a Kalshi API request.
    Signing: timestamp_ms + METHOD + path (no query params, no body).
    Algorithm: RSA-PSS with SHA-256 (matches Kalshi production API).
    """
    ts_ms = str(int(time.time() * 1000))
    path_no_query = path.split("?")[0]
    message = (ts_ms + method.upper() + path_no_query).encode("utf-8")
    key = _load_key()
    sig = key.sign(
        message,
        _padding.PSS(
            mgf=_padding.MGF1(hashes.SHA256()),
            salt_length=_padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "Content-Type":            "application/json",
        "KALSHI-ACCESS-KEY":       KALSHI_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts_ms,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
    }
