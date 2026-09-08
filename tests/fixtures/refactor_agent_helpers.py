# Frozen d42113b26db74d51f064f86a5ac4607a34241845 helpers; test-only compatibility oracle.

def _dispatch_builtin_tool(
    name: str,
    arguments: dict,
):
    if name == "analyze_query":

        plan = get_query_plan(
            arguments["query"]
        )

        analysis = analyze_query_plan(
            plan
        )

        analysis[
            "non_sargable_findings"
        ] = detect_non_sargable_predicates(
            analysis
        )

        analysis[
            "cardinality_findings"
        ] = detect_cardinality_anomalies(
            analysis
        )

        return analysis

    if name == "get_query_plan":
        return get_query_plan(
            arguments["query"]
        )

    if name == "get_estimated_query_plan":
        return get_estimated_query_plan(
            arguments["query"]
        )

    if name == "get_indexes":
        return get_indexes(
            arguments["table_name"]
        )

    if name == "get_column_info":
        return get_column_info(
            table_name=arguments[
                "table_name"
            ],
            column_name=arguments[
                "column_name"
            ],
        )

    if name == "get_column_stats":
        return get_column_stats(
        table_name=arguments[
            "table_name"
        ],
        column_name=arguments[
            "column_name"
        ],
    )

    if name == "get_lock_waits":

        return get_lock_waits()

    if name == "get_transaction_sessions":

        return get_transaction_sessions()

    if name == "get_database_health":

        return get_database_health()

    if name == "get_operational_snapshot":

        return get_operational_snapshot()

    if name == "get_active_sessions":

        return get_active_sessions()

    if name == "propose_create_index":
        return build_create_index_proposal(
            query=arguments["query"],
            table=arguments["table"],
            column=arguments["column"],
            reason=arguments["reason"],
            confidence=arguments[
                "confidence"
            ],
        )

    if name == "propose_query_rewrite":
        return build_query_rewrite_proposal(
            original_query=arguments[
                "original_query"
            ],
            rewritten_query=arguments[
                "rewritten_query"
            ],
            reason=arguments["reason"],
            confidence=arguments[
                "confidence"
            ],
        )

    if name == "propose_analyze_table":

        return build_analyze_table_proposal(
            query=arguments[
                "query"
            ],
            table=arguments[
                "table"
            ],
            columns=arguments[
                "columns"
            ],
            reason=arguments[
                "reason"
            ],
            confidence=arguments[
                "confidence"
            ],
        )

    if name == "propose_terminate_backend":

        return build_terminate_backend_proposal(
            blocked_pid=arguments[
                "blocked_pid"
            ],
            blocker_pid=arguments[
                "blocker_pid"
            ],
            blocker_backend_start=arguments[
                "blocker_backend_start"
            ],
            blocker_xact_start=arguments[
                "blocker_xact_start"
            ],
            reason=arguments[
                "reason"
            ],
            confidence=arguments[
                "confidence"
            ],
        )

    raise ValueError(
        f"Unknown tool: {name}"
    )


def _build_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for wire_tool in TOOLS:
        function = wire_tool["function"]
        name = function["name"]
        category, risk, freshness = _TOOL_CAPABILITIES[name]

        def handler(_name=name, **arguments):
            return _dispatch_builtin_tool(
                _name,
                arguments,
            )

        registry.register(
            ToolSpec(
                name=name,
                description=function["description"],
                parameters=function["parameters"],
                handler=handler,
                category=category,
                risk=risk,
                freshness_seconds=freshness,
                idempotent=(category != "runtime"),
                side_effect=False,
                requires_approval=False,
            )
        )
    return registry


def call_tool(
    name: str,
    arguments: dict,
):
    """Dispatch through the typed capability registry."""
    require_tool(name)

    return TOOL_REGISTRY.dispatch(
        name,
        arguments,
    )


def _safe_usage_count(value) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return parsed if parsed >= 0 else 0


