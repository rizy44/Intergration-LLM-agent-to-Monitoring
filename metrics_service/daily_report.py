"""
daily_report.py — Daily AKS Health Report Orchestrator.

This module coordinates the full daily report flow:

  Kubernetes CronJob
    → collect_metrics()          — calls all metric tools
    → generate_report_text()     — sends to Claude AI
    → send_report()              — posts to Microsoft Teams

Entry point: run_daily_report()
"""

import logging
from typing import Any

from .ai_agent import generate_daily_report_text
from .teams_sender import send_daily_report, send_error_to_teams
from .datasources.prometheus.tools import (
    get_cluster_health,
    get_namespace_resource_usage,
    get_top_resource_consuming_pods,
    get_node_cpu_usage,
    get_node_memory_usage,
    get_pod_restart_count,
    get_service_error_rate,
    get_unhealthy_pods,
)
from .config import get_settings
from .datasources import get_registry

logger = logging.getLogger(__name__)

# Fallback namespaces to include if DAILY_REPORT_NAMESPACES is empty.
DEFAULT_NAMESPACES = ["default", "kube-system", "monitoring"]


def collect_metrics(namespaces: list[str] | None = None) -> dict[str, Any]:
    """
    Collect all relevant metrics for the daily report across all configured
    Prometheus-compatible sources.

    Returns a structured summary dict that will be sent to Claude.
    Errors from individual tools are caught and recorded without
    aborting the entire collection.
    """
    if namespaces is None:
        namespaces = _get_daily_report_namespaces()

    source_names = _get_daily_report_source_names()
    summary: dict[str, Any] = {
        "range": "24h",
        "scope": "selected_configured_sources",
        "selected_sources": source_names,
        "selected_namespaces": namespaces,
        "sources": {},
        "errors": [],
    }

    for source_name in source_names:
        source_summary = _collect_source_metrics(source_name, namespaces)
        summary["sources"][source_name] = source_summary
        summary["errors"].extend(source_summary.get("errors", []))

    return summary


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _get_daily_report_source_names() -> list[str]:
    """
    Return the source names selected for daily reports.

    DAILY_REPORT_SOURCES is a comma-separated allowlist. Keeping this explicit
    avoids sending unnecessary datasource results to Claude.
    """
    registry = get_registry()
    configured = _split_csv(get_settings().daily_report_sources)
    if not configured:
        return registry.list_names()

    missing = [name for name in configured if name not in registry.list_names()]
    if missing:
        raise ValueError(
            "DAILY_REPORT_SOURCES contains unknown source(s): "
            f"{missing}. Available sources: {registry.list_names()}"
        )
    return configured


def _get_daily_report_namespaces() -> list[str]:
    configured = _split_csv(get_settings().daily_report_namespaces)
    return configured or DEFAULT_NAMESPACES


def _collect_source_metrics(source_name: str, namespaces: list[str]) -> dict[str, Any]:
    """
    Collect the daily report metrics for one configured source.

    Each metric call passes source_override so the daily report does not depend
    on default source routing. This lets one CronJob summarize all configured
    Prometheus-compatible datasources in the same report.
    """
    source_info = get_registry().get_by_name(source_name).safe_info()
    source_summary: dict[str, Any] = {
        "source": source_info,
        "cluster_health": None,
        "node_cpu": None,
        "node_memory": None,
        "namespaces": {},
        "errors": [],
    }

    # Cluster health
    try:
        source_summary["cluster_health"] = get_cluster_health(source_override=source_name)
    except RuntimeError as exc:
        logger.warning("Failed to collect cluster health. source=%s error=%s", source_name, exc)
        source_summary["errors"].append(f"{source_name}: cluster_health unavailable")

    # Node CPU
    try:
        source_summary["node_cpu"] = get_node_cpu_usage(range="24h", source_override=source_name)
    except RuntimeError as exc:
        logger.warning("Failed to collect node CPU. source=%s error=%s", source_name, exc)
        source_summary["errors"].append(f"{source_name}: node_cpu unavailable")

    # Node memory
    try:
        source_summary["node_memory"] = get_node_memory_usage(range="24h", source_override=source_name)
    except RuntimeError as exc:
        logger.warning("Failed to collect node memory. source=%s error=%s", source_name, exc)
        source_summary["errors"].append(f"{source_name}: node_memory unavailable")

    # Per-namespace data
    for ns in namespaces:
        ns_data: dict[str, Any] = {}

        try:
            ns_data["pod_restarts"] = get_pod_restart_count(
                namespace=ns, range="24h", source_override=source_name
            )
        except (RuntimeError, ValueError) as exc:
            logger.warning(
                "Failed to collect pod restarts. source=%s namespace=%s error=%s",
                source_name, ns, exc,
            )
            ns_data["pod_restarts"] = None

        try:
            ns_data["unhealthy_pods"] = get_unhealthy_pods(
                namespace=ns, source_override=source_name
            )
        except (RuntimeError, ValueError) as exc:
            logger.warning(
                "Failed to collect unhealthy pods. source=%s namespace=%s error=%s",
                source_name, ns, exc,
            )
            ns_data["unhealthy_pods"] = None

        try:
            ns_data["resource_usage"] = get_namespace_resource_usage(
                namespace=ns, range="24h", source_override=source_name
            )
        except (RuntimeError, ValueError) as exc:
            logger.warning(
                "Failed to collect resource usage. source=%s namespace=%s error=%s",
                source_name, ns, exc,
            )
            ns_data["resource_usage"] = None

        try:
            ns_data["top_consumers"] = get_top_resource_consuming_pods(
                namespace=ns, range="24h", source_override=source_name
            )
        except (RuntimeError, ValueError) as exc:
            logger.warning(
                "Failed to collect top consumers. source=%s namespace=%s error=%s",
                source_name, ns, exc,
            )
            ns_data["top_consumers"] = None

        source_summary["namespaces"][ns] = ns_data

    return source_summary


def run_daily_report(namespaces: list[str] | None = None) -> None:
    """
    Full daily report pipeline:
    1. Collect metrics from all tools.
    2. Send metrics summary to Claude for AI analysis.
    3. Post the formatted report to Microsoft Teams.

    Catches and reports errors safely without leaking internal details.
    """
    logger.info("Starting daily AKS health report.")

    try:
        metrics_summary = collect_metrics(namespaces)
    except Exception as exc:
        logger.exception("Unexpected error during metrics collection.")
        send_error_to_teams(
            "The daily report could not be generated because metrics collection failed."
        )
        return

    try:
        report_text = generate_daily_report_text(metrics_summary)
    except RuntimeError as exc:
        logger.error("AI report generation failed: %s", exc)
        send_error_to_teams(
            "The daily report could not be generated because AI analysis failed."
        )
        return

    try:
        send_daily_report(report_text)
        logger.info("Daily AKS health report sent to Teams successfully.")
    except RuntimeError as exc:
        logger.error("Failed to send daily report to Teams: %s", exc)
        # Already logged; do not re-raise so the CronJob exits cleanly


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
