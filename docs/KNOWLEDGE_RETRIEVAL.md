# Controlled Knowledge Retrieval

SafeDBA supports optional reference retrieval for reviewed runbooks, business
context and incident documentation. It is disabled by default. Enabling it
adds one run-local LangChain tool, `search_knowledge`, to the existing 15 tools.
No vector database, embedding model, external service or new dependency is
required. This implementation uses bounded lexical BM25 ranking over English
terms and Chinese bigrams, with optional operator-curated keywords.

## Reference knowledge is not database authority

| Reference type | Meaning | Can authorize an action? |
|---|---|---|
| `kb-...` | An applicable, reviewed document chunk delivered in this run | No |
| `ev-...` | A current run's database-tool or proposal record | Only successful qualifying observations can satisfy proposal prerequisites; separate approval remains mandatory |

Knowledge replies never enter the database evidence ledger, including failed,
empty and budget-limited replies. Document instructions cannot provide a lock
identity, approve a mutation, or replace fresh observations. A knowledge-only
request cannot create an action proposal without the original evidence checks.
The deterministic execution and independent-worker grant paths are unchanged.

All document text and document-supplied metadata remain untrusted reference data. The model is
instructed to ignore embedded instructions. Citation checks verify that a
referenced chunk was actually delivered and is still applicable, not that every
sentence is entailed by that chunk. Mandatory business restrictions must be
implemented in deterministic policy, not solely in a retrievable document.

## Initial lookup, completion and citation handling

With knowledge enabled, explicit internal-runbook, business-mapping and similar
questions receive one initial read-only lookup before the first model response.
The router uses bounded English/Chinese intent patterns; opaque identifiers in
the request make a focused initial query. This is a lexical heuristic, not
general semantic understanding, and the model may refine the query within its
remaining budget. Pure database diagnosis and explicit knowledge-lookup opt-outs
do not trigger this prefetch.

The lookup goes through the same scoped LangChain tool, runtime checks and output
bounds as a model-selected lookup. It consumes one of the three knowledge
attempts and one total tool attempt, but no extra model turn. Its trace is
explicitly marked `origin=runtime_knowledge_prefetch`; it is not represented as
a decision made by the model. Reference text stays in a tool reply, never a
trusted instruction or database evidence record.

For mixed questions explicitly requesting current lock, operational or estimated
plan observations, the completion gate checks that the corresponding read-only
observation has been attempted. A runbook alone cannot satisfy that part of the
request. Failed observations may be disclosed; results are never fabricated.
The gate honors explicit database-lookup opt-outs and never grants execution
authority. Unresolved requirements stop within the existing iteration budget.

Each local-file hit includes runtime-generated `applicability.status` and
`applicability.checked_at` from the same clock used by the scope/expiry filter.
`applicable_at_check` means applicable at that instant, not permanently current
or necessarily true. A narrow check rejects explicit present-tense expiry claims
that name a cited source's actual expiry date/reference and contradict its
checked applicability. It does not prove arbitrary natural-language entailment;
conditional, negated and unrelated-date statements are not treated as such claims.

Exact citation-format placeholders (bracketed `kb-...` / `ev-...`, including a
single Unicode ellipsis) are rendered as plain, non-link syntax examples. This
prevents an otherwise valid no-source refusal from entering a citation-repair
loop. Real or malformed source IDs are not stripped or accepted by that rule;
delivered-source requirements and unknown/revoked/expired-ID checks still apply.
Fabricated numeric database evidence references are now rejected even when no
observations succeeded and even when RAG is disabled. This is an intentional
correction to previously permissive empty-ledger behavior, not schema drift.

## Publication and scope

Only an operator-side CLI publishes bundles. The Agent has no ingestion,
publication, file-selection, URL-fetch or knowledge-write tool. Source URLs are
provenance metadata and are never fetched. A bundle contains one active revision
per document ID and requires:

- title, source, revision and reviewer;
- timezone-aware review and expiry times;
- explicit deployment scope IDs, environments and PostgreSQL major versions;
- reviewed text and optional keyword aliases.

Unknown fields, duplicate IDs/JSON keys, invalid metadata and common obvious
credential patterns are rejected. Secret detection is heuristic, not a full
data-loss-prevention system; operators must review text and metadata themselves.
`reviewed_by` and the CLI approval flag are attestations, not authenticated
identity or cryptographic signatures. The bundle checksum detects accidental
changes, not an attacker who can replace both content and checksum.

Protect the bundle, its parent directory and the deployment configuration with
OS permissions: the Agent account needs read-only access; only the operator
should publish. The default `knowledge/` directory is ignored by Git. The
repository contains only a template and synthetic retrieval fixtures.

The scope comes from trusted deployment configuration, never model arguments,
user messages, or `thread_id`. Filtering happens before ranking and returns no
counts or contents for out-of-scope records. This is deployment-scoped filtering,
not a user-authentication/RBAC or multi-tenant service layer. Every caller of a
knowledge-enabled deployment must be authorized for its configured scope,
including callers entering through an existing diagnosis adapter.

## Configure

Create a private source file from the template:

```powershell
New-Item -ItemType Directory -Path knowledge -Force
Copy-Item docs/knowledge-source.example.json knowledge/reviewed-source.json
```

Edit that copy with actual reviewed content, reviewer, revision, expiry and
scope. The template is not an approved organization runbook. Validate first,
then explicitly attest to the reviewed content and publish a new file:

