"""
ai_agent.py — Claude AI Analysis Layer.

This module is the ONLY place where the Anthropic API is called.
Claude receives structured JSON from backend metric tools and returns
a human-readable explanation.

Phase 1 constraints enforced here:
- Claude is never given raw PromQL to execute.
- Claude receives only structured metric JSON results.
- The system prompt forbids remediation, scaling, rollback, or kubectl.
- No secrets or internal URLs are sent to the Anthropic API.
"""

import json
import logging
from typing import Any

import anthropic

from .config import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt — defines Claude's Phase 1 behaviour
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyze_metrics(tool_name: str, metric_data: dict[str, Any], user_question: str = "") -> str:
    """
    Send structured *metric_data* to Claude for analysis.

    Parameters
    ----------
    tool_name : str
        The backend tool that produced the data (used for context).
    metric_data : dict
        Structured JSON result from a metric tool.
    user_question : str
        Original user question, if available (optional context).

    Returns
    -------
    str — Claude's plain-English explanation.
    """
    settings = get_settings()

    if not settings.anthropic_api_key:
        logger.error("ANTHROPIC_API_KEY is not configured.")
        raise RuntimeError(
            "AI analysis is not available. The API key is not configured."
        )

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    # Build user message — structured data only, no secrets
    user_parts = [f"Metric tool used: {tool_name}"]
    if user_question:
        user_parts.append(f"User question: {user_question}")
    user_parts.append(f"Metric data:\n```json\n{json.dumps(metric_data, indent=2)}\n```")
    user_message = "\n\n".join(user_parts)

    try:
        response = client.messages.create(
            model=settings.claude_model,
            max_tokens=settings.claude_max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        return response.content[0].text.strip()
    except anthropic.AuthenticationError:
        logger.error("Anthropic authentication failed.")
        raise RuntimeError(
            "AI analysis is not available. Please check the API configuration."
        )
    except anthropic.RateLimitError:
        logger.warning("Anthropic rate limit reached.")
        raise RuntimeError(
            "AI analysis is temporarily unavailable due to rate limiting. "
            "Please try again in a moment."
        )
    except anthropic.APIError as exc:
        logger.error("Anthropic API error: %s", type(exc).__name__)
        raise RuntimeError(
            "AI analysis encountered an error. Please try again later."
        )


def generate_daily_report_text(metrics_summary: dict[str, Any]) -> str:
    """
    Generate the full daily report text from a collected metrics summary.

    *metrics_summary* is a dict produced by daily_report.py containing
    results from all relevant metric tools across all configured sources.

    Returns a formatted string suitable for sending to Microsoft Teams.
    """
    settings = get_settings()

    if not settings.anthropic_api_key:
        raise RuntimeError(
            "AI analysis is not available. The API key is not configured."
        )

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    daily_prompt = (
        "Generate a Daily AKS Health Report for Microsoft Teams based on the "
        "following metrics collected over the last 24 hours from the selected "
        "Prometheus-compatible datasources for the daily report.\n\n"
        "Format:\n"
        "**Daily AKS Health Report**\n"
        "Time Range: Last 24 hours\n"
        "Scope: Selected metric sources\n"
        "Status: <Healthy / Healthy with warnings / Critical>\n\n"
        "**Summary:**\n"
        "- <overall cross-source summary>\n\n"
        "**Source Breakdown:**\n"
        "- <source name>: <key health, CPU, memory, pod findings>\n\n"
        "**Warnings:**\n"
        "- <only if there are warnings; include source name when relevant>\n\n"
        "**Suggested Investigation:**\n"
        "- <read-only steps, only if needed>\n\n"
        "Keep it concise. If a source has missing or unavailable metrics, mention "
        "that as a data availability issue without treating it as a confirmed "
        "cluster incident. Do not suggest remediation or kubectl commands.\n\n"
        f"Metrics data:\n```json\n{json.dumps(metrics_summary, indent=2)}\n```"
    )

    try:
        response = client.messages.create(
            model=settings.claude_model,
            max_tokens=settings.claude_max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": daily_prompt}],
        )
        return response.content[0].text.strip()
    except anthropic.APIError as exc:
        logger.error("Anthropic API error during daily report: %s", type(exc).__name__)
        raise RuntimeError(
            "Could not generate the daily report text. Please try again later."
        )
