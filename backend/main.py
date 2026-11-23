# from fastapi import FastAPI, Form, HTTPException
# from fastapi.middleware.cors import CORSMiddleware
# import uvicorn
# import asyncio
# import json
# import uuid
# import logging
# from pathlib import Path
# from contextlib import asynccontextmanager
# from pydantic import BaseModel
# import re
# from typing import Annotated, List, Dict, Any, Optional
# import httpx
# from fastapi.responses import StreamingResponse

# from google.adk.runners import Runner
# from google.adk.sessions.in_memory_session_service import InMemorySessionService
# from google.genai import types

# from agents.application_agent.agent import root_agent as app_agent
# import os

# from services.azure_log_stream import log_stream_service
# from services.log_stream_manager import log_stream_manager
# from api.log_stream import router as log_stream_router
# from config.settings import settings
# from services.gcp_services import GcpService, service_label
# from prompts import ISSUE_RESPONSE_FORMAT, LOG_ANALYSIS_POLICY

# logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# MOCKDATA_DIR = Path(__file__).resolve().parent / "mockdata"
# MODEL_STREAM_HEARTBEAT_SECONDS = 15


# class ThinkingContentFilter:
#     _OPEN = "<think>"
#     _CLOSE = "</think>"

#     def __init__(self):
#         self._buffer = ""
#         self._inside_thinking = False

#     def feed(self, text: str) -> str:
#         self._buffer += text
#         visible = []

#         while self._buffer:
#             if self._inside_thinking:
#                 close_index = self._buffer.find(self._CLOSE)
#                 if close_index == -1:
#                     self._buffer = self._buffer[-(len(self._CLOSE) - 1):]
#                     break
#                 self._buffer = self._buffer[close_index + len(self._CLOSE):]
#                 self._inside_thinking = False
#                 continue

#             open_index = self._buffer.find(self._OPEN)
#             if open_index != -1:
#                 visible.append(self._buffer[:open_index])
#                 self._buffer = self._buffer[open_index + len(self._OPEN):]
#                 self._inside_thinking = True
#                 continue

#             safe_length = len(self._buffer) - len(self._OPEN) + 1
#             if safe_length <= 0:
#                 break
#             visible.append(self._buffer[:safe_length])
#             self._buffer = self._buffer[safe_length:]

#         return "".join(visible)

#     def finish(self) -> str:
#         if self._inside_thinking:
#             self._buffer = ""
#             return ""
#         remainder = self._buffer
#         self._buffer = ""
#         return remainder


# def call_model_api(prompt: str) -> str:
#     async def collect_response():
#         return "".join(
#             content for content in [chunk async for chunk in stream_model_api(prompt)]
#             if content is not None
#         )

#     return asyncio.run(collect_response())


# async def stream_model_api(prompt: str):
#     if not settings.MODEL_API_URL:
#         raise ValueError("MODEL_API_URL is not configured.")

#     completed = False
#     content_filter = ThinkingContentFilter()
#     timeout = httpx.Timeout(
#         connect=settings.MODEL_API_TIMEOUT_SECONDS,
#         read=None,
#         write=settings.MODEL_API_TIMEOUT_SECONDS,
#         pool=settings.MODEL_API_TIMEOUT_SECONDS,
#     )
#     async with httpx.AsyncClient(timeout=timeout) as client:
#         async with client.stream(
#             "POST",
#             settings.MODEL_API_URL,
#             json={"model": settings.MODEL_NAME, "prompt": prompt},
#         ) as response:
#             if response.is_error:
#                 body = (await response.aread()).decode("utf-8", errors="replace").strip()
#                 try:
#                     error_payload = json.loads(body)
#                 except json.JSONDecodeError:
#                     error_payload = None
#                 if isinstance(error_payload, dict):
#                     detail = ""
#                     for key in ("error", "detail", "message"):
#                         value = error_payload.get(key)
#                         if isinstance(value, str) and value.strip():
#                             detail = value.strip()
#                             break
#                 else:
#                     detail = body[:1000]
#                 detail = detail[:1000]
#                 logging.error(
#                     "Model API request failed (model=%s, status=%s): %s",
#                     settings.MODEL_NAME,
#                     response.status_code,
#                     detail or "<empty response body>",
#                 )
#                 message = f"Model API returned HTTP {response.status_code}"
#                 if detail:
#                     message += f": {detail}"
#                 raise RuntimeError(message)

