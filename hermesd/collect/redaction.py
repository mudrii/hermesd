"""Secret redaction for URLs, argv, config values and log text."""

from __future__ import annotations

import re
import shlex
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

_SECRET_FIELD_NAMES = {
    "api_key",
    "id_token",
    "access_token",
    "authorization",
    "bearer",
    "client_secret",
    "credential",
    "pass",
    "passwd",
    "pin",
    "pwd",
    "refresh_token",
    "secret",
    "token",
    "x_api_key",
    "x-api-key",
    "user_token",
}


_OAUTH_FIELD_NAMES = {"id_token", "access_token", "refresh_token"}


_API_KEY_FIELD_NAMES = {"api_key", "secret", "token", "user_token"}


_SECRET_URL_QUERY_KEYS = {
    "access_token",
    "api_key",
    "auth",
    "auth_token",
    "authorization",
    "bearer",
    "client_secret",
    "credential",
    "id_token",
    "key",
    "pass",
    "passwd",
    "password",
    "pin",
    "pwd",
    "refresh_token",
    "secret",
    "token",
    "x_api_key",
    "x-api-key",
    "user_token",
}


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


def _secret_key_name(value: object) -> str:
    return str(value).strip().lower().replace("_", "-")


def _redact_secret_url(value: str) -> str:
    if not value:
        return ""
    parts = urlsplit(value)
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
        f"{key}={'[REDACTED]' if key.lower() in _SECRET_URL_QUERY_KEYS else item_value}"
        for key, item_value in query_pairs
    )
    return urlunsplit(parts._replace(netloc=netloc, query=redacted_query))


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
        if isinstance(raw_arg, dict) and _has_secret_material(raw_arg):
            redacted.append("[REDACTED]")
            continue
        arg = _redact_secret_url(str(raw_arg))
        if "=" in arg:
            option, _value = arg.split("=", 1)
            if _normalize_secret_option_name(option) in _SECRET_OPTION_NAMES:
                redacted.append(f"{option}=[REDACTED]")
                continue
            if option.startswith(("http://", "https://")):
                redacted.append(_redact_secret_url(arg))
                continue
        if arg.startswith("-") and _normalize_secret_option_name(arg) in _SECRET_OPTION_NAMES:
            redacted.append(arg)
            redact_next = True
            continue
        redacted.append(arg)
    return redacted


def _has_secret_material(data: dict[str, Any]) -> bool:
    for key, value in data.items():
        key_name = _secret_key_name(key)
        if (
            key_name in _SECRET_FIELD_NAMES
            or key_name in _SECRET_OPTION_NAMES
            or key_name in _SECRET_URL_QUERY_KEYS
        ) and value not in (None, ""):
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


def _redact_secret_text(value: str) -> str:
    redacted = re.sub(r"https?://[^,\s]+", lambda match: _redact_secret_url(match.group(0)), value)
    redacted = re.sub(r"(?i)(bearer)\s+[^,\s]+", r"\1 [REDACTED]", redacted)
    return re.sub(
        r"(?i)(access[-_]?token|api[-_]?key|authorization|client[-_]?secret|credential|"
        r"pass(?:word|wd)?|pwd|pin|refresh[-_]?token|secret|token|x[-_]?api[-_]?key)"
        r"([=:]\s*)[^,\s]+",
        r"\1\2[REDACTED]",
        redacted,
    )


def _safe_exception_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {_redact_secret_text(str(exc))[:200]}"


def _redact_command_string(command: str) -> str:
    if not command:
        return ""
    try:
        parts = shlex.split(command)
    except ValueError:
        return _redact_secret_text(command)
    return " ".join(_redact_secret_args(parts))
