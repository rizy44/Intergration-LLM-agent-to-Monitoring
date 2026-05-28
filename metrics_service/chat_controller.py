"""
chat_controller.py — Single orchestration layer for all chat entry points.

All three routes (/chat, /teams/chat, /teams/webhook) delegate to
handle_chat_message(). No route handler contains Conversation Agent,
Tool Dispatcher, or Explanation Agent calls directly.

Remediation detection uses a compiled action-phrase regex — not bare
substring matching — so "show pod restarts" is correctly allowed while
"restart pod api-service" is refused without an Anthropic API call.
"""

import logging
import re
from typing import Literal

from pydantic import BaseModel

from .ai_agent import analyze_metrics
from .conversation_agent import parse_user_message
from .teams_sender import send_to_teams as _send_teams_message
from .tool_dispatcher import ToolDispatchError, dispatch_tool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Remediation detection — action-phrase regex, case-insensitive
# ---------------------------------------------------------------------------

_REMEDIATION_PATTERN = re.compile(
    r'\b(?:restart|reboot)\s+(?:pod|deployment|service|container|workload|all)\b'
    r'|\bscale\s+(?:down|up|out|in|deployment|replicas)\b'
    r'|\brollback\b|\broll\s+back\b'
    r'|\bdelete\s+(?:pod|deployment|namespace|service|resource)\b'
    r'|\bkubectl\b'
    r'|\bexec\s+into\b'
    r'|\bport[-\s]forward\b'
    r'|\bapply\s+(?:\w+\s+)?(?:manifest|yaml|config)\b'
    r'|\bpatch\s+deployment\b'
    r'|\btrigger\s+(?:pipeline|deploy|build|release)\b'
    r'|\bdeploy\s+(?:new|version|release)\b',
    re.IGNORECASE,
)


def is_remediation_request(message: str) -> bool:
    """Return True when message contains an imperative Kubernetes action phrase."""
    return bool(_REMEDIATION_PATTERN.search(message))


# ---------------------------------------------------------------------------
# Shared result model
# ---------------------------------------------------------------------------


class ChatControllerResult(BaseModel):
    status: Literal["answered", "needs_clarification", "refused", "unsupported", "error"]
    reply: str
    tool_used: str = ""
    agent_decision: dict = {}
    metric_data: dict = {}


# ---------------------------------------------------------------------------
# Teams send helper
# ---------------------------------------------------------------------------


def _try_send_teams(message: str, title: str = "AKS Metrics Assistant") -> None:
    """Send to Teams Incoming Webhook. Logs but never raises on failure."""
    try:
        _send_teams_message(message, title=title)
    except Exception:
        logger.warning("Could not send message to Teams (non-fatal).", exc_info=True)


# ---------------------------------------------------------------------------
# Core orchestration
# ---------------------------------------------------------------------------


def handle_chat_message(
    message: str,
    user: str | None = None,
    send_to_teams: bool = False,
    title: str = "AKS Metrics Assistant",
) -> ChatControllerResult:
    """
    Single orchestration function for all chat entry points.

    Flow:
      1. Trim; return error for empty input (no API call).
      2. Remediation guard via regex; return refused (no API call).
      3. Conversation Agent → branch on status.
      4. For ready: Tool Dispatcher → Explanation Agent.
      5. Optionally send reply to Teams Incoming Webhook (non-error statuses only).

    Parameters
    ----------
    message       Raw user text.
    user          Display name for Teams formatting (optional).
    send_to_teams If True, sends the reply via Incoming Webhook for
                  answered/refused/needs_clarification/unsupported statuses.
    title         Card title for Teams messages.
    """
    message = message.strip()
    if not message:
        return ChatControllerResult(status="error", reply="Message must not be empty.")

    # --- Remediation guard (no Anthropic call) ---
    if is_remediation_request(message):
        reply = (
            "Phase 1 is read-only. I cannot restart, scale, roll back, or modify "
            "workloads. I can help inspect metrics and suggest read-only investigation "
            "steps. Try asking: 'Show pod restarts in namespace prod' or "
            "'Is the cluster healthy?'"
        )
        result = ChatControllerResult(status="refused", reply=reply)
        if send_to_teams:
            _try_send_teams(reply, title)
        return result

    # --- Conversation Agent ---
    try:
        decision = parse_user_message(message)
    except RuntimeError as exc:
        return ChatControllerResult(
            status="error",
            reply=(
                f"The AI service is temporarily unavailable. "
                f"Please try again shortly. ({exc})"
            ),
        )

    agent_status = decision.get("status")

    if agent_status in ("needs_clarification", "refused", "unsupported"):
        reply = decision.get("message", "Could you provide more details?")
        result = ChatControllerResult(
            status=agent_status,
            reply=reply,
            agent_decision=decision,
        )
        if send_to_teams:
            _try_send_teams(reply, title)
        return result

    # --- Tool Dispatcher ---
    metric_request = decision.get("request", {})
    try:
        tool_name, metric_data = dispatch_tool(metric_request)
    except ToolDispatchError as exc:
        reply = str(exc)
        result = ChatControllerResult(
            status="needs_clarification",
            reply=reply,
            agent_decision=decision,
        )
        if send_to_teams:
            _try_send_teams(reply, title)
        return result
    except RuntimeError as exc:
        return ChatControllerResult(
            status="error",
            reply=(
                f"Could not retrieve metrics: {exc} "
                "Please check Prometheus connectivity and try again."
            ),
            agent_decision=decision,
        )

    # --- Explanation Agent ---
    try:
        explanation = analyze_metrics(
            tool_name=tool_name,
            metric_data=metric_data,
            user_question=message,
        )
    except RuntimeError as exc:
        return ChatControllerResult(
            status="error",
            reply=(
                f"Metrics were retrieved but AI explanation is temporarily "
                f"unavailable. ({exc})"
            ),
            tool_used=tool_name,
            metric_data=metric_data,
            agent_decision=decision,
        )

    result = ChatControllerResult(
        status="answered",
        reply=explanation,
        tool_used=tool_name,
        metric_data=metric_data,
        agent_decision=decision,
    )

    if send_to_teams:
        teams_message = (
            f"**{user}** asked: {message}\n\n{explanation}" if user else explanation
        )
        _try_send_teams(teams_message, title)

    logger.info(
        "chat answered: tool=%s source=%s",
        tool_name,
        metric_data.get("source", {}).get("name", "unknown"),
    )

    return result
