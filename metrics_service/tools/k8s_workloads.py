"""
tools/k8s_workloads.py - Workload list and detail for a Kubernetes namespace.

get_k8s_workloads(cluster, namespace, source_override)
  - Returns all Deployments, StatefulSets, DaemonSets with replica status.
  - No CPU/memory queries — lightweight for listing.

get_k8s_workload_detail(cluster, namespace, workload_name, range, source_override)
  - Returns replica status + CPU/memory + per-pod breakdown for one workload.
  - Resolves pod→workload via kube_pod_owner + kube_replicaset_owner join in Python.
"""

import logging

from ..config import get_settings, validate_cluster_name, validate_label, validate_range, validate_workload_name
from ..prometheus_client import query_instant
from ..source_registry import get_registry

logger = logging.getLogger(__name__)


def get_k8s_workloads(cluster, namespace, source_override=None):
    cluster = validate_cluster_name(cluster)
    namespace = validate_label(namespace, "namespace")
    source = get_registry().get_for_metric("k8s_workloads", source_override)

    workloads = []

    _collect_deployments(cluster, namespace, source, workloads)
    _collect_statefulsets(cluster, namespace, source, workloads)
    _collect_daemonsets(cluster, namespace, source, workloads)

    workloads.sort(key=lambda w: w["name"])
    return {
        "cluster": cluster,
        "namespace": namespace,
        "source": source.safe_info(),
        "workloads": workloads,
    }


