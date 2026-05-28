"""
app.py - FastAPI application for the AKS Metrics Service.

Read-only. All endpoints accept an optional ?source= query parameter
to target a specific Prometheus instance from the registry.
If source is omitted, the backend auto-routes based on metric type.

Endpoints:
  GET  /health
  GET  /sources                          - list all configured sources
  GET  /metrics/cluster-health
  GET  /metrics/node-cpu
  GET  /metrics/node-memory
  GET  /metrics/pod-restarts
  GET  /metrics/unhealthy-pods
  GET  /metrics/namespace-usage
  GET  /metrics/top-consumers
  GET  /metrics/service-errors
  POST /chat                             - on-demand AI metric question (simple routing)
  POST /teams/chat                       - two-agent Teams chat flow (external caller)
  POST /teams/webhook                    - Teams Outgoing Webhook receiver (HMAC-validated)
  POST /daily-report                     - trigger daily report manually
"""

import json as _json
import logging
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

from .chat_controller import ChatControllerResult, handle_chat_message
from .config import get_settings, validate_hostname, validate_label, validate_range, validate_source_name
from .daily_report import run_daily_report
from .source_registry import get_registry
from .teams_bot import (
    build_teams_response,
    extract_message_text,
    extract_sender_name,
    validate_teams_signature,
)
from .tools.cluster_health import get_cluster_health
from .tools.namespace_usage import (
    get_namespace_resource_usage,
    get_top_resource_consuming_pods,
)
from .tools.node_cpu import get_node_cpu_usage
from .tools.node_memory import get_node_memory_usage
from .tools.pod_restarts import get_pod_restart_count
from .tools.service_errors import get_service_error_rate
from .tools.unhealthy_pods import get_unhealthy_pods

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=get_settings().log_level,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="AKS Metrics Service",
    description=(
        "Read-only multi-source metrics backend for the AI-powered AKS "
        "observability assistant. Phase 1 - no write or remediation endpoints.\n\n"
        "All endpoints accept an optional **?source=** parameter to target a "
        "specific Prometheus instance. If omitted, auto-routing applies."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ---------------------------------------------------------------------------
# Shared query parameter docs
# ---------------------------------------------------------------------------

SOURCE_QUERY = Query(
    description=(
        "Optional source name (from /sources). "
        "If omitted, the backend auto-selects based on metric type."
    ),
)
RANGE_QUERY = Query(description="Time range e.g. 1h, 6h, 24h, 7d")

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    question: str
    namespace: str | None = None
    service: str | None = None
    range: str | None = None
    source: str | None = None


class ChatResponse(BaseModel):
    question: str
    tool_used: str
    source_used: dict
    metric_data: dict
    ai_explanation: str


class TeamsChatRequest(BaseModel):
    """
    Incoming message from Microsoft Teams or a chatbox.

    The 'message' field contains the raw user question in natural language.
    The optional 'user' field is the Teams display name of the sender,
    used only for response formatting (never passed to Prometheus or Claude as input).
    """
    message: str
    user: str | None = None


class TeamsChatResponse(BaseModel):
    """
    Response returned by the /teams/chat endpoint.

    status: "answered" | "needs_clarification" | "refused" | "error"
    reply:  The human-readable text sent back to the user (and to Teams).
    tool_used: which backend tool was called (empty if none).
    agent_decision: the raw Conversation Agent JSON decision for debugging.
    """
    status: str
    reply: str
    tool_used: str = ""
    agent_decision: dict = {}


# ---------------------------------------------------------------------------
# Error helper
# ---------------------------------------------------------------------------


def _runtime_to_http(exc: RuntimeError) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc))


# ---------------------------------------------------------------------------
# Health + Sources
# ---------------------------------------------------------------------------


@app.get("/health", tags=["Health"])
def health_check():
    """Liveness and readiness probe."""
    return {"status": "ok", "service": "aks-metrics-service", "phase": "1"}


@app.get("/sources", tags=["Sources"])
def list_sources():
    """
    List all configured Prometheus sources (credentials-free).
    Use the 'name' field as the ?source= parameter in metric queries.
    """
    return {"sources": get_registry().safe_list()}


# ---------------------------------------------------------------------------
# Metric endpoints
# ---------------------------------------------------------------------------


@app.get("/metrics/cluster-health", tags=["Metrics"])
def cluster_health(source: Annotated[str | None, SOURCE_QUERY] = None):
    """Return overall AKS cluster health status."""
    try:
        _validate_source(source)
        return get_cluster_health(source_override=source)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise _runtime_to_http(exc)


