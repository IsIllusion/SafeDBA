from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import http.client
from http.server import HTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from execution_grants import ExecutionGrantStore, GrantDenied, preview_grant, require_review_digest
from execution_protocol import MAX_BODY, canonical, digest, strict_json, validate_request
from execution_client import execute_remote, RemoteExecutionUncertain
from executor_worker import make_handler


def sample():
    action = {"action_id": str(uuid.uuid4()), "state": "PLANNED", "proposal": {"type": "TERMINATE_BACKEND", "blocker_pid": 123}, "target": {"blocker_pid": 123}, "approved_waiters": [{"blocked_pid": 456}], "allowed_blocked_pids": [456]}
    return {"incident_id": str(uuid.uuid4()), "plan_revision": 1, "actions": [action]}


def request():
    return {"version": 1, "incident_id": str(uuid.uuid4()), "action_id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "worker_id": "worker-1", "plan_revision": 1, "proposal_digest": "a" * 64}


class ProcessProfileTests(unittest.TestCase):
    def run_profile(self, role, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("SAFEDBA_", "PG")) and k not in {"DEEPSEEK_API_KEY", "OPENAI_API_KEY"}}
        env.update(SAFEDBA_PROCESS_ROLE=role, **extra)
        code = (
            "import sys,types; m=types.ModuleType('dotenv'); "
            "m.load_dotenv=lambda *a,**k: print('UNEXPECTED_DOTENV'); sys.modules['dotenv']=m; "
            f"sys.path.insert(0,{str(ROOT / 'src')!r}); import config; "
            "print(config.EXECUTOR_DB_CONFIG['password'] is None, config.TERMINATOR_DB_CONFIG['password'] is None)"
        )
        return subprocess.run([sys.executable, "-B", "-c", code], env=env, capture_output=True, text=True, timeout=10)

    def test_agent_skips_dotenv_and_has_no_privileged_password(self):
        result = self.run_profile("agent")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "True True")

    def test_agent_rejects_privileged_or_ambient_credentials(self):
        for name in ("SAFEDBA_EXECUTOR_DB_PASSWORD", "SAFEDBA_TERMINATOR_DB_PASSWORD", "PGPASSFILE", "PGSERVICE"):
            with self.subTest(name=name):
                result = self.run_profile("agent", **{name: "test-only"})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("must not receive", result.stderr)

    def test_executor_requires_explicit_credentials_and_no_model_key(self):
        self.assertNotEqual(self.run_profile("executor").returncode, 0)
        credentials = dict(SAFEDBA_TERMINATOR_DB_PASSWORD="test-only")
        result = self.run_profile("executor", **credentials)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "True False")
        self.assertNotIn("UNEXPECTED_DOTENV", result.stdout)
        self.assertNotEqual(self.run_profile("executor", **credentials, DEEPSEEK_API_KEY="test-only").returncode, 0)
        self.assertNotEqual(self.run_profile("executor", **credentials, SAFEDBA_EXECUTOR_DB_PASSWORD="test-only").returncode, 0)

    def test_worker_cannot_open_maintenance_connection(self):
        import config
        import db_tools
        with patch.object(config, "PROCESS_ROLE", "executor"), patch.object(db_tools.psycopg, "connect") as connect:
            with self.assertRaises(RuntimeError):
                with db_tools.executor_connection():
                    pass
            connect.assert_not_called()

    def test_agent_connection_contexts_refuse_before_connecting(self):
        import config
        import db_tools
        with patch.object(config, "PROCESS_ROLE", "agent"), patch.object(db_tools.psycopg, "connect") as connect:
            for context in (db_tools.executor_connection, db_tools.terminator_connection):
                with self.assertRaises(RuntimeError):
                    with context():
                        pass
            connect.assert_not_called()


class GrantTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1000.0
        self.grants = ExecutionGrantStore(Path(self.directory.name) / "operator.sqlite3", clock=lambda: self.now)
        self.incident = sample()
        self.target = {"database": "benchmark", "postmaster_start": "one"}

    def issue(self):
        return self.grants.issue(self.incident, self.target, actor="operator", ttl_seconds=10)

    def test_operator_confirmation_is_bound_to_previously_reviewed_scope(self):
        reviewed = preview_grant(self.incident, self.target)["review_digest"]
        require_review_digest(self.incident, self.target, reviewed)
        with self.assertRaises(GrantDenied):
            require_review_digest(self.incident, self.target, None)
        self.incident["actions"][0]["proposal"]["blocker_pid"] = 999
        with self.assertRaises(GrantDenied):
            require_review_digest(self.incident, self.target, reviewed)

    def claim(self, operation_id=None):
        return self.grants.claim(self.incident, self.incident["actions"][0], self.target, operation_id or str(uuid.uuid4()))

    def test_grant_is_single_use_and_result_does_not_allow_reissue(self):
        self.issue()
        op = str(uuid.uuid4())
        grant = self.claim(op)
        self.grants.record_result(grant["grant_id"], {"operation_id": op, "status": "SUCCEEDED"})
        with self.assertRaises(GrantDenied):
            self.claim(op)
        with self.assertRaises(GrantDenied):
            self.issue()

    def test_missing_expired_and_wrong_target_grants_are_denied(self):
        with self.assertRaises(GrantDenied):
            self.claim()
        self.issue()
        self.target["postmaster_start"] = "restarted"
        with self.assertRaises(GrantDenied):
            self.claim()
        self.target["postmaster_start"] = "one"
        self.now += 10
        with self.assertRaises(GrantDenied):
            self.claim()

    def test_modified_agent_state_does_not_expand_operator_grant(self):
        self.issue()
        original = deepcopy(self.incident)
        for key, value in (("target", {"blocker_pid": 999}), ("approved_waiters", [{"blocked_pid": 999}]), ("proposal", {"type": "TERMINATE_BACKEND", "blocker_pid": 999})):
            with self.subTest(key=key):
                self.incident = deepcopy(original)
                self.incident["actions"][0][key] = value
                with self.assertRaises(GrantDenied):
                    self.claim()

    def test_concurrent_claim_has_exactly_one_winner(self):
        self.issue()
        def attempt(_):
            try:
                self.claim()
                return True
            except GrantDenied:
                return False
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(attempt, range(8))), 1)

    def test_invalid_ttl_or_non_lock_action_cannot_be_granted(self):
        for ttl in (0, 301, True, float("nan")):
            with self.assertRaises(ValueError):
                self.grants.issue(self.incident, self.target, actor="operator", ttl_seconds=ttl)
        self.incident["actions"][0]["proposal"]["type"] = "CREATE_INDEX"
        with self.assertRaises(GrantDenied):
            self.issue()


class ProtocolTests(unittest.TestCase):
    def test_strict_payload_rejects_ambiguous_or_unexpected_values(self):
        for body in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e999}', b' ' * (MAX_BODY + 1)):
            with self.assertRaises(ValueError):
                strict_json(body)
        for extra in ({"sql": "DROP TABLE x"}, {"dsn": "other"}, {"store_path": "other"}, {"version": True}, {"operation_id": 1}):
            with self.assertRaises(ValueError):
                validate_request({**request(), **extra})

    def test_http_auth_and_validation_precede_application(self):
        app = Mock()
        app.execute.return_value = {"status": "blocked"}
        server = HTTPServer(("127.0.0.1", 0), make_handler(app, "t" * 32))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for body, token, status in ((canonical(request()), "wrong", 401), ('{"sql":"SELECT 1"}', "t" * 32, 400), ('{"version":1,"version":1}', "t" * 32, 400)):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                try:
                    # Deliver the small test frame in one write. With separate
                    # header/body writes, Windows may reset the socket when the
                    # server correctly rejects authentication before reading the
                    # body, racing the test's attempt to read the 401 response.
                    chunks = []
                    with patch.object(connection, "send", chunks.append):
                        connection.request("POST", "/execute", body=body, headers={"Authorization": "Bearer " + token})
                    connection.send(b"".join(chunks))
                    response = connection.getresponse()
                    response.read()
                    self.assertEqual(response.status, status)
                finally:
                    connection.close()
            app.execute.assert_not_called()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def test_remote_request_contains_only_references_and_never_retries(self):
        value = request()
        proposal = {"type": "TERMINATE_BACKEND", "blocker_pid": 123}
        context = {**value, "store_path": "PRIVATE_PATH", "kind": "LOCK_INCIDENT_EXECUTION"}
        with patch.dict(os.environ, {"SAFEDBA_EXECUTOR_URL": "http://127.0.0.1:54321", "SAFEDBA_EXECUTOR_API_TOKEN": "t" * 32}), patch("runtime_policy.require_operation"), patch("execution_client.http.client.HTTPConnection") as factory:
            connection = factory.return_value
            connection.getresponse.side_effect = OSError("lost result")
            with self.assertRaises(RemoteExecutionUncertain):
                execute_remote(proposal, operation_id=value["operation_id"], approval_context={"private": "APPROVAL"}, execution_context=context)
            connection.request.assert_called_once()
            body = json.loads(connection.request.call_args.kwargs["body"])
            self.assertEqual(set(body), set(value))
            self.assertEqual(body["proposal_digest"], digest(proposal))
            self.assertNotIn("PRIVATE_PATH", str(body))
            connection.close.assert_called_once()

    def test_wrong_result_operation_id_is_uncertain_not_success(self):
        value = request()
        with patch.dict(os.environ, {"SAFEDBA_EXECUTOR_URL": "http://127.0.0.1:54321", "SAFEDBA_EXECUTOR_API_TOKEN": "t" * 32}), patch("runtime_policy.require_operation"), patch("execution_client.http.client.HTTPConnection") as factory:
            response = factory.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = canonical({"operation_id": str(uuid.uuid4()), "status": "SUCCEEDED"}).encode()
            with self.assertRaises(RemoteExecutionUncertain):
                execute_remote({"type": "TERMINATE_BACKEND"}, operation_id=value["operation_id"], approval_context={"approved": True}, execution_context=value)


if __name__ == "__main__":
    unittest.main()
