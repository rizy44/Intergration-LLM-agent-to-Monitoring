# Skill: Prometheus Tools

## Purpose

Use this skill when defining or using the controlled backend metric tools that access Prometheus.

Prometheus must be accessed only by backend tools. Claude must not write or execute arbitrary PromQL directly.

## Access Rule

The Metrics Service is the only allowed Prometheus client.

Claude may request data only through approved backend functions. The backend owns the actual PromQL templates, validation, timeouts, and result limits.

## Required Backend Metric Functions

The Metrics Service should provide these read-only functions:

- `get_cluster_health()`
- `get_node_cpu_usage(range)`
- `get_node_memory_usage(range)`
- `get_pod_restart_count(namespace, range)`
- `get_unhealthy_pods(namespace)`
- `get_namespace_resource_usage(namespace, range)`
- `get_service_error_rate(service, namespace, range)`
- `get_top_resource_consuming_pods(namespace, range)`

All functions should return structured JSON.

## Expected JSON Response Examples

Pod restart count:

```json
{
  "namespace": "prod",
  "range": "24h",
  "pods": [
    {
      "pod": "api-service-7d9f8c9c4f-abcde",
      "restarts": 12
    },
    {
      "pod": "worker-6c4d9f8d7f-xyz12",
      "restarts": 5
    }
  ]
}
```

Cluster health:

```json
{
  "status": "healthy_with_warnings",
  "range": "24h",
  "nodes_not_ready": 0,
  "unhealthy_pods": 2,
  "warnings": [
    "api-service restarted 12 times in namespace prod"
  ]
}
```

Node CPU usage:

```json
{
  "range": "6h",
  "nodes": [
    {
      "node": "aks-nodepool1-12345678-vmss000001",
      "average_cpu_percent": 42.1,
      "max_cpu_percent": 73.4
    }
  ]
}
```

Service error rate:

```json
{
  "service": "api-service",
  "namespace": "prod",
  "range": "1h",
  "error_rate_percent": 2.4,
  "request_count": 12500
}
```

## Query Timeout Rules

Backend tools should:

- Use a configured timeout for Prometheus requests.
- Return a safe timeout error when Prometheus does not respond.
- Avoid retry storms.
- Log timeout details without leaking secrets.

## Result Limit Rules

Backend tools should:

- Limit query time ranges.
- Limit the number of returned series.
- Limit top-N responses.
- Avoid returning huge raw Prometheus payloads to Claude.
- Normalize results before returning them to the AI layer.

## What Claude Must Not Do

Claude must not:

- Generate arbitrary PromQL for direct execution.
- Ask users to paste unrestricted PromQL into the system.
- Bypass backend validation.
- Change Prometheus configuration.
- Expose internal Prometheus URLs or credentials.
