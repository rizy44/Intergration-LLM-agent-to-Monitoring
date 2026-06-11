"""
Tests for the Alertmanager → Teams alert path.

Covers:
- alert_formatter: firing/resolved, severity icons, grouping, missing
  annotations, malformed payloads, block truncation.
- POST /alerts/alertmanager: auth (401), fail-closed (503), malformed body
  (422), empty alerts (200 no-send), happy path (200 + Teams send).
- No-LLM guarantees: no Anthropic usage in alert or daily-report paths.
"""

import importlib

import pytest
from fastapi.testclient import TestClient

from metrics_service import alert_formatter
from metrics_service.alert_formatter import (
    AlertPayloadError,
    format_alertmanager_payload,
)

TEST_TOKEN = "test-alert-token"


def _payload(status="firing", alerts=None):
    if alerts is None:
        alerts = [_alert()]
    return {"version": "4", "status": status, "alerts": alerts}


def _alert(name="NodeHighCPU", severity="warning", **labels):
    base_labels = {"alertname": name, "severity": severity}
    base_labels.update(labels)
    return {
        "status": "firing",
        "labels": base_labels,
        "annotations": {"summary": f"{name} threshold breached."},
        "startsAt": "2026-06-11T07:00:00Z",
    }


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------


class TestAlertFormatter:
    def test_firing_critical(self):
        title, body = format_alertmanager_payload(
            _payload(alerts=[_alert(severity="critical", node="aks-node-1")])
        )
        assert title == "🔴 ALERT FIRING (1)"
        assert "🔴 **NodeHighCPU** (critical)" in body
        assert "node: aks-node-1" in body
        assert "2026-06-11 07:00 UTC" in body

    def test_resolved_title(self):
        title, _ = format_alertmanager_payload(_payload(status="resolved"))
        assert title == "✅ ALERT RESOLVED (1)"

    def test_warning_and_unknown_severity_icons(self):
        _, body = format_alertmanager_payload(
            _payload(alerts=[_alert(severity="warning"), _alert(name="Odd", severity="info")])
        )
        assert "🟠 **NodeHighCPU** (warning)" in body
        assert "⚪ **Odd** (info)" in body

    def test_grouped_alerts_single_message(self):
        title, body = format_alertmanager_payload(
            _payload(alerts=[_alert(name="A"), _alert(name="B"), _alert(name="C")])
        )
        assert "(3)" in title
        for name in ("A", "B", "C"):
            assert f"**{name}**" in body
        # Blocks joined with double newlines for Teams rendering
        assert "\n\n" in body

    def test_missing_annotations_and_severity(self):
        alert = {"status": "firing", "labels": {"alertname": "Bare"}, "startsAt": ""}
        _, body = format_alertmanager_payload(_payload(alerts=[alert]))
        assert "⚪ **Bare** (unspecified)" in body

    def test_block_truncation(self):
        alert = _alert()
        alert["annotations"]["summary"] = "x" * 5000
        _, body = format_alertmanager_payload(_payload(alerts=[alert]))
        assert len(body) < 1000
        assert body.endswith("...")

    def test_empty_alerts_empty_body(self):
        _, body = format_alertmanager_payload(_payload(alerts=[]))
        assert body == ""

    @pytest.mark.parametrize(
        "bad",
        [
            "not-a-dict",
            {},
            {"status": "firing"},
            {"status": "firing", "alerts": "nope"},
            {"status": "firing", "alerts": [{"labels": {}}]},  # missing status
        ],
    )
    def test_malformed_payload_raises(self, bad):
        with pytest.raises(AlertPayloadError):
            format_alertmanager_payload(bad)

    def test_no_llm_in_formatter_module(self):
        source = importlib.import_module("metrics_service.alert_formatter").__file__
        with open(source, encoding="utf-8") as fh:
            text = fh.read().lower()
        assert "anthropic" not in text


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("TEAMS_ALERT_WEBHOOK_URL", "https://example.invalid/webhook")
    from metrics_service.config import get_settings

    get_settings.cache_clear()
    from metrics_service.app import app

    yield TestClient(app)
    get_settings.cache_clear()


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake_send(message, title="Monitoring Alert"):
        calls.append({"message": message, "title": title})

    import metrics_service.app as app_module

    monkeypatch.setattr(app_module, "send_alert_to_teams", fake_send)
    return calls


class TestAlertEndpoint:
    URL = "/alerts/alertmanager"
    AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}

    def test_valid_token_sends_to_teams(self, client, sent):
        resp = client.post(self.URL, json=_payload(), headers=self.AUTH)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "sent": True, "alerts": 1}
        assert len(sent) == 1
        assert "NodeHighCPU" in sent[0]["message"]
        assert sent[0]["title"] == "🔴 ALERT FIRING (1)"

    def test_missing_token_401(self, client, sent):
        resp = client.post(self.URL, json=_payload())
        assert resp.status_code == 401
        assert sent == []

    def test_wrong_token_401(self, client, sent):
        resp = client.post(
            self.URL, json=_payload(), headers={"Authorization": "Bearer wrong"}
        )
        assert resp.status_code == 401
        assert sent == []

    def test_unconfigured_token_503(self, monkeypatch, sent):
        monkeypatch.delenv("ALERT_WEBHOOK_TOKEN", raising=False)
        from metrics_service.config import get_settings

        get_settings.cache_clear()
        from metrics_service.app import app

        resp = TestClient(app).post(self.URL, json=_payload(), headers=self.AUTH)
        get_settings.cache_clear()
        assert resp.status_code == 503
        assert sent == []

    def test_malformed_body_422(self, client, sent):
        resp = client.post(
            self.URL,
            content=b"not json",
            headers={**self.AUTH, "Content-Type": "application/json"},
        )
        assert resp.status_code == 422
        assert sent == []

    def test_missing_alerts_array_422(self, client, sent):
        resp = client.post(self.URL, json={"status": "firing"}, headers=self.AUTH)
        assert resp.status_code == 422
        assert sent == []
        # Safe error: no payload echo
        assert "firing" not in resp.text

    def test_empty_alerts_200_no_send(self, client, sent):
        resp = client.post(self.URL, json=_payload(alerts=[]), headers=self.AUTH)
        assert resp.status_code == 200
        assert resp.json()["sent"] is False
        assert sent == []

    def test_teams_failure_502(self, client, monkeypatch):
        import metrics_service.app as app_module

        def boom(message, title=""):
            raise RuntimeError("delivery failed")

        monkeypatch.setattr(app_module, "send_alert_to_teams", boom)
        resp = client.post(self.URL, json=_payload(), headers=self.AUTH)
        assert resp.status_code == 502


# ---------------------------------------------------------------------------
# No-LLM guarantees
# ---------------------------------------------------------------------------


class TestNoLLM:
    def test_daily_report_path_has_no_llm(self):
        import inspect

        from metrics_service import daily_report, report_formatter

        for module in (daily_report, report_formatter):
            assert "anthropic" not in inspect.getsource(module).lower()

    def test_dead_llm_report_generator_removed(self):
        from metrics_service import ai_agent

        assert not hasattr(ai_agent, "generate_daily_report_text")