HOSTNAME_QUERY = Query(
    description=(
        "Optional hostname filter. Single hostname (e.g. tl-sv02-agent) or "
        "comma-separated list (e.g. tl-sv01,tl-sv03)."
    ),
)


@app.get("/metrics/node-cpu", tags=["Metrics"])
def node_cpu(
    range: Annotated[str, RANGE_QUERY] = "24h",
    source: Annotated[str | None, SOURCE_QUERY] = None,
    hostname: Annotated[str | None, HOSTNAME_QUERY] = None,
):
    """Return per-node CPU usage. Filter by hostname or comma-separated list."""
    try:
        validate_range(range, get_settings().allowed_ranges_set)
        _validate_source(source)
        if hostname:
            validate_hostname(hostname)
        return get_node_cpu_usage(range=range, hostname=hostname, source_override=source)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise _runtime_to_http(exc)


@app.get("/metrics/node-memory", tags=["Metrics"])
def node_memory(
    range: Annotated[str, RANGE_QUERY] = "24h",
    source: Annotated[str | None, SOURCE_QUERY] = None,
    hostname: Annotated[str | None, HOSTNAME_QUERY] = None,
):
    """Return per-node memory usage. Filter by hostname or comma-separated list."""
    try:
        validate_range(range, get_settings().allowed_ranges_set)
        _validate_source(source)
        if hostname:
            validate_hostname(hostname)
        return get_node_memory_usage(range=range, hostname=hostname, source_override=source)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise _runtime_to_http(exc)


@app.get("/metrics/pod-restarts", tags=["Metrics"])
def pod_restarts(
    namespace: Annotated[str, Query(description="Kubernetes namespace")],
    range: Annotated[str, RANGE_QUERY] = "24h",
    source: Annotated[str | None, SOURCE_QUERY] = None,
):
    """Return pod restart counts in the given namespace."""
    try:
        validate_label(namespace, "namespace")
        validate_range(range, get_settings().allowed_ranges_set)
        _validate_source(source)
        return get_pod_restart_count(namespace=namespace, range=range, source_override=source)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise _runtime_to_http(exc)


@app.get("/metrics/unhealthy-pods", tags=["Metrics"])
def unhealthy_pods(
    namespace: Annotated[str, Query(description="Kubernetes namespace")],
    source: Annotated[str | None, SOURCE_QUERY] = None,
):
    """Return pods not in Running or Succeeded state."""
    try:
        validate_label(namespace, "namespace")
        _validate_source(source)
        return get_unhealthy_pods(namespace=namespace, source_override=source)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise _runtime_to_http(exc)


@app.get("/metrics/namespace-usage", tags=["Metrics"])
def namespace_usage(
    namespace: Annotated[str, Query(description="Kubernetes namespace")],
    range: Annotated[str, RANGE_QUERY] = "24h",
    source: Annotated[str | None, SOURCE_QUERY] = None,
):
    """Return aggregated CPU and memory usage for a namespace."""
    try:
        validate_label(namespace, "namespace")
        validate_range(range, get_settings().allowed_ranges_set)
        _validate_source(source)
        return get_namespace_resource_usage(namespace=namespace, range=range, source_override=source)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise _runtime_to_http(exc)


@app.get("/metrics/top-consumers", tags=["Metrics"])
def top_consumers(
    namespace: Annotated[str, Query(description="Kubernetes namespace")],
    range: Annotated[str, RANGE_QUERY] = "1h",
    source: Annotated[str | None, SOURCE_QUERY] = None,
):
    """Return top CPU and memory consuming pods."""
    try:
        validate_label(namespace, "namespace")
        validate_range(range, get_settings().allowed_ranges_set)
        _validate_source(source)
        return get_top_resource_consuming_pods(namespace=namespace, range=range, source_override=source)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise _runtime_to_http(exc)


