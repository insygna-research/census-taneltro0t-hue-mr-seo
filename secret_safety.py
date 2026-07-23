"""Small, dependency-free helpers for keeping credentials out of durable output."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote, quote_plus


_SENSITIVE_ENV_NAME = re.compile(
    r"(?:TOKEN|SECRET|PASS(?:WORD)?|API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY|INDEXNOW_KEY)",
    re.IGNORECASE,
)
_URL_SECRET = re.compile(
    r"(?i)(\b(?:api[_-]?key|apikey|key|token|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|password|passwd)=)[^&\s\"'<>]+"
)
_MAPPING_SECRET = re.compile(
    r"""(?ix)
    (
      ["']?
      (?:api[_-]?key|apikey|token|access[_-]?token|refresh[_-]?token|
         client[_-]?secret|authorization|password|passwd)
      ["']?\s*:\s*["']?
    )
    ([^"'},\]\s]+)
    """
)
_AUTH_SECRET = re.compile(r"(?i)\b(Bearer|OAuth)\s+[A-Za-z0-9._~+/=-]+")
_TELEGRAM_BOT_URL = re.compile(
    r"(?i)(https?://api\.telegram\.org/bot)[^/\s\"'<>]+"
)


def _environment_secret_values() -> list[str]:
    """Return only plausible secret values already loaded into this process."""
    values: set[str] = set()
    for name, value in os.environ.items():
        if not value or len(value) < 6 or not _SENSITIVE_ENV_NAME.search(name):
            continue
        values.add(value)
        values.add(quote(value, safe=""))
        values.add(quote_plus(value, safe=""))
    return sorted(values, key=len, reverse=True)


def redact_secrets(value: object, replacement: str = "[REDACTED]") -> str:
    """Redact credentials from exception text, URLs, JSON-ish payloads and logs."""
    text = str(value)
    for secret in _environment_secret_values():
        if secret:
            text = text.replace(secret, replacement)
    text = _TELEGRAM_BOT_URL.sub(rf"\1{replacement}", text)
    text = _AUTH_SECRET.sub(rf"\1 {replacement}", text)
    text = _URL_SECRET.sub(rf"\1{replacement}", text)
    text = _MAPPING_SECRET.sub(rf"\1{replacement}", text)
    return text


def safe_exception(exc: BaseException, limit: int = 300) -> str:
    """Render an exception without credentials, truncating only after redaction."""
    text = f"{type(exc).__name__}: {redact_secrets(exc)}"
    return text[:limit]


def atomic_write_private(path: str | Path, text: str, *, encoding: str = "utf-8") -> None:
    """Atomically write a secret-bearing text file with POSIX mode 0600."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
        os.chmod(target, 0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)
        raise
