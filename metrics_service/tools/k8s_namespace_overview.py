"""
tools/k8s_namespace_overview.py - get_k8s_namespace_overview(cluster, namespace, range, source_override)

Returns namespace-level CPU/memory usage.
All PromQL includes cluster= filter for correct results against Azure Managed Prometheus.
"""

import logging

from ..config import get_settings, validate_cluster_name, validate_label, validate_range
from ..prometheus_client import query_instant
from ..source_registry import get_registry

logger = logging.getLogger(__name__)

METRIC_NAME = "k8s_namespace_overview"


def get_k8s_namespace_overview(cluster, namespace, range="1h", source_override=None):
    settings = get_settings()
    cluster = validate_cluster_name(cluster)
    namespace = validate_label(namespace, "namespace")
    range = validate_range(range, settings.allowed_ranges_set)
    source = get_registry().get_for_metric(METRIC_NAME, source_override)

    cpu_promql = (
        f'sum(rate(container_cpu_usage_seconds_total{{'
        f'cluster="{cluster}",namespace="{namespace}",container!=""}}[{range}]))'
    )
    mem_promql = (
        f'sum(container_memory_working_set_bytes{{'
        f'cluster="{cluster}",namespace="{namespace}",container!=""}})'
    )
    cpu_cores = None
    mem_bytes = None

    try:
        results = query_instant(cpu_promql, source)
        if results:
            cpu_cores = round(float(results[0]["value"][1]), 4)
    except (RuntimeError, KeyError, ValueError):
        logger.warning("Could not retrieve CPU usage. cluster=%s namespace=%s", cluster, namespace)

    try:
        results = query_instant(mem_promql, source)
        if results:
            mem_bytes = float(results[0]["value"][1])
    except (RuntimeError, KeyError, ValueError):
        logger.warning("Could not retrieve memory usage. cluster=%s namespace=%s", cluster, namespace)

    if cpu_cores is None and mem_bytes is None:
        raise RuntimeError(
            "Could not retrieve namespace CPU or memory usage. "
            "Check Prometheus source credentials and metric availability."
        )

    mem_gb = round(mem_bytes / (1024 ** 3), 3) if mem_bytes is not None else None

    return {
        "cluster": cluster,
        "namespace": namespace,
        "range": range,
        "source": source.safe_info(),
        "cpu_cores": cpu_cores if cpu_cores is not None else 0.0,
        "memory_gb": mem_gb if mem_gb is not None else 0.0,
    }
