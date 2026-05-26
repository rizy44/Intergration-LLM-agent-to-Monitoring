# Task: Add Claude Conversation Agent and Teams Chat Flow

We need to improve the current Teams chat architecture.

Current state:

- The backend already has controlled metric tools.
- The backend already has `ai_agent.py` for explaining metrics after data is collected.
- The backend already has Teams webhook sending logic for daily reports.
- The current `/chat` endpoint routes user intent by simple keywords in `app.py`.
- There is no real Claude Conversation Agent yet.
- There is no clean separation between:
  1. understanding the user chat request
  2. collecting metrics
  3. explaining metrics
  4. sending the final response to Teams

## Goal

Add a clearer two-agent Claude flow:

```text
User message from Teams
  -> Claude Conversation Agent
  -> structured metric request
  -> Metrics Service controlled tools
  -> Prometheus datasource
  -> metric JSON
  -> Claude Explanation Agent
  -> Teams webhook sends final answer to Teams
```

Important clarification:

After the **Claude Explanation Agent** finishes generating the explanation, the backend should call the **Teams webhook** to send the final result back to Microsoft Teams.

## Phase 1 Constraints

Keep everything read-only.

Do not add:

- remediation
- pod restart
- deployment scaling
- rollback
- CI/CD trigger
- Kubernetes resource modification
- arbitrary PromQL execution
- kubectl execution

Claude must not generate arbitrary PromQL.

Prometheus must still be queried only through controlled backend tools.

## Design Requirements

### 0. Claude Configuration

Use one Claude API configuration for both agents.

Both the Conversation Agent and the Explanation Agent can call the same Anthropic API key and the same configured Claude model. They should be separated by prompt, function, and responsibility, not necessarily by separate API credentials.

Configuration should continue to come from environment variables:

```text
ANTHROPIC_API_KEY
CLAUDE_MODEL
CLAUDE_MAX_TOKENS
```

Do not create separate API keys unless there is a future operational need.

The important separation is:

```text
Same Claude API
  -> Conversation Agent prompt for understanding user intent
  -> Explanation Agent prompt for explaining metric results
```

### 0.1 How Claude Should Handle User Questions

When Claude receives a user question from Teams, the Conversation Agent must decide what kind of response is needed before any backend metric tool is called.

Claude should classify the question into one of these outcomes:

1. `ready`
2. `needs_clarification`
3. `refused`
4. `unsupported`

#### A. Ready

Use `ready` when Claude has enough information to create a safe structured metric request.

Example user question:

```text
Which pods restarted the most in prod during the last 24 hours?
```

Expected Conversation Agent output:

```json
{
  "status": "ready",
  "request": {
    "intent": "pod_restarts",
    "tool": "get_pod_restart_count",
    "namespace": "prod",
    "range": "24h",
    "source": null
  }
}
```

#### B. Needs Clarification

Use `needs_clarification` when the question is valid but missing required details.

Example user question:

```text
Check api-service errors today.
```

If the namespace is missing, Claude should ask one short clarification question:

```json
{
  "status": "needs_clarification",
  "message": "Which namespace should I check for api-service?"
}
```

Clarification should be used for missing fields such as:

- namespace
- service name
- time range when no safe default is acceptable
- source when the user explicitly asks about a datasource but does not name it clearly

Use safe defaults only when they are configured and reasonable. For example, `24h` may be a safe default for "today" or "recently".

#### C. Refused

Use `refused` when the user asks for remediation or infrastructure modification.

Example user question:

```text
Restart the failing api-service pod.
```

Expected output:

```json
{
  "status": "refused",
  "message": "Phase 1 is read-only. I cannot restart, scale, roll back, or modify workloads. I can help inspect metrics and suggest read-only investigation steps."
}
```

Refuse requests involving:

- pod restart
- deployment scale
- rollback
- CI/CD trigger
- Kubernetes resource patch/apply/delete
- `kubectl` execution
- arbitrary PromQL execution

