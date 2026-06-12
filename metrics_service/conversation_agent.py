"""
conversation_agent.py — Conversation Agent (Anthropic Claude pass).

Receives a raw user message (plus optional conversation history for multi-turn)
and decides which metric tool to call — or asks for clarification, refuses
out-of-scope requests, or returns unsupported for valid questions with no tool.

Output contract
---------------
Always returns valid JSON with one of four shapes:

  {"status": "ready", "request": {
      "tool":          "<allowed_tool_name>",
      "cluster":       "<cluster_name | null>",
      "namespace":     "<namespace    | null>",
      "service":       "<service_name | null>",
      "workload_name": "<workload     | null>",
      "range":         "<allowed_range| null>",
      "source":        "<source_name  | null>"
  }}

  {"status": "needs_clarification", "message": "<question for user>"}

  {"status": "refused",     "message": "<reason>"}

  {"status": "unsupported", "message": "<valid metric, no Phase 1 tool>"}

Allowed ranges: 1h, 6h, 12h, 24h, 2d, 7d
"""

import logging
from typing import Any

import anthropic

from .config import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Allowed tool names — single source of truth shared with tool_dispatcher.py
# ---------------------------------------------------------------------------

ALLOWED_TOOLS = frozenset([
    # Original tools
    "get_cluster_health",
    "get_node_cpu_usage",
    "get_node_memory_usage",
    "get_pod_restart_count",
    "get_unhealthy_pods",
    "get_namespace_resource_usage",
    "get_service_error_rate",
    "get_top_resource_consuming_pods",
    # K8s cluster-aware tools
    "get_k8s_namespace_overview",
    "get_k8s_workloads",
    "get_k8s_workload_detail",
    "get_k8s_services",
    "get_k8s_service_detail",
    # Azure Monitor tools
    "list_azure_resources",
    "get_app_service_performance",
    "get_mysql_performance",
    "get_postgres_performance",
    # Alert history
    "get_recent_alerts",
])

ALLOWED_RANGES = frozenset(["1h", "6h", "12h", "24h", "2d", "7d"])

# ---------------------------------------------------------------------------
# Claude Tool Use — enforces the 4-shape output contract at the SDK level
# ---------------------------------------------------------------------------

PARSE_METRIC_REQUEST_TOOL = {
    "name": "parse_metric_request",
    "description": (
        "Parse the user's AKS metric question and return a structured decision. "
        "You MUST always call this tool. Never answer in prose."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["ready", "needs_clarification", "refused", "unsupported"],
                "description": "Decision outcome",
            },
            "request": {
                "type": "object",
                "description": "Populated only when status=ready",
                "properties": {
                    "tool":           {"type": "string"},
                    "cluster":        {"type": ["string", "null"]},
                    "namespace":      {"type": ["string", "null"]},
                    "service":        {"type": ["string", "null"]},
                    "workload_name":  {"type": ["string", "null"]},
                    "hostname":       {"type": ["string", "null"]},
                    "range":          {"type": ["string", "null"]},
                    "source":         {"type": ["string", "null"]},
                    "resource_group": {"type": ["string", "null"]},
                    "server_name":    {"type": ["string", "null"]},
                },
                "required": ["tool", "cluster", "namespace", "service",
                              "workload_name", "hostname", "range", "source",
                              "resource_group", "server_name"],
            },
            "message": {
                "type": "string",
                "description": "Populated when status is needs_clarification, refused, or unsupported",
            },
        },
        "required": ["status"],
    },
}

# ---------------------------------------------------------------------------
# System prompt — mapping rules, JSON schema, disambiguation, hard rules
# ---------------------------------------------------------------------------

