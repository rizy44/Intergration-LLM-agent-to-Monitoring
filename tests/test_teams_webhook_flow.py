"""
Tests for the hybrid Teams Outgoing Webhook flow (inline clarification).

- Clarification / refusal → returned in the HTTP response (in-thread),
  Incoming Webhook NOT called.
- Ready decision → ⏳ ack in-thread, answer executed in background and
  sent via Incoming Webhook.
- Slow Agent 1 → fallback to fully-async path within the 5s contract.
- handle_chat_message() regression: composition behaves as before.
"""

import time

import pytest
from fastapi.testclient import TestClient

import metrics_service.app as app_module
import metrics_service.chat_controller as cc
from metrics_service.chat_controller import handle_chat_message


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TEAMS_OUTGOING_WEBHOOK_SECRET", "c2VjcmV0")  # base64 "secret"
    from metrics_service.config import get_settings

    get_settings.cache_clear()
    # Bypass HMAC validation — not under test here
    monkeypatch.setattr(app_module, "validate_teams_signature", lambda *a, **k: True)
    yield TestClient(app_module.app)
    get_settings.cache_clear()


@pytest.fixture
def workflow_sends(monkeypatch):
    """Capture everything sent via the Incoming Webhook."""
    calls = []
    monkeypatch.setattr(
        cc, "_send_teams_message",
        lambda message, title="": calls.append({"message": message, "title": title}),
    )
    return calls


def _post(client, text="show pod restarts"):
    payload = {"type": "message", "text": f"<at>bot</at> {text}", "from": {"name": "ori"}}
    return client.post("/teams/webhook", json=payload)


def _reply_text(resp):
    return resp.json().get("text", "")


class TestHybridWebhook:
    def test_clarification_replies_in_thread(self, client, workflow_sends, monkeypatch):
        monkeypatch.setattr(
            cc, "parse_user_message",
            lambda message, history=None: {
                "status": "needs_clarification",
                "message": "Which namespace do you mean?",
            },
        )
        resp = _post(client)
        assert resp.status_code == 200
        assert "Which namespace" in _reply_text(resp)
        assert workflow_sends == []  # nothing via Workflow

    def test_refused_replies_in_thread(self, client, workflow_sends, monkeypatch):
        monkeypatch.setattr(
            cc, "parse_user_message",
            lambda message, history=None: {"status": "refused", "message": "Not supported in Phase 1."},
        )
        resp = _post(client)
        assert "Not supported" in _reply_text(resp)
        assert workflow_sends == []

    def test_remediation_guard_in_thread_no_llm(self, client, workflow_sends, monkeypatch):
        def boom(message, history=None):
            raise AssertionError("Conversation Agent must not be called")

        monkeypatch.setattr(cc, "parse_user_message", boom)
        resp = _post(client, text="restart pod api-service")
        assert "read-only" in _reply_text(resp)
        assert workflow_sends == []

    def test_ready_acks_then_answers_via_workflow(self, client, workflow_sends, monkeypatch):
        monkeypatch.setattr(
            cc, "parse_user_message",
            lambda message, history=None: {
                "status": "ready",
                "request": {"tool": "get_cluster_health"},
            },
        )
        monkeypatch.setattr(
            cc, "dispatch_tool",
            lambda request: ("get_cluster_health", {"status": "healthy", "source": {"name": "test"}}),
        )
        monkeypatch.setattr(
            cc, "analyze_metrics",
            lambda tool_name, metric_data, user_question="": "Cluster is healthy.",
        )
        resp = _post(client, text="is the cluster healthy?")
        # In-thread: only the ack
        assert "⏳" in _reply_text(resp)
        # TestClient runs background tasks before returning → answer via Workflow
        assert len(workflow_sends) == 1
        assert "Cluster is healthy." in workflow_sends[0]["message"]

    def test_slow_agent_falls_back_to_async(self, client, workflow_sends, monkeypatch):
        monkeypatch.setattr(app_module, "SYNC_DECISION_TIMEOUT_SECONDS", 0.05)

        def slow_decide(message, history=None):
            time.sleep(0.3)
            return {"status": "needs_clarification", "message": "Which namespace?"}

        monkeypatch.setattr(cc, "parse_user_message", slow_decide)
        resp = _post(client)
        # In-thread: generic ack (fallback), clarification arrives via Workflow
        assert "⏳" in _reply_text(resp)
        assert len(workflow_sends) == 1
        assert "Which namespace" in workflow_sends[0]["message"]


class TestHandleChatMessageRegression:
    def test_clarification_status_and_optional_send(self, workflow_sends, monkeypatch):
        monkeypatch.setattr(
            cc, "parse_user_message",
            lambda message, history=None: {"status": "needs_clarification", "message": "Which ns?"},
        )
        r1 = handle_chat_message("show restarts")
        assert r1.status == "needs_clarification"
        assert workflow_sends == []
        r2 = handle_chat_message("show restarts", send_to_teams=True)
        assert r2.status == "needs_clarification"
        assert len(workflow_sends) == 1

    def test_answered_flow(self, workflow_sends, monkeypatch):
        monkeypatch.setattr(
            cc, "parse_user_message",
            lambda message, history=None: {"status": "ready", "request": {"tool": "get_cluster_health"}},
        )
        monkeypatch.setattr(
            cc, "dispatch_tool",
            lambda request: ("get_cluster_health", {"source": {"name": "s"}}),
        )
        monkeypatch.setattr(
            cc, "analyze_metrics",
            lambda tool_name, metric_data, user_question="": "All good.",
        )
        r = handle_chat_message("healthy?", user="ori", send_to_teams=True)
        assert r.status == "answered"
        assert r.tool_used == "get_cluster_health"
        assert len(workflow_sends) == 1
        assert "ori" in workflow_sends[0]["message"]

    def test_empty_message_error_no_send(self, workflow_sends):
        r = handle_chat_message("   ", send_to_teams=True)
        assert r.status == "error"
        assert workflow_sends == []