#### D. Unsupported

Use `unsupported` when the request is not remediation, but Phase 1 does not have a controlled backend tool for it.

Example user question:

```text
Show me container filesystem I/O latency by PVC.
```

If no approved tool supports this yet:

```json
{
  "status": "unsupported",
  "message": "Phase 1 does not currently have a controlled metric tool for PVC I/O latency. I can help with cluster health, node CPU, node memory, pod restarts, unhealthy pods, namespace usage, service error rate, or top resource-consuming pods."
}
```

Do not invent a tool. Do not return PromQL.

#### Question Handling Rules

Claude Conversation Agent must:

- Extract the user's intent.
- Extract namespace, service, range, and source when present.
- Use configured defaults only when safe.
- Ask exactly one concise clarification question when required.
- Return strict JSON only.
- Select only approved backend tools.
- Never return PromQL.
- Never claim metric results before backend data is collected.
- Never perform or suggest remediation.

Claude Explanation Agent must:

- Receive only structured metric JSON.
- Explain what the data says.
- Highlight abnormal patterns only when supported by data.
- Suggest read-only investigation steps.
- Keep the answer suitable for Microsoft Teams.
- Avoid unsupported certainty.

### 1. Separate Claude Roles

Implement two separate Claude roles/modules/prompts.

They may use the same Claude model and API key, but their responsibilities must be separate.

#### A. Claude Conversation Agent

Purpose:

- Receive natural language user message.
- Understand user intent.
- Ask for clarification if required fields are missing.
- Convert the user message into a structured metric request.
- Never query Prometheus directly.
- Never generate PromQL.
- Never perform remediation.

Output should be one of:

```json
{
  "status": "needs_clarification",
  "message": "Which namespace should I check?"
}
```

or:

```json
{
  "status": "ready",
  "request": {
    "intent": "pod_restarts",
    "tool": "get_pod_restart_count",
    "namespace": "prod",
    "range": "24h",
    "source": "aks-dev"
  }
}
```

or:

```json
{
  "status": "refused",
  "message": "Phase 1 is read-only. I cannot restart, scale, roll back, or modify workloads. I can help inspect metrics and suggest read-only investigation steps."
}
```

#### B. Claude Explanation Agent

Purpose:

- Receive structured metric JSON from the backend tools.
- Explain the metric result clearly.
- Highlight abnormal patterns only when supported by data.
- Suggest read-only investigation steps.
- Keep the answer suitable for Microsoft Teams.
- After explanation is generated, the backend sends the final answer to Teams webhook.

This role already partly exists in `ai_agent.py`. Refactor or extend it cleanly.

### 2. Add Structured Request Models

Add Pydantic models for:

- incoming Teams chat request
- conversation agent output
- structured metric request
- final Teams response

Suggested models:

```python
class TeamsChatRequest(BaseModel):
    conversation_id: str | None = None
    user_id: str | None = None
    message: str
    source: str | None = None
    namespace: str | None = None
    service: str | None = None
    range: str | None = None
    send_to_teams: bool = True
```

```python
class StructuredMetricRequest(BaseModel):
    intent: str
    tool: str
    namespace: str | None = None
    service: str | None = None
    range: str = "24h"
    source: str | None = None
```

### 3. Add a New Teams Chat Endpoint

Add an endpoint such as:

```http
POST /teams/chat
```

Flow:

1. Receive user message from Teams integration layer.
2. Send message and optional context to Claude Conversation Agent.
3. If Claude returns `needs_clarification`:
   - send clarification question to Teams webhook
   - return status `needs_clarification`
4. If Claude returns `refused`:
   - send refusal message to Teams webhook
   - return status `refused`
5. If Claude returns `ready`:
   - validate the structured metric request
   - execute the matching controlled backend metric tool
   - send metric JSON to Claude Explanation Agent
   - send final explanation to Teams webhook
   - return status `sent`

