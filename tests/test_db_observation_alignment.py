"""Observation extraction contracts pinned before the refactor.

The narrow reverse transform recovers the exact original function AST, whose
hash is checked before replay. No duplicate legacy runtime is shipped or loaded
by the application. SQL literals, parameters, ordering and result fields remain
inside the frozen hash; only dependency wiring and empty Python-version AST
metadata are normalized.
"""

import ast
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from test_db_tools_safety import DB_TOOLS
import db_catalog
import db_operational
import db_sessions
from db_observation_context import ObservationDependencies

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CONTRACT = json.loads(
    (ROOT / "tests/fixtures/db_observation_contract.json").read_text(encoding="utf-8")
)
MODULES = {
    "db_catalog": db_catalog,
    "db_operational": db_operational,
    "db_sessions": db_sessions,
}
NOW = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)


def source_functions(module):
    tree = ast.parse((SRC / (module + ".py")).read_text(encoding="utf-8-sig"))
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


def ast_hash(node):
    node = deepcopy(node)
    for item in ast.walk(node):
        if hasattr(item, "type_params"):
            # Python 3.12 adds this empty field; generics are not normalized.
            if item.type_params:
                raise AssertionError("Unexpected generic function in frozen contract")
            del item.type_params
    return hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()


class RestoreDependencies(ast.NodeTransformer):
    names = {
        "connect": "readonly_connection",
        "redact_query": "truncate_observed_query",
        "health": "get_database_health",
        "max_rows": "DB_MAX_OBSERVATION_ROWS",
        "long_query_seconds": "HEALTH_LONG_QUERY_SECONDS",
        "long_transaction_seconds": "HEALTH_LONG_TRANSACTION_SECONDS",
    }

    def visit_Call(self, node):
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "context"
            and node.func.attr == "now"
        ):
            if node.args or node.keywords:
                raise AssertionError("Clock contract changed")
            return ast.parse("datetime.now(timezone.utc)", mode="eval").body
        return self.generic_visit(node)

    def visit_Attribute(self, node):
        if isinstance(node.value, ast.Name) and node.value.id == "context":
            if node.attr == "database_name":
                return ast.parse('DB_CONFIG.get("dbname")', mode="eval").body
            if node.attr not in self.names:
                raise AssertionError("Unknown observation dependency: " + node.attr)
            return ast.Name(id=self.names[node.attr], ctx=node.ctx)
        return self.generic_visit(node)


def restored_function(name):
    node = deepcopy(source_functions(CONTRACT["extracted"][name])[name])
    assert [arg.arg for arg in node.args.kwonlyargs] == ["context"]
    assert isinstance(node.args.kwonlyargs[0].annotation, ast.Name)
    assert node.args.kwonlyargs[0].annotation.id == "ObservationDependencies"
    assert node.args.kw_defaults == [None]
    node.args.kwonlyargs = []
    node.args.kw_defaults = []
    node = RestoreDependencies().visit(node)
    assert ast_hash(node) == CONTRACT["functions"][name]["ast_sha256"], name
    return ast.fix_missing_locations(node)


class FrozenClock:
    @staticmethod
    def now(tz):
        assert tz is timezone.utc
        return NOW


class Transcript:
    def __init__(self, one=(), many=(), fail_at=None):
        self.one = deque(deepcopy(one))
        self.many = deque(deepcopy(many))
        self.calls = []
        self.execute_count = 0
        self.fail_at = fail_at

    @contextmanager
    def connect(self):
        self.calls.append(("open",))
        try:
            yield self
        finally:
            self.calls.append(("close",))

    @contextmanager
    def cursor(self):
        self.calls.append(("cursor_open",))
        try:
            yield self
        finally:
            self.calls.append(("cursor_close",))

    def execute(self, statement, params=None, **kwargs):
        self.calls.append(("execute", statement, deepcopy(params), deepcopy(kwargs)))
        self.execute_count += 1
        if self.execute_count == self.fail_at:
            raise RuntimeError("Synthetic observation failure")

    def fetchone(self):
        self.calls.append(("fetchone",))
        return self.one.popleft()

    def fetchall(self):
        self.calls.append(("fetchall",))
        return self.many.popleft()


def health_row():
    return ("synthetic", 14, 2, 1, 1, 0, 1, 12.5, 30.25)


