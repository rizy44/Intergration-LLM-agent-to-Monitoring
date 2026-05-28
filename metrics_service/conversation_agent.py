"""
conversation_agent.py — Conversation Agent (first Claude pass).

This agent receives a raw user message from Teams or the chatbox and
decides what metric query to run — or asks for clarification, refuses
a request that falls outside Phase 1 scope, or returns unsupported for
valid metric questions that have no Phase 1 controlled tool.

The agent NEVER generates PromQL.
The agent NEVER calls Prometheus directly.
The agent ONLY returns a structured JSON decision that the backend
dispatcher can safely execute against the whitelisted tool list.

Output contract
---------------
The agent must always return valid JSON with one of three shapes:

  {"status": "ready", "request": {
      "tool": "<allowed_tool_name>",
      "namespace": "<str|null>",
      "service":   "<str|null>",
      "range":     "<allowed_range|null>",
      "source":    "<source_name|null>"
  }}

  {"status": "needs_clarification", "message": "<question for user>"}

  {"status": "refused",     "message": "<reason — no remediation details>"}

  {"status": "unsupported", "message": "<valid metric question, no Phase 1 tool>"}

Allowed tools (must match ALLOWED_TOOLS below exactly):
  get_cluster_health
  get_node_cpu_usage
  get_node_memory_usage
  get_pod_restart_count
  get_unhealthy_pods
  get_namespace_resource_usage
  get_service_error_rate
  get_top_resource_consuming_pods

Allowed ranges: 1h, 6h, 12h, 24h, 2d, 7d
"""

import json
import logging
from typing import Any

import anthropic

from .config import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Allowed tool names — single source of truth shared with tool_dispatcher.py
# ---------------------------------------------------------------------------

ALLOWED_TOOLS = frozenset([
    "get_cluster_health",
    "get_node_cpu_usage",
    "get_node_memory_usage",
    "get_pod_restart_count",
    "get_unhealthy_pods",
    "get_namespace_resource_usage",
    "get_service_error_rate",
    "get_top_resource_consuming_pods",
])

ALLOWED_RANGES = frozenset(["1h", "6h", "12h", "24h", "2d", "7d"])

# ---------------------------------------------------------------------------
# System prompt — strict JSON-only output, no PromQL, no remediation
# ---------------------------------------------------------------------------

