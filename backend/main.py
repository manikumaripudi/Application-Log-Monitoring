from fastapi import FastAPI, Form
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import asyncio
import uuid
import logging
from contextlib import asynccontextmanager
from pydantic import BaseModel
import re
from typing import Annotated, List, Dict, Any, Optional

from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.genai import types

from agents.application_agent.agent import root_agent as app_agent
import os

from services.azure_log_stream import log_stream_service
from services.log_stream_manager import log_stream_manager
from api.log_stream import router as log_stream_router
from tools.application_log_tools import MOCK_LOG_FILE
from config.settings import settings
from services.gcp_services import GcpService, service_label

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

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
    mockdata_dir = r"C:\Users\vtangeti\Desktop\poc\backend\mockdata"
    try:
        files = [f for f in os.listdir(mockdata_dir) if os.path.isfile(os.path.join(mockdata_dir, f))]
        return {"files": files}
    except Exception as e:
        return {"files": [], "error": str(e)}

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
):
    service_label(gcp_service)
    try:
        from tools.application_log_tools import get_recent_application_logs
        
        is_mock = text.startswith("analyze:") and not text.startswith("analyze:live")
        is_gcp_live = text == "analyze:live:gcp" or (cloud_provider and cloud_provider.lower() == "gcp" and not is_mock)

        if is_mock:
            filename = text.split(":", 1)[1]
            filepath = os.path.join(r"C:\Users\vtangeti\Desktop\poc\backend\mockdata", filename)
            source_name = filename
            with open(filepath, 'r', encoding='utf-8') as f:
                raw_logs = f.read()
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
        response = await asyncio.to_thread(run_agent_sync, app_runner, agent_input, selected_cloud, gcp_service)
        return {"response": response, "file_name": file_name, "analyzed_logs": analyzed_logs}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"response": f"Error occurred: {str(e)}"}

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=5000, reload=True)
