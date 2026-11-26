"""Password scrambling, session tokens and a readable device name."""
import hashlib
import secrets
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

_hasher = PasswordHasher()
# Checked when the email is unknown, so a wrong email takes as long as a wrong password.
_DUMMY_HASH = _hasher.hash("not-a-real-password")

MIN_PASSWORD_LENGTH = 12


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: Optional[str], password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except VerificationError:
        return False


def new_token() -> str:
    """A long random value that identifies one signed-in browser."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def device_label(user_agent: Optional[str]) -> str:
    """'Mac · Chrome' style name from the browser's self-description."""
    ua = user_agent or ""
    system = ("Windows laptop" if "Windows" in ua else "Mac" if "Macintosh" in ua
              else "Linux computer" if "Linux" in ua and "Android" not in ua else "Unknown device")
    browser = ("Edge" if "Edg/" in ua else "Chrome" if "Chrome/" in ua else "Firefox" if "Firefox/" in ua
               else "Safari" if "Safari/" in ua else "browser")
    return f"{system} · {browser}"


TEMP_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no look-alikes (0/O, 1/l/I)


def temporary_password() -> str:
    """A one-time password like 'k7Qm-Xp3a-Rt9w' for a new user or a reset. They must change it at sign-in."""
    raw = "".join(secrets.choice(TEMP_ALPHABET) for _ in range(12))
    return "-".join(raw[i:i + 4] for i in range(0, 12, 4))
