import asyncio
import logging
import json
from collections import deque
from typing import Dict, Any, Optional
import datetime
import os
from google.cloud.logging_v2.types import LogEntry
from google.protobuf.json_format import MessageToDict

from config.settings import settings
from services.gcp_services import (
    GCP_SERVICES, audit_payload, build_log_filter, classify_entry, service_label, validate_service,
)

logger = logging.getLogger(__name__)

class GcpLogStreamService:
    def __init__(self):
        self.buffer = deque(maxlen=settings.LOG_BUFFER_SIZE)
        self.service_buffers = {
            key: deque(maxlen=settings.LOG_BUFFER_SIZE) for key in GCP_SERVICES
        }
        self.service_last_received = {key: None for key in GCP_SERVICES}
        self.last_error: Optional[str] = None
        self.connected = False
        self.last_log_received_at: Optional[datetime.datetime] = None
        self._streaming_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self.listeners = []

    def add_listener(self, queue: asyncio.Queue, service: str = "all"):
        validate_service(service)
        self.listeners.append((queue, service))

    def remove_listener(self, queue: asyncio.Queue):
        self.listeners[:] = [(q, service) for q, service in self.listeners if q is not queue]

    async def broadcast(self, message: str, service: str = "compute_engine"):
        for queue, selected in self.listeners:
            if selected not in ("all", service):
                continue
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass

    def get_status(self, service: str = "all") -> Dict[str, Any]:
        validate_service(service)
        last_received = self.last_log_received_at if service == "all" else self.service_last_received[service]
        buffer = self.buffer if service == "all" else self.service_buffers[service]
        return {
            "connected": self.connected,
            "provider": "gcp",
            "app_service": service_label(service) if settings.GCP_PROJECT_ID else "GCP (Not Configured)",
            "project_id": settings.GCP_PROJECT_ID or "None",
            "instance_id": settings.GCP_INSTANCE_ID or "all",
            "service": service,
            "resource_type": "gce_instance" if service == "compute_engine" else "multiple",
            "resource_types": [
                resource for key, (_, resources) in GCP_SERVICES.items()
                if service in ("all", key) for resource in resources
            ],
            "available_services": [{"id": key, "label": service_label(key)} for key in ("all", *GCP_SERVICES)],
            "last_error": self.last_error,
            "last_log_received_at": last_received.isoformat() if last_received else None,
            "buffer_size": len(buffer),
            "mode": "gcp" if settings.GCP_PROJECT_ID else "unconfigured",
        }

    def get_buffered_logs(self, service: str = "all") -> str:
        validate_service(service)
        return "\n".join(self.buffer if service == "all" else self.service_buffers[service])

    async def start(self):
        if settings.LOG_SOURCE == "mock":
            return
        if self._streaming_task and not self._streaming_task.done():
            return
        self._stop_event.clear()
        self._streaming_task = asyncio.create_task(self._run_stream())
        logger.info("Started multi-service GCP log stream background task.")

    async def stop(self):
        self._stop_event.set()
        if self._streaming_task:
            self._streaming_task.cancel()
            try:
                await self._streaming_task
            except asyncio.CancelledError:
                pass
        self.connected = False

    async def _run_stream(self):
        while not self._stop_event.is_set():
            try:
                await self._connect_and_stream()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.connected = False
                self.last_error = "GCP log stream failed; check credentials, permissions, filter and server logs."
                logger.error(f"GCP log stream error: {e}")
                logger.info(f"Reconnecting to GCP in {settings.LOG_STREAM_RETRY_SECONDS}s...")
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=settings.LOG_STREAM_RETRY_SECONDS)
                except asyncio.TimeoutError:
                    pass

    async def _connect_and_stream(self):
        if not settings.GCP_PROJECT_ID:
            logger.info("GCP_PROJECT_ID is not set in .env. Waiting for GCP configuration...")
            await asyncio.sleep(10)
            return

        # Ensure credentials path is set if provided in settings
        if settings.GOOGLE_APPLICATION_CREDENTIALS and os.path.exists(settings.GOOGLE_APPLICATION_CREDENTIALS):
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = settings.GOOGLE_APPLICATION_CREDENTIALS

        try:
            from google.cloud.logging_v2.services.logging_service_v2 import LoggingServiceV2AsyncClient
            from google.cloud.logging_v2.types import TailLogEntriesRequest
        except ImportError as e:
            logger.error(f"google-cloud-logging not installed properly: {e}")
            await asyncio.sleep(10)
            return

        logger.info(f"Connecting to GCP Cloud Logging for project {settings.GCP_PROJECT_ID}...")

        filter_expr = build_log_filter(settings.GCP_LOG_FILTER, settings.GCP_INSTANCE_ID or "")

        client = LoggingServiceV2AsyncClient()

        async def request_generator():
            yield TailLogEntriesRequest(
                resource_names=[f"projects/{settings.GCP_PROJECT_ID}"],
                filter=filter_expr,
                buffer_window={"seconds": 2}
            )
            while not self._stop_event.is_set():
                await asyncio.sleep(2)

        try:
            stream = await client.tail_log_entries(requests=request_generator())
            logger.info(f"Connected to GCP Cloud Logging stream ✓ (Filter: {filter_expr})")
            self.connected = True
            self.last_error = None

            async for response in stream:
                if self._stop_event.is_set():
                    break
                for entry in response.entries:
                    service = classify_entry(entry)
                    if service:
                        line = self._format_entry(entry, service)
                        if line:
                            await self._process_log_line(line, service)
        finally:
            self.connected = False
            await client.transport.close()

    def _format_entry(self, entry, service: Optional[str] = None) -> Optional[str]:
        text = entry.text_payload
        if not text and entry.json_payload:
            payload = (
                MessageToDict(LogEntry.pb(entry).json_payload)
                if isinstance(entry, LogEntry) else dict(entry.json_payload)
            )
            text = json.dumps(payload)
        elif not text and entry.proto_payload:
            audit = audit_payload(entry)
            text = json.dumps(audit) if audit else str(entry.proto_payload)

        if not text:
            return None

        # Clean / single line representation
        text = text.strip().replace("\r", "\\r").replace("\n", "\\n")
        ts = ""
        if entry.timestamp:
            ts = entry.timestamp.strftime("%Y-%m-%d %H:%M:%S")

        instance = ""
        if hasattr(entry, "resource") and hasattr(entry.resource, "labels"):
            instance = entry.resource.labels.get("instance_id", "")

        prefix_parts = []
        if ts:
            prefix_parts.append(ts)
        prefix_parts.append(service_label(service or classify_entry(entry) or "all"))
        if entry.resource.type:
            prefix_parts.append(entry.resource.type)
        labels = entry.resource.labels
        for key in ("cluster_name", "namespace_name", "pod_name", "container_name", "environment_name", "job_id", "dataset_id", "project_id"):
            if labels.get(key):
                prefix_parts.append(f"{key}={labels[key]}")
        severity = getattr(entry, "severity", None)
        if severity:
            prefix_parts.append(getattr(severity, "name", str(severity)))
        if instance:
            prefix_parts.append(f"GCE:{instance}")

        if prefix_parts:
            return f"[{' | '.join(prefix_parts)}] {text}"
        return text

    async def _process_log_line(self, line: str, service: str = "compute_engine"):
        validate_service(service)
        if service == "all":
            raise ValueError("A log line must belong to a specific GCP service.")
        self.last_log_received_at = datetime.datetime.now(datetime.timezone.utc)
        self.buffer.append(line)
        self.service_buffers[service].append(line)
        self.service_last_received[service] = self.last_log_received_at
        await self.broadcast(line, service)

gcp_log_stream_service = GcpLogStreamService()
