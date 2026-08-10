# Manual Verification Scripts

These scripts are manual integration and safety verification utilities for
SafeDBA. They are not automated unit tests.

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