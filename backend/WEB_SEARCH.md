# Optional Google Search grounding

The existing Azure application agent can call `web_search`, which runs a separate
Gemini research agent with ADK's built-in `google_search` tool. Azure continues
to handle log analysis and alerts. No additional package is needed.

## Configure

Use a Google AI API key with access to a Search-grounding-compatible Gemini model.
Add these settings to the environment file already loaded by the backend, or set
them in the terminal starting the backend:

```powershell
$env:WEB_SEARCH_ENABLED = 'true'
$env:GEMINI_API_KEY = '<your Google AI API key>'
$env:WEB_SEARCH_MODEL = 'gemini-2.5-flash'
python -m uvicorn main:app --reload --host 127.0.0.1 --port 5000
```

`GOOGLE_API_KEY` is also accepted. `GEMINI_API_KEY` takes precedence.
The research client explicitly uses Google AI API key authentication, not Vertex
AI, even if Vertex environment variables exist. Keep the Azure `MODEL_NAME` unchanged.
Model availability and grounding pricing/quota depend on your Google account.
Choose another supported Gemini model if the default is unavailable.

Do not commit any API keys. Restart the backend after configuration changes.
Search is disabled by default. Enabling without a Google key raises an explicit
configuration error. Credentials and model access are verified on the first call.
Set `WEB_SEARCH_ENABLED=false` and restart to restore the original tools/instructions.

## Privacy, behavior, and limits

- The Azure agent chooses when research is useful. Routine analyses need not search.
- Only supported public product and error-pattern identifiers are accepted.
  Queries are assembled from fixed strings, never raw logs or private identifiers.
- Supported technologies are Azure App Service/OpenAI, Compute Engine, Python,
  Node.js, PostgreSQL, MySQL, Redis, NGINX, and Kubernetes. Error categories include
  common HTTP errors, connection/startup failures, DNS/TLS, authentication,
  memory/disk exhaustion, deadlocks, exhausted pools, and missing modules.
- The research runner uses a fresh in-memory session with only the public query.
  It cannot access the main agent's log history or alert tools.
- Queries include official-documentation site hints and the researcher is asked
  to prefer official sources. These are preferences, not an enforced domain filter:
  Google controls grounding queries and can return citation redirect URLs.
- Two research attempts are allowed per analysis, with a 20-second timeout per
  attempt and one Gemini model call per attempt. Google's internal search-query
  count is not controlled by this limit. Failed attempts consume the budget.
- The budget uses ADK session state. The current backend creates a fresh main
  session for each analysis; preserve this isolation if session handling changes.
- Success requires actual search-query metadata, source chunks, and answer text;
  ungrounded model answers are explicitly reported as no results.
- Research text is bounded to 6,000 characters and at most three HTTPS source
  links are returned. No pages are fetched by application code.
- Errors are reported explicitly without logging API keys or provider response
  bodies. The main agent is instructed to continue and disclose limitations.
- Research is untrusted evidence, not instructions or a confirmed root cause.
  References stay inside the existing Remediation field; alert behavior is unchanged.
- Before external deployment, review Google's current grounding attribution and
  Search Suggestions display requirements. This integration returns source links,
  but does not render Google's Search Suggestions HTML in the existing UI.

## Validate

From the backend directory:

```powershell
.\.venv\Scripts\python.exe -B -m pytest tests\test_web_search.py -q
```

Tests mock the research model and make no external requests. For live validation,
configure a key, analyze a supported public error pattern, confirm `web_search`
is called, and check the grounded source links in remediation.
