"""MCP security and filesystem policy primitives."""

import base64
import hashlib
import hmac
import os
import secrets
import tempfile
from pathlib import Path


class PolicyError(ValueError):
    """Raised when an MCP request violates an input or credential policy."""


def generate_token() -> str:
    """Return a URL-safe bearer token suitable for the token file."""

    return secrets.token_urlsafe(32)


def read_token(token_path: Path) -> str:
    """Read a token while rejecting missing, empty, or overly permissive files."""

    try:
        mode = token_path.stat().st_mode & 0o777
    except FileNotFoundError as exc:
        raise PolicyError("MCP authentication token is not configured") from exc

    if mode & 0o077:
        raise PolicyError("MCP authentication token must not be accessible by group or other")

    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise PolicyError("MCP authentication token is empty")
    return token


def token_matches(presented: str, expected: str) -> bool:
    """Compare bearer tokens without leaking length or content through timing."""

    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def rotate_token(token_path: Path) -> str:
    """Atomically replace a token file and return the new token.

    The temporary file is created beside the destination so ``os.replace`` is
    atomic on the same filesystem.  The restrictive mode is applied before
    any token bytes are written.
    """

    token_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = generate_token()
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{token_path.name}.", dir=token_path.parent
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as temporary:
            temporary.write(token)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, token_path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return token


class InputPolicy:
    """Restrict run-file paths to administrator-approved roots."""

    def __init__(self, roots: list[Path], max_bytes: int = 1_048_576):
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._roots = tuple(root.resolve() for root in roots)
        self._max_bytes = max_bytes

    def canonical_input(self, requested_path: Path) -> Path:
        try:
            candidate = requested_path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise PolicyError("run-file does not exist") from exc

        if not candidate.is_file():
            raise PolicyError("run-file must be a regular file")
        if candidate.stat().st_size > self._max_bytes:
            raise PolicyError("run-file exceeds the configured size limit")
        if candidate.stat().st_mode & 0o022:
            raise PolicyError("run-file must not be group- or world-writable")
        if not any(candidate == root or root in candidate.parents for root in self._roots):
            raise PolicyError("run-file is outside the configured input roots")
        return candidate
