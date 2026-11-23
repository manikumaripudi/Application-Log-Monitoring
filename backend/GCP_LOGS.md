# GCP multi-service log monitoring

Live Logs and Application Agent both offer a GCP service selector:
All GCP services, Compute Engine, BigQuery, Cloud Composer, GKE, and Dataflow.
Selecting Analyze in Agent carries the selected service, keyword and severity.

## Setup

Install the backend requirements, including google-cloud-logging. Configure
GCP_PROJECT_ID and optionally GOOGLE_APPLICATION_CREDENTIALS in the existing
backend environment. Without a credential file, Google Application Default
Credentials are used. Grant the identity Cloud Logging read/tail permissions;
Data Access audit logs can additionally require Private Logs Viewer permissions.
Do not commit credentials.

The GCP collector starts alongside Azure when a GCP project is configured and
LOG_SOURCE is not mock. Azure remains the default UI provider. Each provider
has independent connection handling; a GCP failure does not stop Azure.

## Collection and scope

One project-level Cloud Logging tail collects supported resource types and
service audit events, then routes each event into a bounded aggregate buffer
and its service buffer. Each SSE listener receives only its selected service.
The analysis endpoint receives a service-specific snapshot, not all GCP logs.
Changing a selector does not change other clients' selections or restart the
upstream stream. Buffers are in memory, not historical log storage.

| Service | Resource types / audit service |
|---|---|
| Compute Engine | gce_instance; compute.googleapis.com |
| BigQuery | bigquery_resource, bigquery_project, bigquery_dataset; bigquery.googleapis.com |
| Cloud Composer | cloud_composer_environment; composer.googleapis.com |
| GKE | k8s_container, k8s_pod, k8s_node, k8s_cluster, gke_cluster; container.googleapis.com |
| Dataflow | dataflow_step, dataflow_job; dataflow.googleapis.com |

Resource type takes precedence when classifying an event. Composer workload
logs emitted as Kubernetes resources appear under GKE, while Composer environment
logs and Composer audit events appear under Cloud Composer. Entries include a
service/resource label and available job, cluster, pod, environment and VM labels.
Structured/audit payloads are retained and newlines escaped for SSE delivery.

Logs must already reach Cloud Logging:

- Compute Engine: configure Ops Agent/application logging as needed.
- BigQuery: this reads Cloud Audit Logs, not arbitrary BigQuery table rows;
  access to job/query/data-access events depends on audit/IAM configuration.
- Composer: enable relevant environment/Airflow logs and audit logs.
- GKE: enable system/workload logging for the required clusters.
- Dataflow: enable appropriate job/worker logging and ensure jobs emit events.

The application does not configure these cloud services or backfill old logs.
A connected stream with an empty buffer does not prove a workload is healthy.
This is one configured project, not a multi-project inventory.

## Filters and compatibility

GCP_INSTANCE_ID limits only the Compute Engine branch, not other services.
GCP_LOG_FILTER is an optional additional Cloud Logging filter, ANDed with the
combined service filter; an overly narrow custom filter can exclude services.
Leave it blank to collect all supported services.

The old exact default `resource.type="gce_instance"` is treated as a legacy
default and does not restrict collection to VMs. To intentionally restrict
collection, use an explicit custom expression such as
`(resource.type="gce_instance")`.

Existing GCP API requests without a service selector default to all services:

- GET /api/application/logs/stream?source=gcp&gcp_service=bigquery
- GET /api/application/logs/status?source=gcp&gcp_service=dataflow
- POST /api/application/analyze: text=analyze:live:gcp,
  cloud_provider=gcp, gcp_service=cloud_composer

Valid IDs: all, compute_engine, bigquery, cloud_composer, gke, dataflow.
Invalid HTTP selectors return validation errors. Azure/mock request shapes,
response fields, history, severity filtering and alert tools remain unchanged.
The source label in analysis history includes the selected GCP service.

VM SFTP code inspection is available only for explicit Compute Engine analyses;
it is not evidence for BigQuery, Composer, GKE, Dataflow, or mixed service logs.
Web research supports all five public GCP product identifiers.

## Validation

Run the focused mocked tests from the backend directory:

```powershell
.\.venv\Scripts\python.exe -B -m pytest tests\test_gcp_services.py tests\test_codebase_routing.py -q
```

For a live check, emit a new event from each configured service, verify it appears
in its own selector and All GCP services, then analyze it. Verify switching services
does not replay another service's paused logs. This needs real project access;
mocked tests cannot verify cloud IAM, enabled logging or event delivery.
