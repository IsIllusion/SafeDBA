"""Real subprocess credential separation; OS account ACLs require deployment QA."""
from contextlib import closing
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).parent))
import test_postgres_incidents as fixtures

ROOT = fixtures.ROOT
HIDDEN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


@unittest.skipUnless(fixtures.RUN_INTEGRATION, "Opt in to disposable PostgreSQL integration.")
class IsolatedExecutorIntegrationTests(unittest.TestCase):
    def setUp(self):
        fixtures.PostgreSQLIncidentIntegrationTests.setUp(self)

    def environment(self, role, port):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("SAFEDBA_LLM_", "PG")) and k not in {"DEEPSEEK_API_KEY", "OPENAI_API_KEY"}}
        env.update({
            "SAFEDBA_PROCESS_ROLE": role, "SAFEDBA_SKIP_DOTENV": "1",
            "SAFEDBA_INCIDENT_STATE_DB_PATH": str(self.store.path),
            "SAFEDBA_AUDIT_LOG_PATH": str(self.audit_path),
            "SAFEDBA_RUNTIME_CONTROLS_PATH": str(self.controls),
            "SAFEDBA_EXECUTION_GRANT_DB_PATH": str(self.store.path.parent / "operator-grants.sqlite3"),
            "SAFEDBA_EXECUTOR_API_TOKEN": self.token,
            "SAFEDBA_EXECUTOR_URL": f"http://127.0.0.1:{port}",
        })
        env.pop("SAFEDBA_EXECUTOR_DB_PASSWORD", None)
        if role == "agent":
            for key in ("SAFEDBA_EXECUTOR_DB_PASSWORD", "SAFEDBA_TERMINATOR_DB_PASSWORD", "SAFEDBA_EXECUTION_GRANT_DB_PATH"):
                env.pop(key, None)
        return env

    def start_worker(self, *, lose_response=False):
        self.token = secrets.token_urlsafe(32)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = self.environment("executor", port)
        log_path = self.store.path.parent / "worker.log"
        log = self.contexts.enter_context(log_path.open("w", encoding="utf-8"))
        code = f"import sys; sys.path.insert(0, {str(ROOT / 'src')!r}); import executor_worker; "
        if lose_response:
            code += "import os; original=executor_worker.ExecutorApplication.execute; "
            code += "executor_worker.ExecutorApplication.execute=lambda self, request: (original(self,request),os._exit(33))[0]; "
        code += f"executor_worker.main(['serve','--port',{str(port)!r}])"
        process = subprocess.Popen([sys.executable, "-B", "-c", code], env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=HIDDEN)
        def stop():
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        self.addCleanup(stop)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            output = log_path.read_text(encoding="utf-8")
            if "Executor listening" in output:
                return port, env
            if process.poll() is not None:
                self.fail("Worker failed to start: " + output)
            time.sleep(0.05)
        self.fail("Worker startup timed out")

    def grant(self, incident, env):
        command = [sys.executable, "-B", str(ROOT / "src/executor_worker.py"), "approve", "--incident-id", incident["incident_id"], "--actor", "integration-operator"]
        preview = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15, creationflags=HIDDEN)
        self.assertNotEqual(preview.returncode, 0)
        reviewed_digest = json.loads(preview.stdout)["review_digest"]
        result = subprocess.run([*command, "--confirm", "--review-digest", reviewed_digest], env=env, capture_output=True, text=True, timeout=15, creationflags=HIDDEN)
        self.assertEqual(result.returncode, 0, result.stderr)

    def run_agent_workflow(self, incident, port):
        code = f"""
import sys, json, io
from contextlib import redirect_stdout
sys.path.insert(0, {str(ROOT / 'src')!r})
import config
from incident_workflow import run_lock_incident
approvals=[]
with redirect_stdout(io.StringIO()):
    result=run_lock_incident({incident['incident_id']!r}, approval_decider=lambda v: (approvals.append(v), True)[1])
print(json.dumps({{'result':result, 'approval_count':len(approvals), 'has_privileged_password':any(c['password'] for c in (config.EXECUTOR_DB_CONFIG,config.TERMINATOR_DB_CONFIG))}}))
"""
        run = subprocess.run([sys.executable, "-B", "-c", code], env=self.environment("agent", port), capture_output=True, text=True, timeout=45, creationflags=HIDDEN)
        self.assertEqual(run.returncode, 0, run.stderr)
        value = json.loads(run.stdout)
        self.assertFalse(value["has_privileged_password"])
        return value

    def test_three_locks_cross_process_complete_with_one_workflow_approval(self):
        incident = fixtures.PostgreSQLIncidentIntegrationTests.create(self, 3)
        port, env = self.start_worker()
        self.grant(incident, env)
        value = self.run_agent_workflow(incident, port)
        result = value["result"]
        self.assertEqual(result["state"], "COMPLETED", result.get("last_error"))
        self.assertEqual(value["approval_count"], 1)
        self.assertEqual([a["state"] for a in result["actions"]], ["SUCCEEDED"] * 3)
        self.assertEqual(fixtures.db_tools.get_lock_graph_snapshot()["rows"], [])
        with closing(sqlite3.connect(env["SAFEDBA_EXECUTION_GRANT_DB_PATH"])) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM grants WHERE state='RESULT_RECORDED'").fetchone()[0], 3)
        self.assertEqual(self.run_agent_workflow(incident, port)["approval_count"], 0)

    def test_workflow_approval_alone_cannot_authorize_isolated_execution(self):
        incident = fixtures.PostgreSQLIncidentIntegrationTests.create(self)
        port, _ = self.start_worker()
        result = self.run_agent_workflow(incident, port)["result"]
        self.assertEqual(result["state"], "AWAITING_REAPPROVAL")
        self.assertEqual(len(self.locks.wait_for(1)), 1)

    def test_changed_proposal_after_operator_grant_is_denied(self):
        incident = fixtures.PostgreSQLIncidentIntegrationTests.create(self)
        port, env = self.start_worker()
        self.grant(incident, env)
        value = dict(incident["actions"][0]["proposal"], reason="Changed after operator review")
        with closing(sqlite3.connect(self.store.path)) as conn:
            conn.execute("UPDATE actions SET proposal_json=? WHERE action_id=?", (json.dumps(value), incident["actions"][0]["action_id"]))
            conn.commit()
        result = self.run_agent_workflow(incident, port)["result"]
        self.assertEqual(result["state"], "AWAITING_REAPPROVAL")
        self.assertEqual(len(self.locks.wait_for(1)), 1)

    def test_worker_exit_after_effect_requires_review_without_second_execution(self):
        incident = fixtures.PostgreSQLIncidentIntegrationTests.create(self)
        port, env = self.start_worker(lose_response=True)
        self.grant(incident, env)
        result = self.run_agent_workflow(incident, port)["result"]
        # A transport exception is deliberately a manual-review terminal
        # state, unlike a resumable crash before its result checkpoint.
        self.assertEqual(result["state"], "REVIEW_REQUIRED")
        resumed = self.run_agent_workflow(incident, port)["result"]
        self.assertEqual(resumed["actions"][0]["state"], "IN_DOUBT")
        self.assertEqual(resumed["actions"][0]["attempt_count"], 1)
        with closing(sqlite3.connect(env["SAFEDBA_EXECUTION_GRANT_DB_PATH"])) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM grants WHERE state='RESULT_RECORDED'").fetchone()[0], 1)
        self.assertEqual(fixtures.db_tools.get_lock_graph_snapshot()["rows"], [])


if __name__ == "__main__":
    unittest.main()
