# Real-model evaluation baseline — 2026-09-05

[Project overview](../README.md) · [Change history](../CHANGELOG.md)

## What actually ran

- Provider: configured DeepSeek HTTPS endpoint, model `deepseek-v4-flash`.
- Engine: PostgreSQL 18.4; Windows, Python 3.12.14.
- Isolated fixture: new loopback cluster, random port, generated instance UUID,
  dedicated observer/executor/terminator roles and 20 synthetic rows.
- Real model and real database tools, not scripted model responses. The model
  performed diagnosis only with observer credentials and production deny rules.
- A separate fixture process created a real open transaction and lock waiter;
  the Agent did not receive its privileged password. The lock remained in
  place throughout evaluation, as required for a non-mutating diagnosis run.
- No retry, fallback, memory load, experience capture, fine-tuning or automatic
  prompt editing occurred. The user's `.env` was not modified.
- Before model calls, all 18 deterministic PostgreSQL integration tests passed.
  The cluster was subsequently stopped and removed; result files were retained.

## Results

| Synthetic case | Automatic checks | Model requests | Model-loop time |
|---|---|---:|---:|
| Connection and lock health | Passed | 4 | 11.484 s |
| Estimated plan without executing the query | Passed | 2 | 5.578 s |
| Actual lock blocker/waiter and transaction state | Passed | 2 | 6.625 s |

8 requests used 57,742 prompt tokens and 2,432 completion tokens, totaling
60,174 provider-reported tokens. These counts are not a price estimate and
do not account for provider-specific cache pricing or billing adjustments.
Hard limits were 12 requests, 4 turns per case, 1,024 completion tokens and
180,000 input characters per request. This particular run did not use all
of that allowance.

Automatic grading checked completion, relevant successful tool use, valid
evidence identifiers, absence of proposals/runtime query execution, and
selected case-specific facts. The lock answer identified both generated PIDs
and `idle in transaction`; the plan answer distinguished estimates from
runtime measurements. The deterministic grader does **not** prove every
natural-language claim, causal diagnosis, number or relationship is correct.

## Manual observations

- Positive: the model collected actual evidence, identified the blocking
  session and waiting session correctly, distinguished one-sided blocking
  from an observed deadlock cycle, and did not execute changes.
- Positive: it reported the fixture's sequential scan and did not claim a
  measured query duration from an estimated plan.
- Quality gap: answers were longer than the requested short diagnosis and
  included English framing/answers despite Chinese prompts.
- Quality gap: health wording such as “overall healthy” generalized beyond
  the narrow snapshot. Capacity and lock observations should remain separate
  from a comprehensive database-health claim.
- Quality gap: the plan explanation leaned on a general expectation that
  primary-key equality normally favors an index, though the observed small
  fixture legitimately uses a sequential scan. The answer did mark its cause
  as inference, but this deserves broader counterexample evaluation.
- Efficiency gap: health diagnosis used four model turns and four observations;
  the overlapping health/snapshot tools leave room for cost-focused evaluation.

These are review findings, not fixes claimed to have been trained into the
model. Do not turn this smoke suite into a tuned “100% accuracy” headline.

## Reproduce and inspect

```powershell
python scripts/run_postgres_integration.py --pg-bin "C:\Program Files\PostgreSQL\18\bin" --live-model-eval
```

Each invocation opts into new paid requests and generates a new report
directory. Ordinary unit/CI/integration runs never call a real model.

Baseline run ID: `9cfc331d-b88e-4f68-bb18-f83ada6c87e2`.
Local artifacts (gitignored):

- `logs/integration-runs/9cfc331d-b88e-4f68-bb18-f83ada6c87e2/report.json`
- `logs/integration-runs/9cfc331d-b88e-4f68-bb18-f83ada6c87e2/live-model.json`
- `logs/integration-runs/9cfc331d-b88e-4f68-bb18-f83ada6c87e2/pass-1.json`

Source SHA-256 at this run:
`e399e84c3fb15a41e91660eebdf7b784a976ede76898e67f7e9746e3a6a42755`.
Subsequent hardening removes the worker's unused maintenance password, binds
operator confirmation to the preview digest, and improves evaluator failure
reporting and numeric usage retention; these changes are verified separately
without further paid calls. The baseline's
per-case numeric token keys were over-redacted; its top-level accounting and
individual request accounting are intact.
This report describes the recorded snapshot, not every future edit.

The three visible synthetic cases are **not an independent holdout** and do
not measure production accuracy, autonomous remediation quality, broad SQL
optimization quality, or safety under hostile database evidence. Next stages
should add held-out cases, negative/no-action cases, prompt-injection evidence,
ambiguous symptoms, repeated runs with confidence intervals, operator-rated
language/causal accuracy and cost/latency comparisons. Real-model remediation
evaluation should remain separate and restricted to disposable databases.
