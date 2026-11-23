import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.settings import settings
from services.gcp_services import GCP_SERVICES, build_log_filter, classify_entry
from services.gcp_log_stream import GcpLogStreamService
from api.log_stream import router
from google.cloud.audit.audit_log_pb2 import AuditLog
from google.cloud.logging_v2.types import LogEntry
from google.protobuf.any_pb2 import Any


def entry(resource_type="gce_instance", service_name="", text="ERROR failure"):
    return SimpleNamespace(
        resource=SimpleNamespace(type=resource_type, labels={"project_id": "test-project"}),
        text_payload=text, json_payload={},
        proto_payload={"serviceName": service_name} if service_name else {},
        timestamp=None, severity=SimpleNamespace(name="ERROR"),
    )


@pytest.mark.parametrize("service", GCP_SERVICES)
def test_resource_classification(service):
    for resource in GCP_SERVICES[service][1]:
        assert classify_entry(entry(resource)) == service


@pytest.mark.parametrize("api,service", [
    ("bigquery.googleapis.com", "bigquery"),
    ("composer.googleapis.com", "cloud_composer"),
    ("container.googleapis.com", "gke"),
    ("dataflow.googleapis.com", "dataflow"),
    ("compute.googleapis.com", "compute_engine"),
])
def test_audit_classification(api, service):
    assert classify_entry(entry("audited_resource", api)) == service
    assert classify_entry(entry("unsupported")) is None


def test_real_protobuf_audit_payload():
    payload = Any()
    payload.Pack(AuditLog(service_name="bigquery.googleapis.com", method_name="JobService.InsertJob"))
    log = LogEntry(resource={"type": "audited_resource"}, proto_payload=payload)
    assert classify_entry(log) == "bigquery"
    formatted = GcpLogStreamService()._format_entry(log)
    assert "JobService.InsertJob" in formatted
    assert "BigQuery" in formatted
    structured = LogEntry(
        resource={"type": "dataflow_step"},
        json_payload={"message": "ERROR failed", "context": {"job": "job-1"}},
    )
    assert '"job": "job-1"' in GcpLogStreamService()._format_entry(structured)


def test_union_filter_and_vm_scope():
    combined = build_log_filter(instance_id="123")
    for _, resources in GCP_SERVICES.values():
        assert all(f'resource.type="{resource}"' in combined for resource in resources)
    assert combined.count('resource.labels.instance_id="123"') == 1
    assert 'protoPayload.serviceName="bigquery.googleapis.com"' in combined
    assert build_log_filter('resource.type="gce_instance"') == build_log_filter()
    assert build_log_filter("severity>=ERROR").endswith("AND (severity>=ERROR)")
    assert r'instance_id="a\"b"' in build_log_filter(instance_id='a"b')


def test_isolated_buffers_listeners_and_status(monkeypatch):
    monkeypatch.setattr(settings, "LOG_BUFFER_SIZE", 2)
    service = GcpLogStreamService()

    async def exercise():
        all_logs, bigquery, gke = asyncio.Queue(), asyncio.Queue(), asyncio.Queue()
        service.add_listener(all_logs)
        service.add_listener(bigquery, "bigquery")
        service.add_listener(gke, "gke")
        await service._process_log_line("GKE one", "gke")
        for n in range(3):
            await service._process_log_line(f"BQ {n}", "bigquery")
        assert list(service.buffer) == ["BQ 1", "BQ 2"]
        assert service.get_buffered_logs("gke") == "GKE one"
        assert service.get_buffered_logs("bigquery") == "BQ 1\nBQ 2"
        assert gke.qsize() == 1 and bigquery.qsize() == 3 and all_logs.qsize() == 4
        assert service.get_status("gke")["buffer_size"] == 1
        assert service.get_status("dataflow")["last_log_received_at"] is None
        service.remove_listener(bigquery)
        await service._process_log_line("next", "bigquery")
        assert bigquery.qsize() == 3
    asyncio.run(exercise())
    with pytest.raises(ValueError):
        service.get_buffered_logs("invalid")


def test_format_preserves_service_severity_and_payload():
    service = GcpLogStreamService()
    log = entry("dataflow_step", text="first\nsecond")
    log.resource.labels["job_id"] = "job-1"
    line = service._format_entry(log)
    assert "Dataflow" in line and "job_id=job-1" in line and "ERROR" in line
    assert "\n" not in line and r"first\nsecond" in line
    audit = entry("audited_resource", "bigquery.googleapis.com", "")
    assert "bigquery.googleapis.com" in service._format_entry(audit)


def test_api_selection_and_validation(monkeypatch):
    from api import log_stream
    service = GcpLogStreamService()
    asyncio.run(service._process_log_line("query failed", "bigquery"))
    monkeypatch.setattr(log_stream, "gcp_log_stream", service)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get("/status?source=gcp&gcp_service=bigquery")
        assert response.status_code == 200
        assert response.json()["service"] == "bigquery"
        assert response.json()["buffer_size"] == 1
        assert client.get("/status?source=gcp&gcp_service=gke").json()["buffer_size"] == 0
        assert client.get("/status?source=gcp&gcp_service=invalid").status_code == 422
        assert client.get("/stream?source=gcp&gcp_service=invalid").status_code == 422
        assert client.get("/status?source=azure").json()["mode"] == settings.LOG_SOURCE


def test_codebase_managed_service_guard(monkeypatch):
    from tools.codebase_tools import _access
    monkeypatch.setattr(settings, "CODEBASE_ACCESS_ENABLED", True)
    for service in ("all", "bigquery", "cloud_composer", "gke", "dataflow"):
        context = SimpleNamespace(state={"monitoring_cloud": "gcp", "monitoring_gcp_service": service})
        cloud, failure = _access(context)
        assert cloud is None and failure["status"] == "unavailable"
    context = SimpleNamespace(state={"monitoring_cloud": "gcp", "monitoring_gcp_service": "compute_engine"})
    assert _access(context) == ("gcp", None)


def test_stream_filters_history_and_new_events(monkeypatch):
    from api import log_stream
    service = GcpLogStreamService()
    monkeypatch.setattr(log_stream, "gcp_log_stream", service)

    async def disconnected():
        return False

    async def exercise():
        await service._process_log_line("BQ history", "bigquery")
        await service._process_log_line("GKE history", "gke")
        response = await log_stream.stream_logs(
            SimpleNamespace(is_disconnected=disconnected), source="gcp", gcp_service="bigquery",
        )
        iterator = response.body_iterator
        assert await anext(iterator) == "data: BQ history\n\n"
        await service._process_log_line("GKE new", "gke")
        await service._process_log_line("BQ new", "bigquery")
        assert await anext(iterator) == "data: BQ new\n\n"
        await iterator.aclose()
        assert service.listeners == []
    asyncio.run(exercise())


def test_startup_does_not_couple_provider_failures(monkeypatch):
    from services.log_stream_manager import LogStreamManager
    monkeypatch.setattr(settings, "GCP_PROJECT_ID", "test-project")
    monkeypatch.setattr(settings, "LOG_SOURCE", "azure")
    calls = []

    async def azure_start():
        calls.append("azure")

    async def gcp_start():
        calls.append("gcp")
        raise RuntimeError("GCP unavailable")

    manager = LogStreamManager()
    manager.azure = SimpleNamespace(start=azure_start)
    manager.gcp = SimpleNamespace(start=gcp_start)
    asyncio.run(manager.start_all())
    assert calls == ["azure", "gcp"]
    calls.clear()
    monkeypatch.setattr(settings, "GCP_PROJECT_ID", None)
    asyncio.run(manager.start_all())
    assert calls == ["azure"]