def lock_row():
    return (
        101,
        "app",
        "synthetic",
        "waiter",
        "active",
        "Lock",
        "transactionid",
        "SELECT blocked",
        NOW,
        NOW,
        NOW,
        5.0,
        6.0,
        202,
        "app",
        "blocker",
        "synthetic",
        "client backend",
        "idle in transaction",
        "Client",
        "ClientRead",
        "SELECT blocker",
        NOW,
        NOW,
        NOW,
        30.0,
        [],
    )


class ObservationAlignmentTests(unittest.TestCase):
    def test_frozen_hash_does_not_normalize_sql_or_output_field_changes(self):
        original = restored_function("get_indexes")
        expected = CONTRACT["functions"]["get_indexes"]["ast_sha256"]
        for value, replacement in (
            ("pg_indexes", "pg_tables"),
            ("index_name", "renamed_field"),
        ):
            changed = deepcopy(original)
            found = False
            for node in ast.walk(changed):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and value in node.value
                ):
                    node.value = node.value.replace(value, replacement)
                    found = True
                    break
            self.assertTrue(found)
            self.assertNotEqual(ast_hash(changed), expected)

    def test_parallel_observation_contexts_do_not_share_settings_or_results(self):
        base = DB_TOOLS._observation_dependencies()
        first = Transcript(many=[[]])
        second = Transcript(many=[[]])
        contexts = [
            replace(
                base,
                connect=first.connect,
                database_name="scope-a",
                max_rows=1,
                now=lambda: NOW,
            ),
            replace(
                base,
                connect=second.connect,
                database_name="scope-b",
                max_rows=7,
                now=lambda: NOW,
            ),
        ]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda context: db_sessions.get_lock_graph_snapshot(
                        context=context
                    ),
                    contexts,
                )
            )
        self.assertEqual(
            [row["database_name"] for row in results], ["scope-a", "scope-b"]
        )
        self.assertEqual(
            [call[2] for call in first.calls if call[0] == "execute"], [(2,)]
        )
        self.assertEqual(
            [call[2] for call in second.calls if call[0] == "execute"], [(8,)]
        )

    def test_all_existing_function_signatures_are_preserved(self):
        current = source_functions("db_tools")
        self.assertEqual(
            set(current), set(CONTRACT["functions"]) | {"_observation_dependencies"}
        )
        for name, info in CONTRACT["functions"].items():
            with self.subTest(function=name):
                node = current[name]
                signature = ast.Tuple(
                    elts=[node.args, node.returns or ast.Constant(value=None)],
                    ctx=ast.Load(),
                )
                self.assertEqual(ast_hash(signature), info["signature_sha256"])

    def test_extracted_functions_recover_exact_frozen_asts(self):
        for name in CONTRACT["extracted"]:
            with self.subTest(function=name):
                restored_function(name)

    def test_connection_policy_and_mutation_functions_are_unchanged(self):
        current = source_functions("db_tools")
        for name, info in CONTRACT["functions"].items():
            if name not in CONTRACT["extracted"]:
                with self.subTest(function=name):
                    self.assertEqual(ast_hash(current[name]), info["ast_sha256"])

    def test_observation_modules_import_without_deployment_or_driver(self):
        code = (
            f"import sys; sys.path.insert(0, {str(SRC)!r}); "
            "import db_catalog, db_operational, db_sessions; "
            "assert not ({'config','db_tools','psycopg','executor'} & set(sys.modules))"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dependencies_are_immutable_and_have_no_credential_fields(self):
        context = DB_TOOLS._observation_dependencies()
        self.assertEqual(
            {field.name for field in fields(context)},
            {
                "connect",
                "redact_query",
                "health",
                "now",
                "database_name",
                "max_rows",
                "long_query_seconds",
                "long_transaction_seconds",
            },
        )
        with self.assertRaises(FrozenInstanceError):
            context.max_rows = 1

    def test_every_facade_dispatch_preserves_arguments_and_live_dependencies(self):
        for name, module in CONTRACT["extracted"].items():
            with self.subTest(function=name):
                sentinel = object()
                function = getattr(DB_TOOLS, name)
                args = [
                    "synthetic_" + key for key in inspect.signature(function).parameters
                ]
                with patch.object(
                    MODULES[module], name, return_value=sentinel
                ) as called:
                    self.assertIs(function(*args), sentinel)
                passed_args, kwargs = called.call_args
                self.assertEqual(passed_args, tuple(args))
                self.assertEqual(set(kwargs), {"context"})
                context = kwargs["context"]
                self.assertIsInstance(context, ObservationDependencies)
                self.assertIs(context.connect, DB_TOOLS.readonly_connection)
                self.assertIs(context.redact_query, DB_TOOLS.truncate_observed_query)
                self.assertIs(context.health, DB_TOOLS.get_database_health)
                self.assertEqual(
                    context.long_query_seconds, DB_TOOLS.HEALTH_LONG_QUERY_SECONDS
                )
                self.assertEqual(
                    context.long_transaction_seconds,
                    DB_TOOLS.HEALTH_LONG_TRANSACTION_SECONDS,
                )

    def test_contexts_are_fresh_and_overrides_are_not_cached(self):
        with patch.object(DB_TOOLS, "DB_MAX_OBSERVATION_ROWS", 3):
            first = DB_TOOLS._observation_dependencies()
        with patch.object(DB_TOOLS, "DB_MAX_OBSERVATION_ROWS", 7):
            second = DB_TOOLS._observation_dependencies()
        self.assertIsNot(first, second)
        self.assertEqual((first.max_rows, second.max_rows), (3, 7))

    def test_all_observations_still_pass_the_connection_policy_gate(self):
        for name in CONTRACT["extracted"]:
            with self.subTest(function=name):
                function = getattr(DB_TOOLS, name)
                args = ["synthetic" for _ in inspect.signature(function).parameters]
                with patch.object(
                    DB_TOOLS, "require_operation", side_effect=PermissionError("denied")
                ) as gate, patch.object(DB_TOOLS.psycopg, "connect") as connect:
                    with self.assertRaisesRegex(PermissionError, "denied"):
                        function(*args)
                    gate.assert_called_once_with("OBSERVE")
                    connect.assert_not_called()

    def compare(self, name, args=(), *, one=(), many=(), fail_at=None, max_rows=2):
        outcomes = []
        for legacy in (True, False):
            trace = Transcript(one, many, fail_at)
            with patch.multiple(
                DB_TOOLS,
                readonly_connection=trace.connect,
                datetime=FrozenClock,
                DB_CONFIG={"dbname": "synthetic"},
                DB_MAX_OBSERVATION_ROWS=max_rows,
                DB_INCLUDE_OBSERVED_QUERY_TEXT=False,
            ):
                if legacy:
                    scope = dict(vars(DB_TOOLS))
                    tree = ast.Module(
                        body=[restored_function(n) for n in CONTRACT["extracted"]],
                        type_ignores=[],
                    )
                    exec(compile(tree, "<frozen-observation-contract>", "exec"), scope)
                    function = scope[name]
                else:
                    function = getattr(DB_TOOLS, name)
                try:
                    result = ("returned", function(*args))
                except Exception as exc:
                    result = ("raised", type(exc).__name__, str(exc))
            outcomes.append((result, trace.calls, list(trace.one), list(trace.many)))
        self.assertEqual(outcomes[0], outcomes[1])
        return outcomes[1][0]

    def test_catalog_results_and_parameter_order_align(self):
        self.assertEqual(
            self.compare(
                "get_indexes", ("synthetic",), many=[[("idx_x", "definition")]]
            )[1],
            [{"index_name": "idx_x", "index_definition": "definition"}],
        )
        self.assertEqual(
            self.compare(
                "get_table_columns", ("synthetic",), many=[[("id",), ("value",)]]
            )[1],
            ["id", "value"],
        )
        self.assertFalse(
            self.compare(
                "get_column_info",
                ("synthetic", "id"),
                one=[("id", "bigint", "int8", "NO")],
            )[1]["is_nullable"]
        )

    def test_catalog_missing_and_partial_statistics_align(self):
        self.assertIsNone(
            self.compare("get_column_info", ("synthetic", "absent"), one=[None])[1]
        )
        column = ("id", 0.1, 25.0, "{1}", [0.2], "{1,2}")
        table = (50, 5, NOW, None)
        for column_row, table_row in (
            (column, table),
            (None, table),
            (column, None),
            (None, None),
        ):
            with self.subTest(
                column=column_row is not None, table=table_row is not None
            ):
                result = self.compare(
                    "get_column_stats", ("synthetic", "id"), one=[column_row, table_row]
                )
                self.assertEqual(result[0], "returned")
                if column_row is None and table_row is None:
                    self.assertIsNone(result[1])

    def test_health_results_and_missing_data_align(self):
        self.assertEqual(
            self.compare("get_database_health", one=[health_row()])[1]["database"],
            "synthetic",
        )
        self.assertEqual(self.compare("get_database_health", one=[None])[0], "raised")

    def operational_fixture(self, standby=False):
        replication = (
            [("streaming", "slot", "0/1", "0/1", "0/1", NOW, None, NOW)]
            if standby
            else [("standby", "streaming", "async", 128, None, 1.0, 2.0, NOW, None)]
        )
        return {
            "one": [
                health_row(),
                ("synthetic", 100, 3, 2, 14, 10, 2, 1),
                (standby,),
                (1000, 2, 30, 0, NOW),
            ],
            "many": [
                [
                    (
                        "public",
                        "events",
                        100,
                        25,
                        20.0,
                        NOW,
                        None,
                        1,
                        0,
                        NOW,
                        None,
                        15,
                        200,
                        7.5,
                    )
                ],
                replication,
                [("public", "events", "r", 700, 300, 1000)],
            ],
        }

    def test_operational_primary_and_standby_align(self):
        for standby in (False, True):
            with self.subTest(standby=standby):
                result = self.compare(
                    "get_operational_snapshot", **self.operational_fixture(standby)
                )
                self.assertEqual(result[0], "returned")
                self.assertEqual(result[1]["captured_at"], NOW.isoformat())
                self.assertEqual(
                    result[1]["replication"]["server_role"],
                    "standby" if standby else "primary",
                )
                self.assertFalse(
                    result[1]["storage_usage"]["filesystem_free_space_available"]
                )

    def test_operational_missing_rows_and_query_failures_align(self):
        for missing_at in range(4):
            fixture = self.operational_fixture()
            fixture["one"][missing_at] = None
            with self.subTest(missing_row=missing_at):
                self.assertEqual(
                    self.compare("get_operational_snapshot", **fixture)[0], "raised"
                )
        for fail_at in range(1, 8):
            with self.subTest(query_failure=fail_at):
                self.assertEqual(
                    self.compare(
                        "get_operational_snapshot",
                        fail_at=fail_at,
                        **self.operational_fixture(),
                    )[0],
                    "raised",
                )

    def test_session_mapping_nulls_and_redaction_align(self):
        row = (
            101,
            "app",
            "worker",
            "active",
            "Lock",
            "transactionid",
            "SELECT private",
            NOW,
            None,
            5.0,
            None,
            1,
        )
        for name, values in (
            ("get_active_sessions", row),
            ("get_transaction_sessions", (*row[:11], [202])),
        ):
            with self.subTest(function=name):
                result = self.compare(name, many=[[values]])
                self.assertEqual(result[0], "returned")
                self.assertNotIn("SELECT private", result[1][0]["query"])
                self.assertIn("sha256=", result[1][0]["query"])
                self.assertIsNone(result[1][0]["transaction_start"])

    def test_lock_snapshot_identity_digest_limits_and_empty_results_align(self):
        for count in (0, 1, 3):
            with self.subTest(rows=count):
                result = self.compare(
                    "get_lock_graph_snapshot", many=[[lock_row()] * count]
                )
                self.assertEqual(result[0], "returned")
                snapshot = result[1]
                self.assertEqual(snapshot["database_name"], "synthetic")
                self.assertEqual(snapshot["captured_at"], NOW.isoformat())
                self.assertEqual(snapshot["row_count"], min(count, 2))
                self.assertEqual(snapshot["truncated"], count > 2)
                self.assertEqual(len(snapshot["snapshot_digest"]), 64)

    def test_observation_errors_keep_their_type_and_close_connections(self):
        for name in CONTRACT["extracted"]:
            function = getattr(DB_TOOLS, name)
            args = ["synthetic" for _ in inspect.signature(function).parameters]
            with self.subTest(function=name):
                result = self.compare(name, args, fail_at=1)
                self.assertEqual(
                    result, ("raised", "RuntimeError", "Synthetic observation failure")
                )


if __name__ == "__main__":
    unittest.main()
