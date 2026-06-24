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

from .explanation_agent import analyze_metrics
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


def decide_chat_message(
    message: str,
    history: list[dict] | None = None,
) -> tuple[str, "ChatControllerResult | dict"]:
    """
    Fast decision stage: input guards + Conversation Agent only.
    No metric query, no Explanation Agent, no Teams send.

    Returns
    -------
    ("result", ChatControllerResult)  for empty input, remediation refusal,
                                      agent error, clarification / refused /
                                      unsupported — the reply is final.
    ("ready", decision_dict)          when the Conversation Agent mapped the
                                      message to a tool; pass the dict to
                                      execute_chat_decision().
    """
    message = message.strip()
    if not message:
        return "result", ChatControllerResult(
            status="error", reply="Message must not be empty."
        )

    # --- Remediation guard (no API call) ---
    if is_remediation_request(message):
        reply = (
            "Phase 1 is read-only. I cannot restart, scale, roll back, or modify "
            "workloads. I can help inspect metrics and suggest read-only investigation "
            "steps. Try asking: 'Show pod restarts in namespace prod' or "
            "'Is the cluster healthy?'"
        )
        return "result", ChatControllerResult(status="refused", reply=reply)

    # --- Conversation Agent ---
    try:
        decision = parse_user_message(message, history=history)
    except RuntimeError as exc:
        return "result", ChatControllerResult(
            status="error",
            reply=(
                f"The AI service is temporarily unavailable. "
                f"Please try again shortly. ({exc})"
            ),
        )

    agent_status = decision.get("status")

    if agent_status in ("needs_clarification", "refused", "unsupported"):
        reply = decision.get("message", "Could you provide more details?")
        return "result", ChatControllerResult(
            status=agent_status,
            reply=reply,
            agent_decision=decision,
        )

    return "ready", decision


def handle_chat_message(
    message: str,
    user: str | None = None,
    send_to_teams: bool = False,
    title: str = "AKS Metrics Assistant",
    history: list[dict] | None = None,
) -> ChatControllerResult:
    """
    Single orchestration function for all chat entry points.

    Flow:
      1. Trim; return error for empty input (no API call).
      2. Remediation guard via regex; return refused (no API call).
      3. Conversation Agent (with optional history) → branch on status.
      4. For ready: Tool Dispatcher → Explanation Agent.
      5. Optionally send reply to Teams Incoming Webhook (non-error statuses only).

    Parameters
    ----------
    message       Raw user text.
    user          Display name for Teams formatting (optional).
    send_to_teams If True, sends the reply via Incoming Webhook for
                  answered/refused/needs_clarification/unsupported statuses.
    title         Card title for Teams messages.
    history       Optional prior conversation turns for multi-turn context.
                  Each dict: {"role": "user"|"assistant", "content": "..."}.
    """
    stage, payload = decide_chat_message(message, history=history)

    if stage == "result":
        result: ChatControllerResult = payload
        if send_to_teams and result.status != "error":
            _try_send_teams(result.reply, title)
        return result

    return execute_chat_decision(
        payload, message, user=user, send_to_teams=send_to_teams, title=title
    )


def execute_chat_decision(
    decision: dict,
    message: str,
    user: str | None = None,
    send_to_teams: bool = False,
    title: str = "AKS Metrics Assistant",
) -> ChatControllerResult:
    """
    Slow execution stage for a 'ready' Conversation Agent decision:
    Tool Dispatcher → Explanation Agent → optional Teams send.
    """
    message = message.strip()

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
