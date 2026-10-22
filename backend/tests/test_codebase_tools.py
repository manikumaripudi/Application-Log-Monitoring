from contextlib import contextmanager
import importlib
from types import SimpleNamespace

import pytest
from google.adk.tools import FunctionTool

from services.codebase_sources import CodebaseAccessError, SourceEntry
from tools import codebase_tools as code
from tools import web_search as search


@pytest.fixture
def repository(monkeypatch):
    files = {"worker.py": b"def process_job():\n    raise RuntimeError('queue unavailable')\n"}
    calls = []

    class Source:
        def list_directory(self, path):
            calls.append(("list", path))
            return [SourceEntry(name, False, len(content)) for name, content in files.items()]

        def read_file(self, path):
            calls.append(("read", path))
            return files[path]

    @contextmanager
    def open_source(cloud):
        calls.append(("cloud", cloud))
        yield Source()

    monkeypatch.setattr(code, "open_source", open_source)
    monkeypatch.setattr(code.settings, "CODEBASE_ACCESS_ENABLED", True)
    return files, calls, SimpleNamespace(state={"monitoring_cloud": "azure"})


@pytest.mark.parametrize("cloud", ["azure", "gcp"])
def test_cloud_search_and_numbered_read(repository, cloud):
    files, calls, context = repository
    context.state["monitoring_cloud"] = cloud
    original = dict(files)
    result = code.scan_codebase("queue unavailable", context)
    assert result["status"] == "ok"
    assert result["matches"] == [{
        "path": "worker.py", "line_number": 2,
        "text": "    raise RuntimeError('queue unavailable')", "line_truncated": False,
    }]
    excerpt = code.read_codebase_file("worker.py", 1, context, 2)
    assert excerpt["total_lines"] == 2
    assert [line["line_number"] for line in excerpt["lines"]] == [1, 2]
    assert ("cloud", cloud) in calls
    assert files == original
    assert code.scan_codebase(".*queue", context)["status"] == "no_matches"


@pytest.mark.parametrize("path", [
    "../outside.py", "/outside.py", "C:/outside.py", "C:outside.py",
    "\\\\server\\share\\outside.py", "worker.py:stream",
    ".git/config.py", ".env", "settings.py", "secret.py", "credentials.py",
    "%2e%2e/worker.py", "a//worker.py",
])
def test_paths_blocked_before_network(repository, path):
    _, calls, context = repository
    assert code.read_codebase_file(path, 1, context)["status"] == "unavailable"
    assert calls == []


def test_exclusions_and_binary_failure_explicit(repository):
    files, _, context = repository
    for filename in ("settings.py", "secret.py", ".env", "data.json"):
        files[filename] = b"needle"
    assert code.scan_codebase("needle", context)["status"] == "no_matches"
    files["binary.py"] = b"needle\x00"
    result = code.scan_codebase("needle", context)
    assert result["status"] == "unavailable"
    assert result["truncated"]


def test_budget_shared(repository):
    _, _, context = repository
    for _ in range(code.MAX_CALLS // 2):
        assert code.scan_codebase("queue", context)["status"] == "ok"
        assert code.read_codebase_file("worker.py", 1, context)["status"] == "ok"
    assert code.scan_codebase("queue", context)["status"] == "limit_reached"
    assert code.read_codebase_file("worker.py", 1, context)["status"] == "limit_reached"


def test_validation_and_limits(repository, monkeypatch):
    files, _, context = repository
    assert code.scan_codebase("x", context)["status"] == "invalid_input"
    assert code.read_codebase_file("worker.py", 0, context)["status"] == "invalid_input"
    assert code.read_codebase_file("worker.py", 1, context, 81)["status"] == "invalid_input"
    assert code.read_codebase_file("worker.py", 100, context)["status"] == "invalid_input"
    files["long.py"] = b"needle" * 1000
    excerpt = code.read_codebase_file("long.py", 1, context)
    assert excerpt["truncated"]
    assert len(excerpt["lines"][0]["text"]) == 400
    monkeypatch.setattr(code, "MAX_MATCHES", 1)
    assert code.scan_codebase("queue", context)["truncated"]


def test_scan_limits(repository, monkeypatch):
    _, _, context = repository
    monkeypatch.setattr(code, "MAX_ENTRIES", 0)
    assert code.scan_codebase("queue", context)["truncated"]
    monkeypatch.setattr(code, "MAX_ENTRIES", 100)
    monkeypatch.setattr(code, "MAX_SCAN_BYTES", 1)
    assert code.scan_codebase("queue", context)["truncated"]
    monkeypatch.setattr(code, "MAX_FILE_BYTES", 1)
    assert code.scan_codebase("queue", context)["files_skipped"] == 1
    assert code.read_codebase_file("worker.py", 1, context)["status"] == "unavailable"


def test_no_model_selected_cloud_or_mock_fallback(repository):
    _, calls, context = repository
    for cloud in (None, "mock", "unknown"):
        context.state = {"monitoring_cloud": cloud}
        assert code.scan_codebase("queue", context)["status"] == "unavailable"
    assert not calls


def test_access_failure_is_explicit(repository, monkeypatch):
    _, _, context = repository

    @contextmanager
    def fail(cloud):
        raise CodebaseAccessError("Access unavailable.")
        yield

    monkeypatch.setattr(code, "open_source", fail)
    assert code.scan_codebase("queue", context)["status"] == "unavailable"
    assert code.read_codebase_file("worker.py", 1, context)["status"] == "unavailable"


def test_disabled(repository, monkeypatch):
    _, calls, context = repository
    monkeypatch.setattr(code.settings, "CODEBASE_ACCESS_ENABLED", False)
    assert code.get_codebase_tools() == []
    assert code.scan_codebase("queue", context)["status"] == "disabled"
    assert not calls


@pytest.mark.parametrize("web_enabled", [False, True])
@pytest.mark.parametrize("code_enabled", [False, True])
def test_optional_agent_tools_independent(repository, monkeypatch, web_enabled, code_enabled):
    monkeypatch.setattr(code.settings, "CODEBASE_ACCESS_ENABLED", code_enabled)
    monkeypatch.setattr(search.settings, "WEB_SEARCH_ENABLED", web_enabled)
    monkeypatch.setattr(search.settings, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(search.settings, "WEB_SEARCH_MODEL", "gemini-2.5-flash")
    from agents.application_agent import agent

    agent = importlib.reload(agent)
    names = [tool.__name__ for tool in agent.root_agent.tools]
    assert names[:4] == [
        "get_recent_application_logs", "analyze_application_logs",
        "detect_log_severity", "send_critical_alert",
    ]
    assert ("web_search" in names) is web_enabled
    assert ("scan_codebase" in names) is code_enabled
    assert ("read_codebase_file" in names) is code_enabled
    assert (code.CODEBASE_INSTRUCTION in agent.root_agent.instruction) is code_enabled
    assert (search.WEB_SEARCH_INSTRUCTION in agent.root_agent.instruction) is web_enabled


def test_adk_tool_schemas():
    for tool, fields in (
        (code.scan_codebase, {"query"}),
        (code.read_codebase_file, {"relative_path", "start_line", "end_line"}),
    ):
        declaration = FunctionTool(tool)._get_declaration()
        assert set(declaration.parameters.properties) == fields
