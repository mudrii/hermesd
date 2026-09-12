"""Secret redaction for URLs, argv, config values and log text."""

from __future__ import annotations

import re
import shlex
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

_OAUTH_FIELD_NAMES = {"id_token", "access_token", "refresh_token"}


_API_KEY_FIELD_NAMES = {"api_key", "secret", "token", "user_token"}


# Canonical secret-key vocabulary, matched against normalized names
# (lowercase, `_` folded to `-` — see _secret_key_name).
_SECRET_KEY_NAMES = {
    "access-token",
    "api-key",
    "auth",
    "auth-token",
    "authorization",
    "bearer",
    "client-secret",
    "credential",
    "id-token",
    "key",
    "pass",
    "passwd",
    "password",
    "pin",
    "pwd",
    "refresh-token",
    "secret",
    "token",
    "x-api-key",
    "user-token",
}


_SECRET_KEY_SUFFIXES = ("token", "key", "secret")


_SECRET_KEY_FUSED = {"apikey", "sessionid"}


def _secret_key_name(value: object) -> str:
    return str(value).strip().lower().replace("_", "-")


def _is_secret_key(normalized_key: str) -> bool:
    # Composed names match only at a `-` boundary (private-token, session-key),
    # so fused words like monkey or tokenize stay visible.
    if normalized_key in _SECRET_KEY_NAMES or normalized_key in _SECRET_KEY_FUSED:
        return True
    return any(normalized_key.endswith("-" + suffix) for suffix in _SECRET_KEY_SUFFIXES)


def _is_secret_url_query_key(key: str) -> bool:
    return _is_secret_key(_secret_key_name(key))


_SECRET_OPTION_NAMES = {
    "access-token",
    "api-key",
    "apikey",
    "auth",
    "auth-token",
    "authorization",
    "bearer",
    "client-secret",
    "credential",
    "h",
    "header",
    "id-token",
    "k",
    "key",
    "p",
    "pass",
    "passwd",
    "password",
    "pin",
    "pwd",
    "refresh-token",
    "secret",
    "t",
    "token",
    "x-api-key",
    "user-token",
}


def _normalize_secret_option_name(option: str) -> str:
    return option.lstrip("-").lower().replace("_", "-")


def _is_secret_option(option: str) -> bool:
    normalized = _normalize_secret_option_name(option)
    return normalized in _SECRET_OPTION_NAMES or _is_secret_key(normalized)


def _redact_secret_url(value: str) -> str:
    if not value:
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return _redact_malformed_url(value)
    if not parts.scheme or not parts.netloc:
        return value
    netloc = parts.netloc
    if parts.username or parts.password:
        host = parts.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = parts.port
        except ValueError:
            port = None
        if port is not None:
            host = f"{host}:{port}"
        netloc = f"[REDACTED]@{host}"
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not query_pairs:
        return urlunsplit(parts._replace(netloc=netloc))
    redacted_query = "&".join(
        f"{key}={'[REDACTED]' if _is_secret_url_query_key(key) else item_value}"
        for key, item_value in query_pairs
    )
    return urlunsplit(parts._replace(netloc=netloc, query=redacted_query))


def _redact_malformed_query_pair(pair: str) -> str:
    key, sep, _value = pair.partition("=")
    if sep and _is_secret_url_query_key(key):
        return f"{key}=[REDACTED]"
    return pair


def _redact_malformed_url(value: str) -> str:
    # urlsplit failed (e.g. an unmatched IPv6 bracket); strip credentials with
    # bounded string ops so userinfo and secret query params never pass through.
    scheme_end = value.find("://")
    if scheme_end < 0:
        return "[REDACTED]"
    authority_start = scheme_end + 3
    path_start = value.find("/", authority_start)
    if path_start < 0:
        path_start = len(value)
    authority = value[authority_start:path_start]
    if "@" in authority:
        authority = f"[REDACTED]@{authority.rsplit('@', 1)[1]}"
    rest = value[path_start:]
    query_start = rest.find("?")
    if query_start >= 0:
        redacted_pairs = [
            _redact_malformed_query_pair(p) for p in rest[query_start + 1 :].split("&")
        ]
        rest = f"{rest[: query_start + 1]}{'&'.join(redacted_pairs)}"
    return f"{value[:authority_start]}{authority}{rest}"


