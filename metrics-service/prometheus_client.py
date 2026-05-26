"""
prometheus_client.py - HTTP client for the Prometheus Query API.
The ONLY place that communicates with Prometheus.

Auth routing per source.auth_type:
  none     - no auth headers
  basic    - HTTP Basic Auth
  azure_ad - Azure AD Bearer token (per-source app reg or DefaultAzureCredential)
"""

import logging
import time

import httpx

from .config import get_settings
from .source_registry import PrometheusSource

logger = logging.getLogger(__name__)


def _range_to_seconds(range_str):
    unit = range_str[-1]
    value = int(range_str[:-1])
    if unit == "h":
        return value * 3600
    if unit == "d":
        return value * 86400
    raise ValueError(f"Unsupported range unit in '{range_str}'")


def _get_request_kwargs(source):
    """Return httpx kwargs for the source auth type. Never logs credentials."""
    if source.auth_type == "basic":
        return {"auth": httpx.BasicAuth(source.username, source.password)}
    if source.auth_type == "azure_ad":
        from .auth_helper import get_token_for_source
        token = get_token_for_source(source)
        return {"headers": {"Authorization": f"Bearer {token}"}}
    # auth_type == "none"
    return {}


def _safe_error(source, detail):
    return (
        f"Could not retrieve metrics from source '{source.name}' "
        f"({source.type}): {detail}"
    )


def _handle_http_error(exc, source):
    status = exc.response.status_code
    if status == 401:
        logger.error(
            "Prometheus auth failed. source=%s auth_type=%s",
            source.name, source.auth_type,
        )
        raise RuntimeError(
            _safe_error(source, "authentication failed. Check credentials or Azure AD config.")
        )
    logger.error("Prometheus HTTP error. source=%s status=%s", source.name, status)
    raise RuntimeError(_safe_error(source, f"HTTP {status}. Check metrics service health."))


def query_instant(promql, source):
    """Execute a Prometheus instant query (GET /api/v1/query)."""
    settings = get_settings()
    url = f"{source.url}/api/v1/query"
    logger.debug("query_instant source=%s query_hash=%s", source.name, hash(promql))
    try:
        kwargs = _get_request_kwargs(source)
        response = httpx.get(
            url,
            params={"query": promql},
            timeout=settings.prometheus_timeout_seconds,
            **kwargs,
        )
        response.raise_for_status()
    except httpx.TimeoutException:
        logger.warning("Prometheus instant query timed out. source=%s", source.name)
        raise RuntimeError(_safe_error(source, "request timed out. Please try again later."))
    except httpx.HTTPStatusError as exc:
        _handle_http_error(exc, source)
    except httpx.RequestError:
        logger.exception("Prometheus connection error. source=%s", source.name)
        raise RuntimeError(_safe_error(source, "connection failed. Check network and endpoint config."))

    payload = response.json()
    if payload.get("status") != "success":
        logger.error(
            "Prometheus query error. source=%s error_type=%s",
            source.name, payload.get("errorType", "unknown"),
        )
        raise RuntimeError(_safe_error(source, "Prometheus returned a query error."))

    results = payload["data"]["result"]
    cap = settings.prometheus_max_results
    if len(results) > cap:
        logger.warning("Result capped: source=%s got=%d returning=%d", source.name, len(results), cap)
        results = results[:cap]
    return results


def query_range(promql, source, range_str, step="5m"):
    """Execute a Prometheus range query (GET /api/v1/query_range)."""
    settings = get_settings()
    url = f"{source.url}/api/v1/query_range"
    end = int(time.time())
    start = end - _range_to_seconds(range_str)
    logger.debug("query_range source=%s range=%s query_hash=%s", source.name, range_str, hash(promql))
    try:
        kwargs = _get_request_kwargs(source)
        response = httpx.get(
            url,
            params={"query": promql, "start": start, "end": end, "step": step},
            timeout=settings.prometheus_timeout_seconds,
            **kwargs,
        )
        response.raise_for_status()
    except httpx.TimeoutException:
        logger.warning("Prometheus range query timed out. source=%s", source.name)
        raise RuntimeError(_safe_error(source, "request timed out. Please try again later."))
    except httpx.HTTPStatusError as exc:
        _handle_http_error(exc, source)
    except httpx.RequestError:
        logger.exception("Prometheus connection error. source=%s", source.name)
        raise RuntimeError(_safe_error(source, "connection failed. Check network and endpoint config."))

    payload = response.json()
    if payload.get("status") != "success":
        logger.error(
            "Prometheus range error. source=%s error_type=%s",
            source.name, payload.get("errorType", "unknown"),
        )
        raise RuntimeError(_safe_error(source, "Prometheus returned a query error."))

    results = payload["data"]["result"]
    cap = settings.prometheus_max_results
    if len(results) > cap:
        results = results[:cap]
    return results