def get_k8s_workload_detail(cluster, namespace, workload_name, range="1h", source_override=None):
    settings = get_settings()
    cluster = validate_cluster_name(cluster)
    namespace = validate_label(namespace, "namespace")
    workload_name = validate_workload_name(workload_name)
    range = validate_range(range, settings.allowed_ranges_set)
    source = get_registry().get_for_metric("k8s_workload_detail", source_override)

    # Resolve workload kind and replica status
    workload_meta = _find_workload(cluster, namespace, workload_name, source)
    if workload_meta is None:
        raise KeyError(
            f"Workload '{workload_name}' not found in cluster='{cluster}' namespace='{namespace}'. "
            "Check that the name matches a Deployment, StatefulSet, or DaemonSet."
        )

    # Build pod→workload map
    pod_to_workload = _build_pod_workload_map(cluster, namespace, source)

    # Pods belonging to this workload
    workload_pods = {
        pod for pod, wl in pod_to_workload.items() if wl == workload_name
    }

    # CPU and memory per pod
    cpu_by_pod = _query_pod_cpu(cluster, namespace, range, source)
    mem_by_pod = _query_pod_memory(cluster, namespace, source)

    # Pod metadata: node, phase, restarts
    pod_info = _query_pod_info(cluster, namespace, source)
    pod_phases = _query_pod_phases(cluster, namespace, source)
    pod_restarts = _query_pod_restarts(cluster, namespace, range, source)

    pods = []
    total_cpu = 0.0
    total_mem = 0.0

    for pod in sorted(workload_pods):
        cpu = cpu_by_pod.get(pod)
        mem = mem_by_pod.get(pod)
        if cpu is not None:
            total_cpu += cpu
        if mem is not None:
            total_mem += mem
        pods.append({
            "name": pod,
            "node": pod_info.get(pod, {}).get("node"),
            "phase": pod_phases.get(pod, "Unknown"),
            "restarts": pod_restarts.get(pod, 0),
            "cpu_cores": round(cpu, 4) if cpu is not None else None,
            "memory_gb": round(mem / (1024 ** 3), 3) if mem is not None else None,
        })

    return {
        "cluster": cluster,
        "namespace": namespace,
        "name": workload_meta["name"],
        "kind": workload_meta["kind"],
        "replicas_ready": workload_meta["replicas_ready"],
        "replicas_desired": workload_meta["replicas_desired"],
        "healthy": workload_meta["healthy"],
        "range": range,
        "source": source.safe_info(),
        "cpu_cores": round(total_cpu, 4),
        "memory_gb": round(total_mem / (1024 ** 3), 3),
        "pods": pods,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _derive_healthy(ready, desired):
    if desired is None or ready is None:
        return False
    return ready == desired and desired > 0


def _collect_deployments(cluster, namespace, source, out):
    ready_map = {}
    desired_map = {}
    try:
        for r in query_instant(
            f'kube_deployment_status_replicas_available{{cluster="{cluster}",namespace="{namespace}"}}',
            source,
        ):
            name = r.get("metric", {}).get("deployment")
            if name:
                ready_map[name] = int(float(r["value"][1]))
    except (RuntimeError, KeyError, ValueError):
        logger.warning("Could not retrieve deployment available replicas. cluster=%s ns=%s", cluster, namespace)

    try:
        for r in query_instant(
            f'kube_deployment_spec_replicas{{cluster="{cluster}",namespace="{namespace}"}}',
            source,
        ):
            name = r.get("metric", {}).get("deployment")
            if name:
                desired_map[name] = int(float(r["value"][1]))
    except (RuntimeError, KeyError, ValueError):
        logger.warning("Could not retrieve deployment spec replicas. cluster=%s ns=%s", cluster, namespace)

    all_names = set(ready_map) | set(desired_map)
    for name in sorted(all_names):
        ready = ready_map.get(name, 0)
        desired = desired_map.get(name, 0)
        out.append({
            "name": name,
            "kind": "Deployment",
            "replicas_ready": ready,
            "replicas_desired": desired,
            "healthy": _derive_healthy(ready, desired),
        })


def _collect_statefulsets(cluster, namespace, source, out):
    ready_map = {}
    desired_map = {}
    try:
        for r in query_instant(
            f'kube_statefulset_status_replicas_ready{{cluster="{cluster}",namespace="{namespace}"}}',
            source,
        ):
            name = r.get("metric", {}).get("statefulset")
            if name:
                ready_map[name] = int(float(r["value"][1]))
    except (RuntimeError, KeyError, ValueError):
        logger.warning("Could not retrieve statefulset ready replicas. cluster=%s ns=%s", cluster, namespace)

    try:
        for r in query_instant(
            f'kube_statefulset_replicas{{cluster="{cluster}",namespace="{namespace}"}}',
            source,
        ):
            name = r.get("metric", {}).get("statefulset")
            if name:
                desired_map[name] = int(float(r["value"][1]))
    except (RuntimeError, KeyError, ValueError):
        logger.warning("Could not retrieve statefulset spec replicas. cluster=%s ns=%s", cluster, namespace)

    all_names = set(ready_map) | set(desired_map)
    for name in sorted(all_names):
        ready = ready_map.get(name, 0)
        desired = desired_map.get(name, 0)
        out.append({
            "name": name,
            "kind": "StatefulSet",
            "replicas_ready": ready,
            "replicas_desired": desired,
            "healthy": _derive_healthy(ready, desired),
        })


def _collect_daemonsets(cluster, namespace, source, out):
    ready_map = {}
    desired_map = {}
    try:
        for r in query_instant(
            f'kube_daemonset_status_number_ready{{cluster="{cluster}",namespace="{namespace}"}}',
            source,
        ):
            name = r.get("metric", {}).get("daemonset")
            if name:
                ready_map[name] = int(float(r["value"][1]))
    except (RuntimeError, KeyError, ValueError):
        logger.warning("Could not retrieve daemonset ready count. cluster=%s ns=%s", cluster, namespace)

    try:
        for r in query_instant(
            f'kube_daemonset_status_desired_number_scheduled{{cluster="{cluster}",namespace="{namespace}"}}',
            source,
        ):
            name = r.get("metric", {}).get("daemonset")
            if name:
                desired_map[name] = int(float(r["value"][1]))
    except (RuntimeError, KeyError, ValueError):
        logger.warning("Could not retrieve daemonset desired count. cluster=%s ns=%s", cluster, namespace)

    all_names = set(ready_map) | set(desired_map)
    for name in sorted(all_names):
        ready = ready_map.get(name, 0)
        desired = desired_map.get(name, 0)
        out.append({
            "name": name,
            "kind": "DaemonSet",
            "replicas_ready": ready,
            "replicas_desired": desired,
            "healthy": _derive_healthy(ready, desired),
        })


def _find_workload(cluster, namespace, workload_name, source):
    """Return workload meta dict for the named workload, or None if not found."""
    bucket = []
    _collect_deployments(cluster, namespace, source, bucket)
    _collect_statefulsets(cluster, namespace, source, bucket)
    _collect_daemonsets(cluster, namespace, source, bucket)
    for w in bucket:
        if w["name"] == workload_name:
            return w
    return None


def _build_pod_workload_map(cluster, namespace, source):
    """Return {pod_name: workload_name} by resolving pod owner chain."""
    # pod → immediate owner
    pod_owner = {}       # pod_name -> (owner_kind, owner_name)
    try:
        for r in query_instant(
            f'kube_pod_owner{{cluster="{cluster}",namespace="{namespace}"}}',
            source,
        ):
            m = r.get("metric", {})
            pod = m.get("pod")
            kind = m.get("owner_kind", "")
            name = m.get("owner_name", "")
            if pod and name:
                pod_owner[pod] = (kind, name)
    except (RuntimeError, KeyError, ValueError):
        logger.warning("Could not retrieve pod owners. cluster=%s ns=%s", cluster, namespace)

    # replicaset → deployment (second hop)
    rs_to_deploy = {}
    try:
        for r in query_instant(
            f'kube_replicaset_owner{{cluster="{cluster}",namespace="{namespace}"}}',
            source,
        ):
            m = r.get("metric", {})
            rs = m.get("replicaset")
            deploy = m.get("owner_name", "")
            if rs and deploy:
                rs_to_deploy[rs] = deploy
    except (RuntimeError, KeyError, ValueError):
        logger.warning("Could not retrieve replicaset owners. cluster=%s ns=%s", cluster, namespace)

    pod_to_workload = {}
    for pod, (kind, owner_name) in pod_owner.items():
        if kind == "ReplicaSet":
            workload = rs_to_deploy.get(owner_name, owner_name)
        else:
            workload = owner_name
        pod_to_workload[pod] = workload

    return pod_to_workload


def _query_pod_cpu(cluster, namespace, range, source):
    result = {}
    try:
        for r in query_instant(
            f'sum by (pod) (rate(container_cpu_usage_seconds_total{{'
            f'cluster="{cluster}",namespace="{namespace}",container!=""}}[{range}]))',
            source,
        ):
            pod = r.get("metric", {}).get("pod")
            if pod:
                try:
                    result[pod] = float(r["value"][1])
                except (KeyError, ValueError):
                    pass
    except (RuntimeError, KeyError, ValueError):
        logger.warning("Could not retrieve pod CPU. cluster=%s ns=%s", cluster, namespace)
    return result


def _query_pod_memory(cluster, namespace, source):
    result = {}
    try:
        for r in query_instant(
            f'sum by (pod) (container_memory_working_set_bytes{{'
            f'cluster="{cluster}",namespace="{namespace}",container!=""}})',
            source,
        ):
            pod = r.get("metric", {}).get("pod")
            if pod:
                try:
                    result[pod] = float(r["value"][1])
                except (KeyError, ValueError):
                    pass
    except (RuntimeError, KeyError, ValueError):
        logger.warning("Could not retrieve pod memory. cluster=%s ns=%s", cluster, namespace)
    return result


def _query_pod_info(cluster, namespace, source):
    """Return {pod: {node: ...}} from kube_pod_info."""
    result = {}
    try:
        for r in query_instant(
            f'kube_pod_info{{cluster="{cluster}",namespace="{namespace}"}}',
            source,
        ):
            m = r.get("metric", {})
            pod = m.get("pod")
            if pod:
                result[pod] = {"node": m.get("node")}
    except (RuntimeError, KeyError, ValueError):
        logger.warning("Could not retrieve pod info. cluster=%s ns=%s", cluster, namespace)
    return result


def _query_pod_phases(cluster, namespace, source):
    """Return {pod: phase_string}."""
    result = {}
    try:
        for r in query_instant(
            f'kube_pod_status_phase{{cluster="{cluster}",namespace="{namespace}"}} == 1',
            source,
        ):
            m = r.get("metric", {})
            pod = m.get("pod")
            phase = m.get("phase", "Unknown")
            if pod:
                result[pod] = phase
    except (RuntimeError, KeyError, ValueError):
        logger.warning("Could not retrieve pod phases. cluster=%s ns=%s", cluster, namespace)
    return result


def _query_pod_restarts(cluster, namespace, range, source):
    """Return {pod: restart_count} summed across containers."""
    result = {}
    try:
        for r in query_instant(
            f'sum by (pod) (increase(kube_pod_container_status_restarts_total{{'
            f'cluster="{cluster}",namespace="{namespace}"}}[{range}]))',
            source,
        ):
            pod = r.get("metric", {}).get("pod")
            if pod:
                try:
                    result[pod] = int(float(r["value"][1]))
                except (KeyError, ValueError):
                    pass
    except (RuntimeError, KeyError, ValueError):
        logger.warning("Could not retrieve pod restarts. cluster=%s ns=%s", cluster, namespace)
    return result
