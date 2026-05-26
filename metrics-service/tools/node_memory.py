"""
tools/node_memory.py - get_node_memory_usage(range, source_override)
Default source type: 'aks'. Pass source_override to query VM nodes instead.
"""

import logging
from ..config import get_settings, validate_range
from ..prometheus_client import query_range
from ..source_registry import get_registry

logger = logging.getLogger(__name__)
METRIC_NAME = "node_memory"


def get_node_memory_usage(range="24h", source_override=None):
    settings = get_settings()
    range = validate_range(range, settings.allowed_ranges_set)
    source = get_registry().get_for_metric(METRIC_NAME, source_override)

    promql = "100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)"

    try:
        results = query_range(promql, source, range_str=range, step="5m")
    except RuntimeError as exc:
        logger.warning("get_node_memory_usage failed: %s", exc)
        raise

    nodes = []
    for result in results:
        instance = result.get("metric", {}).get("instance", "unknown")
        values = [float(v[1]) for v in result.get("values", []) if v[1] != "NaN"]
        if not values:
            continue
        nodes.append({
            "node": instance,
            "average_memory_percent": round(sum(values) / len(values), 1),
            "max_memory_percent": round(max(values), 1),
        })

    nodes.sort(key=lambda n: n["average_memory_percent"], reverse=True)
    return {"range": range, "source": source.safe_info(), "nodes": nodes}
