"""
auth_helper.py - Authentication helpers for Prometheus sources.

Azure AD token resolution order for a source:
  1. Per-source app registration (tenant_id + client_id + client_secret)
     -> Uses ClientSecretCredential directly
  2. DefaultAzureCredential (when per-source creds are absent)
     -> Workload Identity > Managed Identity > env vars > Azure CLI

Token audience for Azure Monitor Managed Prometheus:
  https://prometheus.monitor.azure.com/.default

Security rules:
  - Tokens and secrets are never logged (only token length at DEBUG).
  - client_secret is never included in any log or response.
"""

import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

AZURE_MONITOR_PROMETHEUS_SCOPE = "https://prometheus.monitor.azure.com/.default"


def get_token_for_source(source) -> str:
    """
    Acquire an Azure AD Bearer token for *source*.

    Uses per-source service principal credentials if configured,
    otherwise falls back to DefaultAzureCredential.

    Returns the raw token string (never logged).
    Raises RuntimeError with a safe message on failure.
    """
    try:
        from azure.identity import ClientSecretCredential, DefaultAzureCredential
    except ImportError:
        raise RuntimeError(
            "azure-identity is not installed. "
            "Add 'azure-identity' to requirements.txt and rebuild the image."
        )

    try:
        if source.has_per_source_azure_creds():
            logger.debug(
                "Using per-source ClientSecretCredential. source=%s tenant=%s client=%s",
                source.name, source.tenant_id, source.client_id,
            )
            credential = _get_client_secret_credential(
                source.tenant_id,
                source.client_id,
                source.client_secret,
            )
        else:
            logger.debug(
                "Using DefaultAzureCredential. source=%s", source.name
            )
            credential = _get_default_credential()

        token = credential.get_token(AZURE_MONITOR_PROMETHEUS_SCOPE)
        logger.debug(
            "Azure AD token acquired. source=%s token_length=%d",
            source.name, len(token.token),
        )
        return token.token

    except Exception as exc:
        # Log type only — exception message may contain credential details
        logger.error(
            "Azure AD token acquisition failed. source=%s error_type=%s",
            source.name, type(exc).__name__,
        )
        raise RuntimeError(
            f"Could not acquire Azure AD token for source '{source.name}'. "
            "Check that the tenant_id, client_id, and client_secret are correct, "
            "or that Managed Identity / Workload Identity is properly configured. "
            "Required for service principal: tenant_id, client_id, client_secret in PROMETHEUS_SOURCES."
        )


@lru_cache(maxsize=1)
def _get_default_credential():
    """Cached DefaultAzureCredential instance (token refresh is handled internally)."""
    from azure.identity import DefaultAzureCredential
    return DefaultAzureCredential()


@lru_cache(maxsize=32)
def _get_client_secret_credential(tenant_id: str, client_id: str, client_secret: str):
    """Cached ClientSecretCredential instance (token refresh is handled internally)."""
    from azure.identity import ClientSecretCredential
    return ClientSecretCredential(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
    )
