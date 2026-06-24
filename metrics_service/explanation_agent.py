"""
explanation_agent.py — Anthropic Claude AI Analysis Layer (Explanation Agent).

Receives structured JSON from backend metric tools and returns a
human-readable explanation. No PromQL, no remediation, no secrets.
"""

import json
import logging
from typing import Any

import anthropic

from .config import get_settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """
You are an AI observability assistant for Azure Kubernetes Service (AKS).

You receive structured JSON metric results from a backend metrics service.
Your job is to explain those results clearly and concisely in plain English.

Rules you must always follow:
1. Base every statement on the data provided. Never invent metrics.
2. Identify and highlight abnormal patterns only when the data supports it.
3. Suggest read-only investigation steps such as reviewing logs, dashboards,
   deployment history, or recent alerts.
4. Never restart pods, scale workloads, roll back deployments, modify
   Kubernetes resources, or run kubectl commands.
5. Never execute or suggest arbitrary PromQL queries.
6. State uncertainty clearly when the data does not prove a root cause.
7. Keep responses concise and suitable for a Microsoft Teams message.

Response format:
- Summary: one or two sentences describing the main finding.
- Details: bullet list of key data points.
- Suggested investigation: read-only follow-up steps (when warranted).
""".strip()


def analyze_metrics(tool_name: str, metric_data: dict[str, Any], user_question: str = "") -> str:
    """
    Send structured *metric_data* to Claude for analysis.

    Returns a plain-English explanation suitable for Teams.
    Raises RuntimeError on API failure.
    """
    settings = get_settings()

    if not settings.anthropic_api_key:
        logger.error("ANTHROPIC_API_KEY is not configured.")
        raise RuntimeError("AI analysis is not available. The API key is not configured.")

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    user_parts = [f"Metric tool used: {tool_name}"]
    if user_question:
        user_parts.append(f"User question: {user_question}")
    user_parts.append(f"Metric data:\n```json\n{json.dumps(metric_data, indent=2)}\n```")
    user_message = "\n\n".join(user_parts)

    try:
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=settings.anthropic_max_tokens,
            system=SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": user_message},
            ],
        )
        return response.content[0].text.strip()
    except anthropic.AuthenticationError:
        logger.error("Anthropic authentication failed.")
        raise RuntimeError("AI analysis is not available. Please check the API configuration.")
    except anthropic.RateLimitError:
        logger.warning("Anthropic rate limit reached.")
        raise RuntimeError(
            "AI analysis is temporarily unavailable due to rate limiting. "
            "Please try again in a moment."
        )
    except anthropic.APIError as exc:
        logger.error("Anthropic API error: %s", type(exc).__name__)
        raise RuntimeError("AI analysis encountered an error. Please try again later.")