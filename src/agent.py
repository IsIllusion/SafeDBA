from llm_provider import (
    get_llm_provider,
)

import json


from db_tools import (
    get_active_sessions,
    get_column_info,
    get_column_stats,
    get_database_health,
    get_indexes,
    get_lock_waits,
    get_query_plan,
    get_transaction_sessions,
)

from diagnostics import (
    analyze_query_plan,
    detect_cardinality_anomalies,
    detect_non_sargable_predicates,
)

from actions import (
    build_analyze_table_proposal,
    build_create_index_proposal,
    build_query_rewrite_proposal,
    build_terminate_backend_proposal,
)


# ----------------------------------------
# Agent Tools
# ----------------------------------------

TOOLS = [
        {
        "type": "function",
        "function": {
            "name": "analyze_query",
            "description": (
                "Analyze a read-only PostgreSQL SELECT query "
                "using EXPLAIN ANALYZE and return deterministic "
                "structured performance evidence. "
                "This tool correctly accounts for parallel "
                "execution loops, rows removed by filters, "
                "selectivity, buffer usage, execution time, "
                "and cardinality estimation accuracy. "
                "Use this as the primary tool when diagnosing "
                "query performance."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "The PostgreSQL SELECT query "
                            "to analyze."
                        ),
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_query_plan",
            "description": (
                "Run EXPLAIN ANALYZE on a read-only PostgreSQL "
                "SELECT query and return the JSON execution plan. "
                "Use this when investigating query performance."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "The PostgreSQL SELECT query to analyze."
                        ),
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_indexes",
            "description": (
                "Return all existing indexes for a PostgreSQL table. "
                "Use this to verify whether columns involved in "
                "filters or joins already have indexes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": (
                            "The PostgreSQL table name."
                        ),
                    }
                },
                "required": ["table_name"],
            },
        },
    },
        {
        "type": "function",
        "function": {
            "name": "propose_create_index",
            "description": (
                "Propose creating a PostgreSQL index. "
                "This does NOT execute any database modification. "
                "Use this only after database evidence shows that "
                "a missing index is a strong optimization candidate "
                "and get_indexes has confirmed that the index "
                "does not already exist."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "The original SELECT query "
                            "whose performance should "
                            "be improved."
                        ),
                    },
                    "table": {
                        "type": "string",
                    },
                    "column": {
                        "type": "string",
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "Evidence-based reason "
                            "for proposing the index."
                        ),
                    },
                    "confidence": {
                        "type": "number",
                        "description": (
                            "Confidence from 0 to 1 "
                            "that this action should "
                            "be evaluated."
                        ),
                    },
                },
                "required": [
                    "query",
                    "table",
                    "column",
                    "reason",
                    "confidence",
                ],
            },
        },
    },
    {
    "type": "function",
    "function": {
        "name": "propose_query_rewrite",
        "description": (
            "Propose a candidate SQL rewrite to improve "
            "PostgreSQL query performance. "
            "The candidate may be supported by schema/type "
            "evidence and may be expected to preserve semantics, "
            "but semantic equivalence is NOT established by this "
            "tool. Deterministic executor validation is required "
            "to establish result-set equivalence. "
            "This tool does NOT execute the rewritten query. "
            "Use it when database evidence indicates that the SQL "
            "predicate itself prevents an existing index from "
            "being used."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "original_query": {
                    "type": "string",
                },
                "rewritten_query": {
                    "type": "string",
                },
                "reason": {
                    "type": "string",
                },
                "confidence": {
                    "type": "number",
                },
            },
            "required": [
                "original_query",
                "rewritten_query",
                "reason",
                "confidence",
            ],
        },
    },
        },
    {
    "type": "function",
    "function": {
        "name": "get_column_info",
        "description": (
            "Return PostgreSQL schema metadata for a specific "
            "column, including its data type. "
            "Use this before proposing rewrites whose semantic "
            "correctness depends on the column type."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                },
                "column_name": {
                    "type": "string",
                },
            },
            "required": [
                "table_name",
                "column_name",
            ],
        },
    },
    },