#             line_iterator = response.aiter_lines().__aiter__()
#             pending_line = None
#             try:
#                 while True:
#                     if pending_line is None:
#                         pending_line = asyncio.create_task(line_iterator.__anext__())
#                     ready, _ = await asyncio.wait(
#                         {pending_line},
#                         timeout=MODEL_STREAM_HEARTBEAT_SECONDS,
#                     )
#                     if not ready:
#                         yield None
#                         continue

#                     try:
#                         line = pending_line.result()
#                     except StopAsyncIteration:
#                         break
#                     pending_line = None

#                     if not line.strip():
#                         yield None
#                         continue

#                     try:
#                         chunk = json.loads(line)
#                     except json.JSONDecodeError as error:
#                         raise ValueError("The model API returned an invalid streaming JSON chunk.") from error

#                     if not isinstance(chunk, dict):
#                         raise ValueError("The model API returned an unsupported streaming chunk.")
#                     if chunk.get("error"):
#                         raise RuntimeError(f"Model API error: {chunk['error']}")

#                     message = chunk.get("message")
#                     if isinstance(message, dict):
#                         content = message.get("content")
#                         if isinstance(content, str) and content:
#                             visible_content = content_filter.feed(content)
#                             if visible_content:
#                                 yield visible_content
#                             else:
#                                 yield None

#                     if chunk.get("done") is True:
#                         completed = True
#             finally:
#                 if pending_line is not None and not pending_line.done():
#                     pending_line.cancel()
#                     try:
#                         await pending_line
#                     except asyncio.CancelledError:
#                         pass

#     if not completed:
#         raise RuntimeError("The model API stream ended before the model marked it complete.")
#     remaining_content = content_filter.finish()
#     if remaining_content:
#         yield remaining_content


# def build_model_prompt(logs: str) -> str:
#     return (
#         f"{LOG_ANALYSIS_POLICY}\n\n"
#         "Analyze only the supplied logs. Do not reveal or output internal reasoning or thinking; "
#         "provide only the final analysis in the required format. You do not have tools; do not "
#         "claim to have sent alerts.\n\n"
#         f"{ISSUE_RESPONSE_FORMAT}\n\n"
#         f"Supplied logs:\n{logs}"
#     )


# @asynccontextmanager
# async def lifespan(app: FastAPI):
#     # Start both Azure and GCP log streaming background tasks
#     await log_stream_manager.start_all()
#     yield
#     # Stop background tasks on shutdown
#     await log_stream_manager.stop_all()

# app = FastAPI(lifespan=lifespan)

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

# app.include_router(log_stream_router, prefix="/api/application/logs", tags=["Logs"])

# # Initialize runners with in-memory sessions
# app_runner = Runner(
#     agent=app_agent, 
#     app_name="app_agent_app", 
#     session_service=InMemorySessionService(), 
#     auto_create_session=True
# )

# def run_agent_sync(runner, text, cloud_provider=None, gcp_service="all"):
#     session_id = str(uuid.uuid4())
#     if settings.CODEBASE_ACCESS_ENABLED:
#         # Routing is trusted session state, not a tool argument chosen by the model.
#         cloud = cloud_provider
#         if cloud is None:
#             cloud = (
#                 "gcp" if text == "analyze:live:gcp" else
#                 "azure" if text in ("analyze", "analyze:live", "analyze:live:azure") else
#                 "unknown"
#             )
#         asyncio.run(runner.session_service.create_session(
#             app_name=runner.app_name, user_id="default_user", session_id=session_id,
#             state={"monitoring_cloud": cloud, "monitoring_gcp_service": gcp_service},
#         ))
#     if text.startswith("analyze:"):
#         payload = text.split(":", 1)[1]
#         if payload == "live:gcp":
#             prompt_text = (
#                 f"Please analyze GCP {service_label(gcp_service)} logs. "
#                 f"Call analyze_application_logs with log_content='analyze:live:gcp:{gcp_service}'."
#             )
#         elif payload in ("live", "live:azure"):
#             prompt_text = "Please analyze the Azure App Service live application logs. Call the analyze_application_logs tool with log_content='analyze:live:azure'."
#         else:
#             filepath = os.path.join(r"C:\Users\vtangeti\Desktop\poc\backend\mockdata", payload)
#             prompt_text = f"Please analyze the log file at {filepath}. Call the analyze_application_logs tool passing this exact file path as the log_content."
#     elif text.strip().lower() == "analyze":
#         prompt_text = "Please analyze the logs."
#     else:
#         prompt_text = f"Please analyze these logs:\n{text}"
        
