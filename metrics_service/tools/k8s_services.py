"""
tools/k8s_services.py - Service list and detail for a Kubernetes namespace.

get_k8s_services(cluster, namespace, source_override)
  - Lists all K8s Services with type, endpoint counts, and derived status.

get_k8s_service_detail(cluster, namespace, service_name, source_override)
  - Returns metadata and endpoint health for one named K8s Service.

Status derivation rules:
  endpoints_ready > 0 and not_ready == 0  → "healthy"
  endpoints_ready > 0 and not_ready > 0   → "degraded"
  endpoints_ready == 0 and not_ready > 0  → "down"
  both zero                               → "no-endpoints"
"""

import logging

from ..config import validate_cluster_name, validate_label
from ..prometheus_client import query_instant
from ..source_registry import get_registry

logger = logging.getLogger(__name__)


def get_k8s_services(cluster, namespace, source_override=None):
    cluster = validate_cluster_name(cluster)
    namespace = validate_label(namespace, "namespace")
    source = get_registry().get_for_metric("k8s_services", source_override)

    svc_info = _query_service_info(cluster, namespace, source)
    ready_map = _query_endpoints_ready(cluster, namespace, source)
    not_ready_map = _query_endpoints_not_ready(cluster, namespace, source)

    services = []
    for name, meta in sorted(svc_info.items()):
        ready = ready_map.get(name, 0)
        not_ready = not_ready_map.get(name, 0)
        services.append({
            "name": name,
            "type": meta.get("type"),
            "endpoints_ready": ready,
            "endpoints_not_ready": not_ready,
            "status": _derive_status(ready, not_ready),
        })

    return {
        "cluster": cluster,
        "namespace": namespace,
        "source": source.safe_info(),
        "services": services,
    }


def get_k8s_service_detail(cluster, namespace, service_name, source_override=None):
    cluster = validate_cluster_name(cluster)
    namespace = validate_label(namespace, "namespace")
    service_name = validate_label(service_name, "service")
    source = get_registry().get_for_metric("k8s_service_detail", source_override)

    svc_info = _query_service_info(cluster, namespace, source, filter_name=service_name)

    if service_name not in svc_info:
        raise KeyError(
            f"Service '{service_name}' not found in cluster='{cluster}' namespace='{namespace}'."
        )

    meta = svc_info[service_name]
    ready_map = _query_endpoints_ready(cluster, namespace, source, filter_name=service_name)
    not_ready_map = _query_endpoints_not_ready(cluster, namespace, source, filter_name=service_name)

    ready = ready_map.get(service_name, 0)
    not_ready = not_ready_map.get(service_name, 0)

    return {
        "cluster": cluster,
        "namespace": namespace,
        "name": service_name,
        "type": meta.get("type"),
        "cluster_ip": meta.get("cluster_ip"),
        "source": source.safe_info(),
        "endpoints_ready": ready,
        "endpoints_not_ready": not_ready,
        "status": _derive_status(ready, not_ready),
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _derive_status(ready, not_ready):
    if ready > 0 and not_ready == 0:
        return "healthy"
    if ready > 0 and not_ready > 0:
        return "degraded"
    if ready == 0 and not_ready > 0:
        return "down"
    return "no-endpoints"


def _query_service_info(cluster, namespace, source, filter_name=None):
    """Return {service_name: {type, cluster_ip}}."""
    label_filter = f'cluster="{cluster}",namespace="{namespace}"'
    if filter_name:
        label_filter += f',service="{filter_name}"'
    result = {}
    try:
        for r in query_instant(f'kube_service_info{{{label_filter}}}', source):
            m = r.get("metric", {})
            name = m.get("service")
            if name:
                result[name] = {
                    "type": m.get("type"),
                    "cluster_ip": m.get("cluster_ip") or None,
                }
    except (RuntimeError, KeyError, ValueError):
        logger.warning("Could not retrieve service info. cluster=%s ns=%s", cluster, namespace)
    return result


def _query_endpoints_ready(cluster, namespace, source, filter_name=None):
    """Return {service_name: ready_count}."""
    label_filter = f'cluster="{cluster}",namespace="{namespace}"'
    if filter_name:
        label_filter += f',endpoint="{filter_name}"'
    result = {}
    try:
        for r in query_instant(f'kube_endpoint_address_available{{{label_filter}}}', source):
            name = r.get("metric", {}).get("endpoint")
            if name:
                try:
                    result[name] = int(float(r["value"][1]))
                except (KeyError, ValueError):
                    pass
    except (RuntimeError, KeyError, ValueError):
        logger.warning("Could not retrieve ready endpoints. cluster=%s ns=%s", cluster, namespace)
    return result


def _query_endpoints_not_ready(cluster, namespace, source, filter_name=None):
    """Return {service_name: not_ready_count}."""
    label_filter = f'cluster="{cluster}",namespace="{namespace}"'
    if filter_name:
        label_filter += f',endpoint="{filter_name}"'
    result = {}
    try:
        for r in query_instant(f'kube_endpoint_address_not_ready{{{label_filter}}}', source):
            name = r.get("metric", {}).get("endpoint")
            if name:
                try:
                    result[name] = int(float(r["value"][1]))
                except (KeyError, ValueError):
                    pass
    except (RuntimeError, KeyError, ValueError):
        logger.warning("Could not retrieve not-ready endpoints. cluster=%s ns=%s", cluster, namespace)
    return result
