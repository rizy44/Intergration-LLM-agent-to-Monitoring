"""
daily_report.py — Daily Production Health Report Orchestrator.

Full pipeline:

  Kubernetes CronJob
    → collect_azure_project_metrics() — App Services, DBs, Redis, Service Bus
    → format_daily_report()           — rule-based formatter (no AI)
    → send_daily_report()             — posts to Microsoft Teams

Entry point: run_daily_report()
"""

import logging
from datetime import date
from typing import Any

from .teams_sender import send_daily_report, send_error_to_teams
from .datasources.azure_monitor.tools import (
    get_app_service_performance,
    get_mysql_performance,
    get_postgres_performance,
    get_redis_performance,
    get_service_bus_performance,
)
from . import storage
from .prod_projects import PROD_PROJECTS
from .report_formatter import _DIVIDER, format_alerts_24h_section, format_daily_report
from .config import get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Azure project metrics collection
# ---------------------------------------------------------------------------

def collect_azure_project_metrics() -> dict[str, Any]:
    """
    Collect Azure Monitor metrics for all production project resource groups.
    Per-resource failures record null and append to errors without aborting collection.
    """
    result: dict[str, Any] = {"projects": [], "errors": []}

    for project in PROD_PROJECTS:
        project_name = project["name"]
        rg = project["resource_group"]
        project_data: dict[str, Any] = {
            "name": project_name,
            "resource_group": rg,
            "app_services": [],
            "mysql": [],
            "postgres": [],
            "redis": [],
            "service_bus": [],
        }

        for app_name in project.get("app_services", []):
            project_data["app_services"].append(
                _safe_call(get_app_service_performance, rg, app_name, result["errors"],
                           label=f"{project_name}/{app_name}")
            )

        for server_name in project.get("mysql", []):
            project_data["mysql"].append(
                _safe_call(get_mysql_performance, rg, server_name, result["errors"],
                           label=f"{project_name}/{server_name}")
            )

        for server_name in project.get("postgres", []):
            project_data["postgres"].append(
                _safe_call(get_postgres_performance, rg, server_name, result["errors"],
                           label=f"{project_name}/{server_name}")
            )

        for cache_name in project.get("redis", []):
            project_data["redis"].append(
                _safe_call(get_redis_performance, rg, cache_name, result["errors"],
                           label=f"{project_name}/{cache_name}")
            )

        for ns_name in project.get("service_bus", []):
            project_data["service_bus"].append(
                _safe_call(get_service_bus_performance, rg, ns_name, result["errors"],
                           label=f"{project_name}/{ns_name}")
            )

        result["projects"].append(project_data)

    return result


def _safe_call(fn, resource_group: str, resource_name: str, errors: list, label: str) -> dict | None:
    try:
        return fn(resource_group, resource_name)
    except Exception as exc:
        logger.warning("Failed to collect metrics. resource=%s error=%s", label, exc)
        errors.append(f"{label}: unavailable")
        return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_daily_report() -> None:
    """
    Full daily report pipeline:
    1. Collect Azure project metrics (Azure Monitor REST API).
    2. Format the report using rule-based formatter.
    3. Post to Microsoft Teams.
    """
    logger.info("Starting daily production health report.")

    try:
        azure_data = collect_azure_project_metrics()
    except Exception:
        logger.exception("Unexpected error collecting Azure project metrics.")
        send_error_to_teams("The daily report could not be generated because Azure metrics collection failed.")
        return

    try:
        report_text = format_daily_report(azure_data, report_date=date.today())
    except Exception:
        logger.exception("Unexpected error formatting daily report.")
        send_error_to_teams("The daily report could not be generated because report formatting failed.")
        return

    # Alerts-24h section from the ledger (best-effort; skipped when storage off/down)
    try:
        if storage.storage_enabled():
            storage.init_db()
            alert_section = format_alerts_24h_section(storage.get_alert_summary_24h())
            if alert_section:
                report_text += f"\n\n{_DIVIDER}\n\n{alert_section}"
            purged = storage.purge_old_alerts(get_settings().alert_retention_days)
            if purged:
                logger.info("Purged %d alert episodes past retention.", purged)
    except Exception:
        logger.exception("Alert summary section failed (non-fatal); sending report without it.")

    try:
        send_daily_report(report_text)
        logger.info("Daily production health report sent to Teams successfully.")
    except RuntimeError as exc:
        logger.error("Failed to send daily report to Teams: %s", exc)


# ---------------------------------------------------------------------------
# CLI entry point (for use by the Kubernetes CronJob)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_daily_report()
    sys.exit(0)
