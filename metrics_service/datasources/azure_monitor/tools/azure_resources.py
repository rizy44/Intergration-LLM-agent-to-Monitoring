"""
datasources/azure_monitor/tools/azure_resources.py - list_azure_resources(resource_group, source_override)

Lists all Azure resources in a resource group and groups them by supported type.
Use this tool first to discover what resources are queryable before calling
metric tools (get_app_service_performance, get_mysql_performance, etc.).
"""

import logging

from ..client import list_resources
from ....config import validate_azure_name
from ..registry import get_azure_registry

logger = logging.getLogger(__name__)

# Supported resource types and their category keys in the response
_RESOURCE_TYPE_MAP = {
    "microsoft.web/sites":                         "app_service",
    "microsoft.dbformysql/flexibleservers":        "mysql",
    "microsoft.dbforpostgresql/flexibleservers":   "postgres",
    "microsoft.cache/redis":                       "redis",
    "microsoft.servicebus/namespaces":             "service_bus",
}


def list_azure_resources(resource_group: str, source_override: str | None = None) -> dict:
    """
    List all resources in an Azure resource group, grouped by supported type.

    Parameters
    ----------
    resource_group : str
        Name of the Azure resource group to inspect.
    source_override : str | None
        Optional Azure Monitor source name override.

    Returns
    -------
    dict with keys:
        resource_group, subscription_id, source,
        resources: {
            app_service: [...],
            mysql: [...],
            postgres: [...],
            redis: [...],
            service_bus: [...],
            other: [...]
        }
    """
    validate_azure_name(resource_group, "resource_group")

    registry = get_azure_registry()
    source = registry.get_by_name(source_override) if source_override else registry.get_default()

    raw = list_resources(resource_group, source)

    grouped: dict[str, list] = {
        "app_service": [],
        "mysql": [],
        "postgres": [],
        "redis": [],
        "service_bus": [],
        "other": [],
    }

    for item in raw:
        name = item.get("name", "")
        rtype = item.get("type", "")
        location = item.get("location", "")
        key = _RESOURCE_TYPE_MAP.get(rtype.lower())
        entry = {"name": name, "type": rtype, "location": location}
        if key:
            grouped[key].append(entry)
        else:
            grouped["other"].append({"name": name, "type": rtype})

    logger.info(
        "list_azure_resources rg=%s found=%d app_service=%d mysql=%d postgres=%d redis=%d service_bus=%d other=%d",
        resource_group, len(raw),
        len(grouped["app_service"]), len(grouped["mysql"]), len(grouped["postgres"]),
        len(grouped["redis"]), len(grouped["service_bus"]), len(grouped["other"]),
    )

    return {
        "resource_group": resource_group,
        "subscription_id": source.subscription_id,
        "source": source.safe_info(),
        "resources": grouped,
    }
