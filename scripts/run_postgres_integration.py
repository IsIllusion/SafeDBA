"""Run integration tests in a new local PostgreSQL cluster, never in .env's DB.

Uses installed PostgreSQL binaries; does not install software. Paid model
calls require --live-model-eval and are limited to a small observer-only suite.
The generated cluster is removed only after its own server has stopped.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg

ROOT = Path(__file__).resolve().parents[1]
HIDDEN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def command(args, *, env=None, timeout=60):
    return subprocess.run(
        [str(value) for value in args], env=env, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
        creationflags=HIDDEN,
    )


def binary(directory, name):
    candidate = Path(directory) / (name + (".exe" if os.name == "nt" else "")) if directory else shutil.which(name)
    if not candidate or not Path(candidate).is_file():
        raise ValueError(f"PostgreSQL {name} not found; pass --pg-bin pointing at the installed bin directory.")
    return str(Path(candidate).resolve())


def isolated_environment(port, output, instance_id):
    # Do not allow inherited production flags, remote endpoints, credentials,
    # provider fallbacks or a user's .env to steer this integration run.
    env = {key: value for key, value in os.environ.items() if not key.startswith(("SAFEDBA_", "PG")) and key not in {"DEEPSEEK_API_KEY", "OPENAI_API_KEY"}}
    env.update({
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHON_DOTENV_DISABLED": "1",
        "SAFEDBA_SKIP_DOTENV": "1",
        "SAFEDBA_ENV": "benchmark", "SAFEDBA_RUN_POSTGRES_INTEGRATION": "1",
        "SAFEDBA_TEST_INSTANCE_ID": instance_id,
        "SAFEDBA_LLM_API_KEY": "integration-do-not-call",
        "SAFEDBA_LLM_PROVIDER": "openai_compatible",
        "SAFEDBA_LLM_BASE_URL": "http://127.0.0.1:1/v1",
        "SAFEDBA_LLM_MODEL": "scripted-integration-only",
        "SAFEDBA_LLM_MAX_RETRIES": "0",
        "SAFEDBA_AGENT_MEMORY_ENABLED": "false",
        "SAFEDBA_EXPERIENCE_CAPTURE_ENABLED": "false",
        "SAFEDBA_OTEL_ENABLED": "false",
        "SAFEDBA_AUDIT_LOG_PATH": str(output / "audit.jsonl"),
        "SAFEDBA_INCIDENT_STATE_DB_PATH": str(output / "incidents.sqlite3"),
        "SAFEDBA_AGENT_STATE_DB_PATH": str(output / "agent.sqlite3"),
        "SAFEDBA_EXPERIENCE_DB_PATH": str(output / "experience.sqlite3"),
        "SAFEDBA_RUNTIME_CONTROLS_PATH": str(output / "controls.json"),
        "SAFEDBA_RUNTIME_CONTROLS_REQUIRED": "true",
    })
    for prefix, role, password in [
        ("DB", "observer", "local-observer-only"),
        ("EXECUTOR_DB", "executor", "local-executor-only"),
        ("TERMINATOR_DB", "terminator", "local-terminator-only"),
    ]:
        for key, value in {"HOST": "127.0.0.1", "PORT": str(port), "NAME": "benchmark", "USER": f"safedba_{role}", "PASSWORD": password}.items():
            env[f"SAFEDBA_{prefix}_{key}"] = value
    return env


def live_model_environment(env):
    """Load only primary model settings, never the user's database settings."""
    from dotenv import dotenv_values
    local = dotenv_values(ROOT / ".env", interpolate=False)
    def setting(name, default=None):
        return os.environ.get(name, local.get(name, default))
    provider = setting("SAFEDBA_LLM_PROVIDER", "deepseek")
    model = setting("SAFEDBA_LLM_MODEL")
    key = setting("SAFEDBA_LLM_API_KEY") or setting("DEEPSEEK_API_KEY" if provider == "deepseek" else "OPENAI_API_KEY")
    endpoint = setting("SAFEDBA_LLM_BASE_URL") or ("https://api.deepseek.com" if provider == "deepseek" else None)
    if not key or not model or not endpoint:
        raise ValueError("Live evaluation requires an explicitly configured primary provider, model, endpoint and key.")
    from urllib.parse import urlsplit
    url = urlsplit(endpoint)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("Live provider endpoint must use HTTPS without URL credentials, queries or fragments.")
    result = {k: v for k, v in env.items() if not k.startswith("SAFEDBA_LLM_") and k not in {"SAFEDBA_EXECUTOR_DB_PASSWORD", "SAFEDBA_TERMINATOR_DB_PASSWORD", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"}}
    result.update({
        "SAFEDBA_PROCESS_ROLE": "agent", "SAFEDBA_ENV": "production",
        "SAFEDBA_LIVE_MODEL_EVAL": "1", "SAFEDBA_LLM_PROVIDER": provider,
        "SAFEDBA_LLM_MODEL": model, "SAFEDBA_LLM_API_KEY": key,
        "SAFEDBA_LLM_BASE_URL": endpoint, "SAFEDBA_LLM_MAX_RETRIES": "0",
        "SAFEDBA_LLM_REASONING_ENABLED": "false", "SAFEDBA_ENABLE_MUTATIONS": "false",
        "SAFEDBA_ALLOW_RUNTIME_ANALYSIS": "false", "SAFEDBA_ALLOW_BENCHMARK": "false",
    })
    return result


def run_live_evaluation(env, output):
    # Only this fixture orchestrator holds the disposable executor password.
    # The child Agent receives observation credentials and the selected model
    # key; its prompts contain generated fixture evidence, never .env data.
    live_env = live_model_environment(env)
    connection_args = dict(host="127.0.0.1", hostaddr="127.0.0.1", port=int(env["SAFEDBA_DB_PORT"]), dbname="benchmark", user="safedba_executor", password=env["SAFEDBA_EXECUTOR_DB_PASSWORD"], connect_timeout=5, options="-c statement_timeout=480000 -c lock_timeout=480000")
    with psycopg.connect(**connection_args) as blocker, psycopg.connect(**connection_args) as waiter:
        blocker.execute("SELECT id FROM public.integration_probe WHERE id=1 FOR UPDATE").fetchone()
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(waiter.execute, "SELECT id FROM public.integration_probe WHERE id=1 FOR UPDATE")
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                pids = blocker.execute("SELECT pg_blocking_pids(%s)", (waiter.info.backend_pid,)).fetchone()[0]
                if blocker.info.backend_pid in pids:
                    break
                time.sleep(0.05)
            else:
                raise RuntimeError("Live model lock fixture did not become ready.")
            result = command([sys.executable, "-B", ROOT / "src/live_evaluate.py", "--report", output / "live-model.json", "--blocker-pid", blocker.info.backend_pid, "--waiter-pid", waiter.info.backend_pid], env=live_env, timeout=420)
            # Never persist raw stderr: a provider may echo request details.
            print(result.stdout, end="", flush=True)
            report_path = output / "live-model.json"
            details = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {"status": "failed", "error_type": "MissingEvaluationReport"}
            return {"exit_code": result.returncode, **details}
        finally:
            blocker.rollback()
            try:
                future.result(timeout=10)
            finally:
                waiter.rollback()
                pool.shutdown(wait=True)


def source_fingerprint():
    digest = hashlib.sha256()
    paths = [*ROOT.glob("src/*.py"), *ROOT.glob("tests/integration/*.py"), ROOT / "tests/fixtures/postgres_integration.sql", Path(__file__)]
    for path in sorted(paths):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg-bin", help="Installed PostgreSQL bin directory containing initdb/pg_ctl/psql.")
    parser.add_argument("--repeat", type=int, default=1, help="Fresh test passes against the disposable instance, 1..10.")
    parser.add_argument("--live-model-eval", action="store_true", help="Opt in to at most 12 primary-provider requests on three synthetic read-only cases. No retries/fallback.")
    args = parser.parse_args(argv)
    if not 1 <= args.repeat <= 10:
        parser.error("--repeat must be between 1 and 10")
    # libpq can otherwise inherit PGSERVICE/PGHOSTADDR/PGOPTIONS even when a
    # connection supplies host, port and user explicitly. This affects only
    # this disposable runner process, never the invoking shell's environment.
    for key in list(os.environ):
        if key.startswith("PG"):
            del os.environ[key]
    initdb, pg_ctl, postgres = [binary(args.pg_bin, name) for name in ("initdb", "pg_ctl", "postgres")]
    run_id = str(uuid.uuid4())
    output = ROOT / "logs" / "integration-runs" / run_id
    output.mkdir(parents=True, exist_ok=False)
    cluster = Path(tempfile.mkdtemp(prefix="safedba-pg-")).resolve()
    # Cleanup is only ever allowed within the exact freshly created temp root.
    if cluster.parent != Path(tempfile.gettempdir()).resolve() or not cluster.name.startswith("safedba-pg-"):
        raise RuntimeError("Temporary cluster escaped the expected root.")
    data = cluster / "data"
    password_path = cluster / "bootstrap-password"
    password = secrets.token_urlsafe(32)
    password_path.write_text(password, encoding="utf-8")
    (output / "controls.json").write_text('{"version": 1}\n', encoding="utf-8")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    report = {
        "schema_version": 1, "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "source_sha256": source_fingerprint(), "live_llm_used": False,
        "postgres_version": command([postgres, "--version"]).stdout.strip(),
        "platform": sys.platform, "python": sys.version.split()[0],
        "passes": [], "status": "failed", "server_stopped": False,
    }
    started = time.monotonic()
    start_attempted = False
    try:
        initialized = command([initdb, "-D", data, "-U", "safedba_bootstrap", "--pwfile", password_path, "--auth=scram-sha-256", "--no-locale", "-E", "UTF8"])
        (output / "initdb.log").write_text(initialized.stdout + initialized.stderr, encoding="utf-8")
        if initialized.returncode:
            raise RuntimeError("initdb failed; see initdb.log")
        password_path.unlink()
        start_attempted = True
        # A detached PostgreSQL child may inherit pg_ctl's pipe handles on
        # Windows. Use a real log file so communicate() cannot wait forever
        # for EOF from a server that is supposed to stay alive.
        with (output / "pg_ctl-start.log").open("w", encoding="utf-8") as launch_log:
            launch = subprocess.run(
                [pg_ctl, "-D", str(data), "-l", str(output / "postgres.log"), "-o", f"-h 127.0.0.1 -p {port} -c max_connections=40", "-w", "-t", "20", "start"],
                stdout=launch_log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                timeout=30, creationflags=HIDDEN,
            )
        if launch.returncode:
            raise RuntimeError("Disposable server did not start; see postgres.log")
        bootstrap = dict(host="127.0.0.1", hostaddr="127.0.0.1", port=port, user="safedba_bootstrap", password=password, connect_timeout=5, options="-c statement_timeout=10000 -c lock_timeout=3000")
        with psycopg.connect(**bootstrap, dbname="postgres", autocommit=True) as conn:
            conn.execute("CREATE DATABASE benchmark")
        with psycopg.connect(**bootstrap, dbname="benchmark", autocommit=True) as conn:
            conn.execute((ROOT / "tests/fixtures/postgres_integration.sql").read_text(encoding="utf-8"))
            instance_id = str(conn.execute("SELECT instance_id FROM safedba_test_control.instance").fetchone()[0])
        report["instance_id"] = instance_id
        env = isolated_environment(port, output, instance_id)
        for number in range(1, args.repeat + 1):
            pass_start = time.monotonic()
            result = command([sys.executable, "-B", ROOT / "tests/integration/run_suite.py", "--report", output / f"pass-{number}.json"], env=env, timeout=180)
            (output / f"pass-{number}.log").write_text(result.stdout + result.stderr, encoding="utf-8")
            pass_report = output / f"pass-{number}.json"
            details = json.loads(pass_report.read_text(encoding="utf-8")) if pass_report.exists() else {}
            report["passes"].append({"number": number, "exit_code": result.returncode, "elapsed_seconds": round(time.monotonic() - pass_start, 3), **details})
            print(f"Pass {number}: exit={result.returncode}, tests={details.get('tests_run', 'unavailable')}", flush=True)
            if result.returncode != 0 or details.get("passed") is not True:
                break
        if len(report["passes"]) == args.repeat and all(p["exit_code"] == 0 and p.get("passed") is True for p in report["passes"]):
            report["status"] = "passed"
            if args.live_model_eval:
                live = run_live_evaluation(env, output)
                report["live_model_evaluation"] = {"exit_code": live["exit_code"], "status": live["status"], "request_count": live.get("request_count"), "report": "live-model.json"}
                report["live_llm_used"] = live.get("live_llm_used", False)
                if live["exit_code"] != 0 or live["status"] != "passed":
                    report["status"] = "failed"
    except Exception as exc:
        # In particular, a live evaluation setup/timeout failure must not
        # inherit the earlier successful deterministic-test status.
        report["status"] = "failed"
        report["error_type"] = type(exc).__name__
        if args.live_model_eval:
            partial_path = output / "live-model.json"
            try:
                partial = json.loads(partial_path.read_text(encoding="utf-8"))
                report["live_llm_used"] = partial.get("live_llm_used", None)
                report["live_model_evaluation"] = {"status": "interrupted", "report": "live-model.json", "error_type": type(exc).__name__}
            except (OSError, ValueError):
                # If the evaluator died before its first checkpoint, do not
                # assert that no billed request could have reached a provider.
                report["live_llm_used"] = None
        print(f"Integration run failed ({type(exc).__name__}); inspect the report and logs.", flush=True)
    finally:
        stopped = not start_attempted
        if start_attempted:
            try:
                shutdown = command([pg_ctl, "-D", data, "-m", "fast", "-w", "-t", "20", "stop"], timeout=30)
                status = command([pg_ctl, "-D", data, "status"], timeout=10)
                stopped = status.returncode == 3
                report["shutdown_exit_code"] = shutdown.returncode
            except Exception:
                stopped = False
        report["server_stopped"] = stopped
        if stopped:
            try:
                shutil.rmtree(cluster)
                report["cluster_removed"] = True
            except OSError:
                report["cluster_removed"] = False
                report["retained_cluster"] = str(cluster)
        else:
            report["status"] = "failed"
            report["retained_cluster"] = str(cluster)
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        (output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        print(f"Report: {output / 'report.json'}", flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
