"""
tools/namespace_usage.py -- get_namespace_resource_usage / get_top_resource_consuming_pods
"""

import logging
from typing import Any

from ..config import get_settings, validate_label, validate_range
from ..prometheus_client import query_instant
from ..source_registry import get_registry

logger = logging.getLogger(__name__)

TOP_N = 10


def get_namespace_resource_usage(namespace, range="24h", source_override=None):
    settings = get_settings()
    namespace = validate_label(namespace, "namespace")
    range = validate_range(range, settings.allowed_ranges_set)
    source = get_registry().get_for_metric("namespace_usage", source_override)

    cpu_promql = (
        f'sum(rate(container_cpu_usage_seconds_total'
        f'{{namespace="{namespace}",container!=""}}[{range}]))'
    )
    mem_promql = (
        f'avg_over_time(sum(container_memory_working_set_bytes'
        f'{{namespace="{namespace}",container!=""}}) [{range}])'
    )

    avg_cpu = None
    avg_mem_bytes = None

    try:
        cpu_results = query_instant(cpu_promql, source)
        if cpu_results:
            avg_cpu = round(float(cpu_results[0]["value"][1]), 4)
    except (RuntimeError, KeyError, ValueError):
        logger.warning("Could not retrieve CPU usage. namespace=%s source=%s", namespace, source.name)

    try:
        mem_results = query_instant(mem_promql, source)
        if mem_results:
            avg_mem_bytes = float(mem_results[0]["value"][1])
    except (RuntimeError, KeyError, ValueError):
        logger.warning("Could not retrieve memory usage. namespace=%s source=%s", namespace, source.name)

    avg_mem_mb = round(avg_mem_bytes / (1024 * 1024), 1) if avg_mem_bytes is not None else None

    return {
        "namespace": namespace,
        "range": range,
        "source": source.safe_info(),
        "average_cpu_cores": avg_cpu,
        "average_memory_bytes": int(avg_mem_bytes) if avg_mem_bytes is not None else None,
        "average_memory_mb": avg_mem_mb,
    }


def get_top_resource_consuming_pods(namespace, range="1h", source_override=None):
    settings = get_settings()
    namespace = validate_label(namespace, "namespace")
    range = validate_range(range, settings.allowed_ranges_set)
    source = get_registry().get_for_metric("top_consumers", source_override)

    cpu_promql = (
        f'topk({TOP_N}, sum by (pod) (rate(container_cpu_usage_seconds_total'
        f'{{namespace="{namespace}",container!=""}}[{range}])))'
    )
    mem_promql = (
        f'topk({TOP_N}, sum by (pod) (avg_over_time('
        f'container_memory_working_set_bytes'
        f'{{namespace="{namespace}",container!=""}}[{range}])))'
    )

    top_cpu = []
    top_mem = []

    try:
        for r in query_instant(cpu_promql, source):
            pod = r.get("metric", {}).get("pod", "unknown")
            try:
                top_cpu.append({"pod": pod, "cpu_cores": round(float(r["value"][1]), 4)})
            except (KeyError, ValueError):
                continue
        top_cpu.sort(key=lambda x: x["cpu_cores"], reverse=True)
    except RuntimeError:
        logger.warning("Could not retrieve top CPU pods. namespace=%s source=%s", namespace, source.name)

    try:
        for r in query_instant(mem_promql, source):
            pod = r.get("metric", {}).get("pod", "unknown")
            try:
                top_mem.append({"pod": pod, "memory_mb": round(float(r["value"][1]) / (1024 * 1024), 1)})
            except (KeyError, ValueError):
                continue
        top_mem.sort(key=lambda x: x["memory_mb"], reverse=True)
    except RuntimeError:
        logger.warning("Could not retrieve top memory pods. namespace=%s source=%s", namespace, source.name)

    return {
        "namespace": namespace,
        "range": range,
        "source": source.safe_info(),
        "top_cpu_pods": top_cpu,
        "top_memory_pods": top_mem,
    }