#     msg = types.Content(role="user", parts=[types.Part.from_text(text=prompt_text)])
    
#     events = runner.run(
#         user_id="default_user",
#         session_id=session_id,
#         new_message=msg
#     )
    
#     response_text = ""
#     for event in events:
#         if getattr(event, 'message', None):
#             if hasattr(event.message, 'parts'):
#                 for part in event.message.parts:
#                     if hasattr(part, 'text') and part.text:
#                         response_text += part.text
#         elif getattr(event, 'output', None):
#             if hasattr(event.output, 'text'):
#                 response_text += event.output.text
#             elif hasattr(event.output, 'content'):
#                 response_text += event.output.content
#             else:
#                 response_text += str(event.output)
                
#     return response_text

# class SessionData(BaseModel):
#     sessions: List[Dict[str, Any]]

# chat_history_store = {"sessions": []}

# @app.get("/api/application/sessions")
# async def get_sessions():
#     return chat_history_store

# @app.post("/api/application/sessions")
# async def save_sessions(data: SessionData):
#     chat_history_store["sessions"] = data.sessions
#     return {"status": "success"}

# @app.get("/api/mockfiles")
# async def get_mock_files():
#     files = sorted(path.name for path in MOCKDATA_DIR.iterdir() if path.is_file())
#     return {"files": files}


# @app.get("/api/mockfiles/{filename}")
# async def get_mock_file(filename: str):
#     if not filename or Path(filename).name != filename:
#         raise HTTPException(status_code=400, detail="Invalid mock log filename.")
#     filepath = MOCKDATA_DIR / filename
#     if not filepath.is_file():
#         raise HTTPException(status_code=404, detail="Mock log file not found.")
#     return {"file": filename, "content": filepath.read_text(encoding="utf-8")}


# def filter_log_content(raw_text: str, severity: Optional[str] = None, keyword: Optional[str] = None) -> tuple[str, int]:
#     if not raw_text:
#         return "", 0
#     lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    
#     # 1. Keyword filter
#     if keyword and keyword.strip():
#         kw = keyword.strip().lower()
#         lines = [l for l in lines if kw in l.lower()]
        
#     # 2. Severity filter
#     if severity and severity.upper() != "ALL":
#         sev = severity.upper()
#         sev_patterns = {
#             "CRITICAL": re.compile(r"\b(500|out of memory|oom|fatal|panic|crash)\b", re.I),
#             "ERROR": re.compile(r"\b(error|exception|failed|failure|401|403|400|502|503|refused|sig=|token)\b", re.I),
#             "HIGH": re.compile(r"\b(error|exception|failed|failure|401|403|400|502|503|refused|sig=|token)\b", re.I),
#             "WARNING": re.compile(r"\b(warn|warning|timeout|retry|deprecated)\b", re.I),
#             "INFO": re.compile(r"\b(info|information|started|completed|success)\b", re.I),
#         }
#         pat = sev_patterns.get(sev)
#         if pat:
#             lines = [l for l in lines if pat.search(l)]
            
#     return "\n".join(lines), len(lines)

# @app.post("/api/application/analyze")
# async def analyze_application(
#     text: str = Form(...),
#     severity_filter: Optional[str] = Form(None),
#     keyword_filter: Optional[str] = Form(None),
#     cloud_provider: Optional[str] = Form(None),
#     gcp_service: Annotated[GcpService, Form()] = "all",
#     stream: bool = Form(False),
# ):
#     service_label(gcp_service)
#     try:
#         from tools.application_log_tools import get_recent_application_logs
        
#         is_mock = text.startswith("analyze:") and not text.startswith("analyze:live")
#         is_gcp_live = text == "analyze:live:gcp" or (cloud_provider and cloud_provider.lower() == "gcp" and not is_mock)

