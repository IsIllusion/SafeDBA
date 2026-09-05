"""No privileged credentials and no retry on an uncertain execution result."""
import http.client
import os
from urllib.parse import urlsplit
from execution_protocol import MAX_BODY, canonical, digest, strict_json, validate_request


class RemoteExecutionUncertain(RuntimeError):
    pass


def execute_remote(proposal, *, operation_id=None, approval_context=None, execution_context=None):
    if not isinstance(proposal, dict) or proposal.get("type") != "TERMINATE_BACKEND" or not approval_context or not execution_context:
        return {"operation_id": operation_id, "status": "BLOCKED_INCIDENT_APPROVAL", "decision": "BLOCK", "executed": False, "errors": ["Isolated execution supports persisted lock workflows only."]}
    from runtime_policy import require_operation
    require_operation("TERMINATE_BACKEND")
    request = validate_request({
        "version": 1, "incident_id": execution_context["incident_id"],
        "action_id": execution_context["action_id"], "operation_id": operation_id,
        "worker_id": execution_context["worker_id"], "plan_revision": execution_context["plan_revision"],
        "proposal_digest": digest(proposal),
    })
    endpoint = urlsplit(os.getenv("SAFEDBA_EXECUTOR_URL", ""))
    token = os.getenv("SAFEDBA_EXECUTOR_API_TOKEN", "")
    if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or not endpoint.port or endpoint.username or endpoint.password or endpoint.path not in {"", "/"} or endpoint.query or endpoint.fragment or len(token) < 32:
        raise ValueError("Isolated executor requires an explicit loopback HTTP endpoint and a token of at least 32 characters.")
    connection = http.client.HTTPConnection("127.0.0.1", endpoint.port, timeout=30)
    try:
        connection.request("POST", "/execute", body=canonical(request).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        response = connection.getresponse()
        raw = response.read(MAX_BODY + 1)
        if response.status != 200:
            raise RemoteExecutionUncertain("Executor transport did not return a confirmed result; do not retry automatically.")
        result = strict_json(raw)
        if not isinstance(result, dict) or result.get("operation_id") != operation_id or not isinstance(result.get("status"), str):
            raise RemoteExecutionUncertain("Executor response identity is invalid; reconciliation is required.")
        return result
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise RemoteExecutionUncertain("Executor result unavailable; reconciliation is required before any retry.") from exc
    finally:
        connection.close()