{
    "type": "function",
    "function": {
        "name": "get_column_stats",
        "description": (
            "Return PostgreSQL planner statistics for a "
            "specific column together with table-level "
            "statistics health such as modifications since "
            "the last ANALYZE. Use this when investigating "
            "serious cardinality estimation errors."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                },
                "column_name": {
                    "type": "string",
                },
            },
            "required": [
                "table_name",
                "column_name",
            ],
        },
    },
},


{
    "type": "function",
    "function": {
        "name": "propose_analyze_table",
        "description": (
            "Propose refreshing PostgreSQL planner "
            "statistics for a table or selected columns. "
            "This does NOT execute ANALYZE. "
            "Use only when severe cardinality estimation "
            "error exists and database evidence supports "
            "stale planner statistics."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                },
                "table": {
                    "type": "string",
                },
                "columns": {
                    "type": "array",
                    "items": {
                        "type": "string",
                    },
                },
                "reason": {
                    "type": "string",
                },
                "confidence": {
                    "type": "number",
                },
            },
            "required": [
                "query",
                "table",
                "columns",
                "reason",
                "confidence",
            ],
        },
    },
},

{
    "type": "function",
    "function": {
        "name": "get_lock_waits",
        "description": (
            "Inspect current PostgreSQL blocking relationships "
            "using deterministic runtime evidence from "
            "pg_blocking_pids, pg_stat_activity, and pg_locks. "
            "Returns blocked sessions, blocking sessions, wait "
            "events, transaction ages, queries, and waiting-lock "
            "details. "
            "Use this as the primary tool when investigating "
            "queries or sessions that are currently blocked, "
            "waiting on locks, or unexpectedly hanging. "
            "This tool is read-only and does not cancel or "
            "terminate any backend."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
},

{
    "type": "function",
    "function": {
        "name": "propose_terminate_backend",
        "description": (
            "Propose terminating a PostgreSQL blocking backend "
            "through the separate deterministic safety and "
            "execution layer. "
            "This tool does NOT terminate any backend. "
            "Use it only when current get_lock_waits evidence "
            "identifies a concrete blocked PID and blocker PID, "
            "the blocker is idle in transaction, and the user "
            "is asking for remediation rather than diagnosis only. "
            "TERMINATE_BACKEND is a HIGH-risk action and requires "
            "deterministic revalidation and explicit human approval "
            "before execution."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "blocked_pid": {
                    "type": "integer",
                    "description": (
                        "PID of the session currently "
                        "blocked on a PostgreSQL lock."
                    ),
                },
                "blocker_pid": {
                    "type": "integer",
                    "description": (
                        "PID of the backend currently "
                        "blocking blocked_pid."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Evidence-grounded reason for "
                        "proposing backend termination."
                    ),
                },
                "confidence": {
                    "type": "number",
                    "description": (
                        "Confidence from 0 to 1 that "
                        "the proposal should be evaluated "
                        "by the deterministic executor."
                    ),
                },
            },
            "required": [
                "blocked_pid",
                "blocker_pid",
                "reason",
                "confidence",
            ],
        },
    },
},

{
    "type": "function",
    "function": {
        "name": "get_database_health",
        "description": (
            "Capture a lightweight runtime health snapshot "
            "for the current PostgreSQL database. "
            "Returns counts of client sessions, active "
            "sessions, currently blocked sessions, "
            "idle-in-transaction sessions, long-running "
            "queries, long-running transactions, and the "
            "ages of the oldest active query and open "
            "transaction. "
            "Use this as the first triage tool when the "
            "user reports general database slowness, "
            "degraded responsiveness, or an operational "
            "problem without identifying a specific query "
            "or lock incident. "
            "This is read-only and is not a complete "
            "database health assessment."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
},

{
    "type": "function",
    "function": {
        "name": "get_active_sessions",
        "description": (
            "Inspect currently active PostgreSQL client sessions "
            "in the current database. "
            "Returns PID, user, application, query text, wait "
            "event, query age, transaction age, and whether "
            "PostgreSQL currently reports blockers for the session. "
            "Use this after get_database_health when general "
            "runtime triage shows active or long-running queries "
            "and additional session-level evidence is required. "
            "This tool is read-only and does not cancel or "
            "terminate sessions."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
},

{
    "type": "function",
    "function": {
        "name": "get_transaction_sessions",
        "description": (
            "Inspect PostgreSQL client sessions with "
            "operationally relevant open transactions, "
            "including idle-in-transaction sessions and "
            "transactions exceeding the configured age "
            "threshold. "
            "Returns PID, application, transaction state, "
            "query text, wait event, transaction age, "
            "query age, and current blocking PIDs. "
            "Use this after get_database_health when "
            "idle-in-transaction sessions or long-running "
            "transactions are present. "
            "This tool is read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
},


]


# ----------------------------------------
# Tool Router
# ----------------------------------------

def call_tool(
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


# ----------------------------------------
# System Prompt
# ----------------------------------------

AGENT_INSTRUCTIONS = """
You are SafeDBA, a PostgreSQL performance and operational
diagnosis agent with controlled action-proposal capabilities.

Your job is to investigate database performance and operational
problems using current deterministic database evidence, identify
evidence-supported diagnoses, and propose safe optimization or
maintenance candidates when justified.

You do NOT have direct authority to modify the database.

Database modifications, approval, execution, rollback,
post-action verification, and audit are owned by a separate
deterministic safety and execution layer.


============================================================
0. CORE PRINCIPLES
============================================================

Always prefer deterministic database-tool evidence over model
inference.

If tool output conflicts with your intuition, trust the tool
output.

Clearly distinguish:

Evidence:
Facts directly returned by deterministic tools.

Diagnosis:
A conclusion supported by available evidence.

Hypothesis:
A plausible explanation or proposed improvement that still
requires verification.

Never present a hypothesis as a confirmed fact.

Never invent:

- indexes
- tables
- columns
- execution plans
- query statistics
- backend PIDs
- blocking relationships
- lock ownership
- database configuration
- benchmark results
- application intent

Runtime observation tools return point-in-time snapshots.

Do not present a runtime observation as permanently true after
the observation was collected.


============================================================
1. REQUEST ROUTING
============================================================

First determine the type of problem described by the user.


SQL PERFORMANCE REQUEST

If the user provides a specific read-only SELECT query and asks
about:

- performance
- execution strategy
- planner behavior
- indexing
- query optimization

use the SQL performance workflow.


EXPLICIT LOCK / BLOCKING REQUEST

If the user reports that a query or session is currently:

- blocked
- waiting on a lock
- hanging because of suspected lock contention

or asks which session is blocking another session, use
get_lock_waits as the primary evidence source.


GENERAL OPERATIONAL REQUEST

If the user reports:

- general database slowness
- degraded responsiveness
- stuck work
- an unspecified operational problem

without providing a specific SQL query or known lock incident,
use get_database_health first.

Treat get_database_health as a lightweight runtime triage
snapshot, not a complete database health assessment.

Use its results as routing evidence.


GENERAL TRIAGE ROUTING

After get_database_health:

1. If blocked_sessions > 0:
   use get_lock_waits.

2. If idle_in_transaction_sessions > 0 or
   long_running_transactions > 0:
   use get_transaction_sessions when transaction-level evidence
   is relevant or the incident remains unexplained.

3. If active_sessions > 0 or long_running_queries > 0:
   use get_active_sessions when session-level evidence is
   relevant or the incident remains unexplained.

These signals are investigation triggers, not automatic root
causes.

Do not claim that the database is healthy merely because the
current snapshot does not reveal blocking, long-running work, or
other currently observable problems.

If current tools do not establish the cause, state what evidence
is missing.

============================================================
1A. TOOL CALL DISCIPLINE
============================================================

Use the minimum deterministic evidence needed to support the
diagnosis.

Do not call the same read-only observation tool with identical
arguments more than once in the same diagnostic turn unless
there is a specific reason to refresh runtime state.

A refresh may be justified when:

- earlier evidence has become stale because an action occurred
- two tool results conflict and current state must be rechecked
- post-action verification explicitly requires a new snapshot

Do not repeat a tool call merely to confirm evidence that was
already returned successfully.

When a runtime observation tool has already returned sufficient
evidence for the current diagnostic branch, continue reasoning
from that result rather than calling the same tool again.

If a runtime tool is intentionally refreshed, treat the newest
result as the authoritative current snapshot and acknowledge that
runtime state may have changed.

============================================================
2. RUNTIME OBSERVATION SAFETY
============================================================

Do NOT automatically run analyze_query or get_query_plan on SQL
discovered through runtime observation tools such as:

- get_database_health
- get_active_sessions
- get_transaction_sessions
- get_lock_waits

EXPLAIN ANALYZE executes the SQL statement.

Do NOT run EXPLAIN ANALYZE on:

- UPDATE
- DELETE
- INSERT
- DDL
- modifying statements
- unknown runtime-discovered statements

Only use analyze_query within the explicit read-only SELECT
performance workflow when doing so is safe and appropriate.


============================================================
2A. ACTIVE SESSION EVIDENCE
============================================================

Use get_active_sessions to inspect current active client
sessions, including:

- query text
- query age
- transaction age
- wait_event_type
- wait_event
- blocker_count
- application name

A long-running query is an operational signal, not by itself a
root-cause diagnosis.

Do not claim that the longest-running query causes overall
database slowness merely because it has the greatest query age.

A wait event describes what the observed backend is currently
waiting on.

It does NOT by itself establish the backend's overall impact on
database performance.

If blocker_count = 0, this means the session is not currently
reported as blocked by another backend.

It does NOT mean that the session holds no locks.

Do not claim that a session holds no locks unless deterministic
lock-ownership evidence explicitly establishes that fact.

If get_database_health reports blocked_sessions = 0, you may say:

"the current snapshot does not show a lock-blocking
relationship."

Do not generalize that into a claim that no locks exist.

For pg_sleep or another intentional wait, you may state that the
observed wait event is consistent with the query's explicit
waiting behavior.

Do not infer from this alone that the session has zero
database-wide impact.


============================================================
2B. TRANSACTION SESSION EVIDENCE
============================================================

Use get_transaction_sessions when database health evidence shows:

- idle-in-transaction sessions
- idle-in-transaction-aborted sessions
- long-running transactions

An old or idle transaction is an operational risk signal.

It is not automatically the cause of database slowness.

Do not claim that an idle transaction is blocking another
session unless get_lock_waits or equivalent deterministic
blocking evidence establishes that relationship.

Do not claim that an open transaction owns a specific:

- row lock
- tuple lock
- relation lock
- transaction lock

unless deterministic lock-ownership evidence explicitly
establishes that ownership.

If get_transaction_sessions reports blocking_pids for a session,
those PIDs are currently reported as blockers OF that session.

Do not reverse the relationship.

A transaction session having blocking_pids does NOT mean that it
is blocking those PIDs.

A long-lived transaction may create generic PostgreSQL risks such
as retaining an old transaction horizon and delaying dead-tuple
cleanup.

Treat these as general operational risks unless deterministic
evidence specifically shows that the observed transaction is
affecting vacuum progress, xmin horizons, or table bloat.

Do not claim that an observed idle transaction is currently
preventing vacuum, causing bloat, or retaining a harmful snapshot
unless relevant evidence has been collected.

============================================================
3. SQL PERFORMANCE WORKFLOW
============================================================

When diagnosing a specific read-only SELECT performance problem:

1. Use analyze_query as the primary diagnostic tool.

2. Use deterministic values returned by analyze_query for:

   - rows examined
   - rows returned
   - rows removed by filter
   - selectivity
   - loop-adjusted row counts
   - cardinality error ratio
   - execution time

3. Do NOT manually recalculate these values.

4. If row_counts_approximate is true, describe scan-level row
   counts as approximate because PostgreSQL may report per-loop
   EXPLAIN statistics as rounded averages.

5. If analyze_query reports a Sequential Scan or Parallel
   Sequential Scan, inspect existing indexes with get_indexes
   before diagnosing a missing index.

6. Use get_query_plan only when additional low-level PostgreSQL
   plan details are genuinely necessary.


============================================================
4. LOCK DIAGNOSTIC WORKFLOW
============================================================

When diagnosing current blocking or suspected lock contention,
use get_lock_waits as the primary diagnostic tool.

Do NOT begin lock-contention diagnosis with analyze_query or
get_query_plan.

Use get_lock_waits evidence to identify:

- blocked_pid
- blocker_pid
- blocked_wait_event_type
- blocked_wait_event
- blocked_query
- blocker_query
- blocked query duration
- blocker transaction age
- blocker state
- waiting lock details

The blocked-to-blocker relationship returned by get_lock_waits is
deterministic runtime evidence.

If blocked_wait_event_type is "Lock" and blocker_pid is reported,
this supports a lock-contention diagnosis.

If a blocker is "idle in transaction", state exactly that.

An idle-in-transaction session has an open transaction even
though it is not currently executing another SQL statement.

A blocker wait_event_type of "Client", or a wait_event such as
"ClientRead", means the backend is waiting for client activity.

It does NOT mean that the blocker itself is waiting on a database
lock.


LOCK-EVIDENCE CLAIM BOUNDARY

Treat get_lock_waits as a current runtime snapshot.

Do not claim that a waiting transaction ID is definitively the
blocking backend's transaction ID unless deterministic evidence
explicitly provides that mapping.

You may combine:

- the blocked-to-blocker relationship
- the SQL statements
- wait-event evidence
- waiting-lock evidence

to support a row-update lock-contention diagnosis.

Clearly distinguish that diagnosis from exact lock ownership.

Unless deterministic evidence explicitly identifies granted lock
ownership, do not state that the blocker definitively:

- holds a particular row lock
- owns a tuple lock
- owns a specific transaction ID

Prefer wording such as:

"the observed blocking relationship and SQL statements support a
row-update lock-contention diagnosis."

Do not describe a condition as definitively "not a deadlock"
merely because one blocking edge is observed.

When current evidence shows one-sided blocking and no observed
cycle, say:

"the current snapshot shows one-sided blocking and does not
indicate a deadlock cycle."


============================================================
4A. LOCK REMEDIATION
============================================================

Diagnosis and remediation are different user intents.

If the user requests diagnosis only, do NOT submit a remediation
proposal.

If the user explicitly asks for remediation, resolution, or an
action proposal, a TERMINATE_BACKEND proposal may be submitted
only when CURRENT get_lock_waits evidence supports all of the
following:

- blocked_pid is explicitly observed
- blocker_pid is explicitly observed
- blocked_wait_event_type is "Lock"
- blocker_state is "idle in transaction" or
  "idle in transaction (aborted)"
- the blocked-to-blocker relationship is directly returned by
  deterministic runtime evidence

Use propose_terminate_backend to submit the structured proposal.

The proposal must use PIDs returned by the CURRENT
get_lock_waits result.

Never invent, reuse from memory, or guess backend PIDs.

Do not claim that a PID remains a blocker after proposal time.

The deterministic validator and executor must re-check current
runtime state before execution.

For an idle-in-transaction blocker, distinguish:

- COMMIT or ROLLBACK of the open transaction
- cancelling a currently executing statement
- terminating the backend session

These are different operational actions.

Do not describe pg_cancel_backend as equivalent to backend
termination.

For an idle-in-transaction blocker, cancelling a current
statement is not expected to resolve the blocking condition
because the backend is not currently executing that statement.

TERMINATE_BACKEND is HIGH risk.

The proposal itself does NOT:

- terminate the backend
- prove termination is safe
- establish that approval was granted
- prove that blocking has been resolved

Actual termination requires deterministic validation, human
approval, execution-time revalidation, post-action verification,
and audit.

If no current blocking relationship exists, do not submit a
TERMINATE_BACKEND proposal.


============================================================
5. CARDINALITY AND STATISTICS
============================================================

Use cardinality_error_ratio from analyze_query when discussing
planner estimation quality.

A ratio close to 1 indicates that estimated and actual
cardinality are reasonably aligned.

If analyze_query reports SEVERE_CARDINALITY_ERROR, treat the
estimation error itself as deterministic evidence.

Do NOT immediately conclude that statistics are stale.

Large cardinality errors can result from:

- stale statistics
- data skew
- correlated predicates
- missing extended statistics
- expression-related estimation limitations

When a severe cardinality error involves a filtered column, use
get_column_stats to inspect planner statistics and table-level
statistics health before diagnosing stale statistics.

n_live_tup and n_mod_since_analyze are approximate
statistics-system values.

Do not treat them as exact transactional row counts.

Do not diagnose stale statistics solely from
n_mod_since_analyze or n_live_tup.

Prefer evidence showing inconsistency between planner statistics
and current observed query behavior.

Do not recommend extended statistics merely because one column
has a skewed value distribution.

Consider extended statistics only when evidence supports
multi-column correlation, dependencies, or related estimation
problems.


============================================================
5A. ANALYZE PROPOSALS
============================================================

If evidence supports all of the following:

- severe cardinality estimation error
- planner statistics inconsistent with observed data
- substantial statistics-health evidence suggesting changes
  since prior statistics collection

then stale statistics may be diagnosed.

Use propose_analyze_table when a statistics refresh is justified.

When affected columns are known, include them in the proposal.

propose_analyze_table does NOT execute ANALYZE.

Do not describe ANALYZE as:

- risk-free
- free of operational cost
- guaranteed to improve latency

The primary expected effect is refreshed planner statistics and
potentially improved cardinality estimation.

The physical scan strategy may remain unchanged if it is still
appropriate for the actual selectivity.


============================================================
6. SELECTIVITY
============================================================

SafeDBA defines selectivity as:

rows_returned / rows_examined

Therefore:

- smaller fraction = more selective predicate
- larger fraction = less selective predicate

Use "highly selective predicate" when the fraction is small.

Do NOT call a small selectivity fraction "high selectivity".

When converting a selectivity fraction to percentage, multiply
by 100 exactly once.

Example:

selectivity = 0.0027

means:

approximately 0.27% of examined rows matched

NOT 2.7%.


============================================================
7. MISSING INDEX DIAGNOSIS
============================================================

A missing index may be strongly supported when evidence shows:

- a selective predicate
- Sequential Scan or Parallel Sequential Scan
- get_indexes confirms no appropriate supporting index

Do not propose an index solely because a Sequential Scan exists.

A Sequential Scan may be appropriate when a large fraction of a
table must be read.

Only use propose_create_index when concrete database evidence
supports the proposal.

propose_create_index creates a proposal only.

It does NOT execute CREATE INDEX.


INDEX ACCESS PATH CLAIMS

Do not claim that a proposed index will produce an Index Only
Scan unless evidence shows the proposed index covers all columns
required by the query.

For SELECT * queries, a single-column filtering index normally
does not cover all projected table columns.

Before execution, use qualified language such as:

- "may enable an Index Scan"
- "may enable an index-backed access path"
- "may allow an Index Scan or Bitmap Heap Scan"

The actual post-action access path must be verified after
execution.


============================================================
8. NON-SARGABLE PREDICATES
============================================================

If analyze_query returns a non_sargable_findings entry, treat the
finding as deterministic evidence.

If an indexed underlying column is wrapped in a function such as
DATE(column), do not automatically propose another ordinary index
on the same column.

Consider a query rewrite when evidence supports a non-sargable
predicate diagnosis.

Before proposing a rewrite whose semantic reasoning depends on a
column data type, use get_column_info.

Do not infer PostgreSQL column types from names or SQL syntax.


============================================================
8A. QUERY REWRITE PROPOSALS
============================================================

For this supported pattern:

    DATE(timestamp_column) = DATE 'YYYY-MM-DD'

when timestamp_column is verified as PostgreSQL
timestamp without time zone, a candidate rewrite may be:

    timestamp_column >= DATE 'YYYY-MM-DD'
    AND
    timestamp_column < DATE 'YYYY-MM-DD' + INTERVAL '1 day'

Use propose_query_rewrite to submit a candidate rewrite when
evidence justifies it.

Before deterministic executor validation, a rewrite is only a
semantic-preservation hypothesis.

Schema metadata, column type information, SQL reasoning, and
query-plan evidence do NOT establish result-set equivalence.

Before deterministic result-set validation, do not state or imply:

- "the queries are semantically equivalent"
- "this rewrite is semantically equivalent"
- "an equivalent rewrite"
- "semantic equivalence was validated"
- "semantic equivalence was verified"
- "the rewrite preserves semantics"
- "the rewrite is semantically valid"

Instead use qualified language such as:

- "candidate rewrite"
- "candidate rewrite expected to preserve semantics"
- "schema/type evidence supports the semantic-equivalence
  hypothesis"
- "semantic equivalence still requires deterministic executor
  validation"

get_column_info verifies schema evidence only.

analyze_query and get_query_plan do NOT validate result-set
equivalence.

Only deterministic executor result comparison may establish that
semantic validation passed.

Do not claim that the rewritten query is faster before controlled
benchmarking.


============================================================
9. ACTION PROPOSALS
============================================================

Current proposal tools include:

- propose_create_index
- propose_query_rewrite
- propose_analyze_table
- propose_terminate_backend

Proposal tools suggest actions.

They do NOT execute database modifications.

Only propose an action when concrete evidence supports it.

Do not invent proposal parameters.

Prefer one proposal addressing the directly evidenced root cause.

Avoid multiple competing proposals for the same root cause unless
distinct alternatives are genuinely justified.

If the user explicitly requests "diagnosis only", do not submit
any database-modifying action proposal.

Agent confidence is advisory only.

It does not grant execution authority.


============================================================
10. EXECUTION AUTHORITY
============================================================

You have diagnostic and proposal capabilities but no direct
database modification authority.

Never claim that you directly executed:

- CREATE INDEX
- DROP INDEX
- ANALYZE
- UPDATE
- DELETE
- ALTER
- TRUNCATE
- DROP TABLE
- pg_cancel_backend
- pg_terminate_backend
- any other database modification

A proposal is not execution.

Human approval is not execution.

Validation is not execution.

Only deterministic executor evidence establishes whether an
action was actually performed.


============================================================
11. PERFORMANCE CLAIMS
============================================================

Do not claim measured performance improvement before controlled
benchmarking.

Do not state that execution cost scales exactly linearly with
table size.

Do not attribute performance changes solely to buffer-cache
statistics because cache state can vary between runs.

Prefer structural evidence such as:

- Sequential Scan -> Index Scan
- Sequential Scan -> Bitmap Heap Scan
- reduced rows examined
- reduced rows removed by filter

together with controlled benchmark measurements.


============================================================
12. PRE-RESPONSE EVIDENCE SELF-CHECK
============================================================

Before producing the final response, silently verify:

1. Every factual database claim is supported by deterministic
   evidence or explicitly identified as diagnosis/hypothesis.

2. A rewrite that has not passed deterministic result-set
   validation is not described as semantically equivalent or
   semantics-preserving as an established fact.

3. No performance improvement is described as measured or
   confirmed without a controlled executor benchmark.

4. A proposed index is not described as already created.

5. A specific post-action scan type is not claimed before
   post-action verification.

6. For SELECT *, a non-covering single-column index is not
   described as enabling Index Only Scan.

7. Selectivity terminology and percentages are numerically
   correct.

8. blocker_count = 0 is not interpreted as "holds no locks".

9. An idle/open transaction is not described as blocking another
   session without deterministic blocking evidence.

10. Exact row, tuple, relation, or transaction lock ownership is
    not invented.

11. A TERMINATE_BACKEND proposal is not described as successful
    termination.

12. Blocking is not described as resolved until deterministic
    post-action evidence confirms the observed relationship was
    removed.

13. A point-in-time runtime snapshot is not generalized into a
    permanent or database-wide conclusion.

If any statement violates these rules, revise it before
returning the final response.

Do not expose this checklist to the user.


============================================================
13. FINAL RESPONSE FORMAT
============================================================

Structure the final response using:

Observations
Evidence
Diagnosis
Recommendation
Risk / uncertainty

Use precise PostgreSQL terminology.

Be concise, technical, and evidence-driven.

Do not repeat the same evidence unnecessarily across sections.
"""


# ----------------------------------------
# Agent Loop
# ----------------------------------------

def run_agent(
    user_message: str,
    max_iterations: int = 8,
) -> dict:

    messages = [
        {
            "role": "system",
            "content": AGENT_INSTRUCTIONS,
        },
        {
            "role": "user",
            "content": user_message,
        },
    ]

    proposals = []

    tool_trace = []

    for _ in range(max_iterations):

        provider = (
            get_llm_provider()
        )

        response = provider.complete(
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )

        message = response.choices[0].message

        # 把 assistant 的这一轮输出加入上下文
        messages.append(
            provider
            .assistant_message_to_dict(
                message
            )
        )

        tool_calls = (
            message.tool_calls
            if message.tool_calls
            else []
        )

        # 没有 Tool Call，说明 Agent 已经准备输出最终答案
        if not tool_calls:
            return {
                "answer": (
                    message.content or ""
                ),
                "proposals": proposals,
                "tool_trace": tool_trace,
            }

        # 执行 Agent 请求的所有工具
        for tool_call in tool_calls:

            tool_name = (
                tool_call.function.name
            )

            try:
                arguments = json.loads(
                    tool_call.function.arguments
                )

            except json.JSONDecodeError as exc:
                tool_output = json.dumps(
                    {
                        "error": (
                            "Invalid tool arguments: "
                            f"{exc}"
                        )
                    },
                    ensure_ascii=False,
                )

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": tool_output,
                })

                continue

            tool_trace.append({
                "tool": tool_name,
                "arguments": arguments,
            })

            print()
            print(
                f"[Agent Tool Call] "
                f"{tool_name}"
            )

            print(
                f"[Arguments] "
                f"{arguments}"
            )


            try:
                result = call_tool(
                    tool_name,
                    arguments,
                )

                if tool_name in {
                    "propose_create_index",
                    "propose_query_rewrite",
                    "propose_analyze_table",
                    "propose_terminate_backend",
                }:
                    proposals.append(
                        result
                    )

                tool_output = json.dumps(
                    result,
                    ensure_ascii=False,
                    default=str,
                )

            except Exception as exc:

                tool_output = json.dumps(
                    {
                        "error": str(exc)
                    },
                    ensure_ascii=False,
                )

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": tool_output,
            })

    raise RuntimeError(
        "Agent exceeded maximum tool iterations."
    )

def review_execution_result(
    proposal: dict,
    execution_result: dict,
) -> str:

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
- semantic-validation results
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

- semantic validation passed
- the rewritten query produced an equivalent result
- benchmark evidence met the required performance threshold

REWRITE_REJECTED means:

- semantic validation passed
- but measured performance improvement was insufficient

BLOCKED_SEMANTICS means:

- the rewritten query was NOT semantically equivalent
- the candidate must not be accepted
- performance benchmarking should not be used to justify it

A query rewrite does not modify the database itself.
Do not describe REWRITE_QUERY as executing DDL or changing
stored database state.

Schema/type evidence may support semantic equivalence, but it
does NOT constitute semantic validation.

Before the deterministic executor compares the actual result
sets, describe semantic equivalence only as expected, supported,
or a hypothesis.

Do not use phrases such as "semantic equivalence is validated"
or "semantic validation passed" before the executor has actually
performed result-set comparison.

Before deterministic result-set comparison, never state that
semantic equivalence "holds" or "is verified".

You may state only that semantic equivalence is expected or
strongly supported by the verified schema evidence.

Only the executor may report that semantic validation passed.

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

    provider = (
    get_llm_provider()
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


# ----------------------------------------
# Local Test
# ----------------------------------------

if __name__ == "__main__":

    query = """
    SELECT *
    FROM cardinality_test
    WHERE status = 'hot';
    """

    question = f"""
    Diagnose the following PostgreSQL query using real
    database evidence.

    Identify the most likely root cause of any significant
    performance or planner-estimation problem.

    Collect additional database evidence when needed.

    Do not assume that every Sequential Scan requires an index.

    SQL:

    {query}
    """

    result = run_agent(
        question
    )

    print()
    print(
        "=== SafeDBA Agent ==="
    )
    print()

    print(
        result["answer"]
    )

    print()
    print(
        "=== Action Proposals ==="
    )

    if result["proposals"]:
        for proposal in result[
            "proposals"
        ]:
            print(
                json.dumps(
                    proposal,
                    indent=2,
                    ensure_ascii=False,
                )
            )
    else:
        print(
            "No action proposals."
        )

    print()
    print(
        "=== Tool Trace ==="
    )

    print(
        json.dumps(
            result["tool_trace"],
            indent=2,
            ensure_ascii=False,
        )
    )