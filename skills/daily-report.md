# Skill: Daily Report

## Purpose

Use this skill when generating the automated daily AKS health report for Microsoft Teams.

The daily report should summarize the last 24 hours of cluster and workload health using metrics returned by the backend across selected Prometheus-compatible datasources.

Use `DAILY_REPORT_SOURCES` as a comma-separated allowlist so the report does not send unnecessary datasource results to Claude. Use `DAILY_REPORT_NAMESPACES` to keep namespace collection focused.

## Required Report Sections

The daily report should include:

- Report title.
- Time range.
- Scope / selected sources.
- Overall cluster status.
- Key metric summary.
- Source-by-source breakdown.
- Warning section.
- Read-only investigation suggestions.

## Recommended Microsoft Teams Format

Keep the report short enough to read quickly in Teams:

```text
Daily AKS Health Report

Time Range: Last 24 hours
Scope: Selected metric sources
Status: Healthy with warnings

Summary:
- aks-dev is healthy with warnings
- uat-monitor-workspace-prometheus reported elevated restarts in prod

Source Breakdown:
- aks-dev: average node CPU 42%, memory 67%, no NotReady nodes
- uat-monitor-workspace-prometheus: 3 pods restarted in namespace prod

Warnings:
- api-service restarted 12 times

Suggested investigation:
- Review api-service logs, memory usage, and recent deployment history around the restart times.
```

## Metrics to Include

Include these metrics per configured source when available:

- Cluster health status.
- Node CPU usage.
- Node memory usage.
- Pod restart count.
- Unhealthy pods.
- Pods not ready.
- Namespace-level resource usage.
- Service error rate.
- High resource usage warnings.
- Short investigation hints.

## Warning Style

Warnings should be:

- Specific.
- Based on returned data.
- Labeled with the source name when the report covers multiple datasources.
- Prioritized by likely operational impact.
- Free of speculation presented as fact.

Good warning:

```text
api-service restarted 12 times in namespace prod during the last 24 hours.
```

Bad warning:

```text
api-service is broken and must be restarted.
```

## Recommendation Style

Recommendations must be read-only and advisory.

Allowed:

- "Review pod logs."
- "Check memory usage around the restart time."
- "Compare with recent deployment history."
- "Inspect existing dashboards for the affected namespace."

Forbidden:

- "Restart the pod."
- "Scale the deployment."
- "Rollback the release."
- "Trigger a pipeline."

## Example Daily Report

```text
Daily AKS Health Report

Time Range: Last 24 hours
Scope: All configured metric sources
Status: Healthy with warnings

Summary:
- aks-dev is healthy with warnings.
- uat-monitor-workspace-prometheus reported elevated restart count in prod.

Source Breakdown:
- aks-dev: average node CPU 42%, average node memory 67%, no NotReady nodes.
- uat-monitor-workspace-prometheus: api-service restarted 12 times in namespace prod.

Warnings:
- uat-monitor-workspace-prometheus: api-service restarted 12 times in namespace prod.
- aks-dev: worker had memory usage above the configured warning threshold.

Suggested investigation:
- Review api-service logs and memory usage around the restart windows.
- Check whether worker memory usage changed after recent deployments.
```
