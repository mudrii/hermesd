"""Integration store readers: pairing, webhook subscriptions, shared metrics, rate limits.

Each store holds material hermesd must never surface — hashed pairing codes and
user ids, per-route HMAC secrets, telemetry payloads and send errors — so every
helper here reduces its input to names, counts, flags and timestamps before it
reaches a model. The collector owns path resolution and caching; these helpers
only shape already-read data.
"""

from __future__ import annotations

import math
import sqlite3
from typing import Any

from hermesd.collect.common import _age_seconds, _as_dict
from hermesd.collect.sqlite_util import _column_exists, _count_by, _count_rows, _table_exists
from hermesd.models import PairingPlatformSummary, RateLimitHold

# CODE_TTL_SECONDS (gateway/pairing.py:32): a pending code older than this is
# expired, and upstream's _cleanup_expired drops it (and malformed entries).
_PAIRING_CODE_TTL_SECONDS = 3600.0
PAIRING_SUFFIXES = ("-pending.json", "-approved.json")
# The two files record_nous_rate_limit writes (agent/nous_rate_guard.py:37-46).
RATE_LIMIT_NAMES = ("nous", "nous-anonymous")
_MAX_LISTED_NAMES = 20


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def pairing_platforms(file_names: list[str]) -> list[str]:
    """Platforms with a ``<platform>-{pending,approved}.json`` file.

    ``_all_platforms`` (``gateway/pairing.py:613-617``): ``_``-prefixed files are
    the store's shared state (rate limits, decline stamps), not platforms.
    """
    platforms = {
        name[: -len(suffix)]
        for name in file_names
        for suffix in PAIRING_SUFFIXES
        if name.endswith(suffix) and len(name) > len(suffix)
    }
    return sorted(platform for platform in platforms if not platform.startswith("_"))


def pairing_summary(
    platform: str, pending: dict[str, Any], approved: dict[str, Any], *, now: float
) -> PairingPlatformSummary:
    """Live pending codes and approvals for one platform — counts only.

    A pending entry is live while its numeric ``created_at`` is within the code
    TTL; anything else is what ``_cleanup_expired`` would delete
    (``gateway/pairing.py:356-369``). ``approved_at`` is written by
    ``_approve_user`` (``:388-395``).
    """
    live_pending = 0
    for entry in pending.values():
        created = _finite_number(_as_dict(entry).get("created_at"))
        if created is not None and now - created <= _PAIRING_CODE_TTL_SECONDS:
            live_pending += 1
    approved_stamps = [
        stamp
        for entry in approved.values()
        if (stamp := _finite_number(_as_dict(entry).get("approved_at"))) is not None
    ]
    return PairingPlatformSummary(
        platform=platform,
        pending_count=live_pending,
        approved_count=len(approved),
        newest_approved_age_seconds=(
            _age_seconds(max(approved_stamps), now) if approved_stamps else None
        ),
    )


def webhook_summary(subscriptions: dict[str, Any]) -> dict[str, Any]:
    """Route names and enabled counts from ``webhook_subscriptions.json``.

    Enabled by default; only an explicit ``enabled: false`` turns a route off
    (``hermes_cli/web_routers/ops.py:138-152``). Route secrets are never read.
    """
    names = sorted(str(name) for name in subscriptions)
    enabled = sum(
        1 for route in subscriptions.values() if _as_dict(route).get("enabled", True) is not False
    )
    return {
        "webhook_subscription_count": len(names),
        "webhook_enabled_count": enabled,
        "webhook_route_names": names[:_MAX_LISTED_NAMES],
    }


def rate_limit_hold(name: str, data: dict[str, Any], *, now: float) -> RateLimitHold | None:
    """An active hold, as ``nous_rate_limit_remaining`` decides (``:89-102``)."""
    reset_at = _finite_number(data.get("reset_at"))
    if reset_at is None or reset_at - now <= 0:
        return None
    return RateLimitHold(
        name=name,
        remaining_seconds=reset_at - now,
        recorded_age_seconds=_age_seconds(_finite_number(data.get("recorded_at")), now),
    )


# _PENDING_PERIOD_COUNT_SQL (hermes_cli/observability/shared_metrics.py:103-109).
_PENDING_PERIOD_COUNT_SQL = """
    SELECT COUNT(*) FROM (
        SELECT period_start, hermes_version, os_family, architecture, install_method
        FROM counter_aggregates WHERE value > packaged_value
        GROUP BY period_start, hermes_version, os_family, architecture, install_method
    )
"""


def shared_metrics_readout(conn: sqlite3.Connection) -> dict[str, Any]:
    """Bookkeeping counts over ``metrics.sqlite3`` — never payloads or errors.

    Tables per ``hermes_cli/observability/shared_metrics.py:38-88``; the send
    columns (``:115-132``) are additive, so an outbox without ``send_state``
    holds only eligible (pending) packages. ``NULL``/``'pending'`` both mean
    eligible upstream.
    """
    counter_rows = 0
    pending_periods = 0
    if _table_exists(conn, "counter_aggregates"):
        counter_rows = _count_rows(conn, "SELECT COUNT(*) FROM counter_aggregates")
        pending_periods = _count_rows(conn, _PENDING_PERIOD_COUNT_SQL)
    outbox: dict[str, int] = {}
    outbox_errors = 0
    if _table_exists(conn, "package_outbox"):
        if _column_exists(conn, "package_outbox", "send_state"):
            outbox = _count_by(
                conn,
                "SELECT COALESCE(NULLIF(send_state, ''), 'pending'), COUNT(*)"
                " FROM package_outbox GROUP BY 1",
            )
        else:
            total = _count_rows(conn, "SELECT COUNT(*) FROM package_outbox")
            outbox = {"pending": total} if total else {}
        if _column_exists(conn, "package_outbox", "last_error"):
            outbox_errors = _count_rows(
                conn,
                "SELECT COUNT(*) FROM package_outbox"
                " WHERE last_error IS NOT NULL AND last_error != ''",
            )
    marks: dict[str, str] = {}
    if _table_exists(conn, "consent_marks"):
        rows = conn.execute("SELECT name, stamp FROM consent_marks ORDER BY name LIMIT 8")
        marks = {str(name or ""): str(stamp or "") for name, stamp in rows.fetchall() if name}
    return {
        "shared_metrics_counter_rows": counter_rows,
        "shared_metrics_pending_periods": pending_periods,
        "shared_metrics_outbox_by_state": outbox,
        "shared_metrics_outbox_error_count": outbox_errors,
        "shared_metrics_consent_marks": marks,
    }
