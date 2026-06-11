"""
alert_formatter.py — Rule-based formatter for Alertmanager webhook payloads.

Converts an Alertmanager webhook payload (schema version 4) into a concise
Microsoft Teams Markdown message. Pure rule-based: NO LLM is involved
anywhere in the alert path.

Security rules:
- No secrets, tokens, internal URLs, stack traces, or raw label dumps.
- Per-alert text is length-capped; final truncation happens in teams_sender.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Severity label -> indicator emoji
_SEVERITY_ICONS = {
    "critical": "🔴",
    "warning": "🟠",
}
_DEFAULT_SEVERITY_ICON = "⚪"

# Scope labels worth surfacing, in display order
_SCOPE_LABELS = ("node", "instance", "namespace", "pod", "service", "deployment")

# Cap each alert block to keep grouped messages readable
_MAX_BLOCK_LENGTH = 900


class AlertPayloadError(ValueError):
    """Raised when an Alertmanager payload is malformed."""


def validate_payload(payload: Any) -> list[dict]:
    """
    Validate the minimal Alertmanager v4 payload structure.

    Returns the list of alerts. Raises AlertPayloadError on malformed input.
    """
    if not isinstance(payload, dict):
        raise AlertPayloadError("Payload must be a JSON object.")

    if "status" not in payload:
        raise AlertPayloadError("Payload is missing 'status'.")

    alerts = payload.get("alerts")
    if not isinstance(alerts, list):
        raise AlertPayloadError("Payload is missing the 'alerts' array.")

    for alert in alerts:
        if not isinstance(alert, dict) or "labels" not in alert or "status" not in alert:
            raise AlertPayloadError("Each alert must contain 'labels' and 'status'.")

    return alerts


def format_alertmanager_payload(payload: dict) -> tuple[str, str]:
    """
    Format an Alertmanager webhook payload into a Teams message.

    Returns
    -------
    (title, body) : tuple[str, str]
        title — card title with firing/resolved state and alert count.
        body  — Markdown body, blocks joined with double newlines so Teams
                renders each on its own line.
    """
    alerts = validate_payload(payload)
    status = str(payload.get("status", "firing")).lower()
    count = len(alerts)

    if status == "resolved":
        title = f"✅ ALERT RESOLVED ({count})"
    else:
        title = f"🔴 ALERT FIRING ({count})"

    blocks = [_format_alert(alert) for alert in alerts]
    body = "\n\n".join(blocks)

    return title, body


def _format_alert(alert: dict) -> str:
    """Format a single alert into a Markdown block."""
    labels: dict = alert.get("labels", {}) or {}
    annotations: dict = alert.get("annotations", {}) or {}

    name = str(labels.get("alertname", "UnknownAlert"))
    severity = str(labels.get("severity", "")).lower()
    icon = _SEVERITY_ICONS.get(severity, _DEFAULT_SEVERITY_ICON)
    severity_text = severity if severity else "unspecified"

    lines = [f"{icon} **{name}** ({severity_text})"]

    summary = annotations.get("summary") or annotations.get("description") or ""
    if summary:
        lines.append(str(summary))

    scope_parts = [
        f"{label}: {labels[label]}" for label in _SCOPE_LABELS if labels.get(label)
    ]
    if scope_parts:
        lines.append(" | ".join(scope_parts))

    starts_at = str(alert.get("startsAt", ""))
    if starts_at and not starts_at.startswith("0001"):
        lines.append(f"Started: {_format_timestamp(starts_at)}")

    block = "\n\n".join(lines)
    if len(block) > _MAX_BLOCK_LENGTH:
        block = block[: _MAX_BLOCK_LENGTH - 4] + " ..."
    return block


def _format_timestamp(raw: str) -> str:
    """Render an RFC3339 timestamp as 'YYYY-MM-DD HH:MM UTC' (best effort)."""
    try:
        from datetime import datetime

        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return ts.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        # Fall back to the raw value rather than failing the whole alert
        return raw
