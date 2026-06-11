"""
azure_alert_trigger.py — CronJob entry point for Azure metrics poll alerts.

Thin trigger: POSTs to the long-running metrics service so the alert
cooldown state lives in one place (the service process), not in this
ephemeral CronJob pod.

Usage (Kubernetes CronJob, every 15 minutes):
    python -m metrics_service.azure_alert_trigger

Required environment variables:
    METRICS_SERVICE_URL   e.g. http://moni-agent.monitoring.svc.cluster.local:8000
    ALERT_WEBHOOK_TOKEN   bearer token shared with the service
"""

import logging
import os
import sys

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 120  # full Azure collection cycle can take a while


def main() -> int:
    base_url = os.environ.get("METRICS_SERVICE_URL", "").rstrip("/")
    token = os.environ.get("ALERT_WEBHOOK_TOKEN", "")

    if not base_url:
        logger.error("METRICS_SERVICE_URL is not set.")
        return 1
    if not token:
        logger.error("ALERT_WEBHOOK_TOKEN is not set.")
        return 1

    url = f"{base_url}/alerts/azure-check"

    try:
        response = httpx.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except httpx.TimeoutException:
        logger.error("Azure alert check trigger timed out.")
        return 1
    except httpx.HTTPStatusError as exc:
        logger.error("Azure alert check trigger failed: HTTP %s", exc.response.status_code)
        return 1
    except httpx.RequestError as exc:
        logger.error("Azure alert check trigger connection error: %s", type(exc).__name__)
        return 1

    logger.info("Azure alert check completed: %s", response.json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
