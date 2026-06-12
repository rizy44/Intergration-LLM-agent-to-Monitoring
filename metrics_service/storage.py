"""
storage.py — PostgreSQL alert ledger + persistent cooldown state.

Design (see assets/db/database-design.md):
- `alerts`      : episode ledger — 1 row per alert episode (firing → resolved).
- `alert_state` : mutable cooldown state for the Azure Alert Evaluator.

Principles:
- BEST-EFFORT: storage must never block or break alert delivery. Every
  public function swallows exceptions (logged as warnings) and returns a
  safe default.
- DISABLED-BY-DEFAULT: when DATABASE_URL is empty, all functions no-op and
  callers fall back to in-memory behavior.
- PORTABLE: SQLAlchemy Core; tests run on SQLite, production on PostgreSQL.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    create_engine,
    delete,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB

from .config import get_settings

logger = logging.getLogger(__name__)

_metadata = MetaData()

alerts_table = Table(
    "alerts",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source", Text, nullable=False),          # kubernetes | azure
    Column("fingerprint", Text, nullable=False),
    Column("alertname", Text, nullable=False),
    Column("severity", Text, nullable=False, default="warning"),
    Column("project", Text),                          # azure project / k8s namespace
    Column("resource", Text),
    Column("status", Text, nullable=False),           # firing | resolved
    Column("metric_field", Text),
    Column("value", Float),
    Column("threshold", Float),
    Column("summary", Text),
    Column("payload", JSON().with_variant(JSONB, "postgresql")),
    Column("starts_at", DateTime(timezone=True), nullable=False),
    Column("resolved_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False,
           server_default=func.now()),
)

# One open episode per fingerprint
Index(
    "uq_alerts_open_episode",
    alerts_table.c.fingerprint,
    unique=True,
    postgresql_where=text("resolved_at IS NULL"),
    sqlite_where=text("resolved_at IS NULL"),
)
Index("idx_alerts_created_at", alerts_table.c.created_at.desc())
Index("idx_alerts_name_resource", alerts_table.c.alertname, alerts_table.c.resource)

alert_state_table = Table(
    "alert_state",
    _metadata,
    Column("state_key", Text, primary_key=True),
    Column("firing_since", DateTime(timezone=True), nullable=False),
    Column("last_sent", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False,
           server_default=func.now()),
)

_engine = None
_init_done = False


def storage_enabled() -> bool:
    """True when DATABASE_URL is configured."""
    return bool(get_settings().database_url)


def _get_engine():
    global _engine
    if _engine is None:
        url = get_settings().database_url
        if not url:
            return None
        _engine = create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=2)
    return _engine


def reset_engine() -> None:
    """Dispose and forget the engine (used by tests)."""
    global _engine, _init_done
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _init_done = False


def init_db() -> bool:
    """
    Create tables if they do not exist. Returns True on success.
    Safe to call multiple times; never raises.
    """
    global _init_done
    if not storage_enabled():
        return False
    try:
        engine = _get_engine()
        _metadata.create_all(engine)
        _init_done = True
        logger.info("Alert storage initialised.")
        return True
    except Exception as exc:
        logger.warning("Alert storage init failed (continuing without): %s",
                       type(exc).__name__)
        return False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Episode ledger
# ---------------------------------------------------------------------------


def record_firing(
    source: str,
    fingerprint: str,
    alertname: str,
    severity: str = "warning",
    project: str | None = None,
    resource: str | None = None,
    metric_field: str | None = None,
    value: float | None = None,
    threshold: float | None = None,
    summary: str | None = None,
    payload: dict | None = None,
    starts_at: datetime | None = None,
) -> bool:
    """
    Open an episode for *fingerprint* if none is open. Idempotent:
    repeated firing (cooldown re-notifications) does not duplicate rows.
    """
    if not storage_enabled():
        return False
    try:
        engine = _get_engine()
        with engine.begin() as conn:
            open_episode = conn.execute(
                select(alerts_table.c.id)
                .where(alerts_table.c.fingerprint == fingerprint)
                .where(alerts_table.c.resolved_at.is_(None))
            ).first()
            if open_episode:
                return True
            conn.execute(alerts_table.insert().values(
                source=source,
                fingerprint=fingerprint,
                alertname=alertname,
                severity=severity or "warning",
                project=project,
                resource=resource,
                status="firing",
                metric_field=metric_field,
                value=value,
                threshold=threshold,
                summary=summary,
                payload=payload,
                starts_at=starts_at or _utcnow(),
            ))
        return True
    except Exception as exc:
        logger.warning("record_firing failed (non-fatal): %s", type(exc).__name__)
        return False


def record_resolved(fingerprint: str, resolved_at: datetime | None = None) -> bool:
    """Close the open episode for *fingerprint* (if any)."""
    if not storage_enabled():
        return False
    try:
        engine = _get_engine()
        with engine.begin() as conn:
            conn.execute(
                update(alerts_table)
                .where(alerts_table.c.fingerprint == fingerprint)
                .where(alerts_table.c.resolved_at.is_(None))
                .values(status="resolved", resolved_at=resolved_at or _utcnow())
            )
        return True
    except Exception as exc:
        logger.warning("record_resolved failed (non-fatal): %s", type(exc).__name__)
        return False


def get_recent_alerts(hours: int = 24, limit: int = 50) -> list[dict[str, Any]] | None:
    """
    Episodes created in the last *hours*. Returns None when storage is
    disabled or unavailable (callers must handle).
    """
    if not storage_enabled():
        return None
    try:
        engine = _get_engine()
        since = _utcnow() - timedelta(hours=hours)
        with engine.connect() as conn:
            rows = conn.execute(
                select(
                    alerts_table.c.alertname,
                    alerts_table.c.severity,
                    alerts_table.c.source,
                    alerts_table.c.project,
                    alerts_table.c.resource,
                    alerts_table.c.status,
                    alerts_table.c.value,
                    alerts_table.c.threshold,
                    alerts_table.c.summary,
                    alerts_table.c.starts_at,
                    alerts_table.c.resolved_at,
                )
                .where(alerts_table.c.created_at > since)
                .order_by(alerts_table.c.created_at.desc())
                .limit(limit)
            ).mappings().all()
        result = []
        for r in rows:
            d = dict(r)
            for k in ("starts_at", "resolved_at"):
                if d.get(k) is not None:
                    d[k] = d[k].isoformat()
            result.append(d)
        return result
    except Exception as exc:
        logger.warning("get_recent_alerts failed (non-fatal): %s", type(exc).__name__)
        return None


def get_alert_summary_24h() -> dict[str, Any] | None:
    """
    Summary for the daily report: episode counts by severity, still-firing
    count, top recurring (alertname, resource). None when unavailable.
    """
    if not storage_enabled():
        return None
    try:
        engine = _get_engine()
        since = _utcnow() - timedelta(hours=24)
        with engine.connect() as conn:
            by_severity = {
                r.severity: r.cnt
                for r in conn.execute(
                    select(alerts_table.c.severity, func.count().label("cnt"))
                    .where(alerts_table.c.created_at > since)
                    .group_by(alerts_table.c.severity)
                )
            }
            still_firing = conn.execute(
                select(func.count())
                .where(alerts_table.c.created_at > since)
                .where(alerts_table.c.resolved_at.is_(None))
            ).scalar() or 0
            top = [
                {"alertname": r.alertname, "resource": r.resource, "times": r.cnt}
                for r in conn.execute(
                    select(
                        alerts_table.c.alertname,
                        alerts_table.c.resource,
                        func.count().label("cnt"),
                    )
                    .where(alerts_table.c.created_at > since)
                    .group_by(alerts_table.c.alertname, alerts_table.c.resource)
                    .order_by(func.count().desc())
                    .limit(5)
                )
            ]
        return {
            "total": sum(by_severity.values()),
            "by_severity": by_severity,
            "still_firing": still_firing,
            "top_alerts": top,
        }
    except Exception as exc:
        logger.warning("get_alert_summary_24h failed (non-fatal): %s", type(exc).__name__)
        return None


def purge_old_alerts(days: int) -> int:
    """Delete episodes older than *days*. Returns number deleted (0 on failure)."""
    if not storage_enabled():
        return 0
    try:
        engine = _get_engine()
        cutoff = _utcnow() - timedelta(days=days)
        with engine.begin() as conn:
            result = conn.execute(
                delete(alerts_table).where(alerts_table.c.created_at < cutoff)
            )
        return result.rowcount or 0
    except Exception as exc:
        logger.warning("purge_old_alerts failed (non-fatal): %s", type(exc).__name__)
        return 0


# ---------------------------------------------------------------------------
# Cooldown state (alert_state)
# ---------------------------------------------------------------------------


def get_state(state_key: str) -> dict[str, datetime] | None:
    """Return {'firing_since', 'last_sent'} or None (no state / unavailable)."""
    if not storage_enabled():
        return None
    try:
        engine = _get_engine()
        with engine.connect() as conn:
            row = conn.execute(
                select(alert_state_table.c.firing_since, alert_state_table.c.last_sent)
                .where(alert_state_table.c.state_key == state_key)
            ).first()
        if row is None:
            return None
        firing_since, last_sent = row
        # SQLite returns naive datetimes — normalize to UTC
        if last_sent.tzinfo is None:
            last_sent = last_sent.replace(tzinfo=timezone.utc)
        if firing_since.tzinfo is None:
            firing_since = firing_since.replace(tzinfo=timezone.utc)
        return {"firing_since": firing_since, "last_sent": last_sent}
    except Exception as exc:
        logger.warning("get_state failed (non-fatal): %s", type(exc).__name__)
        return None


def set_state(state_key: str, firing_since: datetime, last_sent: datetime) -> bool:
    """Upsert cooldown state."""
    if not storage_enabled():
        return False
    try:
        engine = _get_engine()
        with engine.begin() as conn:
            updated = conn.execute(
                update(alert_state_table)
                .where(alert_state_table.c.state_key == state_key)
                .values(firing_since=firing_since, last_sent=last_sent,
                        updated_at=_utcnow())
            )
            if updated.rowcount == 0:
                conn.execute(alert_state_table.insert().values(
                    state_key=state_key,
                    firing_since=firing_since,
                    last_sent=last_sent,
                ))
        return True
    except Exception as exc:
        logger.warning("set_state failed (non-fatal): %s", type(exc).__name__)
        return False


def delete_state(state_key: str) -> bool:
    """Remove cooldown state (alert resolved)."""
    if not storage_enabled():
        return False
    try:
        engine = _get_engine()
        with engine.begin() as conn:
            conn.execute(
                delete(alert_state_table)
                .where(alert_state_table.c.state_key == state_key)
            )
        return True
    except Exception as exc:
        logger.warning("delete_state failed (non-fatal): %s", type(exc).__name__)
        return False