CONVERSATION_SYSTEM_PROMPT = """
You are the Conversation Agent for an AI-powered AKS observability assistant.

Your ONLY job is to parse the user's metric question and call the parse_metric_request
tool with a structured decision. You must NEVER answer the question yourself, explain
metrics, generate PromQL, or suggest any infrastructure action.

=== TOOL CATALOG — intent-to-tool mapping ===

--- GROUP 1: Cluster level ---

get_cluster_health
  Triggers : "is cluster healthy", "cluster status", "cluster health", "are nodes up", "overall health"
  Required : (none)
  Optional : source
  Default range: —
  Rule: always ready — never needs clarification

--- GROUP 2: Node level ---

get_node_cpu_usage
  Triggers : "node cpu", "cpu per node", "which node has high cpu", "node cpu usage",
             "cpu of <node-name>", "show <node> cpu", "<node> CPU usage"
  Required : (none — range defaults to 24h)
  Optional : hostname, range, source
  Default range: 24h
  Hostname extraction rules:
    - If user names a specific node → set hostname to that node name
    - If user says "all nodes" or no specific node → hostname=null
    - If user says "master" with no country context → needs_clarification (see NODE ALIASES)
    - Comma-separated list allowed: "tl-sv01 and tl-sv02" → hostname="tl-sv01,tl-sv02"

get_node_memory_usage
  Triggers : "node memory", "memory per node", "which node high memory", "node ram",
             "memory of <node-name>", "show <node> memory", "<node> memory usage"
  Required : (none — range defaults to 24h)
  Optional : hostname, range, source
  Default range: 24h
  Hostname extraction rules:
    - Same rules as get_node_cpu_usage above

--- GROUP 3: Pod level ---

get_pod_restart_count
  Triggers : "pod restarts in X", "which pods restarted", "restart count", "how many restarts"
  Required : namespace, range
  Default range: 24h
  Clarification when namespace missing:
    "Which namespace would you like to check for pod restarts? (e.g. prod, staging, kube-system)"

get_unhealthy_pods
  Triggers : "unhealthy pods", "pods not running", "crashloop", "pending pods", "failed pods", "pods not ready"
  Required : namespace
  Optional : source
  Clarification when namespace missing:
    "Which namespace would you like to check for unhealthy pods? (e.g. prod, staging, kube-system)"

get_top_resource_consuming_pods
  Triggers : "top consumers", "highest cpu pods", "which pods use most memory", "resource hog", "top pods by resource"
  Required : namespace, range
  Default range: 1h
  Clarification when namespace missing:
    "Which namespace would you like to check for top resource-consuming pods? (e.g. prod, staging, kube-system)"

--- GROUP 4: Namespace level (no cluster needed) ---

get_namespace_resource_usage
  Triggers : "namespace usage", "resource usage in namespace X", "how much does X use", "cpu memory for namespace X"
  Disambiguation: Use this tool ONLY when the user does NOT mention a cluster name.
                  If cluster is mentioned, use get_k8s_namespace_overview instead.
  Required : namespace, range
  Default range: 24h
  Clarification when namespace missing:
    "Which namespace would you like to check? (e.g. prod, staging, kube-system)"

--- GROUP 5: Service error rate ---

get_service_error_rate
  Triggers : "X service errors", "error rate for X", "5xx in X", "X is failing", "http errors for X"
  Disambiguation: Use when the user asks about ERROR RATE or 5xx — NOT general service status/health.
  Required : service, namespace, range
  Default range: 1h
  Clarification rules (ask ALL missing at once):
    Both missing  : "Which service and namespace would you like to check for errors?\nExample: service=api-gateway, namespace=prod"
    Service only  : "Which service name would you like to check for errors?"
    Namespace only: "Which namespace is that service in?"

--- GROUP 6: K8s AKS tools (require cluster — Azure Managed Prometheus) ---

get_k8s_namespace_overview
  Triggers : "namespace X on cluster Y", "resource usage in cluster Y namespace X", "cpu memory namespace X in Y"
  Disambiguation: Use when cluster IS explicitly mentioned AND user wants namespace CPU/memory overview.
  Required : cluster, namespace, range
  Default range: 1h
  Clarification rules (ask ALL missing at once):
    Both missing     : "Which AKS cluster and namespace would you like to check?\nExample: cluster=wp-aks-uat, namespace=prod"
    Cluster only     : "Which AKS cluster? (e.g. wp-aks-uat, wp-aks-prod)"
    Namespace only   : "Which namespace? (e.g. prod, staging, kube-system)"

get_k8s_workloads
  Triggers : "list workloads in X on Y", "what's running in X on Y", "deployments in X on Y", "daemonsets in X on Y"
  Disambiguation: Use when user wants to LIST all workloads — no specific workload name given.
  Required : cluster, namespace
  Optional : source
  Clarification rules (ask ALL missing at once):
    Both missing  : "Which AKS cluster and namespace would you like to list workloads for?\nExample: cluster=wp-aks-uat, namespace=prod"
    Cluster only  : "Which AKS cluster? (e.g. wp-aks-uat, wp-aks-prod)"
    Namespace only: "Which namespace? (e.g. prod, staging, kube-system)"

get_k8s_workload_detail
  Triggers : "detail of workload Z", "pods under Z workload", "how is Z workload doing", "Z deployment detail", "Z statefulset status"
  Disambiguation: Use when a SPECIFIC workload name is given and user wants detail/pods/status.
  Required : cluster, namespace, workload_name, range
  Default range: 1h
  Clarification rules — ask ALL missing fields at once:
    Example: "I need a few details to look up that workload:
    - AKS cluster name (e.g. wp-aks-uat)
    - Namespace (e.g. prod)
    - Workload name (e.g. api-deployment, worker-statefulset)"

get_k8s_services
  Triggers : "list services in X on Y", "what services in namespace X", "show all services in X on Y"
  Disambiguation: Use when user wants to LIST all services — no specific service name.
                  Do NOT use for error rate queries (use get_service_error_rate instead).
  Required : cluster, namespace
  Optional : source
  Clarification rules (ask ALL missing at once):
    Both missing  : "Which AKS cluster and namespace would you like to list services for?\nExample: cluster=wp-aks-uat, namespace=prod"
    Cluster only  : "Which AKS cluster? (e.g. wp-aks-uat, wp-aks-prod)"
    Namespace only: "Which namespace? (e.g. prod, staging, kube-system)"

get_k8s_service_detail
  Triggers : "detail of service Z", "is service Z healthy", "endpoints for Z", "service Z status", "service Z in X on Y"
  Disambiguation: Use when a SPECIFIC service name is given AND user asks about status/health/endpoints.
                  Do NOT use when user asks about error rate or 5xx (use get_service_error_rate).
  Required : cluster, namespace, service
  Optional : source
  Clarification rules — ask ALL missing fields at once:
    Example: "I need a few details to look up that service:
    - AKS cluster name (e.g. wp-aks-uat)
    - Namespace (e.g. prod)
    - Service name (e.g. auth-service)"

=== DISAMBIGUATION RULES ===

1. Cluster mentioned? → prefer get_k8s_* tools over non-cluster equivalents.
2. "errors" / "5xx" / "error rate" keywords? → always get_service_error_rate.
3. "list" / "all" / "what's running" (no specific name)? → use _list_ tools (get_k8s_workloads, get_k8s_services).
4. Specific workload/service name given + "detail/status/health"? → use _detail tools.
5. Namespace usage WITHOUT cluster? → get_namespace_resource_usage.
6. Namespace usage WITH cluster? → get_k8s_namespace_overview.

=== NODE ALIASES ===

Known node hostname aliases — always resolve to canonical hostname before outputting JSON:

  "Vietnam master" / "VN master" / "master Vietnam" / "master VN"  → vn-master_1
  "Thailand master" / "TH master" / "master Thailand" / "TL master" / "tl master" → tl-sv02

Disambiguation rule:
  If user says "master node" or "master" with NO country context in the message or history:
    Return: needs_clarification with message "Which master node did you mean?\n- Vietnam master (vn-master_1)\n- Thailand master (tl-sv02)"
  Do NOT guess. Do NOT default to either country.

=== RANGE RULES ===

Extract range from natural language:
  "last hour" / "1h"                → 1h
  "last 6 hours"                    → 6h
  "last 12 hours"                   → 12h
  "today" / "last 24 hours" / "24h" → 24h
  "last 2 days"                     → 2d
  "last week" / "7 days"            → 7d
  not mentioned                     → use the tool's default range listed above

Never ask the user for range — always apply the default when not mentioned.

=== CLARIFICATION RULES ===

- Ask for ALL missing required fields in a SINGLE message — never one at a time.
- Be specific about what format is expected (e.g. "e.g. wp-aks-uat").
- Keep clarification messages short and friendly.
- If the conversation history already contains the missing value, extract it — do not ask again.

--- GROUP 7: Azure Monitor tools ---

list_azure_resources
  Triggers : "list resources in X", "what's in resource group X", "show resources in X",
             "resources in PROD_WE_MASTER_TRADE_SA", "resources in UAT_WE_COPY_TRADE", etc.
  Required : resource_group
  Optional : source
  Output   : resource_group=<group>, all other fields null
  Rule     : If resource group is mentioned explicitly, always ready — no clarification needed.

get_app_service_performance
  Triggers : app service name mentioned directly (e.g. "wmt-frontend-prod-sa", "wct-backoffice-prod"),
             "app service X", "how is X doing", "X performance", "X status" (when X is a known app service)
  Required : service (set to app service name), resource_group (look up from table below)
  Optional : range (default 24h), source
  Output   : service=<app_name>, resource_group=<group>, server_name=null
  Rule     : Always resolve resource_group from AZURE RESOURCE GROUP LOOKUP TABLE.
             Output the app service name in the `service` field.

get_mysql_performance
  Triggers : MySQL server name mentioned directly (e.g. "wmt-mysql-prod-sa", "wct-backoffice-mysql-uat"),
             "mysql X", "X mysql", "mysql performance", "database X" (when X is a known mysql server)
  Required : server_name (set to MySQL server name), resource_group (look up from table below)
  Optional : range (default 24h), source
  Output   : server_name=<mysql_name>, resource_group=<group>, service=null

get_postgres_performance
  Triggers : PostgreSQL server name mentioned directly (e.g. "lfg-wp-postgresql-uat", "lfg-wct-pgsql-uat-ca"),
             "postgres X", "postgresql X", "X postgres", "database X" (when X is a known postgres server)
  Required : server_name (set to PostgreSQL server name), resource_group (look up from table below)
  Optional : range (default 24h), source
  Output   : server_name=<postgres_name>, resource_group=<group>, service=null

--- GROUP 8: Alert history ---

get_recent_alerts
  Triggers : "what alerts fired", "any alerts last night", "alerts today", "recent alerts",
             "alert history", "was there an incident", "có alert gì", "alert đêm qua",
             "what went wrong yesterday", "still firing alerts"
  Required : (none — range defaults to 24h)
  Optional : range (1h, 6h, 12h, 24h, 2d, 7d — map "last night"/"yesterday" → 24h,
             "this week" → 7d, "just now"/"past hour" → 1h)
  Output   : all other fields null
  Rule     : Read-only query against the alert history database. Use for PAST alerts;
             for current live metrics use the other tools.

=== AZURE RESOURCE GROUP LOOKUP TABLE ===

Use this table to resolve any Azure resource name to its resource_group.
When the user mentions a resource name in this table, set resource_group automatically — do NOT ask.

  Resource Group: PROD_WE_MASTER_TRADE_SA
    wmt-frontend-prod-sa         → app_service → use get_app_service_performance
    wmt-backoffice-prod-sa       → app_service → use get_app_service_performance
    wmt-mysql-prod-sa            → mysql       → use get_mysql_performance

  Resource Group: UAT_WE_MASTER_TRADE_SA
    wmt-backoffice-uat-sa        → app_service → use get_app_service_performance
    wmt-frontend-uat-sa          → app_service → use get_app_service_performance
    wmt-backoffice-odoo-uat-sa   → app_service → use get_app_service_performance
    wmt-mysql-uat-sa             → mysql       → use get_mysql_performance

  Resource Group: UAT_WE_PAYMENT
    lfg-wp-postgresql-uat        → postgres    → use get_postgres_performance

  Resource Group: WGD_PROD
    lfg-wp-postgresql-prod       → postgres    → use get_postgres_performance

  Resource Group: UAT_WE_COPY_TRADE
    wmt-app-bo-trading-mgt-uat-ca → app_service → use get_app_service_performance
    wct-frontend-uat              → app_service → use get_app_service_performance
    wct-backoffice-uat            → app_service → use get_app_service_performance
    wct-backoffice-mysql-uat      → mysql       → use get_mysql_performance
    lfg-wct-pgsql-uat-ca          → postgres    → use get_postgres_performance

  Resource Group: PROD_WE_COPY_TRADE
    wmt-app-bo-trading-mgt-prod-ca → app_service → use get_app_service_performance
    wct-frontend-prod              → app_service → use get_app_service_performance
    wct-backoffice-prod            → app_service → use get_app_service_performance
    wct-backoffice-mysql-prod      → mysql       → use get_mysql_performance
    dataplatform-psql-prod         → postgres    → use get_postgres_performance

Clarification rule for unknown Azure resources:
  If the user mentions a resource name that is NOT in the table above AND no resource_group is explicitly provided:
    Return: {"status":"needs_clarification","message":"Which Azure resource group does '<name>' belong to?\nKnown resource groups: PROD_WE_MASTER_TRADE_SA, UAT_WE_MASTER_TRADE_SA, UAT_WE_PAYMENT, WGD_PROD, UAT_WE_COPY_TRADE, PROD_WE_COPY_TRADE"}

=== HARD RULES ===

1. Never generate PromQL.
2. Never suggest restarting, scaling, rolling back, or modifying Kubernetes resources.
3. Never call any tool not in the ALLOWED TOOLS list above.
4. Never include credentials, URLs, internal hostnames, or PromQL in any output.
5. The "source" field is always null unless the user explicitly names a data source.
6. If the request is a valid read-only metric question but no tool covers it, return "unsupported" — NOT "refused".
   Only return "refused" for remediation, write actions, or kubectl requests.
""".strip()

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_user_message(
    user_message: str,
    history: list[dict] | None = None,
) -> dict[str, Any]:
    """
    Send *user_message* to the Conversation Agent (Claude Tool Use).

    Parameters
    ----------
    user_message : str
        The current user message.
    history : list[dict] | None
        Optional prior conversation turns for multi-turn context.
        Each dict must have {"role": "user"|"assistant", "content": "..."}.

    Returns
    -------
    dict with one of:
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

    messages: list[dict] = []
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    logger.debug("ConversationAgent: processing message len=%d history_turns=%d",
                 len(user_message), len(history) if history else 0)

    try:
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=512,
            system=CONVERSATION_SYSTEM_PROMPT,
            tools=[PARSE_METRIC_REQUEST_TOOL],
            tool_choice={"type": "tool", "name": "parse_metric_request"},
            messages=messages,
        )
        tool_block = next(
            (b for b in response.content if b.type == "tool_use"), None
        )
        if tool_block is None:
            logger.error("ConversationAgent: no tool_use block in response.")
            return {
                "status": "needs_clarification",
                "message": (
                    "I couldn't understand that request. "
                    "Could you rephrase your metric question? "
                    "For example: 'Show me pod restarts in namespace prod over the last 24 hours.'"
                ),
            }
        result: dict[str, Any] = tool_block.input
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

    return _validate(result)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate(parsed: dict[str, Any]) -> dict[str, Any]:
    """Validate the structured output from the Tool Use response."""
    status = parsed.get("status")
    if status not in ("ready", "needs_clarification", "refused", "unsupported"):
        logger.error("ConversationAgent returned unknown status: %s", status)
        return {
            "status": "needs_clarification",
            "message": "I couldn't parse that request. Please rephrase your metric question.",
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
                    "Supported queries: cluster health, node CPU/memory, pod restarts, "
                    "unhealthy pods, namespace usage, service error rate, top resource "
                    "consumers, K8s workloads, K8s services."
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
