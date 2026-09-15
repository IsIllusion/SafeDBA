"""Opt-in RAG A/B evaluation: real configured model, synthetic observations only."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
FIXTURE = ROOT / "benchmarks/retrieval/agent_ab_cases.json"
MODEL_KEYS = {
    "SAFEDBA_LLM_PROVIDER",
    "SAFEDBA_LLM_MODEL",
    "SAFEDBA_LLM_BASE_URL",
    "SAFEDBA_LLM_API_KEY",
    "DEEPSEEK_API_KEY",
}


def isolated_environment():
    from dotenv import dotenv_values

    # Whitelist only the primary model configuration; never forward a business
    # DB target, privileged credential, fallback key, tracing config or proxy.
    local = dotenv_values(ROOT / ".env", interpolate=False)
    values = {key: os.environ.get(key, local.get(key)) for key in MODEL_KEYS}
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        in {
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "PATH",
            "PATHEXT",
            "TEMP",
            "TMP",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
        }
    }
    env.update({key: value for key, value in values.items() if value})
    env.update(
        {
            "SAFEDBA_RAG_AB_WORKER": "1",
            "SAFEDBA_SKIP_DOTENV": "1",
            "SAFEDBA_PROCESS_ROLE": "agent",
            "SAFEDBA_ENV": "benchmark",
            "SAFEDBA_KNOWLEDGE_ENABLED": "false",
            "SAFEDBA_ENABLE_MUTATIONS": "false",
            "SAFEDBA_ALLOW_RUNTIME_ANALYSIS": "false",
            "SAFEDBA_ALLOW_BENCHMARK": "false",
            "SAFEDBA_MEMORY_ENABLED": "false",
            "SAFEDBA_EXPERIENCE_CAPTURE_ENABLED": "false",
            "LANGSMITH_TRACING": "false",
            "LANGCHAIN_TRACING_V2": "false",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return env


def worker(output, repeats):
    if (
        os.getenv("SAFEDBA_RAG_AB_WORKER") != "1"
        or os.getenv("SAFEDBA_SKIP_DOTENV") != "1"
        or os.getenv("SAFEDBA_PROCESS_ROLE") != "agent"
    ):
        raise ValueError("Use the isolated launcher.")
    import config
    from llm_provider import OpenAICompatibleProvider
    from rag_ab_evaluate import (
        ABProvider,
        evaluate,
        load_fixture,
        MAX_OUTPUT_TOKENS,
        MAX_TURNS,
    )

    parsed = urlsplit(config.LLM_BASE_URL or "https://api.deepseek.com")
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
    ):
        raise ValueError("Evaluation requires a credential-free HTTPS model endpoint.")
    fixture, _, _ = load_fixture(FIXTURE)
    provider = OpenAICompatibleProvider(
        provider_name=config.LLM_PROVIDER,
        api_key=config.LLM_API_KEY,
        model=config.LLM_MODEL,
        base_url=config.LLM_BASE_URL,
        reasoning_enabled=False,
        reasoning_effort="medium",
        timeout_seconds=35,
        max_retries=0,
        max_completion_tokens=MAX_OUTPUT_TOKENS,
    )
    budget = ABProvider(
        provider,
        max_requests=len(fixture["cases"]) * 2 * MAX_TURNS * repeats,
        live_llm=True,
    )
    output_path = output / "report.json"

    def progress(report, row):
        report["endpoint_host"] = parsed.hostname
        report["provider"] = config.LLM_PROVIDER
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        grade = row.get("grade", {})
        print(
            f"repeat={row['repeat']} case={row['id']} rag={row['arm']} "
            f"pass={grade.get('passed')} facts={grade.get('fact_hits')}/{grade.get('fact_count')} "
            f"requests={row.get('request_count', 0)}",
            flush=True,
        )

    try:
        report = evaluate(FIXTURE, budget, repeats=repeats, on_progress=progress)
        report.update(endpoint_host=parsed.hostname, provider=config.LLM_PROVIDER)
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "summary": report["summary"],
                    "token_usage": report["token_usage"],
                    "report": str(output_path),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0 if report["status"] == "completed" else 1
    finally:
        provider.client.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Explicitly allow billed primary-model API calls with synthetic data.",
    )
    parser.add_argument("--repeat", type=int, choices=(1, 2), default=1)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--output", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    from rag_ab_evaluate import load_fixture, fingerprint

    fixture, bundle, _ = load_fixture(FIXTURE)
    if not args.live:
        print(
            json.dumps(
                {
                    "status": "validated_no_model_calls",
                    "cases": len(fixture["cases"]),
                    "bundle_sha256": bundle["sha256"],
                    "suite_sha256": fingerprint([FIXTURE]),
                }
            )
        )
        return 0
    if args.worker:
        return worker(Path(args.output), args.repeat)
    output = ROOT / "logs/rag-ab" / str(uuid.uuid4())
    output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "suite_sha256": fingerprint([FIXTURE]),
        "bundle_sha256": bundle["sha256"],
        "launcher_sha256": fingerprint([Path(__file__)]),
        "repeats": args.repeat,
        "max_requests": len(fixture["cases"]) * 8 * args.repeat,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(
        f"Starting isolated paired evaluation; max_requests={manifest['max_requests']}; output={output}",
        flush=True,
    )
    process = subprocess.run(
        [
            sys.executable,
            "-B",
            str(Path(__file__).resolve()),
            "--live",
            "--worker",
            "--repeat",
            str(args.repeat),
            "--output",
            str(output),
        ],
        env=isolated_environment(),
        cwd=ROOT,
        timeout=1800,
    )
    return process.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Never print a provider exception/traceback that could echo credentials.
        print(
            json.dumps({"status": "failed", "error_type": type(exc).__name__}),
            flush=True,
        )
        raise SystemExit(1)