Important:

- The endpoint can be used by a future Teams Bot.
- For now, using Teams webhook to send the final response is acceptable.
- Do not assume Incoming Webhook can receive user messages. It only sends messages to Teams.

### 4. Add Conversation Agent Module

Create a new module, for example:

```text
metrics-service/conversation_agent.py
```

Responsibilities:

- Build a strict system prompt for conversation understanding.
- Call Claude.
- Force JSON output.
- Parse and validate Claude output.
- Refuse unsafe remediation requests.
- Ask clarification when missing namespace/service/range.
- Map user intent only to approved tools.

Allowed tools:

- `get_cluster_health`
- `get_node_cpu_usage`
- `get_node_memory_usage`
- `get_pod_restart_count`
- `get_unhealthy_pods`
- `get_namespace_resource_usage`
- `get_service_error_rate`
- `get_top_resource_consuming_pods`

The conversation agent must never return PromQL.

### 5. Add Safe Tool Execution Layer

Do not let Claude directly call functions by arbitrary name.

Implement a whitelist dispatcher like:

```python
ALLOWED_TOOL_DISPATCH = {
    "get_cluster_health": get_cluster_health,
    "get_node_cpu_usage": get_node_cpu_usage,
    "get_node_memory_usage": get_node_memory_usage,
    "get_pod_restart_count": get_pod_restart_count,
    "get_unhealthy_pods": get_unhealthy_pods,
    "get_namespace_resource_usage": get_namespace_resource_usage,
    "get_service_error_rate": get_service_error_rate,
    "get_top_resource_consuming_pods": get_top_resource_consuming_pods,
}
```

Validate required fields before executing each tool.

Do not support an arbitrary PromQL field.

### 6. Reuse Teams Webhook Sender

Use existing `teams_sender.py`.

After Claude Explanation Agent generates the final message:

```python
send_to_teams(final_explanation, title="AKS Metrics Assistant")
```

For clarification:

```python
send_to_teams(clarification_message, title="AKS Metrics Assistant")
```

For refusal:

```python
send_to_teams(refusal_message, title="AKS Metrics Assistant")
```

### 7. Keep Old `/chat` Endpoint If Useful

The existing `/chat` endpoint may remain for simple API testing.

But the new `/teams/chat` endpoint should use the new flow:

```text
Claude Conversation Agent
  -> structured request
  -> controlled tools
  -> Claude Explanation Agent
  -> Teams webhook
```

### 8. Update Documentation

Update:

- `CLAUDE.md`
- `ARCHITECTURE.md`
- relevant `skills/*.md`

Document the new chat flow clearly:

```text
Teams Bot / chat integration receives user message
  -> calls POST /teams/chat
  -> Claude Conversation Agent understands intent
  -> backend executes controlled metric tools
  -> Claude Explanation Agent explains result
  -> Teams webhook sends final answer
```

Also document:

- Incoming Webhook is send-only.
- A real Teams Bot or integration layer is still needed to receive user messages.
- Phase 1 remains read-only.
- Claude never generates arbitrary PromQL.

## Acceptance Criteria

- A new Claude Conversation Agent exists.
- Conversation Agent outputs structured JSON only.
- Conversation Agent can return:
  - `needs_clarification`
  - `ready`
  - `refused`
- A new `/teams/chat` endpoint exists.
- `/teams/chat` sends clarification, refusal, or final explanation to Teams webhook.
- Final metric explanation is generated by Claude Explanation Agent.
- Final explanation is sent to Teams using existing Teams webhook code.
- Controlled metric tools are executed only through a whitelist dispatcher.
- No arbitrary PromQL endpoint is added.
- No remediation behavior is added.
- Existing daily report behavior still works.

## Key Reminder

Claude Conversation Agent manages the chat and produces a structured request.

Claude Explanation Agent explains metrics.

After the explanation is generated, the backend sends the result to Microsoft Teams through the Teams webhook.
