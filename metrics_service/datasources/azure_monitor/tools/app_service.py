"""
datasources/azure_monitor/tools/app_service.py - get_app_service_performance(resource_group, app_name, range, source_override)

Queries Azure Monitor metrics for a Microsoft.Web/sites (App Service) resource.
"""

import logging

from ..client import extract_metric_value, query_metrics
from ....config import get_settings, validate_azure_name, validate_range
from ..registry import get_azure_registry

logger = logging.getLogger(__name__)

_RESOURCE_TYPE = "Microsoft.Web/sites"

_METRIC_NAMES = [
    "CpuTime",
    "MemoryWorkingSet",
    "Requests",
    "AverageResponseTime",
    "Http2xx",
    "Http4xx",
    "Http5xx",
    "BytesReceived",
    "BytesSent",
]


def get_app_service_performance(
    resource_group: str,
    app_name: str,
    range: str = "24h",
    source_override: str | None = None,
) -> dict:
    """
    Return performance metrics for an Azure App Service.

    Parameters
    ----------
    resource_group : str
        Azure resource group containing the App Service.
    app_name : str
        Name of the App Service (Microsoft.Web/sites resource).
    range : str
        Time range: "1h", "6h", "12h", "24h", "2d", "7d".
    source_override : str | None
        Optional Azure Monitor source name override.

    Returns
    -------
    dict with fields:
        app_name, resource_group, range, source,
        cpu_time_seconds, memory_working_set_bytes_avg,
        requests_total, avg_response_time_ms,
        http_2xx, http_4xx, http_5xx,
        bytes_received, bytes_sent,
        error_rate_percent
    """
    settings = get_settings()
    validate_azure_name(resource_group, "resource_group")
    if not app_name or not app_name.strip():
        raise ValueError("app_name must not be empty.")
    validate_azure_name(app_name.strip(), "app_name")
    validate_range(range, settings.allowed_ranges_set)

    registry = get_azure_registry()
    source = registry.get_by_name(source_override) if source_override else registry.get_default()

    resource_uri = (
        f"/subscriptions/{source.subscription_id}"
        f"/resourceGroups/{resource_group}"
        f"/providers/{_RESOURCE_TYPE}/{app_name.strip()}"
    )

    raw = query_metrics(resource_uri, _METRIC_NAMES, range, source)

    cpu_time = extract_metric_value(raw, "CpuTime", "total")
    memory = extract_metric_value(raw, "MemoryWorkingSet", "average")
    requests_total = extract_metric_value(raw, "Requests", "total") or 0.0
    avg_response = extract_metric_value(raw, "AverageResponseTime", "average")
    http_2xx = extract_metric_value(raw, "Http2xx", "total") or 0.0
    http_4xx = extract_metric_value(raw, "Http4xx", "total") or 0.0
    http_5xx = extract_metric_value(raw, "Http5xx", "total") or 0.0
    bytes_rx = extract_metric_value(raw, "BytesReceived", "total")
    bytes_tx = extract_metric_value(raw, "BytesSent", "total")

    error_rate = None
    if requests_total > 0:
        error_rate = round((http_4xx + http_5xx) / requests_total * 100, 2)

    return {
        "app_name": app_name.strip(),
        "resource_group": resource_group,
        "range": range,
        "source": source.safe_info(),
        "cpu_time_seconds": round(cpu_time, 2) if cpu_time is not None else None,
        "memory_working_set_bytes_avg": round(memory) if memory is not None else None,
        "requests_total": round(requests_total),
        "avg_response_time_ms": round(avg_response, 2) if avg_response is not None else None,
        "http_2xx": round(http_2xx),
        "http_4xx": round(http_4xx),
        "http_5xx": round(http_5xx),
        "bytes_received": round(bytes_rx) if bytes_rx is not None else None,
        "bytes_sent": round(bytes_tx) if bytes_tx is not None else None,
        "error_rate_percent": error_rate,
    }
