"""
teams_sender.py — Microsoft Teams Incoming Webhook integration.

Sends formatted messages to a configured Teams channel using the
Incoming Webhook connector.

Security rules:
- Webhook URL comes from config only.
- No secrets, tokens, stack traces, or internal URLs are included in messages.
- Messages are truncated if they exceed Teams' maximum payload size.
"""

import logging

import httpx

from .config import get_settings

logger = logging.getLogger(__name__)

# Teams Incoming Webhook maximum message length (characters)
TEAMS_MAX_LENGTH = 28_000


def send_to_teams(message: str, title: str = "AKS Metrics Assistant") -> None:
    """
    Send *message* to the configured Microsoft Teams channel.

    Parameters
    ----------
    message : str
        The Markdown-formatted message body.
    title : str
        Card title shown in Teams.

    Raises
    ------
    RuntimeError
        When the webhook URL is not configured or the HTTP request fails.
    """
    settings = get_settings()

    if not settings.teams_webhook_url:
        logger.error("TEAMS_WEBHOOK_URL is not configured.")
        raise RuntimeError("Teams integration is not configured.")

    # Truncate if needed to avoid Teams payload rejection
    if len(message) > TEAMS_MAX_LENGTH:
        message = message[: TEAMS_MAX_LENGTH - 100] + "\n\n*(message truncated)*"

    payload = _build_adaptive_card(title, message)

    try:
        response = httpx.post(
            settings.teams_webhook_url,
            json=payload,
            timeout=15,
        )
        response.raise_for_status()
    except httpx.TimeoutException:
        logger.warning("Teams webhook request timed out.")
        raise RuntimeError("Could not deliver the Teams message: request timed out.")
    except httpx.HTTPStatusError as exc:
        logger.error(
            "Teams webhook HTTP error status=%s", exc.response.status_code
        )
        raise RuntimeError(
            f"Could not deliver the Teams message: HTTP {exc.response.status_code}."
        )
    except httpx.RequestError:
        logger.exception("Teams webhook connection error.")
        raise RuntimeError(
            "Could not deliver the Teams message: connection error."
        )

    logger.info("Teams message delivered successfully. title=%s", title)


def send_daily_report(report_text: str) -> None:
    """Convenience wrapper for sending the daily production health report."""
    send_to_teams(report_text, title="Daily Production Health Report")


def send_error_to_teams(error_message: str) -> None:
    """
    Send a safe, user-facing error notification to Teams.
    Never include internal details, secrets, or stack traces.
    """
    safe_message = (
        f"⚠️ **AKS Metrics Assistant — Error**\n\n{error_message}\n\n"
        "Please check the metrics service health or try again later."
    )
    try:
        send_to_teams(safe_message, title="AKS Metrics Assistant — Error")
    except RuntimeError:
        # Swallow send errors in error handler to avoid infinite loops
        logger.error("Could not send error notification to Teams.")


# ---------------------------------------------------------------------------
# Message formatting helpers
# ---------------------------------------------------------------------------


_REPORT_DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"


def _build_adaptive_card(title: str, body: str) -> dict:
    """
    Build a Teams MessageCard payload.

    Splits the report body on the section divider so each logical block
    (header, AKS clusters, each project) becomes its own Teams section.
    sections[].text preserves newlines correctly; activityText collapses them.
    """
    chunks = [c.strip() for c in body.split(_REPORT_DIVIDER) if c.strip()]

    sections = []
    for i, chunk in enumerate(chunks):
        section: dict = {"text": chunk, "markdown": True}
        if i > 0:
            section["separator"] = True
        sections.append(section)

    return {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "themeColor": "0076D7",
        "summary": title,
        "sections": sections,
    }
