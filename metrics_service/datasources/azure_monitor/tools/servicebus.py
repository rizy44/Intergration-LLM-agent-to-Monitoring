"""
datasources/azure_monitor/tools/servicebus.py - get_service_bus_performance(resource_group, namespace_name, range, source_override)

Queries Azure Monitor metrics for a Microsoft.ServiceBus/namespaces resource.

Service Bus metrics split by aggregation type:
  Gauge   (Average): ActiveMessages, DeadletteredMessages
  Counter (Total):   IncomingMessages, OutgoingMessages, ServerErrors

Mixing aggregation types in one call causes HTTP 400.
"""

import logging

from ..client import extract_metric_value, query_metrics
from ....config import get_settings, validate_azure_name, validate_range
from ..registry import get_azure_registry

logger = logging.getLogger(__name__)

_RESOURCE_TYPE = "Microsoft.ServiceBus/namespaces"

_GAUGE_METRICS = [
    "ActiveMessages",
    "DeadletteredMessages",
]

_COUNTER_METRICS = [
    "IncomingMessages",
    "OutgoingMessages",
    "ServerErrors",
]


def get_service_bus_performance(
    resource_group: str,
    namespace_name: str,
    range: str = "24h",
    source_override: str | None = None,
) -> dict:
    """
    Return performance metrics for an Azure Service Bus namespace.

    Parameters
    ----------
    resource_group : str
        Azure resource group containing the Service Bus namespace.
    namespace_name : str
        Name of the Azure Service Bus namespace.
    range : str
        Time range: "1h", "6h", "12h", "24h", "2d", "7d".
    source_override : str | None
        Optional Azure Monitor source name override.

    Returns
    -------
    dict with fields:
        namespace_name, resource_group, range, source,
        active_messages_avg, deadlettered_messages_avg,
        incoming_messages_total, outgoing_messages_total,
        server_errors_total
    """
    settings = get_settings()
    validate_azure_name(resource_group, "resource_group")
    if not namespace_name or not namespace_name.strip():
        raise ValueError("namespace_name must not be empty.")
    validate_azure_name(namespace_name.strip(), "namespace_name")
    validate_range(range, settings.allowed_ranges_set)

    registry = get_azure_registry()
    source = registry.get_by_name(source_override) if source_override else registry.get_default()

    resource_uri = (
        f"/subscriptions/{source.subscription_id}"
        f"/resourceGroups/{resource_group}"
        f"/providers/{_RESOURCE_TYPE}/{namespace_name.strip()}"
    )

    # Gauge metrics — snapshot values, use Average
    gauge_raw = query_metrics(resource_uri, _GAUGE_METRICS, range, source, aggregation="Average,Maximum")

    # Counter metrics — cumulative counts, use Total only
    counter_raw = query_metrics(resource_uri, _COUNTER_METRICS, range, source, aggregation="Total")

    return {
        "namespace_name": namespace_name.strip(),
        "resource_group": resource_group,
        "range": range,
        "source": source.safe_info(),
        "active_messages_avg":        _avg(gauge_raw,   "ActiveMessages"),
        "deadlettered_messages_avg":  _avg(gauge_raw,   "DeadletteredMessages"),
        "incoming_messages_total":    _total(counter_raw, "IncomingMessages"),
        "outgoing_messages_total":    _total(counter_raw, "OutgoingMessages"),
        "server_errors_total":        _total(counter_raw, "ServerErrors"),
    }


def _avg(raw, name):
    v = extract_metric_value(raw, name, "average")
    return round(v, 2) if v is not None else None


def _total(raw, name):
    v = extract_metric_value(raw, name, "total")
    return round(v) if v is not None else None
