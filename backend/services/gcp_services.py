import json
from typing import Literal
from collections.abc import Mapping

from google.cloud.audit.audit_log_pb2 import AuditLog
from google.protobuf.json_format import MessageToDict

GcpService = Literal["all", "compute_engine", "bigquery", "cloud_composer", "gke", "dataflow"]

GCP_SERVICES = {
    "compute_engine": ("Compute Engine", ("gce_instance",)),
    "bigquery": ("BigQuery", ("bigquery_resource", "bigquery_project", "bigquery_dataset")),
    "cloud_composer": ("Cloud Composer", ("cloud_composer_environment",)),
    "gke": ("GKE", ("k8s_container", "k8s_pod", "k8s_node", "k8s_cluster", "gke_cluster")),
    "dataflow": ("Dataflow", ("dataflow_step", "dataflow_job")),
}
SERVICE_NAMES = {
    "bigquery.googleapis.com": "bigquery",
    "composer.googleapis.com": "cloud_composer",
    "container.googleapis.com": "gke",
    "dataflow.googleapis.com": "dataflow",
    "compute.googleapis.com": "compute_engine",
}


def validate_service(service: str) -> str:
    if service != "all" and service not in GCP_SERVICES:
        raise ValueError("Unsupported GCP service.")
    return service


def service_label(service: str) -> str:
    validate_service(service)
    return "All GCP services" if service == "all" else GCP_SERVICES[service][0]


def build_log_filter(extra_filter: str = "", instance_id: str = "") -> str:
    branches = []
    for service, (_, resources) in GCP_SERVICES.items():
        resource_filter = " OR ".join(f'resource.type="{resource}"' for resource in resources)
        api = next(name for name, key in SERVICE_NAMES.items() if key == service)
        branch = f'({resource_filter} OR protoPayload.serviceName="{api}")'
        if service == "compute_engine" and instance_id:
            branch = f"({branch} AND resource.labels.instance_id={json.dumps(instance_id)})"
        branches.append(branch)
    combined = "(" + " OR ".join(branches) + ")"
    # Older installations used this as the default, not an intentional global filter.
    if extra_filter.strip() and extra_filter.strip() != 'resource.type="gce_instance"':
        combined = f"({combined}) AND ({extra_filter})"
    return combined


def classify_entry(entry) -> str | None:
    resource_type = entry.resource.type
    for service, (_, resources) in GCP_SERVICES.items():
        if resource_type in resources:
            return service
    return SERVICE_NAMES.get(audit_payload(entry).get("serviceName", ""))


def audit_payload(entry) -> dict:
    payload = entry.proto_payload
    if isinstance(payload, Mapping):
        return dict(payload)
    audit = AuditLog()
    if payload.Is(audit.DESCRIPTOR):
        payload.Unpack(audit)
        return MessageToDict(audit)
    return {}
