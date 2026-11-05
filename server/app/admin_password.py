"""Admin password hashes for OP login (``MINDPROBE_ADMIN_PASSWORD_HASH``).

Format: ``scrypt:<n>:<r>:<p>:<salt>:<digest>``, salt and digest unpadded
base64url. There is no ``$`` in it, so docker compose does not try to
interpolate the value when it sits in the host ``.env``.

Generate one on the host, then paste the printed line into ``runtime.env``::

    python -m app.admin_password
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import secrets
import sys

SCHEME = "scrypt"
N, R, P = 2**14, 8, 1
SALT_BYTES = 16
DIGEST_BYTES = 32
#: Upper bounds for parameters read back from the env, so a typo cannot pin a CPU.
MAX_N, MAX_R, MAX_P = 2**17, 16, 4


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _derive(password: str, salt: bytes, n: int, r: int, p: int, length: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=length,
        maxmem=256 * r * n + 1024 * 1024,
    )


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt if salt is not None else secrets.token_bytes(SALT_BYTES)
    digest = _derive(password, salt, N, R, P, DIGEST_BYTES)
    return f"{SCHEME}:{N}:{R}:{P}:{_b64(salt)}:{_b64(digest)}"


def _parse(encoded: str) -> tuple[int, int, int, bytes, bytes] | None:
    parts = encoded.strip().split(":")
    if len(parts) != 6 or parts[0] != SCHEME:
        return None
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, digest = _unb64(parts[4]), _unb64(parts[5])
    except ValueError:
        return None
    if not (2 <= n <= MAX_N and n & (n - 1) == 0 and 1 <= r <= MAX_R and 1 <= p <= MAX_P):
        return None
    if len(salt) < 8 or len(digest) < 16:
        return None
    return n, r, p, salt, digest


def is_valid_hash(encoded: str) -> bool:
    return _parse(encoded) is not None


_dummy_hash: str | None = None


def dummy_hash() -> str:
    """A well-formed hash nobody knows the password for, so failures cost the same."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_urlsafe(16))
    return _dummy_hash


def verify_password(password: str, encoded: str) -> bool:
    parsed = _parse(encoded)
    if parsed is None:
        return False
    n, r, p, salt, digest = parsed
    return hmac.compare_digest(_derive(password, salt, n, r, p, len(digest)), digest)


def main() -> int:
    first = getpass.getpass("New admin password: ")
    if len(first) < 12:
        print("Use at least 12 characters.", file=sys.stderr)
        return 1
    if getpass.getpass("Again: ") != first:
        print("The two entries differ.", file=sys.stderr)
        return 1
    print(hash_password(first))
    return 0


if __name__ == "__main__":
    sys.exit(main())
