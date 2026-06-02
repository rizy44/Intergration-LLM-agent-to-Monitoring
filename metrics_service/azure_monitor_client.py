"""
azure_monitor_client.py - HTTP client for the Azure Monitor REST API.

Two operations:
  list_resources(resource_group, source)
    GET management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/resources
    Returns the raw "value" list of resource objects.

  query_metrics(resource_uri, metric_names, timespan_hours, granularity_minutes, source)
    GET management.azure.com/{resource_uri}/providers/microsoft.insights/metrics
    Returns the raw "value" list of metric objects.

Auth: Azure AD Bearer token via auth_helper.get_azure_management_token().
Timeout: prometheus_timeout_seconds from settings (shared config).
Errors: RuntimeError with safe messages — no credentials in logs or exceptions.
"""

import logging
import time

import httpx

from .auth_helper import get_azure_management_token
from .config import get_settings
from .source_registry import AzureMonitorSource

logger = logging.getLogger(__name__)

_ARM_API_RESOURCES = "2021-04-01"
_ARM_API_METRICS = "2023-10-01"


def _range_to_iso_duration(timespan_hours: int) -> str:
    """Convert hours to ISO 8601 duration string for Azure Monitor timespan."""
    if timespan_hours % 24 == 0:
        return f"P{timespan_hours // 24}D"
    return f"PT{timespan_hours}H"


def _granularity_to_iso(granularity_minutes: int) -> str:
    """Convert minutes to ISO 8601 interval string."""
    if granularity_minutes >= 60:
        return f"PT{granularity_minutes // 60}H"
    return f"PT{granularity_minutes}M"


def _range_str_to_hours(range_str: str) -> int:
    """Convert range string like '24h' or '2d' to total hours."""
    if range_str.endswith("h"):
        return int(range_str[:-1])
    if range_str.endswith("d"):
        return int(range_str[:-1]) * 24
    raise ValueError(f"Unsupported range format: '{range_str}'")


def _default_granularity(timespan_hours: int) -> int:
    """Return a sensible granularity in minutes for the given timespan."""
    if timespan_hours <= 24:
        return 5
    return 60


def _get_headers(source: AzureMonitorSource) -> dict:
    token = get_azure_management_token(source)
    return {"Authorization": f"Bearer {token}"}


def _safe_error(source: AzureMonitorSource, detail: str) -> str:
    return (
        f"Could not retrieve data from Azure Monitor source '{source.name}': {detail}"
    )


def _handle_error(exc: httpx.HTTPStatusError, source: AzureMonitorSource) -> None:
    status = exc.response.status_code
    if status == 401:
        logger.error("Azure Monitor auth failed. source=%s", source.name)
        raise RuntimeError(_safe_error(source, "authentication failed. Check AZURE_RESOURCE_* credentials."))
    if status == 404:
        raise RuntimeError(_safe_error(source, "resource not found. Check resource group and resource name."))
    if status == 429:
        logger.warning("Azure Monitor rate limited. source=%s", source.name)
        raise RuntimeError(_safe_error(source, "request was rate-limited (HTTP 429). Please retry later."))
    logger.error("Azure Monitor HTTP error. source=%s status=%s", source.name, status)
    raise RuntimeError(_safe_error(source, f"HTTP {status}."))


def list_resources(resource_group: str, source: AzureMonitorSource) -> list:
    """
    List all resources in a resource group.

    Returns the raw ARM resource list (each item has 'name', 'type', 'location', 'id').
    """
    settings = get_settings()
    url = (
        f"{source.BASE_URL}/subscriptions/{source.subscription_id}"
        f"/resourceGroups/{resource_group}/resources"
    )
    params = {"api-version": _ARM_API_RESOURCES}

    logger.debug("list_resources source=%s rg=%s", source.name, resource_group)
    try:
        response = httpx.get(
            url,
            params=params,
            headers=_get_headers(source),
            timeout=settings.prometheus_timeout_seconds,
        )
        response.raise_for_status()
    except httpx.TimeoutException:
        logger.warning("Azure Monitor list_resources timed out. source=%s rg=%s", source.name, resource_group)
        raise RuntimeError(_safe_error(source, "request timed out. Please try again."))
    except httpx.HTTPStatusError as exc:
        _handle_error(exc, source)
    except httpx.RequestError:
        logger.exception("Azure Monitor connection error. source=%s", source.name)
        raise RuntimeError(_safe_error(source, "connection failed. Check network and endpoint."))

    return response.json().get("value", [])


def query_metrics(
    resource_uri: str,
    metric_names: list,
    range_str: str,
    source: AzureMonitorSource,
    granularity_minutes: int | None = None,
) -> list:
    """
    Query Azure Monitor metrics for a specific resource.

    Parameters
    ----------
    resource_uri : str
        Full ARM resource path, e.g.
        /subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/sites/{name}
    metric_names : list[str]
        Azure Monitor metric names to retrieve.
    range_str : str
        Time range string matching allowed ranges: "1h", "6h", "12h", "24h", "2d", "7d".
    source : AzureMonitorSource
    granularity_minutes : int | None
        Override granularity. If None, auto-selected based on range.

    Returns
    -------
    list
        The "value" list from the Azure Monitor metrics response.
        Each item has "name", "timeseries", and "unit".
    """
    settings = get_settings()
    timespan_hours = _range_str_to_hours(range_str)
    granularity = granularity_minutes or _default_granularity(timespan_hours)

    timespan = _range_to_iso_duration(timespan_hours)
    interval = _granularity_to_iso(granularity)
    metricnames = ",".join(metric_names)

    url = f"{source.BASE_URL}{resource_uri}/providers/microsoft.insights/metrics"
    params = {
        "api-version": _ARM_API_METRICS,
        "metricnames": metricnames,
        "timespan": timespan,
        "interval": interval,
        "aggregation": "Average,Maximum,Total",
    }

    logger.debug(
        "query_metrics source=%s resource=%s metrics=%s range=%s",
        source.name, resource_uri, metricnames, range_str,
    )
    try:
        response = httpx.get(
            url,
            params=params,
            headers=_get_headers(source),
            timeout=settings.prometheus_timeout_seconds,
        )
        response.raise_for_status()
    except httpx.TimeoutException:
        logger.warning("Azure Monitor query_metrics timed out. source=%s", source.name)
        raise RuntimeError(_safe_error(source, "request timed out. Please try again."))
    except httpx.HTTPStatusError as exc:
        _handle_error(exc, source)
    except httpx.RequestError:
        logger.exception("Azure Monitor connection error. source=%s", source.name)
        raise RuntimeError(_safe_error(source, "connection failed. Check network and endpoint."))

    return response.json().get("value", [])


def extract_metric_value(metrics_value: list, metric_name: str, aggregation: str = "average") -> float | None:
    """
    Extract a single aggregated value from query_metrics() output.

    aggregation: "average", "total", "maximum"
    Returns the mean of all data points for that aggregation, or None if no data.
    """
    for metric in metrics_value:
        name = metric.get("name", {}).get("value", "")
        if name.lower() != metric_name.lower():
            continue
        for series in metric.get("timeseries", []):
            values = [
                dp.get(aggregation)
                for dp in series.get("data", [])
                if dp.get(aggregation) is not None
            ]
            if not values:
                return None
            if aggregation == "total":
                return sum(values)
            return sum(values) / len(values)
    return None
