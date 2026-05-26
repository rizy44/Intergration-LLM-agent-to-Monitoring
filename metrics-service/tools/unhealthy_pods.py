"""
tools/unhealthy_pods.py - get_unhealthy_pods(namespace, source_override)
"""

import logging
from ..config import validate_label
from ..prometheus_client import query_instant
from ..source_registry import get_registry

logger = logging.getLogger(__name__)
METRIC_NAME = "unhealthy_pods"


def get_unhealthy_pods(namespace, source_override=None):
    namespace = validate_label(namespace, "namespace")
    source = get_registry().get_for_metric(METRIC_NAME, source_override)

    promql = (
        f'kube_pod_status_phase{{namespace="{namespace}",phase!~"Running|Succeeded"}} == 1'
    )

    try:
        results = query_instant(promql, source)
    except RuntimeError as exc:
        logger.warning("get_unhealthy_pods failed: %s", exc)
        raise

    pods = []
    for result in results:
        metric = result.get("metric", {})
        pods.append({"pod": metric.get("pod", "unknown"), "phase": metric.get("phase", "unknown")})

    return {"namespace": namespace, "source": source.safe_info(), "unhealthy_pods": pods}
