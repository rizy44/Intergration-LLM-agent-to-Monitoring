# Skill: Security Constraints

## Purpose

Use this skill whenever designing, implementing, or reviewing Phase 1 behavior.

Security and read-only safety constraints override convenience.

## Read-Only Design

Phase 1 must only collect, query, summarize, and report metrics.

Allowed:

- Read Prometheus metrics through backend tools.
- Summarize results.
- Send reports and answers.
- Suggest read-only investigation steps.

Forbidden:

- Restarting pods.
- Scaling deployments.
- Rolling back releases.
- Triggering CI/CD pipelines.
- Modifying Kubernetes resources.
- Running AI-initiated `kubectl` commands.

## No Arbitrary PromQL

Claude and other LLMs must not directly execute arbitrary PromQL.

Prometheus access must go through controlled backend functions with predefined query templates, validation, timeouts, and limits.

## No Infrastructure Modification

The system must not expose API routes, tools, jobs, or prompts that modify infrastructure in Phase 1.

If a future phase adds remediation, it must require explicit approval workflows and separate safety design.

## Secrets Management

Store secrets using:

- Environment variables.
- Kubernetes Secrets.
- Approved secret management systems.

Do not:

- Commit secrets.
- Print secrets in logs.
- Include secrets in Teams messages.
- Send secrets to Claude prompts.
- Return secrets in API errors.

## Input Validation

Validate all user-controlled inputs, including:

- Namespace.
- Service name.
- Pod name.
- Time range.
- Top-N limits.
- Query type.

Reject unsupported values with safe error messages.

## Logging Rules

Logs should support debugging without leaking sensitive data.

Log:

- Request IDs.
- Tool names.
- Safe parameter summaries.
- Error categories.
- Timing and timeout events.

Do not log:

- Tokens.
- Passwords.
- Secret headers.
- Full internal URLs with credentials.
- Raw prompts containing sensitive content.

## Access Control

Use the least privilege needed for metrics collection.

The metrics path should be read-only and limited to the namespaces, clusters, or Prometheus endpoints required by the deployment.

## Safe Failure Handling

When an error occurs:

- Return a concise user-safe message.
- Avoid stack traces in user-facing responses.
- Avoid leaking internal endpoint details.
- Suggest retrying or checking the metrics service health.
- Preserve enough sanitized logging for operators.
