"""
tool_dispatcher.py — Whitelisted Tool Dispatcher.

This module is the ONLY place that translates a StructuredMetricRequest
(produced by the Conversation Agent) into an actual metric tool call.

Security model
--------------
Only tools listed in ALLOWED_TOOL_DISPATCH can ever be called.
The dispatcher validates the tool name against this whitelist before
executing anything. This prevents the AI from routing to arbitrary
functions even if the Conversation Agent produces unexpected output.

All inputs (namespace, service, range) are validated before being passed
to the underlying tool functions.
"""

import logging
from typing import Any

from .config import get_settings, validate_cluster_name, validate_label, validate_range, validate_source_name, validate_workload_name
from .source_registry import get_registry
from .tools.cluster_health import get_cluster_health
from .tools.k8s_namespace_overview import get_k8s_namespace_overview
from .tools.k8s_services import get_k8s_service_detail, get_k8s_services
from .tools.k8s_workloads import get_k8s_workload_detail, get_k8s_workloads
from .tools.namespace_usage import (
    get_namespace_resource_usage,
    get_top_resource_consuming_pods,
)
from .tools.node_cpu import get_node_cpu_usage
from .tools.node_memory import get_node_memory_usage
from .tools.pod_restarts import get_pod_restart_count
from .tools.service_errors import get_service_error_rate
from .tools.unhealthy_pods import get_unhealthy_pods

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Whitelist — maps tool name -> callable
# This is the authoritative gate. Any tool not listed here cannot be called.
# ---------------------------------------------------------------------------

ALLOWED_TOOL_DISPATCH: dict[str, Any] = {
    "get_cluster_health":              get_cluster_health,
    "get_node_cpu_usage":             get_node_cpu_usage,
    "get_node_memory_usage":          get_node_memory_usage,
    "get_pod_restart_count":          get_pod_restart_count,
    "get_unhealthy_pods":             get_unhealthy_pods,
    "get_namespace_resource_usage":   get_namespace_resource_usage,
    "get_service_error_rate":         get_service_error_rate,
    "get_top_resource_consuming_pods": get_top_resource_consuming_pods,
    # K8s cluster-aware tools
    "get_k8s_namespace_overview":     get_k8s_namespace_overview,
    "get_k8s_workloads":              get_k8s_workloads,
    "get_k8s_workload_detail":        get_k8s_workload_detail,
    "get_k8s_services":               get_k8s_services,
    "get_k8s_service_detail":         get_k8s_service_detail,
}


class ToolDispatchError(ValueError):
    """Raised when the dispatch request cannot be fulfilled safely."""


