"""Fail-closed target checks for opt-in, mutating integration tests only."""

import uuid
import psycopg


def verify_disposable_target(observer, executor, terminator, instance_id):
    try:
        expected_id = str(uuid.UUID(instance_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("An explicit disposable test instance UUID is required.") from exc
    targets = set()
    for config, expected_role in [
        (observer, "safedba_observer"), (executor, "safedba_executor"),
        (terminator, "safedba_terminator"),
    ]:
        if (
            config.get("host") != "127.0.0.1"
            or config.get("dbname") != "benchmark"
            or config.get("user") != expected_role
            or config.get("service") or config.get("hostaddr")
        ):
            raise RuntimeError("Integration scenarios require explicit loopback test connections and dedicated roles.")
        try:
            port = int(config.get("port"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Integration connections require explicit numeric ports.") from exc
        if not 1 <= port <= 65535:
            raise RuntimeError("Invalid integration port.")
        targets.add((config["host"], port, config["dbname"]))
    if len(targets) != 1:
        raise RuntimeError("All integration connections must use the same disposable target.")

    with psycopg.connect(**observer) as connection:
        rows = connection.execute("""
            SELECT instance.instance_id::text, instance.purpose,
                   current_database(), current_user,
                   pg_get_userbyid(relation.relowner),
                   pg_get_userbyid(namespace.nspowner)
            FROM safedba_test_control.instance AS instance
            JOIN pg_class AS relation
              ON relation.oid = 'safedba_test_control.instance'::regclass
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        """).fetchall()
    expected = (
        expected_id, "DISPOSABLE_SAFEDBA_INTEGRATION", "benchmark",
        "safedba_observer", "safedba_bootstrap", "safedba_bootstrap",
    )
    if rows != [expected]:
        raise RuntimeError("Disposable instance marker or its ownership did not match; refusing integration scenarios.")
    return expected_id
