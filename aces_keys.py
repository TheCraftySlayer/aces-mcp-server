"""
The server's own secret, for signing things it hands to a browser and later gets back
(chat tool results, sign-in sessions). It never leaves the server, and it isn't any of
the keys staff or Copilot Studio hold, so holding the access key doesn't let anyone sign.

Settings (environment variables)
  ACES_SERVER_SECRET       optional, at least 32 characters. Set it only if more than one
                           copy of the server has to accept each other's signatures.
  ACES_SERVER_SECRET_FILE  where the secret is kept when ACES_SERVER_SECRET isn't set.
                           Default: aces_server_secret.key in the system temp folder.
                           On App Service use /home/aces_server_secret.key, and on a county
                           server a folder only the service account can read, so it survives
                           restarts. The file is created on first start.

If the file can't be read or written, the server makes a secret for this run only and
says so in the log. After a restart, chat history from before it then loses its tool
results (the chat says so and calls the tools again) and signed-in staff sign in again.
"""

import hashlib
import hmac
import os
import secrets
import tempfile

from aces_common import _log

MIN_SECRET_CHARS = 32


def _from_file(path: str) -> str:
    try:
        # O_EXCL: if two starts race, only one writes and the other reads what it wrote.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        with open(path, encoding="ascii") as fh:
            value = fh.read().strip()
        if len(value) < MIN_SECRET_CHARS:
            raise ValueError(f"{path} holds a secret shorter than {MIN_SECRET_CHARS} characters") from None
        return value
    value = secrets.token_hex(32)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(value)
    _log("server secret created", file=path)
    return value


def _load() -> bytes:
    value = os.getenv("ACES_SERVER_SECRET", "").strip()
    if value:
        if len(value) < MIN_SECRET_CHARS:
            raise RuntimeError(f"ACES_SERVER_SECRET must be at least {MIN_SECRET_CHARS} characters.")
        return value.encode("utf-8")
    path = os.getenv("ACES_SERVER_SECRET_FILE", "").strip() or os.path.join(
        tempfile.gettempdir(), "aces_server_secret.key"
    )
    try:
        return _from_file(path).encode("ascii")
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        _log(
            "WARNING: server secret file unusable, using a secret for this run only",
            file=path,
            error=f"{exc.__class__.__name__}: {str(exc)[:200]}",
        )
        return secrets.token_hex(32).encode("ascii")


_SECRET = _load()


def derive(purpose: str) -> bytes:
    """A separate key for each use, so a signature made for one purpose is never valid for another."""
    return hmac.new(_SECRET, f"aces:{purpose}".encode(), hashlib.sha256).digest()


def sign(key: bytes, data: bytes) -> str:
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def verify(key: bytes, data: bytes, signature: str) -> bool:
    if not isinstance(signature, str) or not signature:
        return False
    return hmac.compare_digest(sign(key, data), signature)