#         if is_mock:
#             filename = text.split(":", 1)[1]
#             if not filename or Path(filename).name != filename:
#                 raise ValueError("Choose a mock log file from the available mock files.")
#             filepath = MOCKDATA_DIR / filename
#             source_name = filename
#             raw_logs = filepath.read_text(encoding="utf-8")
#         elif is_gcp_live:
#             source_name = f"GCP Live Logs - {service_label(gcp_service)}"
#             raw_logs = get_recent_application_logs(source="gcp", gcp_service=gcp_service)
#         else:
#             source_name = "Azure Live Logs"
#             raw_logs = get_recent_application_logs(source="azure")

#         # Build filter label and verify if active
#         filter_parts = []
#         if severity_filter and severity_filter.upper() != "ALL":
#             filter_parts.append(f"Severity={severity_filter.upper()}")
#         if keyword_filter and keyword_filter.strip():
#             filter_parts.append(f"Keyword='{keyword_filter.strip()}'")
            
#         has_filter = bool(filter_parts)
#         filter_label = f" [{', '.join(filter_parts)}]" if has_filter else ""
#         file_name = f"{source_name}{filter_label}"
#         if is_gcp_live and not raw_logs.strip():
#             return {
#                 "response": f"No buffered logs are available for {source_name}. Check Cloud Logging configuration and connection status.",
#                 "file_name": file_name,
#                 "analyzed_logs": "",
#             }

#         # Apply filtering
#         if has_filter:
#             filtered_logs, match_count = filter_log_content(raw_logs, severity=severity_filter, keyword=keyword_filter)
#             if match_count == 0:
#                 return {
#                     "response": f"No log lines matched the active filter ({', '.join(filter_parts)}). Please try broader search keywords or change the severity level.",
#                     "file_name": file_name,
#                     "analyzed_logs": "No matching log lines found."
#                 }
#             analyzed_logs = filtered_logs
#             agent_input = f"Please analyze these filtered logs ({', '.join(filter_parts)}):\n{filtered_logs}"
#         else:
#             analyzed_logs = raw_logs
#             agent_input = text if is_mock else (
#                 "analyze:live:gcp" if is_gcp_live else "analyze:live:azure"
#             )
#         if is_gcp_live:
#             # Analyze the selected snapshot, not a newer or differently scoped buffer.
#             agent_input = (
#                 f"Analyze these supplied GCP {service_label(gcp_service)} logs only; "
#                 f"do not fetch other live logs:\n{analyzed_logs}"
#             )

#         selected_cloud = "mock" if is_mock else ("gcp" if is_gcp_live else "azure")
#         if stream:
#             async def generate_response():
#                 yield json.dumps({
#                     "type": "metadata",
#                     "file_name": file_name,
#                     "analyzed_logs": analyzed_logs,
#                     "has_context": bool(analyzed_logs.strip()),
#                 }) + "\n"
#                 try:
#                     if not analyzed_logs.strip():
#                         yield json.dumps({
#                             "type": "token",
#                             "content": "No log lines were available for analysis.",
#                         }) + "\n"
#                         yield json.dumps({"type": "done"}) + "\n"
#                         return
#                     if settings.MODEL_API_URL:
#                         async for content in stream_model_api(build_model_prompt(analyzed_logs)):
#                             if content is None:
#                                 yield json.dumps({"type": "keepalive"}) + "\n"
#                             else:
#                                 yield json.dumps({"type": "token", "content": content}) + "\n"
#                     else:
#                         answer = await asyncio.to_thread(
#                             run_agent_sync, app_runner, agent_input, selected_cloud, gcp_service
#                         )
#                         yield json.dumps({"type": "token", "content": answer}) + "\n"
#                     yield json.dumps({"type": "done"}) + "\n"
#                 except Exception as error:
#                     logging.exception("Streaming model analysis failed")
#                     yield json.dumps({"type": "error", "message": str(error)}) + "\n"

#             return StreamingResponse(
#                 generate_response(),
#                 media_type="application/x-ndjson",
#                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
#             )

#         if settings.MODEL_API_URL:
#             response = await asyncio.to_thread(call_model_api, build_model_prompt(analyzed_logs))
#         else:
#             response = await asyncio.to_thread(run_agent_sync, app_runner, agent_input, selected_cloud, gcp_service)
#         return {"response": response, "file_name": file_name, "analyzed_logs": analyzed_logs}
#     except Exception as e:
#         import traceback
#         traceback.print_exc()
#         return {"response": f"Error occurred: {str(e)}"}

# if __name__ == "__main__":
#     uvicorn.run("main:app", host="127.0.0.1", port=5000, reload=True)





