# Teams Integration Setup Guide

This guide explains how to wire Microsoft Teams to the AKS Metrics Assistant
so users can ask metric questions directly inside Teams and receive AI-powered answers.

---

## How the end-to-end flow works

```
User types in Teams channel:
  "@AKSBot show pod restarts in namespace prod"
           │
           ▼
  Teams Outgoing Webhook
  POST /teams/webhook  ──► HMAC signature validated
           │
           ▼
  Conversation Agent (Claude)
  maps message to: get_pod_restart_count, namespace=prod, range=24h
           │
           ▼
  Tool Dispatcher
  calls: get_pod_restart_count(namespace="prod", range="24h")
  queries Prometheus via metrics-service
           │
           ▼
  Explanation Agent (Claude)
  generates: plain-English summary of restart data
           │
           ▼
  HTTP response body → Teams renders as bot reply in thread
```

Two connectors are used:

| Connector | Direction | Purpose |
|---|---|---|
| **Outgoing Webhook** | Teams → Service | Receive user questions |
| **Incoming Webhook** | Service → Teams | Send daily reports and `/teams/chat` replies |

---

## Option A — Teams Outgoing Webhook (Recommended)

This is the primary integration method. Users @mention the bot in any channel
and receive a reply in the same thread.

### Step 1 — Expose the metrics-service endpoint

The `/teams/webhook` endpoint must be reachable from the Microsoft Teams cloud
(or via your internal network if using a Teams data-resident deployment).

Options:
- **Azure API Management** (recommended for enterprise) — front the service
  with APIM and expose only `/teams/webhook`.
- **Azure Application Gateway / Ingress** with TLS termination.
- **Internal network only** — if your Teams tenant is configured for internal
  webhooks (not supported by all tenants).

The endpoint URL will be:
```
https://your-domain.example.com/teams/webhook
```

### Step 2 — Create the Outgoing Webhook in Teams

1. Open the Teams channel where you want the bot.
2. Click the **...** (More options) next to the channel name.
3. Select **Manage channel** → **Apps** tab → **Create an outgoing webhook**.
4. Fill in:
   - **Name**: `AKSBot` (users will @mention this name)
   - **Callback URL**: `https://your-domain.example.com/teams/webhook`
   - **Description**: `AKS Metrics Assistant — Phase 1`
5. Click **Create**.
6. Teams shows a **Security token** — this is a base64-encoded HMAC secret.
   **Copy it immediately** — it is only shown once.

### Step 3 — Store the secret

Set the security token as `TEAMS_OUTGOING_WEBHOOK_SECRET` in your Kubernetes Secret:

```yaml
# k8s/secret-template.yaml
TEAMS_OUTGOING_WEBHOOK_SECRET: "paste-the-base64-token-here"
```

Or for local development in `.env`:
```
TEAMS_OUTGOING_WEBHOOK_SECRET=paste-the-base64-token-here
```

### Step 4 — Test

In the Teams channel, type:
```
@AKSBot is the cluster healthy?
```

Expected reply in the same thread:
```
Summary: The AKS cluster is healthy. All nodes are Ready and 0 pods are
in a failed state across monitored namespaces.
...
```

### Supported example questions

| Question | Tool called |
|---|---|
| `@AKSBot is the cluster healthy?` | `get_cluster_health` |
| `@AKSBot show node CPU over the last 6 hours` | `get_node_cpu_usage` |
| `@AKSBot how much memory are the nodes using?` | `get_node_memory_usage` |
| `@AKSBot show pod restarts in namespace prod` | `get_pod_restart_count` |
| `@AKSBot are there any unhealthy pods in monitoring?` | `get_unhealthy_pods` |
| `@AKSBot namespace resource usage for default` | `get_namespace_resource_usage` |
| `@AKSBot top CPU consumers in namespace prod` | `get_top_resource_consuming_pods` |
| `@AKSBot error rate for api-service in prod` | `get_service_error_rate` |

---

## Option B — Power Automate Flow (No code changes needed)

Use this if:
- You cannot expose the service externally.
- You prefer a no-code Teams integration.
- You want to monitor a specific channel (not @mention-based).

### Setup

1. Go to [Power Automate](https://make.powerautomate.com).
2. Create a new **Automated cloud flow**.
3. Trigger: **When a new message is posted in a channel** (Teams connector).
   - Select your Team and Channel.
   - Filter to only messages that start with a trigger phrase, e.g. `!ask`.
4. Add action: **HTTP** → POST.
   - URI: `http://aks-metrics-service.monitoring.svc.cluster.local:8000/teams/chat`
     (internal service DNS, reachable from Power Automate only if network peered;
     otherwise use the external URL).
   - Body:
     ```json
     {
       "message": "@{triggerBody()?['body']?['content']}",
       "user": "@{triggerBody()?['from']?['user']?['displayName']}"
     }
     ```
5. Add action: **Reply to a message in a channel** (Teams connector).
   - Message: `@{body('HTTP')?['reply']}`

### Comparison

| | Outgoing Webhook | Power Automate |
|---|---|---|
| User experience | @mention in any channel | Specific channel + prefix |
| External exposure | Required | Optional |
| Response time | ~3–8 seconds | ~5–15 seconds |
| Teams admin required | Yes (create webhook) | No (user-level) |
| Cost | Free | Power Automate license |

---

## Daily Report Setup (Kubernetes CronJob)

The daily report runs independently of the chat integration. It uses only
the **Incoming Webhook** to send a formatted report to Teams each morning.

The CronJob in `k8s/cronjob.yaml` triggers `POST /daily-report` at 08:00 UTC daily.

Ensure `TEAMS_WEBHOOK_URL` is set to your Incoming Webhook URL in the Kubernetes Secret.

To create a Teams Incoming Webhook:
1. In Teams, click **...** next to the channel → **Connectors**.
2. Search for **Incoming Webhook** → **Configure**.
3. Name it `AKS Daily Report`, upload an icon if desired.
4. Copy the webhook URL and set it as `TEAMS_WEBHOOK_URL` in your secret.

---

## Security checklist

- [ ] `TEAMS_OUTGOING_WEBHOOK_SECRET` is stored in a Kubernetes Secret, not in code.
- [ ] The `/teams/webhook` endpoint is only reachable via HTTPS (TLS termination at ingress).
- [ ] The service does not expose Prometheus externally.
- [ ] `ANTHROPIC_API_KEY` is stored in a Kubernetes Secret.
- [ ] `TEAMS_WEBHOOK_URL` (Incoming) is stored in a Kubernetes Secret.
- [ ] Log level is `INFO` or higher in production (no `DEBUG` secret exposure).
- [ ] The Kubernetes Secret is NOT committed to source control.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| HTTP 401 from `/teams/webhook` | Wrong HMAC secret | Re-copy the security token from Teams and update the secret |
| Bot does not respond | Endpoint not reachable | Check ingress / network policy |
| "AI service unavailable" | Missing ANTHROPIC_API_KEY | Check secret in namespace `monitoring` |
| Empty or truncated reply | Claude max_tokens too low | Increase `CLAUDE_MAX_TOKENS` in env |
| "Tool not available" reply | Question maps to no tool | Rephrase the question; see supported examples above |
