"""
source_registry.py - Prometheus Multi-Source Registry

Supported auth types:
  none      - no Authorization header (in-cluster Prometheus without auth)
  basic     - HTTP Basic Auth (username + password)
  azure_ad  - Azure AD Bearer token

For azure_ad, credentials resolve in this order:
  1. Per-source: tenant_id + client_id + client_secret  (app registration / service principal)
  2. DefaultAzureCredential: Workload Identity > Managed Identity > env vars > Azure CLI

Source types:
  aks       - Prometheus inside AKS cluster (ClusterIP / in-cluster DNS)
  azure     - Azure Monitor workspace or Managed Prometheus
  vm-cloud  - Prometheus on Azure cloud VMs
  vm-local  - Prometheus on on-premise / local VMs

Security rules:
  - password, client_secret are excluded from repr and never logged.
  - safe_info() returns only name, type, auth_type, description — no credentials.
"""

import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache

logger = logging.getLogger(__name__)

AUTH_TYPES = {"none", "basic", "azure_ad"}

SOURCE_TYPE_LABELS = {
    "node":     "AKS In-Cluster",
    "aks":      "Azure Monitor / Managed Prometheus",
    "vm-cloud": "Azure Cloud VM",
    "vm-local": "On-Premise VM",
}

DEFAULT_SOURCE_TYPE_FOR_METRIC = {
    "cluster_health":  "node",
    "node_cpu":        "node",
    "node_memory":     "node",
    "pod_restarts":    "node",
    "unhealthy_pods":  "node",
    "namespace_usage": "node",
    "top_consumers":   "node",
    "service_errors":        "node",
    "k8s_namespace_overview": "aks",
    "k8s_workloads":          "aks",
    "k8s_workload_detail":    "aks",
    "k8s_services":           "aks",
    "k8s_service_detail":     "aks",
}


@dataclass
class PrometheusSource:
    name: str
    type: str
    url: str
    auth_type: str = "none"

    # basic auth
    username: str = ""
    password: str = field(repr=False, default="")

    # azure_ad per-source app registration (service principal)
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = field(repr=False, default="")
    subscription_id: str = ""        # stored for reference / future Azure Monitor REST API

    description: str = ""

    def __post_init__(self):
        self.url = self.url.rstrip("/")
        if self.auth_type not in AUTH_TYPES:
            raise ValueError(
                f"Source '{self.name}': invalid auth_type '{self.auth_type}'. "
                f"Must be one of: {sorted(AUTH_TYPES)}"
            )
        # Auto-detect basic auth when credentials present but auth_type not explicitly set
        if self.auth_type == "none" and self.username and self.password:
            self.auth_type = "basic"

    def has_per_source_azure_creds(self) -> bool:
        """True when explicit service principal credentials are configured."""
        return bool(self.tenant_id and self.client_id and self.client_secret)

    def safe_info(self) -> dict:
        """Credentials-free dict safe for logs and API responses."""
        info = {
            "name": self.name,
            "type": self.type,
            "type_label": SOURCE_TYPE_LABELS.get(self.type, self.type),
            "auth_type": self.auth_type,
            "description": self.description,
        }
        if self.subscription_id:
            info["subscription_id"] = self.subscription_id
        if self.auth_type == "azure_ad" and self.tenant_id:
            info["tenant_id"] = self.tenant_id
            info["client_id"] = self.client_id
            # client_secret is NEVER included
        return info


class SourceRegistry:
    def __init__(self, sources):
        self._by_name = {s.name: s for s in sources}
        self._by_type = {}
        for s in sources:
            self._by_type.setdefault(s.type, []).append(s)
        logger.info(
            "SourceRegistry loaded %d source(s): %s",
            len(sources), [s.name for s in sources],
        )

    def get_by_name(self, name):
        if name not in self._by_name:
            raise KeyError(
                f"Prometheus source '{name}' not found. "
                f"Available: {self.list_names()}"
            )
        return self._by_name[name]

    def get_primary_by_type(self, source_type):
        sources = self._by_type.get(source_type, [])
        if not sources:
            raise KeyError(
                f"No Prometheus source of type '{source_type}' configured. "
                f"Available types: {self.list_types()}"
            )
        return sources[0]

    def get_all_by_type(self, source_type):
        return self._by_type.get(source_type, [])

    def get_for_metric(self, metric_name, source_override=None):
        if source_override:
            return self.get_by_name(source_override)
        default_type = DEFAULT_SOURCE_TYPE_FOR_METRIC.get(metric_name, "aks")
        return self.get_primary_by_type(default_type)

    def list_names(self):
        return list(self._by_name.keys())

    def list_types(self):
        return list(self._by_type.keys())

    def safe_list(self):
        return [s.safe_info() for s in self._by_name.values()]


@lru_cache(maxsize=1)
def get_registry():
    """Load and cache the SourceRegistry from PROMETHEUS_SOURCES setting."""
    from .config import get_settings
    settings = get_settings()
    raw = settings.prometheus_sources.strip()

    if raw:
        try:
            sources_data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "PROMETHEUS_SOURCES is not valid JSON. "
                "Expected a JSON array of source objects."
            ) from exc

        sources = []
        for item in sources_data:
            try:
                src = PrometheusSource(
                    name=item["name"],
                    type=item["type"],
                    url=item["url"],
                    auth_type=item.get("auth_type", "none"),
                    username=item.get("username", ""),
                    password=item.get("password", ""),
                    tenant_id=item.get("tenant_id", ""),
                    client_id=item.get("client_id", ""),
                    client_secret=item.get("client_secret", ""),
                    subscription_id=item.get("subscription_id", ""),
                    description=item.get("description", ""),
                )
                sources.append(src)
            except KeyError as exc:
                raise ValueError(
                    f"Prometheus source entry missing required field: {exc}"
                ) from exc

        if not sources:
            raise ValueError("PROMETHEUS_SOURCES is empty. At least one source is required.")
        return SourceRegistry(sources)

    readable_sources = _load_readable_sources(settings)
    if readable_sources:
        return SourceRegistry(readable_sources)

    # Legacy fallback
    legacy_url = settings.prometheus_url
    legacy_user = settings.prometheus_username
    legacy_pass = settings.prometheus_password
    logger.warning("PROMETHEUS_SOURCES not set. Falling back to legacy single-source config.")
    auth_type = "basic" if (legacy_user and legacy_pass) else "none"
    fallback = PrometheusSource(
        name="default", type="aks", url=legacy_url,
        auth_type=auth_type, username=legacy_user, password=legacy_pass,
        description="Legacy single-source fallback",
    )
    return SourceRegistry([fallback])


def _load_readable_sources(settings):
    """Build sources from individual env vars when PROMETHEUS_SOURCES is not set."""
    sources = []

    if settings.prometheus_node_url:
        sources.append(
            PrometheusSource(
                name=settings.prometheus_node_name,
                type="node",
                url=settings.prometheus_node_url,
                auth_type=settings.prometheus_node_auth_type,
                username=settings.prometheus_node_username,
                password=settings.prometheus_node_password,
                description=settings.prometheus_node_description,
            )
        )

    if settings.prometheus_aks_url:
        sources.append(
            PrometheusSource(
                name=settings.prometheus_aks_name,
                type="aks",
                url=settings.prometheus_aks_url,
                auth_type=settings.prometheus_aks_auth_type,
                tenant_id=settings.prometheus_aks_tenant_id,
                client_id=settings.prometheus_aks_client_id,
                client_secret=settings.prometheus_aks_client_secret,
                subscription_id=settings.prometheus_aks_subscription_id,
                description=settings.prometheus_aks_description,
            )
        )

    return sources
