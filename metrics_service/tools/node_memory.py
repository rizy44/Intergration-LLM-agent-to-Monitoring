"""
tools/node_memory.py - get_node_memory_usage(range, hostname, source_override)

Queries both Linux (node_exporter, job=linux-exporters) and Windows
(windows_exporter, job=windows-exporters) nodes. Optionally filters by
hostname (single) or comma-separated hostname list (converted to PromQL regex).
"""

import logging
from ..config import get_settings, validate_range
from ..prometheus_client import query_range
from ..source_registry import get_registry

logger = logging.getLogger(__name__)
METRIC_NAME = "node_memory"


def get_node_memory_usage(range="24h", hostname=None, source_override=None):
    settings = get_settings()
    range = validate_range(range, settings.allowed_ranges_set)
    source = get_registry().get_for_metric(METRIC_NAME, source_override)

    hf = _hostname_filter(hostname)

    linux_promql = (
        f"avg by (instance, hostname, os, job) "
        f"(100 * (1 - node_memory_MemAvailable_bytes{{job='linux-exporters'{hf}}}"
        f" / node_memory_MemTotal_bytes{{job='linux-exporters'{hf}}}))"
    )
    windows_promql = (
        f"avg by (instance, hostname, os, job) "
        f"((1 - (windows_os_physical_memory_free_bytes{{job='windows-exporters'{hf}}}"
        f" / windows_cs_physical_memory_bytes{{job='windows-exporters'{hf}}})) * 100)"
    )

    nodes = []

    try:
        linux_results = query_range(linux_promql, source, range_str=range, step="5m")
        nodes.extend(_parse_memory_results(linux_results))
    except RuntimeError as exc:
        logger.warning("get_node_memory_usage Linux query failed: %s", exc)
        raise

    try:
        win_results = query_range(windows_promql, source, range_str=range, step="5m")
        win_nodes = _parse_memory_results(win_results)
        if not win_nodes and not hostname:
            logger.warning(
                "get_node_memory_usage: Windows memory query returned no data. "
                "windows_cs_physical_memory_bytes may not be available."
            )
        nodes.extend(win_nodes)
    except RuntimeError as exc:
        logger.warning("get_node_memory_usage Windows query failed (non-fatal): %s", exc)

    nodes.sort(key=lambda n: n["average_memory_percent"], reverse=True)
    return {
        "range": range,
        "hostname_filter": hostname,
        "source": source.safe_info(),
        "nodes": nodes,
    }


def _hostname_filter(hostname: str | None) -> str:
    """Return a PromQL label filter string (with leading comma) for the hostname."""
    if not hostname:
        return ""
    parts = [p.strip() for p in hostname.split(",") if p.strip()]
    if len(parts) == 1:
        return f',hostname="{parts[0]}"'
    pattern = "|".join(parts)
    return f',hostname=~"{pattern}"'


def _parse_memory_results(results):
    nodes = []
    for result in results:
        metric = result.get("metric", {})
        instance = metric.get("instance", "unknown")
        hostname = metric.get("hostname") or instance
        os_type = metric.get("os", "unknown")
        job = metric.get("job", "unknown")

        values = [float(v[1]) for v in result.get("values", []) if v[1] != "NaN"]
        if not values:
            continue

        nodes.append({
            "hostname": hostname,
            "instance": instance,
            "os": os_type,
            "job": job,
            "average_memory_percent": round(sum(values) / len(values), 1),
            "max_memory_percent": round(max(values), 1),
        })
    return nodes
