"""
datasources/prometheus/tools/aks_cluster_overview.py

Queries Azure Monitor Managed Prometheus for a named AKS cluster.
Groups nodes by agent pool parsed from the AKS node naming convention:
  aks-<poolname>-<hash>-vmss<index>

CPU/memory metrics use the `instance` label (node IP:port) and are joined
to node names via kube_node_info{internal_ip} — required because
node_cpu_seconds_total in Azure Monitor Prometheus does not carry a `node` label.
"""

import logging
import re

from ....config import get_settings, validate_range
from ..client import query_instant, query_range
from ..registry import get_registry

logger = logging.getLogger(__name__)

_AKS_NODE_RE = re.compile(r"^aks-([^-]+(?:-[^-]+)*?)-\d{8,}-vmss[0-9a-z]+$")

METRIC_NAME = "aks_cluster_overview"


def get_aks_cluster_overview(cluster_name: str, source_override: str | None = None) -> dict:
    """
    Return per-agent-pool CPU, memory, and node readiness for a named AKS cluster.

    Parameters
    ----------
    cluster_name : str
        Value of the `cluster` Prometheus label (e.g. "wct-aks-prod").
    source_override : str | None
        Optional Prometheus source name override.

    Returns
    -------
    dict with fields:
        cluster_name, source,
        pools: list of {
            pool_name, node_count, ready_nodes,
            avg_cpu_percent, max_cpu_percent,
            avg_memory_percent, max_memory_percent
        }
    """
    if not cluster_name or not cluster_name.strip():
        raise ValueError("cluster_name must not be empty.")

    source = get_registry().get_for_metric(METRIC_NAME, source_override)
    cluster_label = cluster_name.strip()
    range_str = "24h"

    # --- Node readiness (has `node` label) ---
    ready_by_node: dict[str, bool] = {}
    all_nodes: set[str] = set()
    try:
        for r in query_instant(
            f'kube_node_status_condition{{cluster="{cluster_label}",condition="Ready",status="true"}}',
            source,
        ):
            node = r.get("metric", {}).get("node", "")
            if node:
                val = float(r.get("value", [0, "0"])[1])
                ready_by_node[node] = val == 1.0
                all_nodes.add(node)
        # Also collect not-ready nodes
        for r in query_instant(
            f'kube_node_status_condition{{cluster="{cluster_label}",condition="Ready"}}',
            source,
        ):
            node = r.get("metric", {}).get("node", "")
            if node:
                all_nodes.add(node)
    except RuntimeError as exc:
        logger.warning("AKS readiness query failed. cluster=%s error=%s", cluster_label, exc)

    # --- Build instance-IP → node-name mapping via kube_node_info ---
    # node_cpu_seconds_total uses `instance` = "IP:port", not node name.
    ip_to_node: dict[str, str] = {}
    try:
        for r in query_instant(
            f'kube_node_info{{cluster="{cluster_label}"}}',
            source,
        ):
            metric = r.get("metric", {})
            node = metric.get("node", "")
            internal_ip = metric.get("internal_ip", "")
            if node and internal_ip:
                ip_to_node[internal_ip] = node
                all_nodes.add(node)
    except RuntimeError as exc:
        logger.warning("AKS node_info query failed. cluster=%s error=%s", cluster_label, exc)

    # --- CPU per node ---
    cpu_by_node: dict[str, dict] = {}
    try:
        results = query_range(
            f'100 * (1 - avg by (instance) ('
            f'rate(node_cpu_seconds_total{{cluster="{cluster_label}",mode="idle"}}[5m])'
            f'))',
            source, range_str=range_str, step="5m",
        )
        for r in results:
            node = _resolve_node(r.get("metric", {}), ip_to_node)
            if not node:
                continue
            values = _clean_values(r.get("values", []))
            if values:
                cpu_by_node[node] = {"avg": round(sum(values) / len(values), 1), "max": round(max(values), 1)}
    except RuntimeError as exc:
        logger.warning("AKS CPU query failed. cluster=%s error=%s", cluster_label, exc)

    # --- Memory per node ---
    mem_by_node: dict[str, dict] = {}
    try:
        results = query_range(
            f'100 * (1 - avg by (instance) ('
            f'node_memory_MemAvailable_bytes{{cluster="{cluster_label}"}}'
            f' / node_memory_MemTotal_bytes{{cluster="{cluster_label}"}}'
            f'))',
            source, range_str=range_str, step="5m",
        )
        for r in results:
            node = _resolve_node(r.get("metric", {}), ip_to_node)
            if not node:
                continue
            values = _clean_values(r.get("values", []))
            if values:
                mem_by_node[node] = {"avg": round(sum(values) / len(values), 1), "max": round(max(values), 1)}
    except RuntimeError as exc:
        logger.warning("AKS memory query failed. cluster=%s error=%s", cluster_label, exc)

    # --- Group by agent pool ---
    pools: dict[str, dict] = {}
    for node in sorted(all_nodes):
        pool = _parse_pool_name(node)
        if pool not in pools:
            pools[pool] = {"nodes": [], "ready": 0}
        pools[pool]["nodes"].append(node)
        if ready_by_node.get(node, False):
            pools[pool]["ready"] += 1

    pool_list = []
    for pool_name, pool in sorted(pools.items()):
        nodes = pool["nodes"]
        cpu_avgs = [cpu_by_node[n]["avg"] for n in nodes if n in cpu_by_node]
        cpu_maxs = [cpu_by_node[n]["max"] for n in nodes if n in cpu_by_node]
        mem_avgs = [mem_by_node[n]["avg"] for n in nodes if n in mem_by_node]
        mem_maxs = [mem_by_node[n]["max"] for n in nodes if n in mem_by_node]

        pool_list.append({
            "pool_name": pool_name,
            "node_count": len(nodes),
            "ready_nodes": pool["ready"],
            "avg_cpu_percent": round(sum(cpu_avgs) / len(cpu_avgs), 1) if cpu_avgs else None,
            "max_cpu_percent": round(max(cpu_maxs), 1) if cpu_maxs else None,
            "avg_memory_percent": round(sum(mem_avgs) / len(mem_avgs), 1) if mem_avgs else None,
            "max_memory_percent": round(max(mem_maxs), 1) if mem_maxs else None,
        })

    return {
        "cluster_name": cluster_label,
        "source": source.safe_info(),
        "pools": pool_list,
    }


def _resolve_node(metric: dict, ip_to_node: dict[str, str]) -> str:
    """Get node name from metric labels, trying direct `node` then IP lookup."""
    node = metric.get("node", "")
    if node:
        return node
    instance = metric.get("instance", "")
    ip = instance.split(":")[0] if instance else ""
    return ip_to_node.get(ip, "")


def _clean_values(raw_values: list) -> list[float]:
    bad = {"NaN", "Inf", "+Inf", "-Inf"}
    return [float(v[1]) for v in raw_values if v[1] not in bad]


def _parse_pool_name(node: str) -> str:
    m = _AKS_NODE_RE.match(node)
    return m.group(1) if m else "other"
