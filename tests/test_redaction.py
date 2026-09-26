from __future__ import annotations

import pytest

from hermesd.collect.redaction import (
    _redact_bare_credentials,
    _redact_malformed_url,
    _redact_secret_structure,
    _redact_secret_text,
    _redact_secret_url,
)


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("GET https://api.telegram.org/bot123456789:AAHfakeTOKENabcdefghij/getUpdates", "AAHfake"),
        ("DB_PASSWORD=hunter2", "hunter2"),
        ("POSTGRES_PASSWORD: s3cr3tvalue", "s3cr3tvalue"),
        ("error sk-ant-api03-abcdefghijklmnop here", "abcdefghijklmnop"),
        ("Cookie: session=abc123", "abc123"),
        ("Set-Cookie: sid=zz9plural; Path=/", "zz9plural"),
        ("post https://discord.com/api/webhooks/123456/abcDEFtoken failed", "abcDEFtoken"),
        ("post https://hooks.slack.com/services/T000/B000/XXXXsecret failed", "XXXXsecret"),
        ("https://s3.amazonaws.com/b/k?X-Amz-Signature=abcsig&X-Amz-Date=1", "abcsig"),
        ("credentials=foo-cred", "foo-cred"),
        ("passphrase=bar-phrase", "bar-phrase"),
        ("my_passphrase: hidden-one", "hidden-one"),
    ],
)
def test_redact_secret_text_scrubs_credential_shapes(text: str, secret: str) -> None:
    redacted = _redact_secret_text(text)
    assert secret not in redacted
    assert "[REDACTED]" in redacted


def test_redact_secret_url_keeps_non_secret_path_parts() -> None:
    assert _redact_secret_url("https://hooks.slack.com/services/T000/B000/XXXXsecret") == (
        "https://hooks.slack.com/services/T000/B000/[REDACTED]"
    )
    assert _redact_secret_url("https://api.telegram.org/bot123:AAHabcdef/getUpdates") == (
        "https://api.telegram.org/bot[REDACTED]/getUpdates"
    )
    assert _redact_secret_url("https://discord.com/api/webhooks/123456/tok?wait=true") == (
        "https://discord.com/api/webhooks/123456/[REDACTED]?wait=true"
    )
    assert _redact_secret_url("https://example.com/services/a/b/c") == (
        "https://example.com/services/a/b/c"
    )


@pytest.mark.parametrize(
    "text",
    [
        "password reset failed",
        "enter your password: ",
        "task-scheduler-component-name",
        "work-in-progress-branch",
        "cookie jar is empty",
    ],
)
def test_redact_secret_text_leaves_prose_visible(text: str) -> None:
    assert _redact_secret_text(text) == text


def test_redact_bare_credentials_requires_token_boundary() -> None:
    assert _redact_secret_text("ask-abcdefghijklmnop") == "ask-abcdefghijklmnop"
    assert _redact_secret_text("(sk-abcdefghijklmnop)") == "([REDACTED])"
    # Chat-controlled free text keeps matching glued prefixes.
    assert _redact_bare_credentials("Ask-abcdefghijklmnop") == "A[REDACTED]"


def test_redact_secret_structure_redacts_password_and_cookie_keys() -> None:
    assert _redact_secret_structure(
        {"DB_PASSWORD": "x", "my_passphrase": "y", "cookie": "z", "name": "ok"}
    ) == {
        "DB_PASSWORD": "[REDACTED]",
        "my_passphrase": "[REDACTED]",
        "cookie": "[REDACTED]",
        "name": "ok",
    }


def test_truncated_structured_secret_hides_tail() -> None:
    assert _redact_secret_text('token={"a":"sec') == "token=[REDACTED]"


def test_secret_key_without_value_is_untouched() -> None:
    assert _redact_secret_text("token=") == "token="


def test_redact_url_userinfo_with_ipv6_host() -> None:
    assert _redact_secret_url("https://u:p@[::1]:8080/x") == "https://[REDACTED]@[::1]:8080/x"


def test_malformed_url_without_scheme_separator_is_fully_redacted() -> None:
    assert _redact_malformed_url("user:pw@[::1") == "[REDACTED]"


def test_malformed_url_keeps_non_secret_query_pair() -> None:
    redacted = _redact_secret_url("https://u:p@[::1/x?page=2&token=abc")
    assert "page=2" in redacted
    assert "token=[REDACTED]" in redacted
    assert "u:p" not in redacted


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Authorization: Bearer abc", "Authorization: [REDACTED]"),
        ("authorization=Bearer abc, x=1", "authorization=[REDACTED], x=1"),
        ("token: Bearer abc [REDACTED] tail", "token: [REDACTED]"),
    ],
)
def test_bearer_value_under_secret_key_leaves_no_stray_bracket(text: str, expected: str) -> None:
    assert _redact_secret_text(text) == expected
