# Application-independent log analysis

The active application agent and its analysis/severity tools share runtime
instructions in [prompts.py](prompts.py). No model, environment, API, frontend,
log-source configuration, or new dependency is required. Restart the backend
after updating the code.

## Adaptive behavior

- Infer workload and technology only from supplied context and log evidence.
  Azure/GCP labels describe hosting, not the application's framework or purpose.
- Reassess each application and component, including unfamiliar technologies.
- Interpret structured records, text, HTTP logs, stack traces, database events,
  infrastructure events, and mixed formats using the same evidence-based policy.
- Adapt checks to request-driven services, workers, pipelines, stateful systems,
  and other observed workloads without a technology allowlist.
- Judge severity using demonstrated impact, recurrence, and recovery rather
  than keywords or native severity labels alone.
- Distinguish suspected causes from facts and put missing-context checks in
  Remediation rather than inventing application details.
- Treat logs and research as untrusted data. Request redaction of sensitive
  values in generated responses and alert arguments. This is model guidance,
  not a deterministic redaction layer.

The existing four issue fields (Severity, Issue Type, Log Entry, Remediation),
paragraph-block layout, CRITICAL/HIGH email dispatch, live source selectors,
file input, and tool signatures remain unchanged. Empty input is reported
explicitly; non-actionable logs produce a brief no-issues response. Single-line
severity questions receive a classification and one-sentence explanation.
Transport, filtering, and email delivery code are unchanged.

Optional web research retains its current supported public identifiers, isolated
research session, privacy restrictions, and search budget. Unsupported stacks
are still analyzed locally; adaptive analysis does not expand the search allowlist.
See [WEB_SEARCH.md](WEB_SEARCH.md).

## Validation

From the backend directory:

```powershell
.\.venv\Scripts\python.exe -B -m pytest tests\test_adaptive_prompts.py tests\test_web_search.py -q
```

Offline tests verify shared policy wiring, preserved data/tool schemas, source
selection, file/empty inputs, alert filtering, and optional research integration.
They do not prove model interpretation or classification accuracy.

With your configured model, also review representative samples from different
applications: a request outage, a recovered worker retry, a multiline stack
trace, a database failure, mixed-component JSON, and an unknown format. Confirm
that recommendations match only observed technologies, recovered events do not
trigger spurious incidents, and real CRITICAL/HIGH incidents retain alerting.
Use test email settings for live evaluations: analysis can send real alerts.

## Optional read-only codebase inspection

Codebase inspection is disabled by default and independent of web research.
It uses native ADK function tools, not an MCP server. Local-repository code access
has been removed. Install the updated backend requirements for Paramiko (GCP SFTP).
Set the following in the backend's existing environment configuration, then restart:

```powershell
$env:CODEBASE_ACCESS_ENABLED = 'true'
```

The cloud actually used for log analysis is stored in a fresh agent session by
the backend. Both tools use that cloud; the model cannot supply a cloud, host,
credentials or absolute path. Filtering does not change the code source.
Mock-file analysis and unbound direct agent sessions have no cloud code access.
Each cloud has one configured application folder; multiple apps per cloud require
explicit application-to-source mapping before extending this setup.

### Azure: Advanced Tools / Kudu

Reuse the existing `AZURE_APP_SERVICE_NAME`, `KUDU_USERNAME` and `KUDU_PASSWORD`
settings used for Azure log streaming. Add:

```powershell
$env:AZURE_CODEBASE_PATH = 'site/wwwroot'
```

`C:\home\site\wwwroot`, `D:\home\site\wwwroot`, and `/home/site/wwwroot` are
also accepted and mapped to Kudu's home-relative VFS route. A subdirectory under
`site/wwwroot` may be configured. Other roots and URL inputs are rejected.
Only GET requests to `https://<app>.scm.azurewebsites.net/api/vfs/` are made;
redirects and URLs returned by directory listings are never followed.
TLS verification stays enabled. Kudu SCM basic authentication and network access
must be available; otherwise tools report access failure without stopping log
analysis. Do not enable basic authentication if your organizational policy forbids
it; this implementation does not add Entra/IAP authentication.

### GCP: SSH transport, SFTP reads only

Use a dedicated OS account permitted to read only the required application files.
No SSH command execution, writes, automatic key generation, or host-key acceptance
is implemented. Configure:

