"""Secret redaction for URLs, argv, config values and log text."""

from __future__ import annotations

import json
import re
import shlex
from typing import Any
from urllib.parse import parse_qsl, unquote_plus, urlsplit, urlunsplit

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
    if sep and _is_secret_url_query_key(unquote_plus(key)):
        return f"{key}=[REDACTED]"
    return pair


def _redact_malformed_url(value: str) -> str:
    # urlsplit failed (e.g. an unmatched IPv6 bracket); strip credentials with
    # bounded string ops so userinfo and secret query params never pass through.
    scheme_end = value.find("://")
    if scheme_end < 0:
        return "[REDACTED]"
    authority_start = scheme_end + 3
    path_start = min(
        (pos for sep in "/?#" if (pos := value.find(sep, authority_start)) >= 0),
        default=len(value),
    )
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
            option, option_value = arg.split("=", 1)
            if _is_secret_option(option):
                redacted.append(f"{option}=[REDACTED]")
                continue
            if "://" in option_value:
                redacted.append(f"{option}={_redact_secret_url(option_value)}")
                continue
            if "://" in option:
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
    r"(?P<sep>\s*[=:]\s*)",
    re.IGNORECASE,
)
# Bare (unquoted) values consume to a top-level `,`, `}`, `]`, or end-of-line
# so multi-word secrets cannot leak their tail. A redacted URL that follows the
# value stays visible: URLs are sanitized by the pre-pass in _redact_secret_text
# before field redaction runs.
_SECRET_TEXT_VALUE_RE = re.compile(
    r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|(?:(?!\s+(?i:[a-z][a-z0-9+.-]*)://)[^,}\]\r\n])+"""
)


def _redact_text_fields(text: str) -> str:
    pieces: list[str] = []
    consumed = 0
    for match in _SECRET_TEXT_FIELD_RE.finditer(text):
        if match.start() < consumed or not _is_secret_key(_secret_key_name(match.group("key"))):
            continue
        start = match.end()
        if text.startswith("[REDACTED]", start):
            continue
        if text[start : start + 1] in ("{", "["):
            try:
                _, end = json.JSONDecoder().raw_decode(text, start)
            except (ValueError, RecursionError):
                # An incomplete structured secret cannot safely expose its tail.
                end = len(text)
        else:
            value_match = _SECRET_TEXT_VALUE_RE.match(text, start)
            if value_match is None:
                continue
            end = value_match.end()
        quote = text[start : start + 1]
        if quote in ('"', "'") and (end <= start + 1 or text[end - 1] != quote):
            end = len(text)
        replacement = f"{quote}[REDACTED]{quote}" if quote in ('"', "'") else "[REDACTED]"
        pieces.extend((text[consumed:start], replacement))
        consumed = end
    return "".join((*pieces, text[consumed:]))


def _redact_secret_text(value: str) -> str:
    if value.lstrip().startswith(("{", "[")):
        try:
            structured = json.loads(value)
            return json.dumps(_redact_secret_structure(structured), ensure_ascii=False)
        except Exception:
            # Sanitization must never raise (deeply nested structures exceed the
            # recursion limit during redact/re-serialize even when parsing was
            # guarded): fail closed to the line-oriented path below, which
            # bounds its own structured reads.
            pass
    redacted = re.sub(
        r"(?i)[a-z][a-z0-9+.-]*://[^,\s]+", lambda match: _redact_secret_url(match.group(0)), value
    )
    redacted = re.sub(r"(?i)(bearer)\s+[^,\s]+", r"\1 [REDACTED]", redacted)
    return _redact_text_fields(redacted)


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
