"""
datasources/azure_monitor/tools/redis.py - get_redis_performance(resource_group, cache_name, range, source_override)

Queries Azure Monitor metrics for a Microsoft.Cache/Redis resource.

Azure Cache for Redis has two metric categories:
  Gauge metrics  (Average aggregation): usedmemory_percent, connectedclients,
                                        serverLoad, operationsPerSecond
  Counter metrics (Total aggregation):  cacheHits, cacheMisses

Requesting Average,Maximum,Total for Counter metrics causes HTTP 400.
We query the two groups in separate calls to avoid this.
"""

import logging

from ..client import extract_metric_value, query_metrics
from ....config import get_settings, validate_azure_name, validate_range
from ..registry import get_azure_registry

logger = logging.getLogger(__name__)

_RESOURCE_TYPE = "Microsoft.Cache/Redis"

# Gauge metrics — support Average aggregation
_GAUGE_METRICS = [
    "usedmemory_percent",
    "connectedclients",
    "serverLoad",
    "operationsPerSecond",
]

# Counter metrics — support Total aggregation only
_COUNTER_METRICS = [
    "cacheHits",
    "cacheMisses",
]


def get_redis_performance(
    resource_group: str,
    cache_name: str,
    range: str = "24h",
    source_override: str | None = None,
) -> dict:
    """
    Return performance metrics for an Azure Cache for Redis resource.

    Parameters
    ----------
    resource_group : str
        Azure resource group containing the Redis cache.
    cache_name : str
        Name of the Azure Cache for Redis resource.
    range : str
        Time range: "1h", "6h", "12h", "24h", "2d", "7d".
    source_override : str | None
        Optional Azure Monitor source name override.

    Returns
    -------
    dict with fields:
        cache_name, resource_group, range, source,
        used_memory_percent_avg, connected_clients_avg,
        server_load_avg, ops_per_second_avg,
        cache_hits_total, cache_misses_total
    """
    settings = get_settings()
    validate_azure_name(resource_group, "resource_group")
    if not cache_name or not cache_name.strip():
        raise ValueError("cache_name must not be empty.")
    validate_azure_name(cache_name.strip(), "cache_name")
    validate_range(range, settings.allowed_ranges_set)

    registry = get_azure_registry()
    source = registry.get_by_name(source_override) if source_override else registry.get_default()

    resource_uri = (
        f"/subscriptions/{source.subscription_id}"
        f"/resourceGroups/{resource_group}"
        f"/providers/{_RESOURCE_TYPE}/{cache_name.strip()}"
    )

    # Query gauge metrics (Average)
    gauge_raw = query_metrics(resource_uri, _GAUGE_METRICS, range, source, aggregation="Average,Maximum")

    # Query counter metrics (Total only — requesting Average causes HTTP 400)
    counter_raw = query_metrics(resource_uri, _COUNTER_METRICS, range, source, aggregation="Total")

    return {
        "cache_name": cache_name.strip(),
        "resource_group": resource_group,
        "range": range,
        "source": source.safe_info(),
        "used_memory_percent_avg": _avg(gauge_raw, "usedmemory_percent"),
        "connected_clients_avg":   _avg(gauge_raw, "connectedclients"),
        "server_load_avg":         _avg(gauge_raw, "serverLoad"),
        "ops_per_second_avg":      _avg(gauge_raw, "operationsPerSecond"),
        "cache_hits_total":        _total(counter_raw, "cacheHits"),
        "cache_misses_total":      _total(counter_raw, "cacheMisses"),
    }


def _avg(raw, name):
    v = extract_metric_value(raw, name, "average")
    return round(v, 2) if v is not None else None


def _total(raw, name):
    v = extract_metric_value(raw, name, "total")
    return round(v) if v is not None else None
