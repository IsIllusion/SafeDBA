# Manual Verification Scripts

These scripts are manual integration and safety verification utilities for
SafeDBA. They are not automated unit tests.

For repeatable verification, prefer the disposable runner from the project
root:

```powershell
python scripts/run_postgres_integration.py --pg-bin "C:\Program Files\PostgreSQL\18\bin"
```

It creates its own test database and runs real PostgreSQL scenarios without
paid model calls. See [testing and evaluation](../../README.md#testing-and-evaluation)
for the platform requirements and the separate, opt-in real-model evaluation.
The scripts below use their configured environment; they do not provide that
automatic disposable-cluster boundary.

They may require:

- a running PostgreSQL instance
- SafeDBA database credentials
- an LLM API key
- prepared database state
- concurrent PostgreSQL sessions
- explicit human approval for controlled actions

Some scripts exercise database actions through SafeDBA's deterministic
executor.

## Safety

Do not run these scripts against a production database.

In particular, scripts involving backend termination may terminate a
PostgreSQL client backend after deterministic validation and explicit
human approval.

## Running

From the project root on PowerShell:

```powershell
$env:PYTHONPATH="$PWD\src"
python scripts\manual\verify_lock_waits.py