CONVERSATION_SYSTEM_PROMPT = """
You are the Conversation Agent for an AI-powered AKS observability assistant.

Your ONLY job is to parse a user's metric question and return a structured JSON
decision. You must NEVER answer the question yourself, explain metrics, or
suggest any action other than which backend tool to call.

=== OUTPUT FORMAT ===

You must always return ONLY raw JSON — no markdown, no commentary, no prose.
One of these four shapes:

If you have enough information to call a tool:
{"status": "ready", "request": {"tool": "<tool_name>", "namespace": "<ns or null>", "service": "<svc or null>", "range": "<range or null>", "source": "<source or null>"}}

If you need more information from the user:
{"status": "needs_clarification", "message": "<one clear question>"}

If the request asks for remediation, infrastructure modification, kubectl, PromQL, or any write action:
{"status": "refused", "message": "<brief explanation of why it is out of scope>"}

If the request is a valid read-only metric question but no Phase 1 controlled tool covers it:
{"status": "unsupported", "message": "<acknowledge the valid question, explain no Phase 1 tool exists, list what is supported>"}

=== ALLOWED TOOLS ===

get_cluster_health          — overall cluster status (no args required)
get_node_cpu_usage          — per-node CPU (requires: range)
get_node_memory_usage       — per-node memory (requires: range)
get_pod_restart_count       — restart counts (requires: namespace, range)
get_unhealthy_pods          — pods not Running/Succeeded (requires: namespace)
get_namespace_resource_usage — CPU+memory for a namespace (requires: namespace, range)
get_service_error_rate      — HTTP 5xx rate (requires: service, namespace, range)
get_top_resource_consuming_pods — top pods by CPU/memory (requires: namespace, range)

=== ALLOWED RANGES ===

1h, 6h, 12h, 24h, 2d, 7d

If the user mentions "today" or "last 24 hours" use "24h".
If no range is given and a range is required, use "24h" as default.

=== RULES ===

1. Never generate PromQL.
2. Never suggest restarting, scaling, rolling back, or modifying Kubernetes resources.
3. Never call any tool not in the ALLOWED TOOLS list above.
4. If the user asks about a namespace but does not say which one, ask for clarification.
5. If the user asks about service errors but does not say which service, ask for clarification.
6. If the request is ambiguous but can be reasonably mapped to a tool, prefer "ready".
7. If no tool matches the request but it is a valid read-only metric question,
   return "unsupported" — NOT "refused" — with a message acknowledging the valid
   intent and listing the supported queries.
   Only return "refused" for remediation, write actions, or kubectl requests.
8. Never include credentials, URLs, internal hostnames, or PromQL in any output.
9. The "source" field is optional — set it to null unless the user explicitly
   mentions a specific data source by name.
""".strip()

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_user_message(user_message: str) -> dict[str, Any]:
    """
    Send *user_message* to the Conversation Agent.

    Returns a dict with one of:
      {"status": "ready",               "request": {...}}
      {"status": "needs_clarification", "message": "..."}
      {"status": "refused",             "message": "..."}
      {"status": "unsupported",         "message": "..."}

    Raises RuntimeError on Anthropic API failure.
    """
    settings = get_settings()

    if not settings.anthropic_api_key:
        logger.error("ANTHROPIC_API_KEY is not configured.")
        raise RuntimeError(
            "AI Conversation Agent is not available. The API key is not configured."
        )

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    logger.debug("ConversationAgent: processing user message (len=%d)", len(user_message))

    try:
        response = client.messages.create(
            model=settings.claude_model,
            max_tokens=512,
            system=CONVERSATION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        raw = response.content[0].text.strip()
    except anthropic.AuthenticationError:
        logger.error("Anthropic authentication failed (ConversationAgent).")
        raise RuntimeError(
            "AI service is not available. Please check the API configuration."
        )
    except anthropic.RateLimitError:
        logger.warning("Anthropic rate limit reached (ConversationAgent).")
        raise RuntimeError(
            "AI service is temporarily unavailable due to rate limiting. "
            "Please try again in a moment."
        )
    except anthropic.APIError as exc:
        logger.error("Anthropic API error (ConversationAgent): %s", type(exc).__name__)
        raise RuntimeError("AI service encountered an error. Please try again later.")

    return _parse_and_validate(raw)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_and_validate(raw: str) -> dict[str, Any]:
    """Parse raw JSON from the Conversation Agent and validate the schema."""
    # Strip markdown code fences if the model wraps output anyway
    clean = raw.strip()
    if clean.startswith("```"):
        lines = clean.splitlines()
        clean = "\n".join(
            line for line in lines if not line.strip().startswith("```")
        ).strip()

    try:
        parsed = json.loads(clean)
    except json.JSONDecodeError:
        logger.error(
            "ConversationAgent returned non-JSON output (first 200 chars): %s",
            clean[:200],
        )
        # Degrade gracefully — surface as clarification request
        return {
            "status": "needs_clarification",
            "message": (
                "I couldn't understand that request. "
                "Could you rephrase your metric question? "
                "For example: 'Show me pod restarts in namespace prod over the last 24 hours.'"
            ),
        }

    status = parsed.get("status")
    if status not in ("ready", "needs_clarification", "refused", "unsupported"):
        logger.error("ConversationAgent returned unknown status: %s", status)
        return {
            "status": "needs_clarification",
            "message": (
                "I couldn't parse that request. "
                "Please rephrase your metric question."
            ),
        }

    if status == "ready":
        request = parsed.get("request", {})
        tool = request.get("tool", "")
        if tool not in ALLOWED_TOOLS:
            logger.warning(
                "ConversationAgent chose non-whitelisted tool '%s'. Returning unsupported.", tool
            )
            return {
                "status": "unsupported",
                "message": (
                    f"The tool '{tool}' is not available in Phase 1. "
                    "Supported queries: cluster health, node CPU/memory, "
                    "pod restarts, unhealthy pods, namespace usage, "
                    "service error rate, top resource consumers."
                ),
            }
        rng = request.get("range")
        if rng and rng not in ALLOWED_RANGES:
            logger.warning(
                "ConversationAgent chose non-allowed range '%s'. Defaulting to 24h.", rng
            )
            request["range"] = "24h"
            parsed["request"] = request

    return parsed
