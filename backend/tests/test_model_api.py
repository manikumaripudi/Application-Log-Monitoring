import asyncio

import pytest

import main
from config.settings import settings


def test_stream_model_api_posts_model_and_prompt_and_yields_chunks(monkeypatch):
    calls = {}

    class FakeResponse:
        is_error = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield '{"model":"phi4-mini:3.8b","message":{"role":"assistant","content":"Analysis "},"done":false}'
            yield '{"model":"phi4-mini:3.8b","message":{"role":"assistant","content":"complete."},"done":false}'
            yield '{"model":"phi4-mini:3.8b","done":true,"done_reason":"stop"}'

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            calls["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, method, url, *, json):
            calls.update(method=method, url=url, json=json)
            return FakeResponse()

    monkeypatch.setattr(settings, "MODEL_API_URL", "https://model.example/chat")
    monkeypatch.setattr(settings, "MODEL_NAME", "phi4-mini:3.8b")
    monkeypatch.setattr(settings, "MODEL_API_TIMEOUT_SECONDS", 17)
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)

    async def collect():
        return [content async for content in main.stream_model_api("Analyze these logs.")]

    assert "".join(asyncio.run(collect())) == "Analysis complete."
    assert calls == {
        "method": "POST",
        "url": "https://model.example/chat",
        "json": {"model": "phi4-mini:3.8b", "prompt": "Analyze these logs."},
        "timeout": 17,
    }


def test_stream_model_api_omits_thinking_field_and_think_tags(monkeypatch):
    class FakeResponse:
        is_error = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield '{"message":{"thinking":"private reasoning","content":"<thi"},"done":false}'
            yield '{"message":{"thinking":"more reasoning","content":"nk>hidden</think>Visible "},"done":false}'
            yield '{"message":{"content":"answer."},"done":true}'

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(settings, "MODEL_API_URL", "https://model.example/chat")
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)

    async def collect():
        return "".join([content async for content in main.stream_model_api("Analyze these logs.")])

    assert asyncio.run(collect()) == "Visible answer."


def test_stream_model_api_rejects_incomplete_response(monkeypatch):
    class FakeResponse:
        is_error = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield '{"message":{"content":"partial"},"done":false}'

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(settings, "MODEL_API_URL", "https://model.example/chat")
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)

    async def collect():
        return [content async for content in main.stream_model_api("Analyze these logs.")]

    with pytest.raises(RuntimeError, match="ended before the model marked it complete"):
        asyncio.run(collect())


def test_stream_model_api_logs_upstream_error_detail(monkeypatch, caplog):
    class FakeResponse:
        is_error = True
        status_code = 500

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def aread(self):
            return b'{"detail":"model not found"}'

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(settings, "MODEL_API_URL", "https://model.example/chat")
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)

    async def collect():
        return [content async for content in main.stream_model_api("Analyze these logs.")]

    with pytest.raises(RuntimeError, match="HTTP 500: model not found"):
        asyncio.run(collect())
    assert "model=phi4-mini:3.8b, status=500): model not found" in caplog.text


def test_mock_logs_are_listed_from_backend_directory():
    result = asyncio.run(main.get_mock_files())

    assert {"app1.log", "backend.log", "db.log", "frontend.log"}.issubset(result["files"])


def test_mock_file_endpoint_returns_full_log_content():
    result = asyncio.run(main.get_mock_file("app1.log"))

    assert result["file"] == "app1.log"
    assert result["content"]


def test_mock_log_analysis_uses_configured_model_api(monkeypatch):
    prompts = []
    monkeypatch.setattr(settings, "MODEL_API_URL", "https://model.example/chat")
    monkeypatch.setattr(settings, "MODEL_NAME", "phi4-mini:3.8b")
    monkeypatch.setattr(main, "call_model_api", lambda prompt: prompts.append(prompt) or "Analysis complete.")
    monkeypatch.setattr(
        main,
        "run_agent_sync",
        lambda *args, **kwargs: pytest.fail("The configured model API should handle this request."),
    )

    result = asyncio.run(main.analyze_application(
        text="analyze:app1.log",
        severity_filter=None,
        keyword_filter=None,
        cloud_provider=None,
    ))

    assert result["response"] == "Analysis complete."
    assert result["file_name"] == "app1.log"
    assert result["analyzed_logs"]
    assert result["analyzed_logs"] in prompts[0]
