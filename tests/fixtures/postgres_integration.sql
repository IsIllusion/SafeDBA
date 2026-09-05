-- Lightweight PostgreSQL fixture for automated integration tests.
-- Keep this separate from init.sql: CI validates security boundaries and
-- tool behavior without generating the full benchmark data set.

-- A fresh, per-instance marker owned by bootstrap (not by an Agent role).
-- Destructive integration scenarios must match its UUID before doing work.
CREATE SCHEMA safedba_test_control;
REVOKE ALL ON SCHEMA safedba_test_control FROM PUBLIC;
CREATE TABLE safedba_test_control.instance (
    instance_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    purpose TEXT NOT NULL CHECK (purpose = 'DISPOSABLE_SAFEDBA_INTEGRATION'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO safedba_test_control.instance (purpose)
VALUES ('DISPOSABLE_SAFEDBA_INTEGRATION');

CREATE ROLE safedba_observer
    LOGIN PASSWORD 'local-observer-only';
CREATE ROLE safedba_executor
    LOGIN PASSWORD 'local-executor-only';
CREATE ROLE safedba_terminator
    LOGIN PASSWORD 'local-terminator-only';

GRANT USAGE ON SCHEMA safedba_test_control TO safedba_observer;
GRANT SELECT ON safedba_test_control.instance TO safedba_observer;

REVOKE CONNECT, TEMPORARY ON DATABASE benchmark FROM PUBLIC;
GRANT CONNECT ON DATABASE benchmark
    TO safedba_observer, safedba_executor, safedba_terminator;

GRANT pg_read_all_stats TO safedba_observer;
GRANT pg_read_all_stats TO safedba_terminator;
GRANT pg_signal_backend TO safedba_terminator;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO safedba_executor;
GRANT USAGE ON SCHEMA public TO safedba_observer;

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC;
REVOKE EXECUTE ON ALL PROCEDURES IN SCHEMA public FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE safedba_executor
    IN SCHEMA public
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE safedba_executor
    IN SCHEMA public
    REVOKE EXECUTE ON ROUTINES FROM PUBLIC;

ALTER ROLE safedba_observer SET default_transaction_read_only = on;
ALTER ROLE safedba_observer SET statement_timeout = '30s';
ALTER ROLE safedba_observer SET lock_timeout = '3s';
ALTER ROLE safedba_observer SET search_path = pg_catalog, public;

ALTER ROLE safedba_executor SET statement_timeout = '30s';
ALTER ROLE safedba_executor SET lock_timeout = '3s';
ALTER ROLE safedba_executor SET search_path = pg_catalog, public;

ALTER ROLE safedba_terminator SET default_transaction_read_only = on;
ALTER ROLE safedba_terminator SET statement_timeout = '30s';
ALTER ROLE safedba_terminator SET lock_timeout = '3s';
ALTER ROLE safedba_terminator SET search_path = pg_catalog, public;

SET ROLE safedba_executor;

CREATE TABLE integration_probe (
    id BIGINT PRIMARY KEY,
    payload TEXT NOT NULL
);

INSERT INTO integration_probe (id, payload)
SELECT value, 'probe_' || value
FROM generate_series(1, 20) AS value;

ANALYZE integration_probe;

GRANT SELECT ON ALL TABLES IN SCHEMA public TO safedba_observer;
ALTER DEFAULT PRIVILEGES FOR ROLE safedba_executor
    IN SCHEMA public
    GRANT SELECT ON TABLES TO safedba_observer;

RESET ROLE;
