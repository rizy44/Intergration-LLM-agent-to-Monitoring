"""
datasources/azure_monitor/registry.py - Azure Monitor REST API source registry.

Auth is always azure_ad (management.azure.com/.default scope).
client_secret is excluded from repr to prevent accidental logging.
"""

import logging
from dataclasses import dataclass, field
from functools import lru_cache

logger = logging.getLogger(__name__)


@dataclass
class AzureMonitorSource:
    """
    Configuration for a single Azure Monitor REST API source.

    Auth is always azure_ad (management.azure.com/.default scope).
    The ARM base URL is a fixed constant — not user-configurable.
    client_secret is excluded from repr to prevent accidental logging.
    """

    name: str
    subscription_id: str
    tenant_id: str
    client_id: str
    client_secret: str = field(repr=False, default="")
    description: str = ""

    BASE_URL: str = field(init=False, repr=False, default="https://management.azure.com")

    def __post_init__(self):
        self.BASE_URL = "https://management.azure.com"

    def has_credentials(self) -> bool:
        return bool(self.tenant_id and self.client_id and self.client_secret)

    def safe_info(self) -> dict:
        """Credentials-free dict safe for logs and API responses."""
        return {
            "name": self.name,
            "type": "azure_resource",
            "tenant_id": self.tenant_id,
            "client_id": self.client_id,
            "subscription_id": self.subscription_id,
            "description": self.description,
            # client_secret is NEVER included
        }


class AzureMonitorRegistry:
    """Registry for AzureMonitorSource instances."""

    def __init__(self, sources: list):
        self._sources = sources
        self._by_name = {s.name: s for s in sources}
        logger.info(
            "AzureMonitorRegistry loaded %d source(s): %s",
            len(sources), [s.name for s in sources],
        )

    def get_default(self) -> AzureMonitorSource:
        if not self._sources:
            raise KeyError(
                "No Azure Monitor source configured. "
                "Set AZURE_RESOURCE_TENANT_ID, AZURE_RESOURCE_CLIENT_ID, "
                "AZURE_RESOURCE_CLIENT_SECRET, and AZURE_RESOURCE_SUBSCRIPTION_ID."
            )
        return self._sources[0]

    def get_by_name(self, name: str) -> AzureMonitorSource:
        if name not in self._by_name:
            raise KeyError(
                f"Azure Monitor source '{name}' not found. "
                f"Available: {list(self._by_name.keys())}"
            )
        return self._by_name[name]

    def safe_list(self) -> list:
        return [s.safe_info() for s in self._sources]


@lru_cache(maxsize=1)
def get_azure_registry() -> AzureMonitorRegistry:
    """Load and cache the AzureMonitorRegistry from AZURE_RESOURCE_* settings."""
    from ...config import get_settings
    settings = get_settings()

    _REQUIRED = {
        "AZURE_RESOURCE_TENANT_ID":       settings.azure_resource_tenant_id,
        "AZURE_RESOURCE_CLIENT_ID":       settings.azure_resource_client_id,
        "AZURE_RESOURCE_CLIENT_SECRET":   settings.azure_resource_client_secret,
        "AZURE_RESOURCE_SUBSCRIPTION_ID": settings.azure_resource_subscription_id,
    }
    for var_name, value in _REQUIRED.items():
        if not value or not value.strip():
            raise ValueError(
                f"{var_name} is required for Azure Monitor but is not set. "
                "Add it to the Kubernetes Secret or .env file."
            )

    source = AzureMonitorSource(
        name=settings.azure_resource_name or "uat-monitor-workspace",
        subscription_id=settings.azure_resource_subscription_id.strip(),
        tenant_id=settings.azure_resource_tenant_id.strip(),
        client_id=settings.azure_resource_client_id.strip(),
        client_secret=settings.azure_resource_client_secret,
        description=settings.azure_resource_description,
    )
    return AzureMonitorRegistry([source])
