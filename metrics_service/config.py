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

    # Legacy single-source fallback
    prometheus_url: str = "http://192.168.100.230:9090"
    prometheus_username: str = "admin"
    prometheus_password: str = "admin1234"

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
