import os
import sys
import re
from pathlib import Path
from typing import Any, Dict, Optional
from dotenv import load_dotenv

# Load local .env variables
env_path = Path(__file__).parent / '.env'
load_dotenv(dotenv_path=env_path, override=True)

# Ensure project root is in sys.path so 'config', 'schemas', etc. can always be resolved
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Prefer MODEL_NAME from local .env, fallback to config.settings or default
MODEL_NAME = os.getenv("MODEL_NAME", "azure/gpt-5.6-terra")

# Setup Gemini / Google AI Credentials if configured
gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
if gemini_key:
    os.environ["GEMINI_API_KEY"] = gemini_key
    os.environ["GOOGLE_API_KEY"] = gemini_key

gcp_creds = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
if gcp_creds and os.path.exists(gcp_creds):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = gcp_creds

from google.adk.agents.llm_agent import Agent

from tools.application_log_tools import (
    analyze_application_logs,
    get_recent_application_logs,
    detect_log_severity,
    send_critical_alert,
)
from tools.web_search import WEB_SEARCH_INSTRUCTION, get_web_search_tools
from prompts import APPLICATION_AGENT_INSTRUCTION
from tools.codebase_tools import CODEBASE_INSTRUCTION, get_codebase_tools

search_tools = get_web_search_tools()
codebase_tools = get_codebase_tools()

root_agent = Agent(
    name="application_agent",
    model=MODEL_NAME,
    description=(
        "Application Reliability Agent that adapts to the monitored workload and "
        "log formats, detects issues, and formulates evidence-based remediation."
    ),
    instruction=(
        APPLICATION_AGENT_INSTRUCTION
        + (WEB_SEARCH_INSTRUCTION if search_tools else "")
        + (CODEBASE_INSTRUCTION if codebase_tools else "")
    ),
    tools=[get_recent_application_logs, analyze_application_logs, detect_log_severity, send_critical_alert, *search_tools, *codebase_tools]
)