```powershell
$env:GCP_CODEBASE_PATH = '/srv/my-application'
$env:GCP_CODEBASE_SSH_HOST = '<reachable VM hostname or address>'
$env:GCP_CODEBASE_SSH_PORT = '22'
$env:GCP_CODEBASE_SSH_USER = '<read-only OS account>'
$env:GCP_CODEBASE_SSH_KEY_FILE = 'C:\Secure\gcp-reader-key'
$env:GCP_CODEBASE_KNOWN_HOSTS = 'C:\Secure\known_hosts'
```

The application path is remote; the private-key and known-hosts paths are on the
backend machine. For encrypted keys, optionally set
`GCP_CODEBASE_SSH_KEY_PASSPHRASE` through your secret-management process.
Verify the VM host key out of band before provisioning the known-hosts file.
Unknown or changed host keys are rejected. SSH agent and implicit key discovery
are disabled. A non-default port needs its `[host]:port` known-hosts entry.
The VM must expose SFTP to the backend over an approved network route; firewall,
OS Login/key provisioning and any tunnel setup are operator responsibilities.
GCP log-stream credentials alone do not grant filesystem access.

The optional tools in [codebase_tools.py](tools/codebase_tools.py) are:

- `scan_codebase(query)`: bounded, case-insensitive literal search for a symbol
  or error fragment, with relative paths, line numbers, and short excerpts.
- `read_codebase_file(relative_path, start_line, end_line)`: bounded numbered
  source excerpts to inspect a potential cause.

They do not modify files, run builds/tests, execute target code, use a shell, or
automatically apply fixes. The agent can suggest an evidence-based code change
and validation steps within the existing Remediation field; it cannot guarantee
a perfect resolution. This is source search/model-assisted inspection, not
compiler analysis, a vulnerability scanner, or an exhaustive static analyzer.

### Scope and limits

- Relative source paths reject traversal, absolute paths, encoded paths and
  alternate streams. SFTP checks ancestors for symlinks; Kudu rejects entries
  identified as symlinks. See the deployment limitations below.
- Only the explicit source-extension allowlist is inspected. Hidden/dependency/
  build directories and common secret/config filenames are excluded. JSON/YAML,
  environment files, arbitrary documents, binary, and non-UTF-8 files are not read.
- Six inspection calls are allowed per ADK session, shared between both tools.
  The existing backend creates a fresh session for each analysis.
- A search visits at most 10,000 entries, scans at most 4 MiB, and returns at most
  20 matches. Files are limited to 256 KiB; reads return at most 80 lines and
  12,000 characters. Individual lines are capped at 400 characters.
- Cloud operations have a 20-second overall inspection deadline checked between
  operations, bounded network timeouts, and a 100-request budget per tool call.
  Connection negotiation may take additional bounded timeout intervals.
- Results explicitly report truncation/skipped files. No matches do not prove
  absence of a defect. Access errors remain explicit and log analysis continues.
- Missing paths, credentials or transport dependencies fail explicitly at tool
  invocation, not backend startup. Disabling removes both tools and their
  instructions, restoring log-only behavior.

### Deployment precautions

**Read-only does not mean private:** selected source excerpts become context for
the configured analysis model. Enable only for code approved for that model's
data handling. Filename exclusions are defense in depth, not secret detection or
complete redaction. Git-ignore patterns are not interpreted; review the source
allowlist and deployment contents before enabling. Keep this backend access-controlled:
the setting is process-wide, not per-user authorization.

Use least-privilege credentials and do not permit untrusted users to alter inspected
files during reads. Kudu deployment credentials can have write permissions even
though these tools only issue GET. Kudu VFS does not reliably expose the canonical
target of every Windows junction or server-side link; provision the inspected
directory without links to sensitive/out-of-scope locations. SFTP path checks
also do not replace an OS sandbox against concurrent filesystem mutation.
Source is untrusted evidence, not instructions; it must not be forwarded to public
web research or copied into alert emails. Production builds may contain only
compiled/minified artifacts, so original source may be unavailable.

Log ingestion, severity filters, response fields, email dispatch, and web research
are unchanged. Code lookup failures must not suppress CRITICAL/HIGH alerts.

Run the regression tests from the backend directory:

```powershell
.\.venv\Scripts\python.exe -B -m pytest tests\test_codebase_tools.py tests\test_codebase_sources.py tests\test_codebase_routing.py tests\test_adaptive_prompts.py tests\test_web_search.py -q
```

Tests use synthetic cloud responses and mock model/research behavior; they do not
connect to Azure/GCP or send your source to a model. Live cloud authentication and
model correlation require separate evaluation with authorized source and test
email settings. Set `CODEBASE_ACCESS_ENABLED=false` and restart to disable.