from fastapi import FastAPI, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import asyncio
import json
import uuid
import logging
from pathlib import Path
from contextlib import asynccontextmanager
from pydantic import BaseModel
import re
from typing import Annotated, List, Dict, Any, Optional
import httpx
from fastapi.responses import StreamingResponse

from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.genai import types

from agents.application_agent.agent import root_agent as app_agent
import os

from services.azure_log_stream import log_stream_service
from services.log_stream_manager import log_stream_manager
from api.log_stream import router as log_stream_router
from config.settings import settings
from services.gcp_services import GcpService, service_label
from prompts import ISSUE_RESPONSE_FORMAT, LOG_ANALYSIS_POLICY

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

MOCKDATA_DIR = Path(__file__).resolve().parent / "mockdata"
MODEL_STREAM_HEARTBEAT_SECONDS = 15


class ThinkingContentFilter:
    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(self):
        self._buffer = ""
        self._inside_thinking = False

    def feed(self, text: str) -> str:
        self._buffer += text
        visible = []

        while self._buffer:
            if self._inside_thinking:
                close_index = self._buffer.find(self._CLOSE)
                if close_index == -1:
                    self._buffer = self._buffer[-(len(self._CLOSE) - 1):]
                    break
                self._buffer = self._buffer[close_index + len(self._CLOSE):]
                self._inside_thinking = False
                continue

            open_index = self._buffer.find(self._OPEN)
            if open_index != -1:
                visible.append(self._buffer[:open_index])
                self._buffer = self._buffer[open_index + len(self._OPEN):]
                self._inside_thinking = True
                continue

            safe_length = len(self._buffer) - len(self._OPEN) + 1
            if safe_length <= 0:
                break
            visible.append(self._buffer[:safe_length])
            self._buffer = self._buffer[safe_length:]

        return "".join(visible)

    def finish(self) -> str:
        if self._inside_thinking:
            self._buffer = ""
            return ""
        remainder = self._buffer
        self._buffer = ""
        return remainder


def call_model_api(prompt: str) -> str:
    async def collect_response():
        return "".join(
            content
            for content in [chunk async for chunk in stream_model_api(prompt)]
            if content is not None
        )

    return asyncio.run(collect_response())


