"""
tools/cluster_health.py -- get_cluster_health()

Returns an overall AKS cluster health summary.
Always queries the 'aks' source type.
"""

import logging
from typing import Any

from ..prometheus_client import query_instant
from ..source_registry import get_registry

logger = logging.getLogger(__name__)

CPU_WARN_PERCENT = 80.0
MEMORY_WARN_PERCENT = 85.0
METRIC_NAME = "cluster_health"


def get_cluster_health(source_override=None):
    source = get_registry().get_for_metric(METRIC_NAME, source_override)
    warnings = []

    nodes_not_ready = _count_nodes_not_ready(source)
    if nodes_not_ready > 0:
        warnings.append(f"{nodes_not_ready} node(s) are currently NotReady.")

    unhealthy_pod_count = _count_unhealthy_pods(source)
    if unhealthy_pod_count > 0:
        warnings.append(f"{unhealthy_pod_count} pod(s) are not in Running state.")

    avg_cpu = _average_node_cpu_percent(source)
    if avg_cpu is not None and avg_cpu > CPU_WARN_PERCENT:
        warnings.append(f"Average node CPU usage is {avg_cpu:.1f}% (threshold {CPU_WARN_PERCENT}%).")

    avg_mem = _average_node_memory_percent(source)
    if avg_mem is not None and avg_mem > MEMORY_WARN_PERCENT:
        warnings.append(f"Average node memory usage is {avg_mem:.1f}% (threshold {MEMORY_WARN_PERCENT}%).")

    if nodes_not_ready > 0:
        status = "critical"
    elif warnings:
        status = "healthy_with_warnings"
    else:
        status = "healthy"

    return {
        "status": status,
        "source": source.safe_info(),
        "nodes_not_ready": nodes_not_ready,
        "unhealthy_pods": unhealthy_pod_count,
        "average_cpu_percent": avg_cpu,
        "average_memory_percent": avg_mem,
        "warnings": warnings,
    }


def _count_nodes_not_ready(source):
    try:
        results = query_instant(
            'kube_node_status_condition{condition="Ready",status="false"} == 1', source
        )
        return len(results)
    except RuntimeError:
        logger.warning("Could not retrieve node ready status. source=%s", source.name)
        return 0


def _count_unhealthy_pods(source):
    try:
        results = query_instant(
            'kube_pod_status_phase{phase!~"Running|Succeeded"} == 1', source
        )
        return len(results)
    except RuntimeError:
        logger.warning("Could not retrieve pod phase status. source=%s", source.name)
        return 0


def _average_node_cpu_percent(source):
    try:
        results = query_instant(
            '100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])))',
            source,
        )
        if not results:
            return None
        values = [float(r["value"][1]) for r in results]
        return round(sum(values) / len(values), 1)
    except (RuntimeError, KeyError, ValueError):
        logger.warning("Could not retrieve node CPU. source=%s", source.name)
        return None


def _average_node_memory_percent(source):
    try:
        results = query_instant(
            "100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)",
            source,
        )
        if not results:
            return None
        values = [float(r["value"][1]) for r in results]
        return round(sum(values) / len(values), 1)
    except (RuntimeError, KeyError, ValueError):
        logger.warning("Could not retrieve node memory. source=%s", source.name)
        return None
