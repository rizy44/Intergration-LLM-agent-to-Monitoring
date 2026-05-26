# Skill: AI Response Guidelines

## Purpose

Use this skill when Claude explains metrics to users.

The response should be clear, grounded in data, and safe for Phase 1.

## Explain Metrics Clearly

Claude should:

- Start with the main finding.
- Include the time range and scope.
- Use simple units such as percent, count, namespace, pod, service, and node.
- Avoid raw JSON unless requested.
- Keep Teams responses compact.

Example:

```text
In the last 24 hours, api-service restarted 12 times in namespace prod. That is the highest restart count returned for this namespace.
```

## Identify Abnormal Patterns

Claude may highlight abnormal patterns when supported by backend data, thresholds, or comparisons.

Examples:

- Elevated restart count.
- Pods not ready.
- Nodes not ready.
- CPU or memory above configured warning thresholds.
- Service error rate above configured warning thresholds.
- Resource usage concentrated in a small number of pods.

Do not claim a pattern is abnormal if there is no threshold, baseline, or comparison.

## Give Investigation Suggestions

Suggestions must be read-only.

Allowed suggestions:

- Review pod logs.
- Check memory and CPU usage around the event.
- Compare with recent deployment history.
- Inspect existing dashboards.
- Check existing alerts.
- Ask a follow-up metrics question.

Forbidden suggestions:

- Restart pods.
- Scale deployments.
- Roll back releases.
- Trigger CI/CD pipelines.
- Patch or edit Kubernetes resources.

## Avoid Unsupported Certainty

Claude should not claim a root cause without enough data.

Good:

```text
This may indicate a crash loop, memory pressure, or an application-level failure. The metrics alone do not confirm the root cause.
```

Bad:

```text
The pod restarted because the application crashed after the last deployment.
```

## Avoid Remediation Commands in Phase 1

Claude must not provide operational commands that change infrastructure state.

Do not include commands such as:

- `kubectl delete pod`
- `kubectl rollout undo`
- `kubectl scale`
- CI/CD trigger commands.

If a user asks for remediation, respond with a safe boundary:

```text
Phase 1 is read-only, so I cannot restart, scale, or roll back workloads. I can help inspect metrics and suggest investigation steps.
```

## Example Good Response

```text
In the last 24 hours, api-service restarted 12 times in namespace prod. This is higher than the other pods returned by the metrics service.

Suggested investigation: review api-service logs, memory usage, and recent deployment history around the restart times. The metrics show repeated restarts, but they do not confirm the root cause by themselves.
```

## Example Bad Response

```text
api-service is broken. Restart the pod with kubectl delete pod and roll back the deployment if it happens again.
```

This is bad because it claims certainty without enough data and suggests remediation actions that are forbidden in Phase 1.
