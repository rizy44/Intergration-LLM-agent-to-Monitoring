"""
datasources/prometheus/tools/pod_restarts.py - get_pod_restart_count(namespace, range, source_override)
"""

import logging
from ....config import get_settings, validate_label, validate_range
from ..client import query_instant
from ..registry import get_registry

logger = logging.getLogger(__name__)
METRIC_NAME = "pod_restarts"


def get_pod_restart_count(namespace, range="24h", source_override=None):
    settings = get_settings()
    namespace = validate_label(namespace, "namespace")
    range = validate_range(range, settings.allowed_ranges_set)
    source = get_registry().get_for_metric(METRIC_NAME, source_override)

    promql = (
        f'increase(kube_pod_container_status_restarts_total'
        f'{{namespace="{namespace}"}}[{range}])'
    )

    try:
        results = query_instant(promql, source)
    except RuntimeError as exc:
        logger.warning("get_pod_restart_count failed: %s", exc)
        raise

    pods = []
    for result in results:
        pod = result.get("metric", {}).get("pod", "unknown")
        try:
            restarts = int(float(result["value"][1]))
        except (KeyError, ValueError):
            continue
        if restarts > 0:
            pods.append({"pod": pod, "restarts": restarts})

    pods.sort(key=lambda p: p["restarts"], reverse=True)
    return {"namespace": namespace, "range": range, "source": source.safe_info(), "pods": pods}
