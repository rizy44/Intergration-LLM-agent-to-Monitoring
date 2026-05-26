# Skill: Teams Integration

## Purpose

Use this skill when preparing messages for Microsoft Teams daily reports or on-demand Teams bot responses.

Messages should be concise, readable, and safe for a shared operations channel.

## Daily Teams Report Behavior

Daily reports should:

- Be sent on the configured schedule.
- Cover the last 24 hours unless configured otherwise.
- Start with the report title and time range.
- Show overall status near the top.
- Include only the most important metrics and warnings.
- End with read-only investigation suggestions when needed.

## On-Demand Teams Bot Behavior

For user questions, the bot should:

- Confirm the interpreted scope when useful.
- Answer with the metric result first.
- Include a short explanation.
- Suggest read-only next steps if the result is abnormal.
- Ask for missing namespace, service, or time range only when needed.

## Message Formatting Rules

Use simple Markdown that works well in Teams:

- Short headings.
- Bullet lists for summaries.
- Plain text percentages and counts.
- No large raw JSON blocks unless the user explicitly asks for raw data.
- No secrets, tokens, internal URLs, or stack traces.

Preferred structure:

```text
Status: Healthy with warnings

Summary:
- ...

Warnings:
- ...

Suggested investigation:
- ...
```

## Keep Messages Short and Readable

Teams messages should avoid long explanations. Include enough context for action, but keep details compact.

If there are many results, show the top results and mention that the backend returned more data.

Example:

```text
Top restarting pods in prod during the last 24 hours:
- api-service: 12 restarts
- worker: 5 restarts
- scheduler: 2 restarts

Suggested investigation: review logs and memory usage for api-service around the restart times.
```

## Error Message Behavior

Errors should be safe and actionable.

Good:

```text
I could not retrieve pod restart metrics for namespace prod right now. The metrics service timed out. Please try again later or check the metrics service health.
```

Bad:

```text
Prometheus request failed with token abc123 and internal URL http://prometheus.private...
```

Do not expose secrets, credentials, internal stack traces, or sensitive endpoint details.
