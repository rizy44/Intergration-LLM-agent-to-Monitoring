# Architecture

## High-Level Architecture

Phase 1 uses a read-only observability architecture:

```text
Prometheus
  -> Metrics Service / Tool Backend
  -> Claude / AI Analysis Layer
  -> Microsoft Teams / Chatbox
```

Prometheus remains the metrics source. The Metrics Service is the only component that queries Prometheus. Claude receives structured results from the Metrics Service and turns them into human-readable explanations.

## Daily Report Flow

```text
Kubernetes CronJob
  -> Metrics Service
  -> Prometheus API
  -> Metrics Summary JSON
  -> Claude / AI Analysis
  -> Microsoft Teams Daily Report
```

The daily report summarizes AKS health over the last 24 hours across selected Prometheus-compatible datasources. The selected sources are controlled by `DAILY_REPORT_SOURCES` so the report does not send unnecessary metric data to Claude. It should include an overall cross-source summary, a source-by-source breakdown, node resource usage, pod restart counts, unhealthy pods, namespace usage, service error rate when available, warnings, and read-only investigation suggestions.

## On-Demand Teams Chat Flow (Two-Agent)

```text
User sends a message in Teams
  -> POST /teams/chat
  -> Conversation Agent (Claude — JSON routing decision)
       |
       ├─ "needs_clarification" → reply to Teams asking follow-up
       ├─ "refused"             → reply to Teams with out-of-scope message
       └─ "ready"
             -> Tool Dispatcher (whitelist-validated)
             -> Metric Tool (predefined PromQL)
             -> Prometheus API
             -> Structured metric JSON
             -> Explanation Agent (Claude — human-readable reply)
             -> Teams webhook → answer delivered in Teams
```

### Conversation Agent (`conversation_agent.py`)

The Conversation Agent is the first Claude pass. It receives the raw user
message and returns **only** a structured JSON routing decision:

- `{"status": "ready", "request": {"tool": "...", "namespace": "...", ...}}`
- `{"status": "needs_clarification", "message": "..."}`
- `{"status": "refused", "message": "..."}`

The agent never generates PromQL and never calls Prometheus directly.
It only identifies intent and maps it to a whitelisted tool name.

### Tool Dispatcher (`tool_dispatcher.py`)

The Tool Dispatcher is the security gate between the Conversation Agent and
the metric tools. It:

- Validates the tool name against `ALLOWED_TOOL_DISPATCH` (whitelist).
- Validates all inputs (namespace, service, range, source name).
- Calls the correct metric tool function with safe, validated arguments.
- Raises `ToolDispatchError` for any invalid or missing inputs.

No tool can be called unless it is explicitly listed in `ALLOWED_TOOL_DISPATCH`.

### Explanation Agent (`ai_agent.py` — `analyze_metrics`)

The Explanation Agent is the second Claude pass. It receives:

- The name of the tool that was called.
- The structured JSON metric result.
- The original user question (for context only).

It returns a concise, plain-English explanation suitable for a Teams message.
It never suggests remediation, never generates PromQL, and never invents metrics.

### Legacy `/chat` Endpoint

The `POST /chat` endpoint remains for direct API use (non-Teams integrations).
It uses keyword-based routing instead of the Conversation Agent and does not
send messages to Teams automatically.

## Component Responsibilities

### Prometheus

- Stores AKS and workload metrics.
- Serves metrics through its API.
- Must not be exposed publicly.

### Metrics Service / Tool Backend

- Owns all Prometheus access.
- Runs predefined PromQL queries internally.
- Validates user input.
- Applies query timeouts and result limits.
- Normalizes Prometheus responses into structured JSON.
- Handles Prometheus errors safely.
- Exposes stable read-only metric functions for Claude and future callers.
- For daily reports, iterates through the selected `DAILY_REPORT_SOURCES` instead of using only the default source or blindly collecting every configured source.

### Claude / AI Analysis Layer

- Understands metric questions.
- Selects the appropriate backend tool.
- Interprets structured metric JSON.
- Explains findings clearly.
- Provides read-only investigation suggestions.
- Refuses unsupported remediation or infrastructure modification requests.

### Microsoft Teams / Chatbox

- Delivers daily health reports.
- Accepts on-demand metric questions.
- Shows short, readable answers.
- Presents errors without leaking internal details or secrets.

## Prometheus Access Model

Claude must never query Prometheus directly. The Metrics Service is the only allowed Prometheus client.

The Metrics Service should:

- Keep Prometheus network access private.
- Use configured, controlled PromQL templates.
- Reject unsafe or unsupported inputs.
- Limit time ranges and result sizes.
- Apply request timeouts.
- Return JSON data that Claude can safely interpret.

## AI Analysis Layer

The AI layer is responsible for interpretation, not data collection. It should not invent metrics, thresholds, or causes.

When data is incomplete, Claude should say so and suggest a supported read-only follow-up query.

## Microsoft Teams Integration

Teams integration supports:

- Automated daily health reports.
- On-demand bot replies for metric questions.
- Concise warning and recommendation sections.

Messages should be readable in Teams without requiring the user to inspect raw JSON.

## Why n8n Is Not Required in Phase 1

n8n is not required because Phase 1 has a simple read-only workflow:

- Scheduled daily report generation.
- On-demand metric query handling.
- Teams message delivery.

These flows can be handled directly by the Metrics Service, a CronJob, and Teams integration. Adding n8n in Phase 1 would increase operational complexity without adding required behavior.

## Future n8n Readiness

Future phases may add n8n for orchestration, approvals, incident workflows, and integrations. To prepare for that without implementing it now:

- Keep Metrics Service APIs stable.
- Return predictable JSON.
- Separate metric retrieval from AI interpretation.
- Keep remediation actions out of Phase 1 APIs.
- Document tool contracts clearly.
