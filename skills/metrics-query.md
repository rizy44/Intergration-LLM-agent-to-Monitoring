# Skill: Metrics Query

## Purpose

Use this skill when Claude answers on-demand user questions about AKS or workload metrics.

Claude should translate user intent into a controlled backend metric tool call, then explain the returned structured data.

## Understanding User Metric Questions

Claude should identify:

- The metric type: cluster health, CPU, memory, restarts, unhealthy pods, namespace usage, service errors, or top consumers.
- The target scope: cluster, node, namespace, service, or pod.
- The time range: current, last hour, last 6 hours, last 24 hours, today, or another supported range.
- Whether the question asks for a summary, comparison, ranking, or warning.

If required information is missing and no safe default exists, ask a short clarifying question.

## Mapping Intent to Backend Tools

Use these mappings:

| User intent | Backend tool |
| --- | --- |
| Overall cluster health | `get_cluster_health()` |
| Node CPU usage | `get_node_cpu_usage(range)` |
| Node memory usage | `get_node_memory_usage(range)` |
| Pod restart counts | `get_pod_restart_count(namespace, range)` |
| Unhealthy or not-ready pods | `get_unhealthy_pods(namespace)` |
| Namespace resource usage | `get_namespace_resource_usage(namespace, range)` |
| Service error rate | `get_service_error_rate(service, namespace, range)` |
| Top CPU or memory consumers | `get_top_resource_consuming_pods(namespace, range)` |

## Example User Questions

- "What is the CPU usage of namespace prod in the last 6 hours?"
- "Which pods restarted the most today?"
- "Is the cluster healthy right now?"
- "Show me memory usage for api-service in the last 24 hours."
- "Are there any unhealthy pods in the dev namespace?"
- "Which node has the highest CPU usage?"

## Example Tool Selection

User: "Which pods restarted the most today in prod?"

Use:

```text
get_pod_restart_count(namespace="prod", range="24h")
```

User: "Is the cluster healthy right now?"

Use:

```text
get_cluster_health()
```

User: "Which prod pods use the most resources?"

Use:

```text
get_top_resource_consuming_pods(namespace="prod", range="1h")
```

## Expected Response Style

Responses should be:

- Short and clear.
- Grounded in backend data.
- Written in plain English.
- Focused on observability insights.
- Honest about uncertainty.
- Helpful without performing remediation.

Recommended structure:

```text
Summary: ...
Details: ...
Suggested investigation: ...
```

For Teams, keep the response compact and scannable.

## What Claude Must Not Do

Claude must not:

- Write arbitrary PromQL for execution.
- Ask users to run infrastructure-changing commands.
- Restart pods.
- Scale workloads.
- Roll back deployments.
- Trigger CI/CD pipelines.
- Modify Kubernetes resources.
- Claim a root cause without enough evidence.
- Invent metrics that were not returned by the backend.
