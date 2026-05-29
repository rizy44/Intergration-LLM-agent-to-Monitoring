"""
conversation_agent.py — Conversation Agent (first OpenAI pass).

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

import json
import logging
from typing import Any

import openai

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
])

ALLOWED_RANGES = frozenset(["1h", "6h", "12h", "24h", "2d", "7d"])

# ---------------------------------------------------------------------------
# System prompt — mapping rules, JSON schema, disambiguation, hard rules
# ---------------------------------------------------------------------------

CONVERSATION_SYSTEM_PROMPT = """
You are the Conversation Agent for an AI-powered AKS observability assistant.

Your ONLY job is to parse the user's metric question and return a structured JSON
decision. You must NEVER answer the question yourself, explain metrics, generate
PromQL, or suggest any infrastructure action.

=== OUTPUT FORMAT ===

You must always return ONLY raw JSON — no markdown, no commentary, no prose.
One of these four shapes:

Ready (all required fields known):
{"status":"ready","request":{"tool":"<name>","cluster":"<val or null>","namespace":"<val or null>","service":"<val or null>","workload_name":"<val or null>","hostname":"<val or null>","range":"<val or null>","source":null}}

Needs clarification (required field missing — ask ALL missing fields at once):
{"status":"needs_clarification","message":"<clear question asking for all missing fields at once>"}

Refused (remediation, write action, kubectl, PromQL request):
{"status":"refused","message":"<brief reason, no remediation details>"}

Unsupported (valid read-only question but no Phase 1 tool covers it):
{"status":"unsupported","message":"<acknowledge intent, list supported queries>"}

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
    Return: {"status":"needs_clarification","message":"Which master node did you mean?\n- Vietnam master (vn-master_1)\n- Thailand master (tl-sv02)"}
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

=== HARD RULES ===

1. Never generate PromQL.
2. Never suggest restarting, scaling, rolling back, or modifying Kubernetes resources.
3. Never call any tool not in the ALLOWED TOOLS list above.
4. Never include credentials, URLs, internal hostnames, or PromQL in any output.
5. The "source" field is always null unless the user explicitly names a data source.
6. If the request is a valid read-only metric question but no tool covers it, return "unsupported" — NOT "refused".
   Only return "refused" for remediation, write actions, or kubectl requests.
7. Always return raw JSON only — no markdown fences, no prose.
""".strip()

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_user_message(
    user_message: str,
    history: list[dict] | None = None,
) -> dict[str, Any]:
    """
    Send *user_message* to the Conversation Agent.

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

    Raises RuntimeError on OpenAI API failure.
    """
    settings = get_settings()

    if not settings.openai_api_key:
        logger.error("OPENAI_API_KEY is not configured.")
        raise RuntimeError(
            "AI Conversation Agent is not available. The API key is not configured."
        )

    client = openai.OpenAI(api_key=settings.openai_api_key)

    messages = [{"role": "system", "content": CONVERSATION_SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    logger.debug("ConversationAgent: processing message len=%d history_turns=%d",
                 len(user_message), len(history) if history else 0)

    try:
        response = client.chat.completions.create(
            model=settings.openai_model,
            max_tokens=512,
            response_format={"type": "json_object"},
            messages=messages,
        )
        raw = response.choices[0].message.content.strip()
    except openai.AuthenticationError:
        logger.error("OpenAI authentication failed (ConversationAgent).")
        raise RuntimeError(
            "AI service is not available. Please check the API configuration."
        )
    except openai.RateLimitError:
        logger.warning("OpenAI rate limit reached (ConversationAgent).")
        raise RuntimeError(
            "AI service is temporarily unavailable due to rate limiting. "
            "Please try again in a moment."
        )
    except openai.APIError as exc:
        logger.error("OpenAI API error (ConversationAgent): %s", type(exc).__name__)
        raise RuntimeError("AI service encountered an error. Please try again later.")

    return _parse_and_validate(raw)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_and_validate(raw: str) -> dict[str, Any]:
    """Parse raw JSON from the Conversation Agent and validate the schema."""
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