```powershell
python src/knowledge_cli.py validate --input knowledge/reviewed-source.json
python src/knowledge_cli.py publish --input knowledge/reviewed-source.json --output knowledge/bundle-v1.json --approve-reviewed-content
```

Publication refuses to overwrite an existing file. Publish subsequent revisions
under a new filename, validate them, and switch the configured path using your
normal deployment process. Protect both new and old artifacts with the same
permissions. A damaged or incomplete file is not served as partial knowledge.

For a development deployment using PostgreSQL 18:

```powershell
$env:SAFEDBA_KNOWLEDGE_ENABLED = "true"
$env:SAFEDBA_KNOWLEDGE_PATH = "knowledge/bundle-v1.json"
$env:SAFEDBA_KNOWLEDGE_SCOPE = "my-team"
$env:SAFEDBA_KNOWLEDGE_POSTGRES_MAJOR = "18"
python src/main.py "Consult our reviewed lock runbook and investigate the incident. Diagnosis only."
```

`SAFEDBA_ENV` supplies the environment filter. The major version must match the
actual target database; it is operator configuration, not automatic detection.
Neither path, scope, environment nor version appears in the model's tool
arguments. Restart the process after changing environment configuration.
Existing base tool schemas and `run_agent` arguments remain unchanged. With
knowledge disabled, the system prompt and result contract remain unchanged too.

With knowledge enabled, results additionally contain a `knowledge` object with
retrieval counts, statuses and source metadata. Tool traces contain
`knowledge_refs`, not `evidence_ref`, for knowledge calls. Full document text
is excluded from trace summaries and source metadata, although model answers
may quote it and existing answer/memory capture can retain those quotes.

## Bounds and failure behavior

- Bundle: at most 2 MB, 200 documents, 40,000 characters per document and 2,000
  chunks. Each chunk has at most 1,200 characters.
- Query: 1 to 500 characters. Up to four chunks per search and three retrieval
  attempts per Agent run, also subject to the Agent's existing tool budgets.
- Document replies use at most 12,000 characters per call and a 24,000-character
  run budget, with the existing tool-output cap applied as well. After depletion,
  only small bounded status messages are returned; these notices are outside the
  exhausted document budget and still subject to Agent call limits.
- Inapplicable, expired and future-review documents are excluded. Empty results
  are not invented sources. Unavailable/corrupted bundles produce a safe error
  and diagnosis can continue using live observations.
- Unknown, undelivered, revoked or expired citations trigger bounded answer
  repair and then a stopped result if unresolved. Active references are checked
  again before accepting a cited answer. Retrieval is not LangGraph replay and
  does not change incident durability.

## Evaluation

```powershell
python -m unittest discover -s tests -p "*knowledge*.py" -v
python src/knowledge_evaluate.py
python src/knowledge_evaluate.py --report logs/retrieval-new-report.json
```

The evaluator creates and removes its own temporary synthetic bundle. It never
calls an LLM or PostgreSQL and never modifies the deployment's knowledge file.
An optional report must be a new file. The labeled fixture contains eight
English/Chinese positive queries and five no-match/scope/expiry/version/environment
negative queries. At the fixed fixture date, all 13 pass: Hit@3 and MRR@3 are
1.0, and negative-case empty-result accuracy is 1.0. MRR uses the first relevant
chunk's rank; duplicate chunks do not improve it by disappearing from the count.

The explicit no-knowledge baseline retrieves nothing (positive hit rate 0.0).
This comparison measures reference availability on a small constructed fixture,
not diagnosis improvement or superiority over another retrieval system. It is
not a held-out production benchmark. Latency is reported separately and depends
on corpus size and the local machine.

Before enabling a real corpus, create a held-out set of genuine questions and
irrelevant/adversarial documents. Measure retrieval relevance, source entailment,
diagnostic correctness, abstention, latency and permission leakage. The original
2026-09-12 feature verification made no paid model calls. A subsequent
[paired real-model evaluation](RAG_AB_EVALUATION.md) on 2026-09-15 ran 12 synthetic
tasks twice with RAG off/on: strict passes were 8/24 versus 17/24. It also found
missed retrieval calls and refusal/citation regressions; see that report before
interpreting the aggregate as production accuracy.

The subsequent [reliability repair verification](RAG_REPAIR_VERIFICATION.md)
retains that baseline and all intermediate rounds. The final run on the same
development cases passed 10/24 with RAG disabled and 24/24 enabled. Runtime
prefetch and mixed-request completion checks are part of the deployed-code
treatment, not extra evaluator-side lookup instructions. Reusing the cases for
debugging makes them regression coverage, not independent validation.

## Extension points

- `knowledge_base.py`: validation, scope, `KnowledgeRetriever` protocol and local
  lexical search. Alternative implementations must enforce the same scope and
  support current-reference validation.
- `agent_knowledge.py`: optional run-local tool binding, knowledge budgets and
  citation handling. The global database-tool registry is not mutated.
- `knowledge_cli.py`: operator-only validation/publication.
- `knowledge_evaluate.py`: independently runnable retrieval evaluation.

Vector or hybrid retrieval can later replace the lexical backend after a
real-corpus evaluation demonstrates benefit. A retriever can be exposed through
an Agent tool without replacing the Agent orchestration; see the
[LangChain retrieval documentation](https://docs.langchain.com/oss/python/deepagents/retrieval).