def dispatch_tool(request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """
    Execute the metric tool named in *request* and return its result.

    Parameters
    ----------
    request : dict
        The "request" sub-dict from a Conversation Agent "ready" response.
        Expected keys: tool, namespace, service, range, source.

    Returns
    -------
    tuple[str, dict]
        (tool_name, metric_data) — tool_name for audit/logging,
        metric_data is the structured JSON from the metric tool.

    Raises
    ------
    ToolDispatchError
        If the tool name is not whitelisted, required parameters are missing,
        or inputs fail validation.
    RuntimeError
        If the underlying metric tool raises (e.g., Prometheus unreachable).
    """
    settings = get_settings()

    tool_name     = request.get("tool", "")
    namespace     = (request.get("namespace") or "").strip()
    service       = (request.get("service") or "").strip()
    cluster       = (request.get("cluster") or "").strip()
    workload_name = (request.get("workload_name") or "").strip()
    range_str     = (request.get("range") or settings.prometheus_default_range).strip()
    source        = (request.get("source") or None)

    # ---- Whitelist check ----
    if tool_name not in ALLOWED_TOOL_DISPATCH:
        raise ToolDispatchError(
            f"Tool '{tool_name}' is not in the allowed tool list. "
            f"Allowed tools: {sorted(ALLOWED_TOOL_DISPATCH.keys())}"
        )

    # ---- Input validation ----
    try:
        validate_range(range_str, settings.allowed_ranges_set)
    except ValueError as exc:
        raise ToolDispatchError(str(exc)) from exc

    if namespace:
        try:
            validate_label(namespace, "namespace")
        except ValueError as exc:
            raise ToolDispatchError(str(exc)) from exc

    if service:
        try:
            validate_label(service, "service")
        except ValueError as exc:
            raise ToolDispatchError(str(exc)) from exc

    if source:
        try:
            validate_source_name(source)
            get_registry().get_by_name(source)   # existence check
        except (ValueError, KeyError) as exc:
            raise ToolDispatchError(str(exc)) from exc

    if cluster:
        try:
            validate_cluster_name(cluster)
        except ValueError as exc:
            raise ToolDispatchError(str(exc)) from exc

    if workload_name:
        try:
            validate_workload_name(workload_name)
        except ValueError as exc:
            raise ToolDispatchError(str(exc)) from exc

    logger.info(
        "Dispatching tool=%s cluster=%s namespace=%s service=%s range=%s source=%s",
        tool_name, cluster or "—", namespace or "—", service or "—", range_str, source or "auto",
    )

    fn = ALLOWED_TOOL_DISPATCH[tool_name]
    metric_data = _call_tool(fn, tool_name, namespace, service, cluster, workload_name, range_str, source)
    return tool_name, metric_data


# ---------------------------------------------------------------------------
# Internal dispatch router — keeps the actual call site clean and explicit
# ---------------------------------------------------------------------------


def _call_tool(
    fn,
    tool_name: str,
    namespace: str,
    service: str,
    cluster: str,
    workload_name: str,
    range_str: str,
    source: str | None,
) -> dict[str, Any]:
    """Map tool name to the correct keyword arguments and call the function."""

    if tool_name == "get_cluster_health":
        return fn(source_override=source)

    if tool_name in ("get_node_cpu_usage", "get_node_memory_usage"):
        return fn(range=range_str, source_override=source)

    if tool_name == "get_pod_restart_count":
        if not namespace:
            raise ToolDispatchError(
                "get_pod_restart_count requires a namespace. "
                "Please specify which namespace to check."
            )
        return fn(namespace=namespace, range=range_str, source_override=source)

    if tool_name == "get_unhealthy_pods":
        if not namespace:
            raise ToolDispatchError(
                "get_unhealthy_pods requires a namespace. "
                "Please specify which namespace to check."
            )
        return fn(namespace=namespace, source_override=source)

    if tool_name == "get_namespace_resource_usage":
        if not namespace:
            raise ToolDispatchError(
                "get_namespace_resource_usage requires a namespace. "
                "Please specify which namespace to check."
            )
        return fn(namespace=namespace, range=range_str, source_override=source)

    if tool_name == "get_service_error_rate":
        if not service:
            raise ToolDispatchError(
                "get_service_error_rate requires a service name. "
                "Please specify which service to check."
            )
        if not namespace:
            raise ToolDispatchError(
                "get_service_error_rate requires a namespace. "
                "Please specify which namespace the service is in."
            )
        return fn(service=service, namespace=namespace, range=range_str, source_override=source)

    if tool_name == "get_top_resource_consuming_pods":
        if not namespace:
            raise ToolDispatchError(
                "get_top_resource_consuming_pods requires a namespace. "
                "Please specify which namespace to check."
            )
        return fn(namespace=namespace, range=range_str, source_override=source)

    if tool_name == "get_k8s_namespace_overview":
        if not cluster:
            raise ToolDispatchError("get_k8s_namespace_overview requires a cluster.")
        if not namespace:
            raise ToolDispatchError("get_k8s_namespace_overview requires a namespace.")
        return fn(cluster=cluster, namespace=namespace, range=range_str, source_override=source)

    if tool_name == "get_k8s_workloads":
        if not cluster:
            raise ToolDispatchError("get_k8s_workloads requires a cluster.")
        if not namespace:
            raise ToolDispatchError("get_k8s_workloads requires a namespace.")
        return fn(cluster=cluster, namespace=namespace, source_override=source)

    if tool_name == "get_k8s_workload_detail":
        if not cluster:
            raise ToolDispatchError("get_k8s_workload_detail requires a cluster.")
        if not namespace:
            raise ToolDispatchError("get_k8s_workload_detail requires a namespace.")
        if not workload_name:
            raise ToolDispatchError("get_k8s_workload_detail requires a workload_name.")
        return fn(cluster=cluster, namespace=namespace, workload_name=workload_name, range=range_str, source_override=source)

    if tool_name == "get_k8s_services":
        if not cluster:
            raise ToolDispatchError("get_k8s_services requires a cluster.")
        if not namespace:
            raise ToolDispatchError("get_k8s_services requires a namespace.")
        return fn(cluster=cluster, namespace=namespace, source_override=source)

    if tool_name == "get_k8s_service_detail":
        if not cluster:
            raise ToolDispatchError("get_k8s_service_detail requires a cluster.")
        if not namespace:
            raise ToolDispatchError("get_k8s_service_detail requires a namespace.")
        if not service:
            raise ToolDispatchError("get_k8s_service_detail requires a service name.")
        return fn(cluster=cluster, namespace=namespace, service_name=service, source_override=source)

    # Should never reach here — whitelist check above already guards this
    raise ToolDispatchError(f"No dispatch mapping for tool '{tool_name}'.")
