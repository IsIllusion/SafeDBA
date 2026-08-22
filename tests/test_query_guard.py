import sys
from pathlib import Path
import unittest


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from query_guard import (  # noqa: E402
    QuerySafetyError,
    ensure_read_only_query,
)


class QueryGuardTests(unittest.TestCase):
    def test_accepts_one_select_and_removes_trailing_semicolon(self):
        self.assertEqual(
            ensure_read_only_query(
                "  SELECT * FROM orders;  "
            ),
            "SELECT * FROM orders",
        )

    def test_semicolons_in_literals_comments_and_identifiers_are_not_statements(self):
        queries = [
            "SELECT 'a;b' AS value;",
            "SELECT $$a;b$$ AS value;",
            'SELECT "odd;name" FROM orders;',
            "SELECT 1 /* ; */; -- ;\n",
        ]

        for query in queries:
            with self.subTest(query=query):
                self.assertTrue(
                    ensure_read_only_query(query)
                    .upper()
                    .startswith("SELECT")
                )

    def test_rejects_multiple_statements(self):
        with self.assertRaises(QuerySafetyError):
            ensure_read_only_query(
                "SELECT 1; SELECT 2"
            )

    def test_rejects_non_select_and_with(self):
        for query in [
            "DELETE FROM orders",
            "WITH q AS (SELECT 1) SELECT * FROM q",
            "CALL maintenance()",
        ]:
            with self.subTest(query=query):
                with self.assertRaises(QuerySafetyError):
                    ensure_read_only_query(query)

    def test_rejects_select_into_and_row_locking(self):
        for query in [
            "SELECT * INTO copied_orders FROM orders",
            "SELECT * FROM orders FOR UPDATE",
            "SELECT * FROM orders FOR NO KEY UPDATE",
            "SELECT * FROM orders FOR SHARE",
            "SELECT * FROM orders FOR KEY SHARE",
        ]:
            with self.subTest(query=query):
                with self.assertRaises(QuerySafetyError):
                    ensure_read_only_query(query)

    def test_rejects_blocked_functions_with_obfuscation(self):
        queries = [
            "SELECT pg_terminate_backend(123)",
            "SELECT pg_catalog.pg_terminate_backend(123)",
            'SELECT pg_catalog."pg_terminate_backend"(123)',
            "SELECT pg_sleep /* do not run */ (3600)",
            "SELECT nextval('business_sequence')",
            "SELECT set_config('statement_timeout', '0', false)",
            "SELECT dblink_exec('remote', 'DELETE FROM t')",
            "SELECT pg_read_file('/etc/passwd')",
        ]

        for query in queries:
            with self.subTest(query=query):
                with self.assertRaises(QuerySafetyError):
                    ensure_read_only_query(query)

    def test_rejects_standard_string_backslash_multistatement_bypass(self):
        queries = [
            r"SELECT '\'; SELECT pg_sleep(1) -- '",
            r"SELECT '\'; SELECT pg_notify('channel', 'payload') -- '",
            (
                r"SELECT '\'; SET LOCAL statement_timeout=0; "
                r"SELECT pg_sleep(3600) -- '"
            ),
        ]

        for query in queries:
            with self.subTest(query=query):
                with self.assertRaises(QuerySafetyError):
                    ensure_read_only_query(query)

    def test_rejects_postgresql_unicode_escape_syntax(self):
        queries = [
            r'SELECT U&"pg_sl\0065ep"(1)',
            r'SELECT U&"pg_terminate_ba\0063kend"(123)',
            r'SELECT pg_catalog.U&"pg_notif\0079"(\'c\', \'p\')',
            r'SELECT U&"ne\0078tval"(\'seq\')',
        ]

        for query in queries:
            with self.subTest(query=query):
                with self.assertRaises(QuerySafetyError):
                    ensure_read_only_query(query)

    def test_escape_string_backslash_rules_remain_supported(self):
        self.assertEqual(
            ensure_read_only_query(
                r"SELECT E'escaped\'semicolon;inside'"
            ),
            r"SELECT E'escaped\'semicolon;inside'",
        )

    def test_allows_ordinary_read_functions(self):
        self.assertEqual(
            ensure_read_only_query(
                "SELECT count(*), max(amount) FROM orders"
            ),
            "SELECT count(*), max(amount) FROM orders",
        )

    def test_rejects_unterminated_constructs(self):
        for query in [
            "SELECT 'unterminated",
            "SELECT $$unterminated",
            "SELECT 1 /* unterminated",
            'SELECT "unterminated',
        ]:
            with self.subTest(query=query):
                with self.assertRaises(QuerySafetyError):
                    ensure_read_only_query(query)

    def test_enforces_length_limit(self):
        with self.assertRaises(QuerySafetyError):
            ensure_read_only_query(
                "SELECT 12345",
                max_length=5,
            )


if __name__ == "__main__":
    unittest.main()
