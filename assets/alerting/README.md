# Alerting — Alertmanager → Teams + Azure 15-min Poll

Proactive alert notifications for the AKS Metrics Assistant (Phase 1, read-only). Two paths:

1. **Kubernetes** — Alertmanager push (this page, below).
2. **Azure resources** — CronJob poll every 15 minutes: chart `k8s-helm/moni-agent-alert` → `POST /alerts/azure-check` → thresholds in `AZURE_ALERT_*` env vars on the service → same Teams alert channel. Test locally: `curl -X POST http://localhost:8000/alerts/azure-check -H "Authorization: Bearer $ALERT_WEBHOOK_TOKEN"`.

```text
Prometheus (alert rules + thresholds)
  → Alertmanager (group / dedup / repeat / resolve)
  → POST /alerts/alertmanager   (bearer-token auth)
  → alert_formatter.py          (rule-based, NO LLM)
  → Teams alert channel         (TEAMS_ALERT_WEBHOOK_URL)
```

Alerts go to a **dedicated Teams channel**, separate from the daily-report channel. No LLM is involved anywhere in the alert path.

## Setup

1. **Create a Teams Incoming Webhook** in the alert channel and note the URL.

2. **Configure the metrics service** (Kubernetes Secret → env vars):

   | Variable | Purpose |
   |---|---|
   | `TEAMS_ALERT_WEBHOOK_URL` | Incoming Webhook of the dedicated alert channel |
   | `ALERT_WEBHOOK_TOKEN` | Bearer token Alertmanager must present; endpoint returns 503 if unset (fail closed) |

   Both tokens are templated in `k8s-helm/moni-agent/values.tokenized.yaml`.

3. **Apply the Prometheus rules**: add `prometheus-alert-rules.yaml` to your Prometheus `rule_files` (or wrap in a `PrometheusRule` CRD for kube-prometheus-stack) and reload.

4. **Add the Alertmanager route/receiver**: merge `alertmanager-route-example.yaml` into your Alertmanager config. Point the webhook URL at the cluster-internal service DNS and mount the bearer token from a Secret.

## Tuning thresholds

Thresholds live **only** in `prometheus-alert-rules.yaml`. Edit the value in the `expr`, reload Prometheus, done — no service redeploy.

Starter values:

| Alert | Threshold | For | Severity |
|---|---|---|---|
| NodeHighCPU | > 85% | 10m | warning |
| NodeHighMemory | > 90% | 10m | warning |
| NodeNotReady | Ready == 0 | 5m | critical |
| NodeDiskPressure | > 85% used | 10m | warning |
| PodCrashLooping | > 3 restarts / 15m | — | critical |
| PodNotHealthy | Pending/Failed/Unknown | 10m | warning |
| ServiceHighErrorRate | 5xx > 5% | 5m | critical |

Noise control (cooldown, grouping, resolved notifications) is tuned in the Alertmanager route: `group_wait`, `group_interval`, `repeat_interval`, `send_resolved`.

## Testing

Send a sample payload manually:

```bash
curl -X POST "http://<METRICS_SERVICE_HOST>/alerts/alertmanager" \
  -H "Authorization: Bearer $ALERT_WEBHOOK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "version": "4",
    "status": "firing",
    "alerts": [{
      "status": "firing",
      "labels": {"alertname": "NodeHighCPU", "severity": "warning", "node": "aks-nodepool1-12345"},
      "annotations": {"summary": "Node aks-nodepool1-12345 CPU usage is above 85% (current: 91.2%)."},
      "startsAt": "2026-06-11T07:00:00Z"
    }]
  }'
```

Expected: HTTP 200 `{"status":"ok","sent":true,"alerts":1}` and a red FIRING card in the Teams alert channel.

## Security notes

- Do **not** expose `/alerts/alertmanager` through public ingress; the bearer token is defense-in-depth, not the only barrier.
- The endpoint fails closed (503) when `ALERT_WEBHOOK_TOKEN` is unset.
- Alert messages never include secrets, raw label dumps, internal URLs, or stack traces.
- Delivery is at-least-once: Alertmanager retries failures, so occasional duplicates in Teams are expected and acceptable.
