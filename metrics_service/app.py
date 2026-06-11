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
  POST /alerts/alertmanager              - Alertmanager webhook receiver (bearer-token auth)
  POST /alerts/azure-check               - run one Azure metrics alert evaluation cycle
"""

import json as _json
import logging
from typing import Annotated

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

import hmac as _hmac

from .alert_formatter import AlertPayloadError, format_alertmanager_payload
from .azure_alert_check import run_azure_alert_check
from .chat_controller import ChatControllerResult, handle_chat_message
from .teams_sender import send_alert_to_teams
from .config import get_settings, validate_azure_name, validate_cluster_name, validate_hostname, validate_label, validate_range, validate_source_name, validate_workload_name
from .daily_report import run_daily_report
from .datasources import get_azure_registry, get_registry
from .teams_bot import (
    build_teams_response,
    extract_message_text,
    extract_sender_name,
    validate_teams_signature,
)
from .datasources.prometheus.tools import (
    get_cluster_health,
    get_namespace_resource_usage,
    get_top_resource_consuming_pods,
    get_node_cpu_usage,
    get_node_memory_usage,
    get_pod_restart_count,
    get_k8s_namespace_overview,
    get_k8s_service_detail,
    get_k8s_services,
    get_k8s_workload_detail,
    get_k8s_workloads,
    get_service_error_rate,
    get_unhealthy_pods,
)
from .datasources.azure_monitor.tools import (
    get_app_service_performance,
    list_azure_resources,
    get_mysql_performance,
    get_postgres_performance,
    get_redis_performance,
    get_service_bus_performance,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=get_settings().log_level,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)

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
    history: list[dict] | None = None


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
    used only for response formatting (never passed to Prometheus or AI as input).
    The optional 'history' field carries prior conversation turns so the AI can
    resolve follow-up answers (e.g. user answers "prod" after being asked for namespace).
    Each history entry: {"role": "user"|"assistant", "content": "..."}.
    """
    message: str
    user: str | None = None
    history: list[dict] | None = None


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
    List all configured sources (Prometheus + Azure Monitor), credentials-free.
    Prometheus sources: use the 'name' field as the ?source= parameter.
    Azure Monitor sources: used by /azure/* endpoints.
    """
    sources = get_registry().safe_list()
    try:
        sources = sources + get_azure_registry().safe_list()
    except (ValueError, KeyError):
        pass  # Azure Monitor not configured — omit silently
    return {"sources": sources}


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
# K8s cluster-aware metric endpoints
# ---------------------------------------------------------------------------

CLUSTER_QUERY = Query(description="AKS cluster name (e.g. wp-aks-uat)")
NAMESPACE_QUERY = Query(description="Kubernetes namespace")


@app.get("/metrics/k8s/namespace-overview", tags=["K8s Metrics"])
def k8s_namespace_overview(
    cluster: Annotated[str, CLUSTER_QUERY],
    namespace: Annotated[str, NAMESPACE_QUERY],
    range: Annotated[str, RANGE_QUERY] = "1h",
    source: Annotated[str | None, SOURCE_QUERY] = None,
):
    """Namespace-level CPU and memory usage."""
    try:
        validate_cluster_name(cluster)
        validate_label(namespace, "namespace")
        validate_range(range, get_settings().allowed_ranges_set)
        _validate_source(source)
        return get_k8s_namespace_overview(cluster=cluster, namespace=namespace, range=range, source_override=source)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise _runtime_to_http(exc)


@app.get("/metrics/k8s/workloads", tags=["K8s Metrics"])
def k8s_workloads(
    cluster: Annotated[str, CLUSTER_QUERY],
    namespace: Annotated[str, NAMESPACE_QUERY],
    source: Annotated[str | None, SOURCE_QUERY] = None,
):
    """List all Deployments, StatefulSets, and DaemonSets in a namespace with replica status."""
    try:
        validate_cluster_name(cluster)
        validate_label(namespace, "namespace")
        _validate_source(source)
        return get_k8s_workloads(cluster=cluster, namespace=namespace, source_override=source)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise _runtime_to_http(exc)


@app.get("/metrics/k8s/workloads/{workload_name}", tags=["K8s Metrics"])
def k8s_workload_detail(
    workload_name: str,
    cluster: Annotated[str, CLUSTER_QUERY],
    namespace: Annotated[str, NAMESPACE_QUERY],
    range: Annotated[str, RANGE_QUERY] = "1h",
    source: Annotated[str | None, SOURCE_QUERY] = None,
):
    """CPU/memory usage and per-pod breakdown for one named workload."""
    try:
        validate_cluster_name(cluster)
        validate_label(namespace, "namespace")
        validate_workload_name(workload_name)
        validate_range(range, get_settings().allowed_ranges_set)
        _validate_source(source)
        return get_k8s_workload_detail(
            cluster=cluster, namespace=namespace, workload_name=workload_name,
            range=range, source_override=source,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise _runtime_to_http(exc)


@app.get("/metrics/k8s/services", tags=["K8s Metrics"])
def k8s_services(
    cluster: Annotated[str, CLUSTER_QUERY],
    namespace: Annotated[str, NAMESPACE_QUERY],
    source: Annotated[str | None, SOURCE_QUERY] = None,
):
    """List all Kubernetes Services in a namespace with endpoint health status."""
    try:
        validate_cluster_name(cluster)
        validate_label(namespace, "namespace")
        _validate_source(source)
        return get_k8s_services(cluster=cluster, namespace=namespace, source_override=source)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise _runtime_to_http(exc)


@app.get("/metrics/k8s/services/{service_name}", tags=["K8s Metrics"])
def k8s_service_detail(
    service_name: str,
    cluster: Annotated[str, CLUSTER_QUERY],
    namespace: Annotated[str, NAMESPACE_QUERY],
    source: Annotated[str | None, SOURCE_QUERY] = None,
):
    """Detail for one Kubernetes Service: type, cluster IP, and endpoint health."""
    try:
        validate_cluster_name(cluster)
        validate_label(namespace, "namespace")
        validate_label(service_name, "service")
        _validate_source(source)
        return get_k8s_service_detail(
            cluster=cluster, namespace=namespace, service_name=service_name, source_override=source,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
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

    result = handle_chat_message(question, send_to_teams=False, history=request.history)

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
        history=request.history,
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


def _process_webhook_in_background(message: str, sender: str | None) -> None:
    """Background task: run full two-agent flow and push result via Incoming Webhook."""
    try:
        handle_chat_message(message, user=sender, send_to_teams=True)
    except Exception:
        logger.exception(
            "Background webhook processing failed for message from='%s'", sender
        )


@app.post("/teams/webhook", tags=["Teams"])
async def teams_outgoing_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Teams Outgoing Webhook receiver.

    How it works:
    1. User types a message and @mentions the bot in a Teams channel.
    2. Teams POSTs to this endpoint with an HMAC-SHA256 Authorization header.
    3. We validate the signature and return an immediate acknowledgement (<5s).
    4. The full two-agent flow runs in a background task.
    5. The result is pushed to the Teams channel via the Incoming Webhook (TEAMS_WEBHOOK_URL).

    Why async: Teams Outgoing Webhook has a hard 5-second response timeout.
    The full pipeline (Conversation Agent → metric query → Explanation Agent) can
    take 5–15s, so we must acknowledge immediately and deliver the answer separately.

    Security: requests without a valid HMAC signature are rejected with HTTP 401.
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

    background_tasks.add_task(_process_webhook_in_background, raw_text, sender)
    return build_teams_response("⏳ Processing your request, I'll reply shortly...")


# ---------------------------------------------------------------------------
# Alertmanager webhook receiver
# ---------------------------------------------------------------------------


@app.post("/alerts/alertmanager", tags=["Alerts"])
async def alertmanager_webhook(request: Request):
    """
    Alertmanager webhook receiver (payload schema v4).

    Flow: Prometheus rules → Alertmanager (group/dedup/repeat/resolve)
    → this endpoint → rule-based formatter → dedicated Teams alert channel.

    No LLM is involved anywhere in this path.

    Security:
    - Requires `Authorization: Bearer <ALERT_WEBHOOK_TOKEN>`; 401 otherwise.
    - Fails closed with 503 when the token is not configured.
    - Payload is validated before any Teams call; 422 on malformed input.
    """
    settings = get_settings()

    if not settings.alert_webhook_token:
        logger.error("ALERT_WEBHOOK_TOKEN is not configured.")
        raise HTTPException(
            status_code=503,
            detail="Alert webhook is not configured on this service.",
        )

    auth_header = request.headers.get("Authorization", "")
    expected = f"Bearer {settings.alert_webhook_token}"
    if not _hmac.compare_digest(auth_header, expected):
        raise HTTPException(status_code=401, detail="Invalid alert webhook token.")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid JSON payload.")

    try:
        title, body = format_alertmanager_payload(payload)
    except AlertPayloadError:
        # Safe message only — never echo the payload back
        raise HTTPException(status_code=422, detail="Malformed Alertmanager payload.")

    if not body:
        logger.info("Alertmanager payload contained no alerts; nothing sent.")
        return {"status": "ok", "sent": False, "alerts": 0}

    try:
        send_alert_to_teams(body, title=title)
    except RuntimeError:
        logger.error("Failed to deliver alert notification to Teams.")
        raise HTTPException(
            status_code=502,
            detail="Alert received but could not be delivered to Teams.",
        )

    alert_count = len(payload.get("alerts", []))
    logger.info("Alert notification sent to Teams. status=%s alerts=%d",
                payload.get("status"), alert_count)
    return {"status": "ok", "sent": True, "alerts": alert_count}


@app.post("/alerts/azure-check", tags=["Alerts"])
async def azure_alert_check_endpoint(request: Request):
    """
    Run one Azure metrics alert evaluation cycle (triggered by the
    moni-agent-alert CronJob every 15 minutes).

    Runs in the long-running service process so the firing/resolved
    cooldown state survives between CronJob runs. Rule-based only — no LLM.

    Security: same bearer token as /alerts/alertmanager (ALERT_WEBHOOK_TOKEN);
    401 on mismatch, 503 fail-closed when unset.
    """
    settings = get_settings()

    if not settings.alert_webhook_token:
        logger.error("ALERT_WEBHOOK_TOKEN is not configured.")
        raise HTTPException(
            status_code=503,
            detail="Alert checks are not configured on this service.",
        )

    auth_header = request.headers.get("Authorization", "")
    expected = f"Bearer {settings.alert_webhook_token}"
    if not _hmac.compare_digest(auth_header, expected):
        raise HTTPException(status_code=401, detail="Invalid alert webhook token.")

    try:
        summary = run_azure_alert_check()
    except Exception:
        logger.exception("Unexpected error during Azure alert check.")
        raise HTTPException(status_code=500, detail="Azure alert check failed. Check logs.")

    return {"status": "ok", **summary}


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
# Azure Monitor endpoints
# ---------------------------------------------------------------------------

RG_QUERY = Query(description="Azure resource group name (e.g. my-resource-group)")
AZURE_RANGE_QUERY = Query(description="Time range: 1h, 6h, 12h, 24h, 2d, 7d")


@app.get("/azure/resources", tags=["Azure Monitor"])
def azure_list_resources(
    resource_group: Annotated[str, RG_QUERY],
):
    """
    List all queryable Azure resources in a resource group.
    Returns resources grouped by type: app_service, mysql, postgres, redis, service_bus.
    Call this first to discover resource names before querying metrics.
    """
    try:
        validate_azure_name(resource_group, "resource_group")
        return list_azure_resources(resource_group=resource_group)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise _runtime_to_http(exc)


@app.get("/azure/app-service/{app_name}", tags=["Azure Monitor"])
def azure_app_service(
    app_name: str,
    resource_group: Annotated[str, RG_QUERY],
    range: Annotated[str, AZURE_RANGE_QUERY] = "24h",
):
    """
    Performance metrics for an Azure App Service (Microsoft.Web/sites).
    Returns CPU time, memory, requests, response time, HTTP status breakdown, error rate.
    """
    try:
        validate_azure_name(resource_group, "resource_group")
        validate_azure_name(app_name, "app_name")
        validate_range(range, get_settings().allowed_ranges_set)
        return get_app_service_performance(resource_group=resource_group, app_name=app_name, range=range)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise _runtime_to_http(exc)


@app.get("/azure/mysql/{server_name}", tags=["Azure Monitor"])
def azure_mysql(
    server_name: str,
    resource_group: Annotated[str, RG_QUERY],
    range: Annotated[str, AZURE_RANGE_QUERY] = "24h",
):
    """
    Performance metrics for an Azure MySQL Flexible Server.
    Returns CPU, memory, IO, connections, queries, storage usage.
    """
    try:
        validate_azure_name(resource_group, "resource_group")
        validate_azure_name(server_name, "server_name")
        validate_range(range, get_settings().allowed_ranges_set)
        return get_mysql_performance(resource_group=resource_group, server_name=server_name, range=range)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise _runtime_to_http(exc)


@app.get("/azure/postgres/{server_name}", tags=["Azure Monitor"])
def azure_postgres(
    server_name: str,
    resource_group: Annotated[str, RG_QUERY],
    range: Annotated[str, AZURE_RANGE_QUERY] = "24h",
):
    """
    Performance metrics for an Azure PostgreSQL Flexible Server.
    Returns CPU, memory, storage, backup storage, connections, IOPS, disk bandwidth.
    """
    try:
        validate_azure_name(resource_group, "resource_group")
        validate_azure_name(server_name, "server_name")
        validate_range(range, get_settings().allowed_ranges_set)
        return get_postgres_performance(resource_group=resource_group, server_name=server_name, range=range)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise _runtime_to_http(exc)


@app.get("/azure/redis/{cache_name}", tags=["Azure Monitor"])
def azure_redis(
    cache_name: str,
    resource_group: Annotated[str, RG_QUERY],
    range: Annotated[str, AZURE_RANGE_QUERY] = "24h",
):
    """
    Performance metrics for an Azure Cache for Redis resource (Microsoft.Cache/Redis).
    Returns memory %, connected clients, server load, cache hits/misses, ops/sec.
    """
    try:
        validate_azure_name(resource_group, "resource_group")
        validate_azure_name(cache_name, "cache_name")
        validate_range(range, get_settings().allowed_ranges_set)
        return get_redis_performance(resource_group=resource_group, cache_name=cache_name, range=range)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise _runtime_to_http(exc)


@app.get("/azure/service-bus/{namespace_name}", tags=["Azure Monitor"])
def azure_service_bus(
    namespace_name: str,
    resource_group: Annotated[str, RG_QUERY],
    range: Annotated[str, AZURE_RANGE_QUERY] = "24h",
):
    """
    Performance metrics for an Azure Service Bus namespace (Microsoft.ServiceBus/namespaces).
    Returns active messages, dead-lettered messages, incoming/outgoing counts, server errors.
    """
    try:
        validate_azure_name(resource_group, "resource_group")
        validate_azure_name(namespace_name, "namespace_name")
        validate_range(range, get_settings().allowed_ranges_set)
        return get_service_bus_performance(resource_group=resource_group, namespace_name=namespace_name, range=range)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise _runtime_to_http(exc)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _validate_source(source: str | None) -> None:
    """Validate source name format and existence in the registry."""
    if source is None:
        return
    validate_source_name(source)
    get_registry().get_by_name(source)
