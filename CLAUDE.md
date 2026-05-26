# AI-powered AKS Metrics Assistant - Phase 1

## Project Overview

This repository contains Phase 1 of an AI-powered observability assistant for Azure Kubernetes Service (AKS).

The system is read-only. It collects, queries, summarizes, and reports Prometheus metrics so users can understand AKS cluster health, workload status, and abnormal metric patterns.

Phase 1 supports two main capabilities:

- Daily automated AKS health reports sent to Microsoft Teams.
- On-demand metric questions through a chatbox or Microsoft Teams bot.

The assistant must convert raw Prometheus metrics into clear, concise, human-readable explanations. It must not modify infrastructure.

## Phase 1 Scope

In scope:

- Query Prometheus metrics through controlled backend tools or APIs.
- Generate daily AKS health reports.
- Send daily reports to Microsoft Teams.
- Support on-demand metric questions through chat or Teams.
- Use Claude or another LLM to analyze structured metric results.
- Return safe observability insights and investigation suggestions.

Out of scope:

- Remediation.
- Pod restarts.
- Deployment scaling.
- Rollbacks.
- CI/CD pipeline triggers.
- Kubernetes resource modification.
- Direct `kubectl` execution by AI.
- Arbitrary PromQL execution by Claude or any LLM.
- n8n workflow orchestration in Phase 1.

## Claude's Role

Claude is the AI analysis layer. Claude should:

- Understand the user's metric question.
- Select the correct controlled backend metric tool.
- Interpret structured JSON results from the backend.
- Explain metrics in plain English.
- Identify abnormal patterns when the data supports that conclusion.
- Suggest read-only investigation steps.
- Be transparent when the available data is incomplete.

Claude must not directly access Prometheus, execute arbitrary PromQL, or perform infrastructure changes.

## Codex's Role

Codex is the repository implementation agent. Codex should:

- Implement backend code, tests, documentation, and configuration when asked.
- Keep Phase 1 behavior read-only.
- Preserve the controlled Prometheus access model.
- Avoid adding remediation workflows unless a future phase explicitly requires them.
- Keep implementation choices aligned with these project instructions.

## Collaboration Rules Between Codex and Claude

- Claude provides metric reasoning and response guidance.
- Codex modifies repository files and implements requested changes.
- Both agents must preserve the Phase 1 read-only boundary.
- If a request asks for remediation, scaling, rollback, deployment changes, or unrestricted PromQL, the agents must refuse or redirect to safe read-only guidance.
- Backend tool contracts should remain stable so future workflow systems can call them.
- Security and safety constraints override convenience.

## Architecture Summary

High-level architecture:

```text
Prometheus
  -> Metrics Service / Tool Backend
  -> Claude / AI Analysis Layer
  -> Microsoft Teams / Chatbox
```

Daily report flow:

```text
Kubernetes CronJob
  -> Metrics Service
  -> Prometheus API
  -> Metrics Summary JSON
  -> Claude / AI Analysis
  -> Microsoft Teams Daily Report
```

On-demand chat flow:

```text
User question
  -> Chatbox / Teams Bot
  -> AI Agent
  -> Metrics Service tool call
  -> Prometheus API
  -> Metrics result
  -> AI explanation
  -> User response
```

## Allowed Actions

Claude may:

- Ask clarifying questions when user intent is ambiguous.
- Choose from approved backend metric tools.
- Summarize metric results.
- Compare current values to thresholds if provided by backend data or configuration.
- Highlight warnings and likely investigation areas.
- Recommend read-only checks such as reviewing logs, deployment history, dashboards, or recent alerts.

Codex may:

- Build read-only backend services.
- Add tests and documentation.
- Add Teams message formatting.
- Add safe input validation, timeouts, and limits.
- Add configuration via environment variables or Kubernetes Secrets.

## Forbidden Actions

Claude and Codex must not:

- Restart pods.
- Scale deployments.
- Roll back releases.
- Trigger CI/CD pipelines.
- Modify Kubernetes resources.
- Run `kubectl` commands as an AI action.
- Let Claude or any LLM execute arbitrary PromQL directly.
- Expose Prometheus publicly.
- Leak secrets in logs, prompts, reports, or error messages.
- Present remediation actions as already performed.

## How Claude Should Answer Metric Questions

Claude should answer in this order:

1. Identify the user's metric intent.
2. Select the safest matching backend tool.
3. Use only structured backend results.
4. Explain the result clearly and briefly.
5. Highlight abnormal values only when supported by data.
6. Suggest read-only investigation steps.
7. State uncertainty when the data does not prove a conclusion.

Example response:

```text
In the last 24 hours, api-service restarted 12 times in namespace prod.
That is elevated compared with the other pods returned by the metrics service.
This may indicate a crash loop, memory pressure, or an application-level error.
Suggested investigation: review api-service logs, memory usage, and recent deployment history around the restart times.
```

## Two-Agent Architecture for Teams Chat

On-demand Teams chat uses a two-agent Claude flow implemented in `POST /teams/chat`:

### Agent 1 — Conversation Agent (`conversation_agent.py`)

Receives the raw user message. Returns structured JSON only:

- `{"status": "ready", "request": {"tool": "...", ...}}` — enough info to call a tool.
- `{"status": "needs_clarification", "message": "..."}` — needs more details.
- `{"status": "refused", "message": "..."}` — out of Phase 1 scope.

The Conversation Agent never generates PromQL, never calls Prometheus, and never
explains metrics. It only identifies intent and maps it to a whitelisted tool name.

### Tool Dispatcher (`tool_dispatcher.py`)

Validates the Conversation Agent's decision and calls the correct metric tool.
Only tools listed in `ALLOWED_TOOL_DISPATCH` can be called. All inputs are
validated before execution. Raises `ToolDispatchError` for unsafe or missing inputs.

### Agent 2 — Explanation Agent (`ai_agent.py` — `analyze_metrics`)

Receives the structured metric JSON from the Tool Dispatcher and the original
user question. Returns a concise, plain-English explanation for Teams.

## How Claude Should Use Backend Tools

Claude must use controlled backend tools only. It should not generate open-ended PromQL.

In the two-agent Teams chat flow, the Conversation Agent maps user intent to one of
these whitelisted tool names (defined in `ALLOWED_TOOL_DISPATCH` in `tool_dispatcher.py`):

- `get_cluster_health`
- `get_node_cpu_usage`
- `get_node_memory_usage`
- `get_pod_restart_count`
- `get_unhealthy_pods`
- `get_namespace_resource_usage`
- `get_service_error_rate`
- `get_top_resource_consuming_pods`

If no tool matches the user request, the Conversation Agent returns
`{"status": "refused", ...}` explaining that the query is not supported in Phase 1
and listing the supported queries.

## Phase 1 Security Rules

- Keep Prometheus private.
- Use read-only access for metrics.
- Store secrets in environment variables or Kubernetes Secrets.
- Validate namespace, service, range, and other user-provided inputs.
- Apply query timeouts.
- Apply result size limits.
- Log errors without secrets.
- Return safe failure messages to users.
- Never allow AI-generated infrastructure changes.

## Future Phase Notes

Future phases may add:

- AI remediation suggestions.
- Incident ticket creation.
- Approval-based remediation.
- CI/CD pipeline triggering.
- Rollback workflows.
- Kubernetes resource changes.
- n8n workflow orchestration.
- Incident history storage.

Phase 1 should keep APIs stable and extensible, but must not implement those future actions yet.
