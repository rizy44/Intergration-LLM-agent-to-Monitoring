"""
tools/mysql.py - get_mysql_performance(resource_group, server_name, range, source_override)

Queries Azure Monitor metrics for a Microsoft.DBforMySQL/flexibleServers resource.
Supports MySQL Flexible Server only (not classic single-server).
"""

import logging

from ..azure_monitor_client import extract_metric_value, query_metrics
from ..config import get_settings, validate_azure_name, validate_range
from ..source_registry import get_azure_registry

logger = logging.getLogger(__name__)

_RESOURCE_TYPE = "Microsoft.DBforMySQL/flexibleServers"

_METRIC_NAMES = [
    "cpu_percent",
    "memory_percent",
    "io_consumption_percent",
    "active_connections",
    "queries",
    "storage_percent",
    "storage_used",
    "network_bytes_egress",
    "network_bytes_ingress",
]


def get_mysql_performance(
    resource_group: str,
    server_name: str,
    range: str = "24h",
    source_override: str | None = None,
) -> dict:
    """
    Return performance metrics for an Azure MySQL Flexible Server.

    Parameters
    ----------
    resource_group : str
        Azure resource group containing the MySQL server.
    server_name : str
        Name of the MySQL Flexible Server.
    range : str
        Time range: "1h", "6h", "12h", "24h", "2d", "7d".
    source_override : str | None
        Optional Azure Monitor source name override.

    Returns
    -------
    dict with fields:
        server_name, resource_group, range, source,
        cpu_percent_avg, memory_percent_avg, io_percent_avg,
        active_connections_avg, queries_total,
        storage_percent, storage_used_bytes,
        network_bytes_egress, network_bytes_ingress
    """
    settings = get_settings()
    validate_azure_name(resource_group, "resource_group")
    if not server_name or not server_name.strip():
        raise ValueError("server_name must not be empty.")
    validate_azure_name(server_name.strip(), "server_name")
    validate_range(range, settings.allowed_ranges_set)

    registry = get_azure_registry()
    source = registry.get_by_name(source_override) if source_override else registry.get_default()

    resource_uri = (
        f"/subscriptions/{source.subscription_id}"
        f"/resourceGroups/{resource_group}"
        f"/providers/{_RESOURCE_TYPE}/{server_name.strip()}"
    )

    raw = query_metrics(resource_uri, _METRIC_NAMES, range, source)

    return {
        "server_name": server_name.strip(),
        "resource_group": resource_group,
        "range": range,
        "source": source.safe_info(),
        "cpu_percent_avg": _avg(raw, "cpu_percent"),
        "memory_percent_avg": _avg(raw, "memory_percent"),
        "io_percent_avg": _avg(raw, "io_consumption_percent"),
        "active_connections_avg": _avg(raw, "active_connections"),
        "queries_total": _total(raw, "queries"),
        "storage_percent": _avg(raw, "storage_percent"),
        "storage_used_bytes": _avg_round(raw, "storage_used"),
        "network_bytes_egress": _total_round(raw, "network_bytes_egress"),
        "network_bytes_ingress": _total_round(raw, "network_bytes_ingress"),
    }


def _avg(raw, name):
    v = extract_metric_value(raw, name, "average")
    return round(v, 2) if v is not None else None


def _avg_round(raw, name):
    v = extract_metric_value(raw, name, "average")
    return round(v) if v is not None else None


def _total(raw, name):
    v = extract_metric_value(raw, name, "total")
    return round(v) if v is not None else None


def _total_round(raw, name):
    v = extract_metric_value(raw, name, "total")
    return round(v) if v is not None else None
