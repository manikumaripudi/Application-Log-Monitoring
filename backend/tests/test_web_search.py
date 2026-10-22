import asyncio
import importlib
from types import SimpleNamespace

import pytest
from google.adk.events import Event
from google.adk.tools import FunctionTool
from google.genai import types, errors

from tools import web_search as search


@pytest.fixture
def setup_search(monkeypatch):
    monkeypatch.setattr(search.settings, "WEB_SEARCH_ENABLED", True)
    monkeypatch.setattr(search.settings, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(search.settings, "GOOGLE_API_KEY", None)
    calls = []

    async def research(prompt):
        calls.append(prompt)
        return {"status": "ok", "research": "Possible causes", "results": []}

    monkeypatch.setattr(search, "_grounded_research", research)
    context = SimpleNamespace(state={})

    def run(product="azure_app_service", pattern="startup_failure"):
        return asyncio.run(search.web_search(product, pattern, context))

    return SimpleNamespace(run=run, calls=calls, context=context)


def test_public_query_and_budget(setup_search):
    for _ in range(2):
        assert setup_search.run()["status"] == "ok"
    assert setup_search.run()["status"] == "limit_reached"
    assert setup_search.calls == [
        "Azure App Service startup failure troubleshooting documentation (site:learn.microsoft.com)"
    ] * 2
    fresh = SimpleNamespace(state={})
    assert asyncio.run(search.web_search("azure_app_service", "startup_failure", fresh))["status"] == "ok"


@pytest.mark.parametrize("product,pattern", [
    ("private-host.internal", "startup_failure"),
    ("azure_app_service", "private token"),
    ("azure_app_service", "http_502 raw private log"),
])
def test_unsupported_input_never_sent(setup_search, product, pattern):
    assert setup_search.run(product, pattern)["status"] == "blocked"
    assert not setup_search.calls


def test_configuration(setup_search, monkeypatch):
    monkeypatch.setattr(search.settings, "WEB_SEARCH_ENABLED", False)
    assert search.get_web_search_tools() == []
    assert setup_search.run()["status"] == "disabled"
    monkeypatch.setattr(search.settings, "WEB_SEARCH_ENABLED", True)
    monkeypatch.setattr(search.settings, "GEMINI_API_KEY", None)
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        search.get_web_search_tools()
    assert setup_search.run()["status"] == "unavailable"
    monkeypatch.setattr(search.settings, "GOOGLE_API_KEY", "test-google-key")
    assert search.get_web_search_tools() == [search.web_search]
    monkeypatch.setattr(search.settings, "WEB_SEARCH_MODEL", "azure/model")
    with pytest.raises(ValueError, match="Gemini"):
        search.get_web_search_tools()
    assert not setup_search.calls


@pytest.mark.parametrize("error", [TimeoutError("private"), errors.ClientError(403, {})])
def test_research_failure_is_explicit(setup_search, monkeypatch, caplog, error):
    async def fail(prompt):
        raise error
    monkeypatch.setattr(search, "_grounded_research", fail)
    result = setup_search.run()
    assert result["status"] == "error"
    assert "private" not in str(result) + caplog.text
    assert setup_search.context.state["web_search_count"] == 1


def test_schema():
    declaration = FunctionTool(search.web_search)._get_declaration()
    assert declaration.name == "web_search"
    assert set(declaration.parameters.properties) == {"product", "error_pattern"}
    assert set(declaration.parameters.properties["product"].enum) == set(search.PRODUCTS)
    assert set(declaration.parameters.properties["error_pattern"].enum) == set(search.PATTERNS)


@pytest.mark.parametrize("grounded", [False, True])
def test_isolated_research_and_metadata(monkeypatch, grounded):
    monkeypatch.setattr(search.settings, "GEMINI_API_KEY", "test-key")
    captured = {}
    closed = []

    class Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.aio = self

        async def aclose(self):
            closed.append("async")

        def close(self):
            closed.append("sync")

    class Runner:
        def __init__(self, **kwargs):
            captured["runner"] = kwargs

        async def run_async(self, **kwargs):
            captured["invocation"] = kwargs
            yield Event(
                author="web_research_agent",
                content=types.Content(parts=[types.Part(text="Possible startup causes.")]),
                grounding_metadata=types.GroundingMetadata(
                    web_search_queries=["Azure App Service startup failure"] if grounded else [],
                    grounding_chunks=[types.GroundingChunk(
                        web=types.GroundingChunkWeb(
                            uri="https://learn.microsoft.com/example", title="Documentation",
                        )
                    )] if grounded else [],
                ),
            )

    monkeypatch.setattr(search.genai, "Client", Client)
    monkeypatch.setattr(search, "Runner", Runner)
    # Gemini validates that client is a real SDK client; use a factory spy here.
    monkeypatch.setattr(search, "Gemini", lambda **kwargs: captured.update(model=kwargs) or "gemini-2.5-flash")
    result = asyncio.run(search._grounded_research("public query"))
    assert result["status"] == ("ok" if grounded else "no_results")
    assert captured["client"]["vertexai"] is False
    assert captured["client"]["api_key"] == "test-key"
    assert captured["runner"]["agent"].tools == [search.google_search]
    assert captured["invocation"]["new_message"].parts[0].text == "public query"
    assert closed == ["async", "sync"]
    if grounded:
        assert result["results"][0]["url"] == "https://learn.microsoft.com/example"
        assert result["research"] == "Possible startup causes."


@pytest.mark.parametrize("enabled", [False, True])
def test_existing_agent_tools_preserved(setup_search, monkeypatch, enabled):
    monkeypatch.setattr(search.settings, "CODEBASE_ACCESS_ENABLED", False)
    monkeypatch.setattr(search.settings, "WEB_SEARCH_ENABLED", enabled)
    from agents.application_agent import agent
    agent = importlib.reload(agent)
    names = [tool.__name__ for tool in agent.root_agent.tools]
    assert names[:4] == [
        "get_recent_application_logs", "analyze_application_logs",
        "detect_log_severity", "send_critical_alert",
    ]
    assert ("web_search" in names) is enabled
    assert ("Web research:" in agent.root_agent.instruction) is enabled
    for field in ("Severity", "Issue Type", "Log Entry", "Remediation"):
        assert f"**{field}:**" in agent.root_agent.instruction