def _provider_route_metadata(provider) -> dict:
    """Copy only bounded, non-secret provider-routing metadata."""

    try:
        value = getattr(provider, "last_call_metadata", {})
        if callable(value):
            value = value()
    except Exception:
        return {}
    if not isinstance(value, dict):
        return {}
    allowed = {
        "selected_provider",
        "selected_model",
        "fallback_configured",
        "fallback_used",
        "failover_reason",
        "primary_error_type",
        "fallback_error_type",
        "primary_circuit_state",
        "fallback_circuit_state",
    }
    return {
        key: item[:200] if isinstance(item, str) else item
        for key, item in value.items()
        if key in allowed
        and isinstance(item, (str, bool, int, float))
    }


def review_execution_result(
    proposal: dict,
    execution_result: dict,
    *,
    chat_model=None,
) -> str:
    try:
        require_operation("AGENT_RUN")
    except RuntimePolicyError:
        return (
            "LLM review skipped by runtime policy. Refer to the deterministic "
            "execution result; no additional review request was sent."
        )

    review_instructions = """
You are SafeDBA reviewing the outcome of a controlled database
optimization or operational action.

The deterministic executor has already made the final
execution decision.

You must NOT override that decision.

Your job is to explain whether the original diagnosis
and optimization hypothesis were supported by the
measured evidence.

The proposal may be one of multiple action types,
including:

- CREATE_INDEX
- REWRITE_QUERY
- ANALYZE_TABLE
- TERMINATE_BACKEND

============================================================
EVIDENCE RULES
============================================================

Treat execution_result as authoritative measured evidence.

Do not invent:

- benchmark numbers
- query plans
- current-snapshot result-comparison outcomes
- database changes
- indexes
- execution decisions

Do not recalculate measurements when the executor already
provides them.


============================================================
CREATE_INDEX
============================================================

For CREATE_INDEX:

SUCCESS + KEEP means the index was created, benchmarked,
and retained.

ROLLED_BACK means the index was created experimentally but
removed because it failed the acceptance criteria.

Use before/after execution-plan evidence when available.


============================================================
REWRITE_QUERY
============================================================

For REWRITE_QUERY:

REWRITE_ACCEPTED means:

- the rewritten query matched the original unordered row multiset on
  the current database snapshot
- benchmark evidence met the required performance threshold
- this does not prove universal semantic equivalence or output ordering

REWRITE_REJECTED means:

- the current-snapshot unordered row multiset matched
- but measured performance improvement was insufficient

BLOCKED_SNAPSHOT_MISMATCH means:

- the current-snapshot results did not match
- the candidate must not be accepted
- performance benchmarking should not be used to justify it

A query rewrite does not modify the database itself.
Do not describe REWRITE_QUERY as executing DDL or changing
stored database state.

Schema/type evidence may support a semantic-equivalence hypothesis, but
it does NOT prove semantic equivalence.

Before the deterministic executor compares the actual result
sets, describe semantic equivalence only as expected, supported,
or a hypothesis.

Do not use phrases such as "semantic equivalence is validated" or
"semantic validation passed", including after the snapshot comparison.

Before deterministic result-set comparison, never state that
semantic equivalence "holds" or "is verified".

You may state only that semantic equivalence is expected or
strongly supported by the verified schema evidence.

After comparison, report only the measured scope recorded in
comparison_scope. Never generalize a snapshot match into a proof over
future data, ordering, metadata, or volatile behavior.

============================================================
ANALYZE_TABLE
============================================================

For ANALYZE_TABLE:

STATISTICS_REFRESH_CONFIRMED means the deterministic executor
ran ANALYZE and verified that cardinality estimation accuracy
improved to within the configured acceptance threshold.

STATISTICS_REFRESH_INCONCLUSIVE means ANALYZE completed, but
the post-action cardinality evidence did not satisfy the
acceptance criterion.

The primary success criterion for ANALYZE_TABLE is improved
cardinality estimation accuracy.

Do NOT require:

- lower execution latency
- an execution-plan change
- an Index Scan

A Sequential Scan may remain correct after ANALYZE if the real
predicate selectivity still makes a sequential access path
appropriate.

When before_cardinality_error_ratio and
after_cardinality_error_ratio are provided, use those values
as the authoritative validation evidence.

Do not describe ANALYZE as a performance optimization merely
because execution time happened to be lower in one run.

If PostgreSQL column statistics changed from an outdated
distribution to one aligned with the observed data, this is
useful supporting evidence for a successful statistics refresh.


============================================================
TERMINATE_BACKEND
============================================================

For TERMINATE_BACKEND:

CANCELLED means:

- the human approval gate rejected the operation
- no backend termination should be claimed

BLOCKED_VALIDATION means:

- the proposal failed deterministic validation
- no termination should be claimed

BLOCKED_FINAL_REVALIDATION means:

- human approval may have been granted
- but execution-time runtime evidence no longer satisfied the
  termination safety conditions
- the executor blocked the operation
- no backend termination should be claimed

TERMINATION_FAILED means:

- the requested termination was not confirmed successful
- do not describe the blocking incident as resolved

BACKEND_TERMINATION_CONFIRMED means the deterministic executor
verified all of the following:

- final runtime validation passed
- the target was still the blocker at execution time
- backend termination succeeded
- the original blocked-to-blocker relationship disappeared

Use final_execution_evidence as authoritative evidence for:

- still_blocking
- final_validation_passed
- terminated
- blocker_state
- blocker_backend_type
- executor_pid

Use blocking_relationship_removed as the authoritative
post-action resolution evidence.

Do not claim that a waiting transaction ID is definitively the
blocking backend's transaction ID unless execution_result
explicitly provides that mapping.

Do not describe an exact tuple-level or row-level lock as direct
executor evidence unless execution_result explicitly identifies
that lock ownership.

You may state that the observed SQL statements, wait-event
evidence, and blocked-to-blocker relationship support a
row-update lock-contention diagnosis.

When backend termination is confirmed, state only that the
observed blocked-to-blocker relationship was removed.

Do not generalize that result into claims that all database
problems are resolved.

Do not state that no further action is required without
qualification.

If the current blocking relationship was removed, you may say
that no additional remediation is required for that specific
blocking relationship based on the current post-action evidence.

The underlying application-level cause of an idle transaction
may still require investigation and must not be invented from
database evidence alone.

Do not infer why the application left the transaction open.

Do not describe human approval alone as successful execution.

Do not describe proposal validation alone as successful
execution.

If BACKEND_TERMINATION_INCONCLUSIVE is returned, state that the
post-action verification did not establish successful resolution
and that further review is required.


============================================================
INTERPRETATION
============================================================

Prefer structural evidence together with benchmark evidence.

Examples of useful structural evidence include:

- Parallel Sequential Scan -> Index Scan
- Parallel Sequential Scan -> Bitmap Heap Scan
- Bitmap Index Scan appearing after optimization
- reduced rows examined
- reduced rows removed by filter

Do not attribute performance improvement solely to cache or
buffer statistics.

When row counts from parallel scans are marked approximate,
describe them as approximate.

Do not claim exact causal performance scaling from row counts.

When index_name, index_cond, or recheck_cond are available,
use them as authoritative structural evidence.

Do not say that a predicate was "eliminated entirely" merely
because the plan node has no Filter field.

For Bitmap Heap Scan, distinguish ordinary Filter conditions
from Recheck Cond when that evidence is available.

Do not describe a measured improvement as "stable" unless
evidence from repeated workloads or multiple independent
benchmark sessions supports that claim.

If cardinality_error_ratio becomes closer to 1 after an
optimization, state only that the estimate aligns more closely
with the observed row count. Do not infer improved plan
stability from that fact alone.

============================================================
FINAL FORMAT
============================================================

Use this structure:

Action
Safety Validation
Measured Result
Plan Change
Assessment
Final Decision

Keep the response concise, technical, and evidence-driven.
"""

    evidence = {
        "proposal": proposal,
        "execution_result": (
            execution_result
        ),
    }

    provider = LangChainProvider(
        provider=get_llm_provider() if chat_model is None else None,
        chat_model=chat_model,
    )

    response = (
        provider.complete(
            messages=[
                {
                    "role": "system",
                    "content": (
                        review_instructions
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        evidence,
                        ensure_ascii=False,
                        default=str,
                        indent=2,
                    ),
                },
            ],
        )
    )

    return (
        response
        .choices[0]
        .message
        .content
        or ""
    )
