"""Sensitive info redaction — mask API keys and tokens in logs/audit.

Applied before writing to audit.log, logger output, and any persisted text.
Short tokens (< 18 chars) are fully masked; long tokens show first 6 + last 4.
"""

from __future__ import annotations

import re

# ── Known API key prefixes ────────────────────────────

_KNOWN_PREFIXES: tuple[str, ...] = (
    # OpenAI / Anthropic
    r'sk-(?:ant|admin|proj)-[a-zA-Z0-9_-]+',  # prefixed variants
    r'sk-[a-zA-Z0-9]{20,}',                    # standard OpenAI key
    # GitHub
    r'gh[pousr]_[A-Za-z0-9_]{20,}',
    r'github_pat_[A-Za-z0-9_]{20,}',
    # Google
    r'AIza[0-9A-Za-z\-_]{20,}',
    # AWS
    r'AKIA[0-9A-Z]{16}',
    # Stripe
    r'(?:sk|rk)_(?:live|test)_[0-9a-zA-Z]{24,}',
    # Slack
    r'xox[baprs]-[0-9a-zA-Z\-]{10,}',
    # Generic token patterns
    r'[A-Za-z0-9+/]{40,}={0,2}',  # base64-like tokens (lower priority)
)

# ── JWT pattern ───────────────────────────────────────

_JWT_RE = re.compile(r'(eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,})')

# ── Authorization header ──────────────────────────────

_AUTH_HEADER_RE = re.compile(
    r'(Authorization|X-API-Key|api[_-]?key)\s*[:=]\s*([^\s,;]+)',
    re.IGNORECASE,
)


def redact(text: str) -> str:
    """Redact API keys, tokens, and credentials from text. Returns cleaned copy."""
    if not text:
        return text

    # JWT tokens
    text = _JWT_RE.sub(_replace_jwt, text)

    # Auth headers / key=value pairs
    text = _AUTH_HEADER_RE.sub(_replace_auth_header, text)

    # Known API key prefixes
    for prefix in _KNOWN_PREFIXES:
        pattern = re.compile(r'\b(' + prefix + r')\b')
        text = pattern.sub(_replace_token, text)

    return text


def _replace_jwt(m: re.Match) -> str:
    token = m.group(0)
    return _mask_token(token)


def _replace_auth_header(m: re.Match) -> str:
    key_name = m.group(1)
    value = m.group(2)
    return f"{key_name}: {_mask_token(value)}"


def _replace_token(m: re.Match) -> str:
    return _mask_token(m.group(0))


def _mask_token(token: str) -> str:
    """Short tokens → fully masked; long → first 6 + last 4 visible."""
    if len(token) <= 18:
        return "*" * min(len(token), 12)
    return token[:6] + "*" * (len(token) - 10) + token[-4:]
