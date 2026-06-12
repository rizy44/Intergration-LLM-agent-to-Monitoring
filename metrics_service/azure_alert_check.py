"""
azure_alert_check.py — 15-minute Azure metrics poll alerting.

Flow (triggered by CronJob → POST /alerts/azure-check):

  PROD_PROJECTS resources
    → Azure Monitor tools (range = AZURE_ALERT_RANGE, default 1h)
    → rule-based threshold evaluation (NO LLM)
    → firing/resolved state with cooldown (in-memory, service process)
    → alert_formatter (synthetic Alertmanager-style payload)
    → send_alert_to_teams() — dedicated Teams alert channel

State note: cooldown state lives in this module's memory and is reset when
the service restarts (worst case: one duplicate FIRING notification).

Security rules:
- Collection failures are logged and skipped — never turned into alerts.
- Null metric values mean "no breach".
- No secrets in messages or logs.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import storage
from .alert_formatter import format_alertmanager_payload
from .config import get_settings
from .datasources.azure_monitor.tools import (
    get_app_service_performance,
    get_mysql_performance,
    get_postgres_performance,
    get_redis_performance,
    get_service_bus_performance,
)
from .prod_projects import PROD_PROJECTS
from .teams_sender import send_alert_to_teams

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rule table: resource type → list of (alertname, field, threshold-getter, severity)
# Thresholds come from settings so operators tune env vars, not code.
# ---------------------------------------------------------------------------

_RULES: dict[str, list[tuple[str, str, Callable[[Any], float], str]]] = {
    "app_services": [
        ("AppServiceHighErrorRate", "error_rate_percent",
         lambda s: s.azure_alert_app_error_rate, "critical"),
        ("AppServiceSlowResponse", "avg_response_time_ms",
         lambda s: s.azure_alert_app_response_ms, "warning"),
    ],
    "mysql": [
        ("DatabaseHighCPU", "cpu_percent_avg", lambda s: s.azure_alert_db_cpu, "warning"),
        ("DatabaseHighMemory", "memory_percent_avg", lambda s: s.azure_alert_db_memory, "warning"),
        ("DatabaseHighStorage", "storage_percent", lambda s: s.azure_alert_db_storage, "critical"),
    ],
    "postgres": [
        ("DatabaseHighCPU", "cpu_percent_avg", lambda s: s.azure_alert_db_cpu, "warning"),
        ("DatabaseHighMemory", "memory_percent_avg", lambda s: s.azure_alert_db_memory, "warning"),
        ("DatabaseHighStorage", "storage_percent", lambda s: s.azure_alert_db_storage, "critical"),
    ],
    "redis": [
        ("RedisHighServerLoad", "server_load_avg", lambda s: s.azure_alert_redis_load, "warning"),
        ("RedisHighMemory", "used_memory_percent_avg", lambda s: s.azure_alert_redis_memory, "warning"),
    ],
    "service_bus": [
        ("ServiceBusDeadLetters", "deadlettered_messages_avg",
         lambda s: s.azure_alert_sb_deadletter, "warning"),
        ("ServiceBusServerErrors", "server_errors_total",
         lambda s: s.azure_alert_sb_server_errors, "critical"),
    ],
}

_COLLECTORS: dict[str, tuple[Callable[..., dict], str]] = {
    # resource type → (tool function, name field in the tool result)
    "app_services": (get_app_service_performance, "app_name"),
    "mysql": (get_mysql_performance, "server_name"),
    "postgres": (get_postgres_performance, "server_name"),
    "redis": (get_redis_performance, "cache_name"),
    "service_bus": (get_service_bus_performance, "namespace_name"),
}

# In-memory alert state: (project, resource, alertname) → {"last_sent": datetime}
# Used as primary store when DATABASE_URL is unset, and as fallback when
# the database is unavailable (best-effort semantics).
_alert_state: dict[tuple[str, str, str], dict[str, Any]] = {}


def reset_alert_state() -> None:
    """Clear all alert state (used by tests)."""
    _alert_state.clear()


def _state_key(key: tuple[str, str, str]) -> str:
    project, resource, alertname = key
    return f"azure|{project}|{resource}|{alertname}"


def _fingerprint(key: tuple[str, str, str]) -> str:
    project, resource, alertname = key
    return f"azure|{project}|{resource}|{alertname}"


def _get_last_sent(key: tuple[str, str, str]):
    """Cooldown lookup: DB first (persistent), in-memory fallback."""
    if storage.storage_enabled():
        state = storage.get_state(_state_key(key))
        if state is not None:
            return state["last_sent"]
    state = _alert_state.get(key)
    return state["last_sent"] if state else None


def _set_last_sent(key: tuple[str, str, str], now) -> None:
    _alert_state[key] = {"last_sent": now}
    if storage.storage_enabled():
        existing = storage.get_state(_state_key(key))
        firing_since = existing["firing_since"] if existing else now
        storage.set_state(_state_key(key), firing_since=firing_since, last_sent=now)


def _clear_state(key: tuple[str, str, str]) -> None:
    _alert_state.pop(key, None)
    if storage.storage_enabled():
        storage.delete_state(_state_key(key))


def run_azure_alert_check() -> dict[str, Any]:
    """
    Run one evaluation cycle over all PROD_PROJECTS Azure resources.

    Returns a summary dict:
      {"evaluated": int, "firing": int, "resolved": int,
       "suppressed": int, "errors": int}
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    cooldown = timedelta(minutes=settings.alert_repeat_minutes)

    summary = {"evaluated": 0, "firing": 0, "resolved": 0, "suppressed": 0, "errors": 0}
    firing_alerts: list[dict] = []
    resolved_alerts: list[dict] = []
    breached_keys: set[tuple[str, str, str]] = set()
    evaluated_keys: set[tuple[str, str, str]] = set()

    for project in PROD_PROJECTS:
        project_name = project["name"]
        rg = project["resource_group"]

        for rtype, (tool_fn, name_field) in _COLLECTORS.items():
            for resource_name in project.get(rtype, []):
                try:
                    data = tool_fn(rg, resource_name, range=settings.azure_alert_range)
                except Exception as exc:
                    summary["errors"] += 1
                    logger.warning(
                        "Azure alert check: collection failed. project=%s resource=%s error=%s",
                        project_name, resource_name, type(exc).__name__,
                    )
                    continue

                summary["evaluated"] += 1

                for alertname, field, threshold_fn, severity in _RULES[rtype]:
                    key = (project_name, resource_name, alertname)
                    evaluated_keys.add(key)
                    threshold = threshold_fn(settings)
                    value = data.get(field)

                    if value is not None and float(value) > threshold:
                        breached_keys.add(key)
                        # Episode ledger: idempotent — only opens a row if none open
                        storage.record_firing(
                            source="azure",
                            fingerprint=_fingerprint(key),
                            alertname=alertname,
                            severity=severity,
                            project=project_name,
                            resource=resource_name,
                            metric_field=field,
                            value=float(value),
                            threshold=threshold,
                            summary=(
                                f"{project_name}/{resource_name}: {field} is {value} "
                                f"(threshold: {threshold})."
                            ),
                            starts_at=now,
                        )
                        last_sent = _get_last_sent(key)
                        if last_sent is not None and now - last_sent < cooldown:
                            summary["suppressed"] += 1
                            continue
                        _set_last_sent(key, now)
                        summary["firing"] += 1
                        firing_alerts.append(_synthetic_alert(
                            alertname, severity, project_name, resource_name,
                            field, value, threshold, now,
                        ))

    # Resolved: evaluated keys that had state (memory or DB) and are no longer breached
    for key in evaluated_keys - breached_keys:
        had_state = key in _alert_state or (
            storage.storage_enabled()
            and storage.get_state(_state_key(key)) is not None
        )
        if had_state:
            project_name, resource_name, alertname = key
            _clear_state(key)
            storage.record_resolved(_fingerprint(key), resolved_at=now)
            summary["resolved"] += 1
            resolved_alerts.append(_synthetic_alert(
                alertname, "info", project_name, resource_name,
                None, None, None, now, resolved=True,
            ))

    _send(firing_alerts, "firing")
    _send(resolved_alerts, "resolved")

    logger.info(
        "Azure alert check done. evaluated=%d firing=%d resolved=%d suppressed=%d errors=%d",
        summary["evaluated"], summary["firing"], summary["resolved"],
        summary["suppressed"], summary["errors"],
    )
    return summary


def _synthetic_alert(
    alertname: str,
    severity: str,
    project: str,
    resource: str,
    field: str | None,
    value: Any,
    threshold: Any,
    now: datetime,
    resolved: bool = False,
) -> dict:
    """Build an Alertmanager-style alert dict for the shared formatter."""
    if resolved:
        summary = f"{project}/{resource}: {alertname} has recovered."
    else:
        summary = (
            f"{project}/{resource}: {field} is {value} "
            f"(threshold: {threshold}, window: {get_settings().azure_alert_range})."
        )
    return {
        "status": "resolved" if resolved else "firing",
        "labels": {
            "alertname": alertname,
            "severity": severity,
            "service": resource,
            "namespace": project,
        },
        "annotations": {"summary": summary},
        "startsAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _send(alerts: list[dict], status: str) -> None:
    """Format and deliver one grouped Teams message; never raise."""
    if not alerts:
        return
    payload = {"version": "4", "status": status, "alerts": alerts}
    try:
        title, body = format_alertmanager_payload(payload)
        send_alert_to_teams(body, title=f"Azure {title}")
    except RuntimeError:
        logger.error("Azure alert check: could not deliver %s notification to Teams.", status)
