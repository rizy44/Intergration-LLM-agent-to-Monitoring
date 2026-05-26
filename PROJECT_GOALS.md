Phase 1 Goals: AI-powered AKS Metrics Assistant
Project Overview

This project aims to build the first phase of an AI-powered observability assistant for Azure Kubernetes Service, or AKS.

In Phase 1, the system focuses only on collecting, querying, summarizing, and reporting metrics from Prometheus. The system does not perform remediation, auto-fixing, rollback, scaling, or deployment changes in this phase.

The main goal is to provide two capabilities:

1. Daily automated metrics reporting
2. On-demand metrics query through a chat interface

The AI assistant should help users understand cluster health, workload status, and abnormal metric patterns by converting raw Prometheus metrics into clear human-readable explanations.

Phase 1 Scope
In Scope

The system should support:

- Querying Prometheus metrics through a controlled backend service
- Generating daily AKS health reports
- Sending daily reports to Microsoft Teams
- Providing a chatbox or Teams-based chat interface for on-demand metric questions
- Using Claude or another LLM to analyze metric summaries and generate explanations
- Returning safe, concise, and useful observability insights

The backend should expose predefined tools or API functions for metrics retrieval instead of allowing the LLM to execute arbitrary PromQL queries directly.

Out of Scope

The following features are not required in Phase 1:

- Automatic remediation
- Restarting pods
- Scaling deployments
- Rolling back releases
- Triggering CI/CD pipelines
- Creating tickets
- Modifying Kubernetes resources
- Running kubectl commands directly from AI
- Allowing unrestricted PromQL generation by the LLM
- n8n workflow orchestration

These features may be added in future phases.

Core Architecture

The recommended Phase 1 architecture is:

Prometheus
    ↓
Metrics Service / Tool Backend
    ↓
Claude / AI Analysis Layer
    ↓
Microsoft Teams / Chatbox

There are two main flows.

Flow 1: Daily Report Flow
Kubernetes CronJob
    ↓
Metrics Service
    ↓
Prometheus API
    ↓
Metrics Summary JSON
    ↓
Claude / AI Analysis
    ↓
Microsoft Teams Daily Report

The daily report should summarize AKS health over the last 24 hours.

The report should include metrics such as:

- Cluster health status
- Node CPU usage
- Node memory usage
- Pod restart count
- Unhealthy pods
- Pods not ready
- Namespace-level resource usage
- Service error rate if available
- High resource usage warnings
- Short recommendation or investigation hint

The report should be easy to read in Microsoft Teams.

Flow 2: On-demand Chatbox Flow
User asks a question
    ↓
Chatbox / Teams Bot
    ↓
AI Agent
    ↓
Metrics Service tool call
    ↓
Prometheus API
    ↓
Metrics result
    ↓
AI explanation
    ↓
Response to user

Example user questions:

- What is the CPU usage of namespace prod in the last 6 hours?
- Which pods restarted the most today?
- Is the cluster healthy right now?
- Show me memory usage for api-service in the last 24 hours.
- Are there any unhealthy pods in the dev namespace?
- Which node has the highest CPU usage?

The AI should understand the user’s question, choose the correct backend metric function, and explain the result clearly.

Metrics Service Goals

The Metrics Service is the core backend layer.

It should be responsible for:

- Connecting to Prometheus safely
- Running predefined PromQL queries
- Limiting query range and result size
- Normalizing Prometheus responses into clean JSON
- Handling Prometheus errors and timeouts
- Providing reusable metric functions for the AI layer
- Preventing the AI from directly executing arbitrary PromQL

Recommended backend framework:

FastAPI

Recommended internal modules:

metrics-service/
├── app.py
├── config.py
├── prometheus_client.py
├── tools/
│   ├── cluster_health.py
│   ├── node_cpu.py
│   ├── node_memory.py
│   ├── pod_restarts.py
│   ├── unhealthy_pods.py
│   ├── namespace_usage.py
│   └── service_errors.py
├── ai_agent.py
├── teams_sender.py
└── daily_report.py
Required Metric Tools

The backend should provide controlled metric functions such as:

get_cluster_health()
get_node_cpu_usage(range)
get_node_memory_usage(range)
get_pod_restart_count(namespace, range)
get_unhealthy_pods(namespace)
get_namespace_resource_usage(namespace, range)
get_service_error_rate(service, namespace, range)
get_top_resource_consuming_pods(namespace, range)

Each function should return structured JSON.

Example:

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
AI Layer Goals

The AI layer should not directly access Prometheus.

The AI should only:

- Understand the user request
- Select the appropriate metric tool
- Interpret structured metric results
- Generate clear explanations
- Highlight abnormal values
- Suggest investigation steps

For Phase 1, suggestions should be read-only and advisory.

Example:

The api-service pod restarted 12 times in the last 24 hours. 
This may indicate a crash loop, memory pressure, or application-level failure. 
Please check the pod logs and recent deployment history.

The AI must not perform automatic remediation in Phase 1.

Microsoft Teams Goals

The system should send daily reports to Microsoft Teams.

The Teams message should include:

- Report title
- Time range
- Overall cluster status
- Key metric summary
- Warning section
- Investigation suggestions

Example Teams report:

Daily AKS Health Report

Time Range: Last 24 hours
Status: Healthy with warnings

Summary:
- Average node CPU usage: 42%
- Average node memory usage: 67%
- 3 pods restarted in namespace prod
- No node is currently NotReady
- api-service had elevated restart count

Recommendation:
Check api-service logs and memory usage around the restart time.
Security and Safety Goals

The system should follow these constraints:

- Do not expose Prometheus publicly
- Do not allow arbitrary PromQL from the LLM
- Store secrets using environment variables or Kubernetes Secrets
- Use read-only access for metrics collection
- Validate all user input
- Add query timeout and result limits
- Log errors without leaking secrets
- Do not allow the AI to modify Kubernetes resources in Phase 1
Future Phase Compatibility

Although n8n is not required in Phase 1, the system should be designed so that n8n can be added later.

Future phases may include:

- AI-generated remediation suggestions
- Incident ticket creation
- Approval-based remediation
- Pipeline triggering
- Rollback workflow
- Kubernetes resource changes
- n8n workflow orchestration
- Incident history database

The Metrics Service should expose stable APIs so future workflow tools like n8n can call them easily.

Final Phase 1 Goal Statement

The goal of Phase 1 is to build a read-only AI-powered AKS metrics assistant that can collect metrics from Prometheus, generate daily health reports for Microsoft Teams, and answer user questions through a chatbox or Teams bot. The system should use a controlled Metrics Service as the only layer that queries Prometheus, while Claude or another LLM is responsible for interpreting the metrics and generating human-readable insights. No remediation or infrastructure changes should be performed in this phase.