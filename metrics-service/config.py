"""
config.py - Centralised configuration for the Metrics Service.
"""

import re
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

ALLOWED_RANGES_DEFAULT = {"1h", "6h", "12h", "24h", "2d", "7d"}
_SAFE_LABEL = re.compile(r"^[a-z0-9][a-z0-9\-\.]{0,62}$")


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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Multi-source Prometheus (JSON array string, parsed by source_registry.py)
    prometheus_sources: str = ""

    # Legacy single-source fallback
    prometheus_url: str = "http://localhost:9090"
    prometheus_username: str = ""
    prometheus_password: str = ""

    # Query behaviour
    prometheus_timeout_seconds: int = 10
    prometheus_max_results: int = 100
    prometheus_default_range: str = "24h"
    allowed_ranges: str = "1h,6h,12h,24h,2d,7d"

    # Anthropic
    anthropic_api_key: str = ""
    claude_model: str = "claude-3-5-sonnet-20241022"
    claude_max_tokens: int = 1024

    # Teams — Incoming Webhook (daily reports and /teams/chat outbound)
    teams_webhook_url: str = ""

    # Teams — Outgoing Webhook HMAC secret (base64-encoded, from Teams Admin Center)
    # Required for POST /teams/webhook to validate inbound HMAC signatures.
    # Leave empty to disable the Outgoing Webhook endpoint.
    teams_outgoing_webhook_secret: str = ""

    # Daily report scoping
    daily_report_sources: str = ""
    daily_report_namespaces: str = "default,kube-system,monitoring"

    # Service
    metrics_service_port: int = 8000
    log_level: str = "INFO"

    @property
    def allowed_ranges_set(self):
        return {r.strip() for r in self.allowed_ranges.split(",") if r.strip()}


@lru_cache(maxsize=1)
def get_settings():
    return Settings()
