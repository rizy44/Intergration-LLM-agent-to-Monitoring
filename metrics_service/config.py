"""
config.py - Centralised configuration for the Metrics Service.
"""

import re
from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env relative to this file so it works regardless of cwd
_ENV_FILE = Path(__file__).parent.parent / ".env"

ALLOWED_RANGES_DEFAULT = {"1h", "6h", "12h", "24h", "2d", "7d"}
_SAFE_LABEL = re.compile(r"^[a-z0-9][a-z0-9\-\.]{0,62}$")
_SAFE_HOSTNAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-\_\.]{0,62}$")
# Azure resource/RG names: 1-90 chars, alphanumeric + hyphens + underscores + dots + parens
_SAFE_AZURE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-\_\.\(\)]{0,88}$")


def validate_range(value, allowed=None):
    allowed = allowed or ALLOWED_RANGES_DEFAULT
    if value not in allowed:
        raise ValueError(f"Invalid range '{value}'. Allowed values: {sorted(allowed)}")
    return value


def validate_label(name, field="value"):
    if not _SAFE_LABEL.match(name):
        raise ValueError(
            f"Invalid {field} '{name}'. Must be lowercase alphanumeric with hyphens/dots, 1-63 chars."
        )
    return name


def validate_source_name(name):
    return validate_label(name, "source name")


def validate_cluster_name(name):
    return validate_label(name, "cluster")


def validate_workload_name(name):
    return validate_label(name, "workload")


def validate_azure_name(value: str, field: str = "resource name") -> str:
    """Validate an Azure resource name or resource group name."""
    if not value or not value.strip():
        raise ValueError(f"Invalid {field}: must not be empty.")
    if not _SAFE_AZURE_NAME.match(value.strip()):
        raise ValueError(
            f"Invalid {field} '{value}'. "
            "Use alphanumeric characters, hyphens, underscores, dots, or parentheses. "
            "Max 90 characters."
        )
    return value.strip()


def validate_hostname(value: str) -> str:
    """Validate a hostname or comma-separated hostname list."""
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if not parts:
        raise ValueError("hostname must not be empty.")
    for part in parts:
        if not _SAFE_HOSTNAME.match(part):
            raise ValueError(
                f"Invalid hostname '{part}'. "
                "Use alphanumeric characters, hyphens, underscores, or dots. "
                "Multiple hostnames can be comma-separated."
            )
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Multi-source Prometheus (JSON array string, parsed by source_registry.py)
    prometheus_sources: str = ""

    # Readable two-source Prometheus config. Used when PROMETHEUS_SOURCES is empty.
    prometheus_node_name: str = "Prometheus"
    prometheus_node_url: str = ""
    prometheus_node_auth_type: str = "basic"
    prometheus_node_username: str = ""
    prometheus_node_password: str = ""
    prometheus_node_description: str = "In-cluster Prometheus"

    prometheus_aks_name: str = "uat-monitor-workspace-prometheus"
    prometheus_aks_url: str = ""
    prometheus_aks_auth_type: str = "azure_ad"
    prometheus_aks_tenant_id: str = ""
    prometheus_aks_client_id: str = ""
    prometheus_aks_client_secret: str = ""
    prometheus_aks_subscription_id: str = ""
    prometheus_aks_description: str = "Azure Monitor Managed Prometheus"

    # Legacy single-source fallback
    prometheus_url: str = ""
    prometheus_username: str = ""
    prometheus_password: str = ""

    # Query behaviour
    prometheus_timeout_seconds: int = 10
    prometheus_max_results: int = 100
    prometheus_default_range: str = "24h"
    allowed_ranges: str = "1h,6h,12h,24h,2d,7d"

    # Anthropic
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"
    anthropic_max_tokens: int = 1024

    # Teams — Incoming Webhook (daily reports and /teams/chat outbound)
    teams_webhook_url: str = ""

    # Teams — Outgoing Webhook HMAC secret (base64-encoded, from Teams Admin Center)
    # Required for POST /teams/webhook to validate inbound HMAC signatures.
    # Leave empty to disable the Outgoing Webhook endpoint.
    teams_outgoing_webhook_secret: str = ""

    # Teams — Incoming Webhook for the dedicated ALERT channel.
    # Separate from teams_webhook_url so alerts do not mix with daily reports.
    teams_alert_webhook_url: str = ""

    # Bearer token Alertmanager must present on POST /alerts/alertmanager.
    # Endpoint fails closed (503) when unset. Store in a Kubernetes Secret.
    alert_webhook_token: str = ""

    # Azure metrics poll alerts (CronJob */15) — thresholds are env-tunable.
    azure_alert_range: str = "1h"          # evaluation window per cycle
    alert_repeat_minutes: int = 240        # cooldown before re-notifying a still-firing alert
    azure_alert_app_error_rate: float = 5.0       # App Service error rate % (critical)
    azure_alert_app_response_ms: float = 2000.0   # App Service avg response ms (warning)
    azure_alert_db_cpu: float = 80.0               # MySQL/PostgreSQL CPU % (warning)
    azure_alert_db_memory: float = 85.0            # MySQL/PostgreSQL memory % (warning)
    azure_alert_db_storage: float = 85.0           # MySQL/PostgreSQL storage % (critical)
    azure_alert_redis_load: float = 80.0           # Redis server load % (warning)
    azure_alert_redis_memory: float = 85.0         # Redis used memory % (warning)
    azure_alert_sb_deadletter: float = 100.0       # Service Bus dead-lettered msgs (warning)
    azure_alert_sb_server_errors: float = 0.0      # Service Bus server errors (critical)

    # Azure Monitor REST API (management.azure.com) — separate from Prometheus sources
    azure_resource_name: str = "uat-monitor-workspace"
    azure_resource_tenant_id: str = ""
    azure_resource_client_id: str = ""
    azure_resource_client_secret: str = ""
    azure_resource_subscription_id: str = ""
    azure_resource_description: str = "Azure Monitor Workspace"

    # PostgreSQL alert storage (empty = storage disabled, in-memory fallback)
    database_url: str = ""
    alert_retention_days: int = 90

    # Daily report scoping
    daily_report_sources: str = ""
    daily_report_namespaces: str = "default,kube-system,monitoring"

    # Service settings
    metrics_service_port: int = 8000
    log_level: str = "INFO"

    @property
    def allowed_ranges_set(self):
        return {r.strip() for r in self.allowed_ranges.split(",") if r.strip()}


@lru_cache(maxsize=1)
def get_settings():
    return Settings()
