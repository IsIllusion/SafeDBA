"""Explicitly launched local executor. Requires its own OS identity/ACLs.

HTTP requests cannot create grants, choose DSNs, run SQL or select state paths.
The CLI grant command is reserved for an operator with worker-side access.
"""
import argparse
from copy import deepcopy
from contextlib import redirect_stdout
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
from pathlib import Path

from execution_protocol import MAX_BODY, canonical, digest, strict_json, validate_request
from execution_grants import ExecutionGrantStore, GrantDenied, preview_grant, require_review_digest


def database_identity():
    from db_tools import readonly_connection
    with readonly_connection() as conn:
        row = conn.execute("SELECT current_database(), (SELECT oid FROM pg_database WHERE datname=current_database()), inet_server_addr()::text, inet_server_port(), pg_postmaster_start_time()::text").fetchone()
    return dict(zip(("database", "database_oid", "server_address", "server_port", "postmaster_start"), row))


class ExecutorApplication:
    def __init__(self, store, grants):
        self.store, self.grants = store, grants

    def execute(self, payload):
        request = validate_request(payload)
        operation_id = request["operation_id"]
        try:
            from db_tools import verify_runtime_security
            from executor import execute_action_proposal
            from runtime_policy import require_operation
            require_operation("TERMINATE_BACKEND")
            verify_runtime_security()
            incident = self.store.load_incident(request["incident_id"])
            action = next(a for a in incident["actions"] if a["action_id"] == request["action_id"])
            if (
                action["proposal"].get("type") != "TERMINATE_BACKEND"
                or incident["plan_revision"] != request["plan_revision"]
                or action["operation_id"] != operation_id
                or action["state"] != "EXECUTING"
                or digest(action["proposal"]) != request["proposal_digest"]
                or not incident.get("approval")
                or incident["lease"]["owner"] != request["worker_id"]
            ):
                raise GrantDenied("Request does not match a pending approved execution intent.")
            grant = self.grants.claim(incident, action, database_identity(), operation_id)
        except Exception:
            # No call into the side-effect executor has occurred at this point.
            from audit import write_audit_log
            write_audit_log({"operation_id": operation_id, "status": "BLOCKED_EXECUTOR_GRANT", "incident_id": request["incident_id"], "action_id": request["action_id"]})
            return {"operation_id": operation_id, "status": "BLOCKED_INCIDENT_APPROVAL", "decision": "BLOCK", "executed": False, "approval_error_type": "IsolatedExecutionGateDenied"}
        approval = deepcopy(incident["approval"])
        approval["current_action_id"] = action["action_id"]
        context = {
            "kind": "LOCK_INCIDENT_EXECUTION", "store_path": str(self.store.path.resolve()),
            "worker_id": request["worker_id"], "incident_id": incident["incident_id"],
            "action_id": action["action_id"], "plan_revision": incident["plan_revision"],
            "isolated_grant": grant,
        }
        # Existing executor still checks fresh evidence, approval TTL, lease,
        # waiter scope and atomically claims the workflow action before SQL.
        with redirect_stdout(io.StringIO()):
            result = execute_action_proposal(action["proposal"], operation_id=operation_id, approval_context=approval, execution_context=context)
        self.grants.record_result(grant["grant_id"], result)
        return result


def make_handler(application, token):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            # Bound idle connections even before a complete request header.
            self.request.settimeout(5)
            super().setup()

        def log_message(self, *args):
            pass  # Never print headers, tokens or caller payloads.

        def do_POST(self):
            self.connection.settimeout(5)
            authorization = self.headers.get_all("Authorization", [])
            if len(authorization) != 1 or not hmac.compare_digest(authorization[0].encode("utf-8"), ("Bearer " + token).encode("utf-8")):
                self.send_error(401)
                return
            if self.path != "/execute" or self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                self.send_error(400)
                return
            try:
                length = int(self.headers["Content-Length"])
                if not 0 < length <= MAX_BODY:
                    raise ValueError("Invalid body size")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("Incomplete body")
                request = validate_request(strict_json(raw))
            except Exception:
                self.send_error(400)
                return
            try:
                result = application.execute(request)
                raw = canonical(result).encode()
                if len(raw) > MAX_BODY:
                    raise ValueError("Result exceeds wire budget")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except Exception:
                # No retry hint: an action or grant may already be consumed.
                self.send_error(500, "Execution outcome requires reconciliation")
    return Handler


def main(argv=None):
    import config
    if config.PROCESS_ROLE != "executor":
        raise SystemExit("Start with SAFEDBA_PROCESS_ROLE=executor and an operator-owned environment; .env is not loaded.")
    from workflow_store import SQLiteIncidentStore
    from db_tools import verify_runtime_security
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, required=True)
    approve = commands.add_parser("approve")
    approve.add_argument("--incident-id", required=True)
    approve.add_argument("--actor", required=True)
    approve.add_argument("--ttl", type=int, default=120)
    approve.add_argument("--confirm", action="store_true")
    approve.add_argument("--review-digest", help="Exact digest printed by the preceding operator preview.")
    args = parser.parse_args(argv)
    grant_path = os.getenv("SAFEDBA_EXECUTION_GRANT_DB_PATH", "")
    if not grant_path or not Path(grant_path).is_absolute():
        raise SystemExit("An absolute operator-owned SAFEDBA_EXECUTION_GRANT_DB_PATH is required.")
    if Path(grant_path).resolve() == config.INCIDENT_STATE_DB_PATH.resolve():
        raise SystemExit("Execution grants must not share the Agent workflow database.")
    verify_runtime_security()
    for privileged in (config.EXECUTOR_DB_CONFIG, config.TERMINATOR_DB_CONFIG):
        if any(privileged[key] != config.DB_CONFIG[key] for key in ("host", "port", "dbname")):
            raise SystemExit("Isolated worker roles must use the same fixed database endpoint.")
    store = SQLiteIncidentStore(config.INCIDENT_STATE_DB_PATH)
    grants = ExecutionGrantStore(grant_path)
    if args.command == "approve":
        incident = store.load_incident(args.incident_id)
        identity = database_identity()
        preview = preview_grant(incident, identity)
        print(json.dumps(preview, indent=2))
        if not args.confirm:
            raise SystemExit("Review the exact targets, then repeat with --confirm --review-digest <shown-digest> to issue grants.")
        try:
            require_review_digest(incident, identity, args.review_digest)
        except GrantDenied as exc:
            raise SystemExit(str(exc)) from None
        print(json.dumps(grants.issue(incident, identity, actor=args.actor, ttl_seconds=args.ttl)))
        return
    token = os.getenv("SAFEDBA_EXECUTOR_API_TOKEN", "")
    if len(token) < 32 or not 1 <= args.port <= 65535:
        raise SystemExit("A token of at least 32 characters and a valid explicit port are required.")
    with HTTPServer(("127.0.0.1", args.port), make_handler(ExecutorApplication(store, grants), token)) as server:
        server.timeout = 5
        print(f"Executor listening on 127.0.0.1:{args.port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