@app.get("/metrics/service-errors", tags=["Metrics"])
def service_errors(
    service: Annotated[str, Query(description="Service name")],
    namespace: Annotated[str, Query(description="Kubernetes namespace")],
    range: Annotated[str, RANGE_QUERY] = "1h",
    source: Annotated[str | None, SOURCE_QUERY] = None,
):
    """Return HTTP 5xx error rate for a service."""
    try:
        validate_label(service, "service")
        validate_label(namespace, "namespace")
        validate_range(range, get_settings().allowed_ranges_set)
        _validate_source(source)
        return get_service_error_rate(
            service=service, namespace=namespace, range=range, source_override=source
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise _runtime_to_http(exc)


# ---------------------------------------------------------------------------
# On-demand chat endpoint (simple keyword routing, no Teams webhook)
# ---------------------------------------------------------------------------


@app.post("/chat", response_model=ChatResponse, tags=["Chat"])
def chat(request: ChatRequest):
    """
    On-demand metric question via direct API call.

    Accepts a natural language question and returns a Claude AI explanation
    alongside the raw metric data. Phase 1: read-only.
    """
    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question must not be empty.")

    result = handle_chat_message(question, send_to_teams=False)

    if result.status == "error":
        raise HTTPException(status_code=400, detail=result.reply)

    return ChatResponse(
        question=question,
        tool_used=result.tool_used,
        source_used=result.metric_data.get("source", {}),
        metric_data=result.metric_data,
        ai_explanation=result.reply,
    )


# ---------------------------------------------------------------------------
# Teams two-agent chat endpoint (external caller, sends reply via Incoming Webhook)
# ---------------------------------------------------------------------------


@app.post("/teams/chat", response_model=TeamsChatResponse, tags=["Teams"])
def teams_chat(request: TeamsChatRequest):
    """
    Two-agent Teams chat flow for external API callers (e.g. Power Automate).

    Sends the reply to the configured Teams Incoming Webhook and also returns
    it in the HTTP response body. Use POST /teams/webhook for direct Teams
    Outgoing Webhook integration (reply travels via HTTP response only).
    """
    result = handle_chat_message(
        request.message,
        user=request.user,
        send_to_teams=True,
        title="AKS Metrics Assistant",
    )

    if result.status == "error":
        raise HTTPException(status_code=400, detail=result.reply)

    return TeamsChatResponse(
        status=result.status,
        reply=result.reply,
        tool_used=result.tool_used,
        agent_decision=result.agent_decision,
    )


# ---------------------------------------------------------------------------
# Teams Outgoing Webhook endpoint (HMAC-validated, reply in HTTP response)
# ---------------------------------------------------------------------------


@app.post("/teams/webhook", tags=["Teams"])
async def teams_outgoing_webhook(request: Request):
    """
    Teams Outgoing Webhook receiver.

    How it works:
    1. User types a message and @mentions the bot in a Teams channel.
    2. Teams POSTs to this endpoint with an HMAC-SHA256 Authorization header.
    3. We validate the signature using TEAMS_OUTGOING_WEBHOOK_SECRET.
    4. We extract the clean message text (stripping the @mention tag).
    5. We run the two-agent flow: Conversation Agent -> Tool Dispatcher -> Explanation Agent.
    6. We return {"type": "message", "text": "..."} in the HTTP response.
    7. Teams renders that text as a bot reply in the channel thread.

    Security: requests without a valid HMAC signature are rejected with HTTP 401.
    The reply travels back via the HTTP response body only - no Incoming Webhook call.
    """
    settings = get_settings()

    if not settings.teams_outgoing_webhook_secret:
        logger.error("TEAMS_OUTGOING_WEBHOOK_SECRET is not configured.")
        raise HTTPException(
            status_code=503,
            detail="Teams Outgoing Webhook is not configured on this service.",
        )

    body_bytes = await request.body()
    auth_header = request.headers.get("Authorization")

    if not validate_teams_signature(
        auth_header, body_bytes, settings.teams_outgoing_webhook_secret
    ):
        raise HTTPException(status_code=401, detail="Invalid Teams webhook signature.")

    try:
        payload = _json.loads(body_bytes)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    if payload.get("type") != "message":
        logger.debug("Ignoring non-message Teams event: type=%s", payload.get("type"))
        return build_teams_response("")

    raw_text = extract_message_text(payload)
    sender = extract_sender_name(payload)

    if not raw_text:
        return build_teams_response(
            "Hi! Ask me about your AKS cluster metrics. "
            "Example: *show pod restarts in namespace prod*"
        )

    logger.info(
        "teams/webhook message from='%s' text_len=%d", sender, len(raw_text)
    )

    result = handle_chat_message(raw_text, user=sender, send_to_teams=False)
    return build_teams_response(result.reply)


# ---------------------------------------------------------------------------
# Daily report trigger
# ---------------------------------------------------------------------------


@app.post("/daily-report", tags=["Reports"])
def trigger_daily_report():
    """Manually trigger the daily AKS health report (for testing)."""
    try:
        run_daily_report()
        return {"status": "sent", "message": "Daily report sent to Teams."}
    except Exception:
        logger.exception("Unexpected error triggering daily report.")
        raise HTTPException(status_code=500, detail="Daily report failed. Check logs.")


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _validate_source(source: str | None) -> None:
    """Validate source name format and existence in the registry."""
    if source is None:
        return
    validate_source_name(source)
    get_registry().get_by_name(source)
