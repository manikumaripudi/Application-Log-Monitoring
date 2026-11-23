from fastapi import APIRouter, Request, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional
import asyncio
import json

from services.azure_log_stream import log_stream_service as azure_log_stream
from services.gcp_log_stream import gcp_log_stream_service as gcp_log_stream
from services.gcp_services import GcpService

router = APIRouter()

@router.get("/stream")
async def stream_logs(request: Request, source: str = Query("azure"), gcp_service: GcpService = Query("all")):
    """
    Server-Sent Events (SSE) endpoint for live logs.
    Takes source: 'azure' (default) or 'gcp'.
    Sends buffered history on connect, then streams new lines in real-time.
    """
    active_service = gcp_log_stream if source.lower() == "gcp" else azure_log_stream

    async def log_generator():
        queue = asyncio.Queue()
        if source.lower() == "gcp":
            active_service.add_listener(queue, gcp_service)
        else:
            active_service.add_listener(queue)
        try:
            buffered = active_service.get_buffered_logs(gcp_service) if source.lower() == "gcp" else active_service.get_buffered_logs()
            if buffered:
                for line in buffered.split("\n"):
                    if line:
                        yield f"data: {line}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    log_line = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {log_line}\n\n"
                except asyncio.TimeoutError:
                    # Send keep-alive comment to prevent proxy/browser timeouts
                    yield ": keep-alive\n\n"
        finally:
            active_service.remove_listener(queue)

    return StreamingResponse(
        log_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

@router.get("/status")
async def get_status(source: str = Query("azure"), gcp_service: GcpService = Query("all")):
    """
    Returns connection and buffer status for the selected provider ('azure', 'gcp', or 'all').
    """
    if source.lower() == "all":
        return {
            "azure": azure_log_stream.get_status(),
            "gcp": gcp_log_stream.get_status(gcp_service),
        }
    active_service = gcp_log_stream if source.lower() == "gcp" else azure_log_stream
    return active_service.get_status(gcp_service) if source.lower() == "gcp" else active_service.get_status()

class AnalyzeRequest(BaseModel):
    minutes: Optional[int] = 10
    log_text: Optional[str] = None

@router.post("/analyze")
async def analyze_logs(req: AnalyzeRequest):
    # This endpoint can be wired to the agent directly
    # For now, we will expose this so main.py can include it
    pass