async def stream_model_api(prompt: str):
    if not settings.MODEL_API_URL:
        raise ValueError("MODEL_API_URL is not configured.")

    logging.info(
        "--> [Model API Request] Connecting to: URL=%s | Model=%s | Prompt chars=%d",
        settings.MODEL_API_URL,
        settings.MODEL_NAME,
        len(prompt),
    )

    completed = False
    content_filter = ThinkingContentFilter()
    chunk_count = 0
    token_count = 0

    timeout = httpx.Timeout(
        connect=settings.MODEL_API_TIMEOUT_SECONDS,
        read=None,
        write=settings.MODEL_API_TIMEOUT_SECONDS,
        pool=settings.MODEL_API_TIMEOUT_SECONDS,
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST",
            settings.MODEL_API_URL,
            json={"model": settings.MODEL_NAME, "prompt": prompt},
        ) as response:
            logging.info(
                "<-- [Model API Connected] Status: %s | Content-Type: %s",
                response.status_code,
                response.headers.get("content-type", "unknown"),
            )
            if response.is_error:
                body = (await response.aread()).decode("utf-8", errors="replace").strip()
                try:
                    error_payload = json.loads(body)
                except json.JSONDecodeError:
                    error_payload = None
                if isinstance(error_payload, dict):
                    detail = ""
                    for key in ("error", "detail", "message"):
                        value = error_payload.get(key)
                        if isinstance(value, str) and value.strip():
                            detail = value.strip()
                            break
                else:
                    detail = body[:1000]
                detail = detail[:1000]
                logging.error(
                    "Model API request failed (model=%s, status=%s): %s",
                    settings.MODEL_NAME,
                    response.status_code,
                    detail or "<empty response body>",
                )
                message = f"Model API returned HTTP {response.status_code}"
                if detail:
                    message += f": {detail}"
                raise RuntimeError(message)

            line_iterator = response.aiter_lines().__aiter__()
            pending_line = None
            try:
                while True:
                    if pending_line is None:
                        pending_line = asyncio.create_task(line_iterator.__anext__())
                    ready, _ = await asyncio.wait(
                        {pending_line},
                        timeout=MODEL_STREAM_HEARTBEAT_SECONDS,
                    )
                    if not ready:
                        yield None
                        continue

                    try:
                        line = pending_line.result()
                    except StopAsyncIteration:
                        break
                    pending_line = None

                    if not line.strip():
                        yield None
                        continue

                    chunk_count += 1
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError as error:
                        logging.warning("[Chunk #%d] Invalid JSON line: %r", chunk_count, line[:100])
                        raise ValueError("The model API returned an invalid streaming JSON chunk.") from error

                    if not isinstance(chunk, dict):
                        raise ValueError("The model API returned an unsupported streaming chunk.")
                    if chunk.get("error"):
                        logging.error("[Chunk #%d] Model API reported error: %s", chunk_count, chunk["error"])
                        raise RuntimeError(f"Model API error: {chunk['error']}")

                    content = None
                    message = chunk.get("message")
                    if isinstance(message, dict):
                        content = message.get("content")
                    elif "content" in chunk:
                        content = chunk.get("content")
                    elif "response" in chunk:
                        content = chunk.get("response")

                    if content is not None and isinstance(content, str) and content != "":
                        token_count += 1
                        if token_count <= 5 or token_count % 20 == 0:
                            logging.info(
                                "[Chunk #%d | Token #%d] Token received: %r",
                                chunk_count,
                                token_count,
                                content[:60],
                            )
                        visible_content = content_filter.feed(content)
                        if visible_content:
                            yield visible_content
                        else:
                            yield None
                    else:
                        logging.info(
                            "[Chunk #%d] Heartbeat / Empty content chunk (content=%r, done=%s)",
                            chunk_count,
                            content,
                            chunk.get("done"),
                        )
                        yield None

                    if chunk.get("done") is True:
                        completed = True
                        logging.info(
                            "[Chunk #%d] Model marked DONE! Total tokens received: %d",
                            chunk_count,
                            token_count,
                        )
            finally:
                if pending_line is not None and not pending_line.done():
                    pending_line.cancel()
                    try:
                        await pending_line
                    except asyncio.CancelledError:
                        pass

    if not completed:
        logging.warning("Stream ended before completion flag. Total chunks: %d, Total tokens: %d", chunk_count, token_count)
        raise RuntimeError("The model API stream ended before the model marked it complete.")

    remaining_content = content_filter.finish()
    if remaining_content:
        yield remaining_content
    logging.info("Stream completed successfully. Total chunks: %d | Total tokens: %d", chunk_count, token_count)


