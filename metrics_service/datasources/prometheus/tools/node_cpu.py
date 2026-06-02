"""
datasources/prometheus/tools/node_cpu.py - get_node_cpu_usage(range, hostname, source_override)

Queries both Linux (node_exporter, job=linux-exporters) and Windows
(windows_exporter, job=windows-exporters) nodes. Optionally filters by
hostname (single) or comma-separated hostname list (converted to PromQL regex).
"""

import logging
from ....config import get_settings, validate_range
from ..client import query_range
from ..registry import get_registry

logger = logging.getLogger(__name__)
METRIC_NAME = "node_cpu"


def get_node_cpu_usage(range="24h", hostname=None, source_override=None):
    settings = get_settings()
    range = validate_range(range, settings.allowed_ranges_set)
    source = get_registry().get_for_metric(METRIC_NAME, source_override)

    hf = _hostname_filter(hostname)

    linux_promql = (
        f'100 * (1 - avg by (instance, hostname, os, job) '
        f'(rate(node_cpu_seconds_total{{mode="idle",job="linux-exporters"{hf}}}[{range}])))'
    )
    windows_promql = (
        f'100 * (1 - avg by (instance, hostname, os, job) '
        f'(rate(windows_cpu_time_total{{mode="idle",job="windows-exporters"{hf}}}[{range}])))'
    )

    nodes = []

    try:
        linux_results = query_range(linux_promql, source, range_str=range, step="5m")
        nodes.extend(_parse_cpu_results(linux_results))
    except RuntimeError as exc:
        logger.warning("get_node_cpu_usage Linux query failed: %s", exc)
        raise

    try:
        win_results = query_range(windows_promql, source, range_str=range, step="5m")
        nodes.extend(_parse_cpu_results(win_results))
    except RuntimeError as exc:
        logger.warning("get_node_cpu_usage Windows query failed (non-fatal): %s", exc)

    nodes.sort(key=lambda n: n["average_cpu_percent"], reverse=True)
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


def _parse_cpu_results(results):
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
            "average_cpu_percent": round(sum(values) / len(values), 1),
            "max_cpu_percent": round(max(values), 1),
        })
    return nodes
