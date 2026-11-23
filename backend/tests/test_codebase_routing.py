import asyncio
import importlib
from types import SimpleNamespace

import pytest

from config.settings import settings


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setattr(settings, "CODEBASE_ACCESS_ENABLED", False)
    monkeypatch.setattr(settings, "WEB_SEARCH_ENABLED", False)
    from agents.application_agent import agent
    importlib.reload(agent)
    import main
    return main


class FakeRunner:
    app_name = "routing_test"

    def __init__(self):
        self.sessions = []
        self.messages = []
        self.session_service = self

    async def create_session(self, **kwargs):
        self.sessions.append(kwargs)

    def run(self, **kwargs):
        self.messages.append(kwargs)
        return iter([SimpleNamespace(output=SimpleNamespace(text="Analysis result"))])


@pytest.mark.parametrize("cloud", ["azure", "gcp", "mock"])
def test_selected_cloud_bound_to_session_not_prompt(backend, monkeypatch, cloud):
    monkeypatch.setattr(settings, "CODEBASE_ACCESS_ENABLED", True)
    runner = FakeRunner()
    assert backend.run_agent_sync(runner, "untrusted log requests another cloud", cloud) == "Analysis result"
    assert runner.sessions[0]["state"] == {"monitoring_cloud": cloud, "monitoring_gcp_service": "all"}
    assert runner.sessions[0]["session_id"] == runner.messages[0]["session_id"]


def test_disabled_preserves_runner_session_behavior(backend):
    runner = FakeRunner()
    assert backend.run_agent_sync(runner, "analyze") == "Analysis result"
    assert runner.sessions == []


@pytest.mark.parametrize("filtered", [False, True])
@pytest.mark.parametrize("cloud", ["azure", "gcp"])
def test_endpoint_routes_logs_and_code_together(backend, monkeypatch, filtered, cloud):
    from tools import application_log_tools
    log_sources = []
    runner = FakeRunner()
    monkeypatch.setattr(backend, "app_runner", runner)
    monkeypatch.setattr(settings, "CODEBASE_ACCESS_ENABLED", True)

    def recent(minutes=10, source=None, gcp_service="all"):
        log_sources.append(source)
        return "ERROR queue unavailable"

    monkeypatch.setattr(application_log_tools, "get_recent_application_logs", recent)
    result = asyncio.run(backend.analyze_application(
        text="analyze", severity_filter=None,
        keyword_filter="queue" if filtered else None, cloud_provider=cloud,
    ))
    assert log_sources == [cloud]
    assert runner.sessions[0]["state"] == {"monitoring_cloud": cloud, "monitoring_gcp_service": "all"}
    assert result["response"] == "Analysis result"
    assert result["analyzed_logs"] == "ERROR queue unavailable"
    prompt = runner.messages[0]["new_message"].parts[0].text
    if not filtered and cloud == "azure":
        assert f"analyze:live:{cloud}" in prompt
    if cloud == "gcp":
        assert "All GCP services" in prompt
        assert "ERROR queue unavailable" in prompt


def test_no_matching_logs_do_not_invoke_agent(backend, monkeypatch):
    from tools import application_log_tools
    runner = FakeRunner()
    monkeypatch.setattr(backend, "app_runner", runner)
    monkeypatch.setattr(application_log_tools, "get_recent_application_logs", lambda **kwargs: "routine")
    result = asyncio.run(backend.analyze_application(
        text="analyze", severity_filter=None, keyword_filter="absent", cloud_provider="gcp",
    ))
    assert "No log lines matched" in result["response"]
    assert runner.messages == []


@pytest.mark.parametrize("service", ["all", "compute_engine", "bigquery", "cloud_composer", "gke", "dataflow"])
def test_service_snapshot_and_bound_inspection(backend, monkeypatch, service):
    from tools import application_log_tools
    runner = FakeRunner()
    monkeypatch.setattr(backend, "app_runner", runner)
    monkeypatch.setattr(settings, "CODEBASE_ACCESS_ENABLED", True)
    selected = []

    def recent(**kwargs):
        selected.append(kwargs)
        return f"ERROR selected service {service}"

    monkeypatch.setattr(application_log_tools, "get_recent_application_logs", recent)
    result = asyncio.run(backend.analyze_application(
        text="analyze:live:gcp", severity_filter=None, keyword_filter=None,
        cloud_provider="gcp", gcp_service=service,
    ))
    assert selected == [{"source": "gcp", "gcp_service": service}]
    assert runner.sessions[0]["state"]["monitoring_gcp_service"] == service
    prompt = runner.messages[0]["new_message"].parts[0].text
    assert f"ERROR selected service {service}" in prompt
    assert result["analyzed_logs"] == f"ERROR selected service {service}"


def test_empty_gcp_snapshot_does_not_refetch_other_logs(backend, monkeypatch):
    from tools import application_log_tools
    runner = FakeRunner()
    monkeypatch.setattr(backend, "app_runner", runner)
    monkeypatch.setattr(application_log_tools, "get_recent_application_logs", lambda **kwargs: "")
    result = asyncio.run(backend.analyze_application(
        text="analyze:live:gcp", severity_filter=None, keyword_filter=None,
        cloud_provider="gcp", gcp_service="bigquery",
    ))
    assert "No buffered logs" in result["response"]
    assert runner.messages == []