def _redact_secret_args(args: object) -> list[str]:
    if not isinstance(args, list):
        return []
    redacted: list[str] = []
    redact_next = False
    for raw_arg in args:
        if redact_next:
            redacted.append("[REDACTED]")
            redact_next = False
            continue
        if isinstance(raw_arg, list):
            redacted.extend(_redact_secret_args(raw_arg))
            continue
        if isinstance(raw_arg, dict):
            redacted.append(str(_redact_secret_structure(raw_arg)))
            continue
        arg = _redact_secret_url(str(raw_arg))
        if "=" in arg:
            option, _value = arg.split("=", 1)
            if _is_secret_option(option):
                redacted.append(f"{option}=[REDACTED]")
                continue
            if option.startswith(("http://", "https://")):
                redacted.append(_redact_secret_url(arg))
                continue
        if arg.startswith("-") and _is_secret_option(arg):
            redacted.append(arg)
            redact_next = True
            continue
        redacted.append(arg)
    return redacted


def _redact_secret_structure(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if _is_secret_key(_secret_key_name(key)) and item not in (None, "")
            else _redact_secret_structure(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_secret_structure(item) for item in value]
    if isinstance(value, str):
        return _redact_secret_text(value)
    return value


def _has_secret_material(data: dict[str, Any]) -> bool:
    for key, value in data.items():
        if _is_secret_key(_secret_key_name(key)) and value not in (None, ""):
            return True
        if isinstance(value, str) and _looks_like_secret_value(value):
            return True
        if isinstance(value, dict) and _has_secret_material(value):
            return True
        if isinstance(value, list) and any(_contains_secret_material(item) for item in value):
            return True
    return False


def _contains_secret_material(value: object) -> bool:
    if isinstance(value, dict):
        return _has_secret_material(value)
    if isinstance(value, list):
        return any(_contains_secret_material(item) for item in value)
    return isinstance(value, str) and _looks_like_secret_value(value)


def _looks_like_secret_value(value: str) -> bool:
    lowered = value.lower()
    return "bearer " in lowered or "authorization:" in lowered or "x-api-key" in lowered


_SECRET_TEXT_FIELD_RE = re.compile(
    r"(?P<key_quote>[\"']?)(?P<key>[A-Za-z0-9][A-Za-z0-9_-]*)(?P<key_end_quote>[\"']?)"
    r"(?P<sep>\s*[=:]\s*)"
    # Quoted values consume to the closing quote (spaces included); JSON arrays
    # to the closing bracket; bare scalars must not start with `{` so a nested
    # object's own key is matched on its own instead of being swallowed.
    r"(?P<value>\"[^\"]*\"|'[^']*'|\[[^\]]*\]|[^\s,{]+)",
    re.IGNORECASE,
)


def _redact_text_field(match: re.Match[str]) -> str:
    key = match.group("key")
    if not _is_secret_key(_secret_key_name(key)):
        return match.group(0)
    value = match.group("value")
    if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
        value = f"{value[0]}[REDACTED]{value[0]}"
    else:
        value = "[REDACTED]"
    return (
        f"{match.group('key_quote')}{key}{match.group('key_end_quote')}{match.group('sep')}{value}"
    )


def _redact_secret_text(value: str) -> str:
    redacted = re.sub(r"https?://[^,\s]+", lambda match: _redact_secret_url(match.group(0)), value)
    redacted = re.sub(r"(?i)(bearer)\s+[^,\s]+", r"\1 [REDACTED]", redacted)
    return _SECRET_TEXT_FIELD_RE.sub(_redact_text_field, redacted)


def _safe_exception_text(exc: Exception) -> str:
    try:
        return f"{type(exc).__name__}: {_redact_secret_text(str(exc))[:200]}"
    except Exception:
        # Sanitization must never raise: it runs inside error handling at the
        # collection boundary, where a secondary exception would escape.
        return type(exc).__name__


def _redact_command_string(command: str) -> str:
    if not command:
        return ""
    try:
        parts = shlex.split(command)
    except ValueError:
        return _redact_secret_text(command)
    return " ".join(_redact_secret_args(parts))