def build_model_prompt(logs: str) -> str:
    return (
        f"{LOG_ANALYSIS_POLICY}\n\n"
        "Analyze only the supplied logs. Do not reveal or output internal reasoning or thinking; "
        "provide only the final analysis in the required format. You do not have tools; do not "
        "claim to have sent alerts.\n\n"
        f"{ISSUE_RESPONSE_FORMAT}\n\n"
        f"Supplied logs:\n{logs}"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start both Azure and GCP log streaming background tasks
    await log_stream_manager.start_all()
    yield
    # Stop background tasks on shutdown
    await log_stream_manager.stop_all()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(log_stream_router, prefix="/api/application/logs", tags=["Logs"])

# Initialize runners with in-memory sessions
app_runner = Runner(
    agent=app_agent, 
    app_name="app_agent_app", 
    session_service=InMemorySessionService(), 
    auto_create_session=True
)

def run_agent_sync(runner, text, cloud_provider=None, gcp_service="all"):
    session_id = str(uuid.uuid4())
    if settings.CODEBASE_ACCESS_ENABLED:
        # Routing is trusted session state, not a tool argument chosen by the model.
        cloud = cloud_provider
        if cloud is None:
            cloud = (
                "gcp" if text == "analyze:live:gcp" else
                "azure" if text in ("analyze", "analyze:live", "analyze:live:azure") else
                "unknown"
            )
        asyncio.run(runner.session_service.create_session(
            app_name=runner.app_name, user_id="default_user", session_id=session_id,
            state={"monitoring_cloud": cloud, "monitoring_gcp_service": gcp_service},
        ))
    if text.startswith("analyze:"):
        payload = text.split(":", 1)[1]
        if payload == "live:gcp":
            prompt_text = (
                f"Please analyze GCP {service_label(gcp_service)} logs. "
                f"Call analyze_application_logs with log_content='analyze:live:gcp:{gcp_service}'."
            )
        elif payload in ("live", "live:azure"):
            prompt_text = "Please analyze the Azure App Service live application logs. Call the analyze_application_logs tool with log_content='analyze:live:azure'."
        else:
            filepath = os.path.join(r"C:\Users\vtangeti\Desktop\poc\backend\mockdata", payload)
            prompt_text = f"Please analyze the log file at {filepath}. Call the analyze_application_logs tool passing this exact file path as the log_content."
    elif text.strip().lower() == "analyze":
        prompt_text = "Please analyze the logs."
    else:
        prompt_text = f"Please analyze these logs:\n{text}"
        
    msg = types.Content(role="user", parts=[types.Part.from_text(text=prompt_text)])
    
    events = runner.run(
        user_id="default_user",
        session_id=session_id,
        new_message=msg
    )
    
    response_text = ""
    for event in events:
        if getattr(event, 'message', None):
            if hasattr(event.message, 'parts'):
                for part in event.message.parts:
                    if hasattr(part, 'text') and part.text:
                        response_text += part.text
        elif getattr(event, 'output', None):
            if hasattr(event.output, 'text'):
                response_text += event.output.text
            elif hasattr(event.output, 'content'):
                response_text += event.output.content
            else:
                response_text += str(event.output)
                
    return response_text

class SessionData(BaseModel):
    sessions: List[Dict[str, Any]]

chat_history_store = {"sessions": []}

@app.get("/api/application/sessions")
async def get_sessions():
    return chat_history_store

@app.post("/api/application/sessions")
async def save_sessions(data: SessionData):
    chat_history_store["sessions"] = data.sessions
    return {"status": "success"}

@app.get("/api/mockfiles")
async def get_mock_files():
    files = sorted(path.name for path in MOCKDATA_DIR.iterdir() if path.is_file())
    return {"files": files}


@app.get("/api/mockfiles/{filename}")
async def get_mock_file(filename: str):
    if not filename or Path(filename).name != filename:
        raise HTTPException(status_code=400, detail="Invalid mock log filename.")
    filepath = MOCKDATA_DIR / filename
    if not filepath.is_file():
        raise HTTPException(status_code=404, detail="Mock log file not found.")
    return {"file": filename, "content": filepath.read_text(encoding="utf-8")}


def filter_log_content(raw_text: str, severity: Optional[str] = None, keyword: Optional[str] = None) -> tuple[str, int]:
    if not raw_text:
        return "", 0
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    
    # 1. Keyword filter
    if keyword and keyword.strip():
        kw = keyword.strip().lower()
        lines = [l for l in lines if kw in l.lower()]
        
    # 2. Severity filter
    if severity and severity.upper() != "ALL":
        sev = severity.upper()
        sev_patterns = {
            "CRITICAL": re.compile(r"\b(500|out of memory|oom|fatal|panic|crash)\b", re.I),
            "ERROR": re.compile(r"\b(error|exception|failed|failure|401|403|400|502|503|refused|sig=|token)\b", re.I),
            "HIGH": re.compile(r"\b(error|exception|failed|failure|401|403|400|502|503|refused|sig=|token)\b", re.I),
            "WARNING": re.compile(r"\b(warn|warning|timeout|retry|deprecated)\b", re.I),
            "INFO": re.compile(r"\b(info|information|started|completed|success)\b", re.I),
        }
        pat = sev_patterns.get(sev)
        if pat:
            lines = [l for l in lines if pat.search(l)]
            
    return "\n".join(lines), len(lines)

@app.post("/api/application/analyze")
async def analyze_application(
    text: str = Form(...),
    severity_filter: Optional[str] = Form(None),
    keyword_filter: Optional[str] = Form(None),
    cloud_provider: Optional[str] = Form(None),
    gcp_service: Annotated[GcpService, Form()] = "all",
    stream: bool = Form(False),
):
    service_label(gcp_service)
    try:
        from tools.application_log_tools import get_recent_application_logs
        
        is_mock = text.startswith("analyze:") and not text.startswith("analyze:live")
        is_gcp_live = text == "analyze:live:gcp" or (cloud_provider and cloud_provider.lower() == "gcp" and not is_mock)

        if is_mock:
            filename = text.split(":", 1)[1]
            if not filename or Path(filename).name != filename:
                raise ValueError("Choose a mock log file from the available mock files.")
            filepath = MOCKDATA_DIR / filename
            source_name = filename
            raw_logs = filepath.read_text(encoding="utf-8")
        elif is_gcp_live:
            source_name = f"GCP Live Logs - {service_label(gcp_service)}"
            raw_logs = get_recent_application_logs(source="gcp", gcp_service=gcp_service)
        else:
            source_name = "Azure Live Logs"
            raw_logs = get_recent_application_logs(source="azure")

        # Build filter label and verify if active
        filter_parts = []
        if severity_filter and severity_filter.upper() != "ALL":
            filter_parts.append(f"Severity={severity_filter.upper()}")
        if keyword_filter and keyword_filter.strip():
            filter_parts.append(f"Keyword='{keyword_filter.strip()}'")
            
        has_filter = bool(filter_parts)
        filter_label = f" [{', '.join(filter_parts)}]" if has_filter else ""
        file_name = f"{source_name}{filter_label}"
        if is_gcp_live and not raw_logs.strip():
            return {
                "response": f"No buffered logs are available for {source_name}. Check Cloud Logging configuration and connection status.",
                "file_name": file_name,
                "analyzed_logs": "",
            }

        # Apply filtering
        if has_filter:
            filtered_logs, match_count = filter_log_content(raw_logs, severity=severity_filter, keyword=keyword_filter)
            if match_count == 0:
                return {
                    "response": f"No log lines matched the active filter ({', '.join(filter_parts)}). Please try broader search keywords or change the severity level.",
                    "file_name": file_name,
                    "analyzed_logs": "No matching log lines found."
                }
            analyzed_logs = filtered_logs
            agent_input = f"Please analyze these filtered logs ({', '.join(filter_parts)}):\n{filtered_logs}"
        else:
            analyzed_logs = raw_logs
            agent_input = text if is_mock else (
                "analyze:live:gcp" if is_gcp_live else "analyze:live:azure"
            )
        if is_gcp_live:
            # Analyze the selected snapshot, not a newer or differently scoped buffer.
            agent_input = (
                f"Analyze these supplied GCP {service_label(gcp_service)} logs only; "
                f"do not fetch other live logs:\n{analyzed_logs}"
            )

        selected_cloud = "mock" if is_mock else ("gcp" if is_gcp_live else "azure")
        if stream:
            async def generate_response():
                logging.info("[Stream -> Frontend] Starting stream for '%s'...", file_name)
                yield json.dumps({
                    "type": "metadata",
                    "file_name": file_name,
                    "analyzed_logs": analyzed_logs,
                }) + "\n"
                try:
                    if settings.MODEL_API_URL:
                        token_count = 0
                        async for content in stream_model_api(build_model_prompt(analyzed_logs)):
                            if content is None:
                                yield json.dumps({"type": "heartbeat"}) + "\n"
                                continue
                            token_count += 1
                            yield json.dumps({"type": "token", "content": content}) + "\n"
                        logging.info("[Stream -> Frontend] Finished sending %d token chunks.", token_count)
                    else:
                        logging.info("[Stream -> Frontend] MODEL_API_URL not set; running agent runner synchronously...")
                        answer = await asyncio.to_thread(
                            run_agent_sync, app_runner, agent_input, selected_cloud, gcp_service
                        )
                        yield json.dumps({"type": "token", "content": answer}) + "\n"
                    yield json.dumps({"type": "done"}) + "\n"
                    logging.info("[Stream -> Frontend] Sent 'done' signal.")
                except Exception as error:
                    logging.exception("[Stream -> Frontend] Streaming failed: %s", error)
                    error_event = {"type": "error", "message": str(error)}
                    if isinstance(error, httpx.RemoteProtocolError):
                        error_event["code"] = "upstream_stream_interrupted"
                    yield json.dumps(error_event) + "\n"

            return StreamingResponse(
                generate_response(),
                media_type="application/x-ndjson",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        if settings.MODEL_API_URL:
            response = await asyncio.to_thread(call_model_api, build_model_prompt(analyzed_logs))
        else:
            response = await asyncio.to_thread(run_agent_sync, app_runner, agent_input, selected_cloud, gcp_service)
        return {"response": response, "file_name": file_name, "analyzed_logs": analyzed_logs}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"response": f"Error occurred: {str(e)}"}

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=5000, reload=False)
