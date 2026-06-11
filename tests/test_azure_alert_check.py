"""
Tests for the Azure metrics poll-alert path (azure_alert_check.py).

Covers: breach fires, cooldown suppression, post-cooldown re-fire,
resolved-once semantics, null metrics, collection errors, endpoint auth,
and the no-LLM guarantee.
"""

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from metrics_service import azure_alert_check as aac

TEST_TOKEN = "test-alert-token"

PROJECT = {
    "name": "proj-a",
    "resource_group": "rg-a",
    "app_services": [],
    "mysql": ["db-1"],
    "postgres": [],
    "redis": [],
    "service_bus": [],
}


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    aac.reset_alert_state()
    monkeypatch.setattr(aac, "PROD_PROJECTS", [PROJECT])
    yield
    aac.reset_alert_state()


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(
        aac, "send_alert_to_teams",
        lambda message, title="": calls.append({"message": message, "title": title}),
    )
    return calls


def _mock_mysql(monkeypatch, **fields):
    """Patch the mysql collector to return given metric fields."""
    data = {
        "server_name": "db-1",
        "cpu_percent_avg": 10.0,
        "memory_percent_avg": 20.0,
        "storage_percent": 30.0,
    }
    data.update(fields)
    monkeypatch.setitem(aac._COLLECTORS, "mysql", (lambda rg, name, range: data, "server_name"))


class TestEvaluation:
    def test_breach_fires(self, monkeypatch, sent):
        _mock_mysql(monkeypatch, storage_percent=92.0)
        summary = aac.run_azure_alert_check()
        assert summary["firing"] == 1
        assert len(sent) == 1
        msg = sent[0]["message"]
        assert "DatabaseHighStorage" in msg
        assert "proj-a" in msg and "db-1" in msg
        assert "92.0" in msg and "85.0" in msg
        assert "FIRING" in sent[0]["title"]
        assert sent[0]["title"].startswith("Azure")

    def test_no_breach_no_send(self, monkeypatch, sent):
        _mock_mysql(monkeypatch)
        summary = aac.run_azure_alert_check()
        assert summary["firing"] == 0
        assert sent == []

    def test_cooldown_suppresses_repeat(self, monkeypatch, sent):
        _mock_mysql(monkeypatch, storage_percent=92.0)
        aac.run_azure_alert_check()
        summary2 = aac.run_azure_alert_check()
        assert summary2["firing"] == 0
        assert summary2["suppressed"] == 1
        assert len(sent) == 1  # only the first cycle sent

    def test_refires_after_cooldown(self, monkeypatch, sent):
        _mock_mysql(monkeypatch, storage_percent=92.0)
        aac.run_azure_alert_check()
        # Age the state past the cooldown window
        for key in aac._alert_state:
            aac._alert_state[key]["last_sent"] = (
                datetime.now(timezone.utc) - timedelta(minutes=999)
            )
        summary2 = aac.run_azure_alert_check()
        assert summary2["firing"] == 1
        assert len(sent) == 2

    def test_resolved_sent_once(self, monkeypatch, sent):
        _mock_mysql(monkeypatch, storage_percent=92.0)
        aac.run_azure_alert_check()
        _mock_mysql(monkeypatch, storage_percent=40.0)
        summary2 = aac.run_azure_alert_check()
        assert summary2["resolved"] == 1
        assert len(sent) == 2
        assert "RESOLVED" in sent[1]["title"]
        assert "recovered" in sent[1]["message"]
        # Third cycle: nothing more
        summary3 = aac.run_azure_alert_check()
        assert summary3["resolved"] == 0 and summary3["firing"] == 0
        assert len(sent) == 2

    def test_null_metric_no_fire(self, monkeypatch, sent):
        _mock_mysql(monkeypatch, storage_percent=None, cpu_percent_avg=None)
        summary = aac.run_azure_alert_check()
        assert summary["firing"] == 0
        assert sent == []

    def test_collection_error_no_fire(self, monkeypatch, sent):
        def boom(rg, name, range):
            raise RuntimeError("azure down")

        monkeypatch.setitem(aac._COLLECTORS, "mysql", (boom, "server_name"))
        summary = aac.run_azure_alert_check()
        assert summary["errors"] == 1
        assert summary["firing"] == 0
        assert sent == []

    def test_teams_failure_does_not_raise(self, monkeypatch):
        _mock_mysql(monkeypatch, storage_percent=92.0)

        def fail(message, title=""):
            raise RuntimeError("teams down")

        monkeypatch.setattr(aac, "send_alert_to_teams", fail)
        summary = aac.run_azure_alert_check()  # must not raise
        assert summary["firing"] == 1


class TestEndpoint:
    URL = "/alerts/azure-check"
    AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}

    @pytest.fixture
    def client(self, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setenv("ALERT_WEBHOOK_TOKEN", TEST_TOKEN)
        from metrics_service.config import get_settings

        get_settings.cache_clear()
        from metrics_service.app import app

        yield TestClient(app)
        get_settings.cache_clear()

    def test_valid_token_returns_summary(self, client, monkeypatch):
        import metrics_service.app as app_module

        monkeypatch.setattr(
            app_module, "run_azure_alert_check",
            lambda: {"evaluated": 3, "firing": 1, "resolved": 0, "suppressed": 0, "errors": 0},
        )
        resp = client.post(self.URL, headers=self.AUTH)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert resp.json()["firing"] == 1

    def test_invalid_token_401(self, client):
        resp = client.post(self.URL, headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401

    def test_unconfigured_503(self, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.delenv("ALERT_WEBHOOK_TOKEN", raising=False)
        from metrics_service.config import get_settings

        get_settings.cache_clear()
        from metrics_service.app import app

        resp = TestClient(app).post(self.URL, headers=self.AUTH)
        get_settings.cache_clear()
        assert resp.status_code == 503


class TestNoLLM:
    def test_no_llm_in_poll_path(self):
        from metrics_service import azure_alert_trigger

        for module in (aac, azure_alert_trigger):
            assert "anthropic" not in inspect.getsource(module).lower()
