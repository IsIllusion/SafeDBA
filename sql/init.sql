-- Runtime roles are deliberately separate from the bootstrap superuser.
CREATE ROLE safedba_observer
    LOGIN PASSWORD 'local-observer-only';

CREATE ROLE safedba_executor
    LOGIN PASSWORD 'local-executor-only';

CREATE ROLE safedba_terminator
    LOGIN PASSWORD 'local-terminator-only';

REVOKE CONNECT, TEMPORARY ON DATABASE benchmark FROM PUBLIC;

GRANT CONNECT ON DATABASE benchmark
    TO safedba_observer, safedba_executor,
       safedba_terminator;

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

ALTER ROLE safedba_observer
    SET default_transaction_read_only = on;
ALTER ROLE safedba_observer
    SET statement_timeout = '30s';
ALTER ROLE safedba_observer
    SET lock_timeout = '3s';
ALTER ROLE safedba_observer
    SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE safedba_observer
    SET search_path = pg_catalog, public;
ALTER ROLE safedba_observer
    SET standard_conforming_strings = on;
ALTER ROLE safedba_observer
    SET temp_file_limit = '64MB';
ALTER ROLE safedba_observer
    SET work_mem = '16MB';
ALTER ROLE safedba_observer
    SET max_parallel_workers_per_gather = 2;

ALTER ROLE safedba_executor
    SET statement_timeout = '30s';
ALTER ROLE safedba_executor
    SET lock_timeout = '3s';
ALTER ROLE safedba_executor
    SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE safedba_executor
    SET search_path = pg_catalog, public;
ALTER ROLE safedba_executor
    SET temp_file_limit = '256MB';
ALTER ROLE safedba_executor
    SET maintenance_work_mem = '128MB';

ALTER ROLE safedba_terminator
    SET default_transaction_read_only = on;
ALTER ROLE safedba_terminator
    SET statement_timeout = '30s';
ALTER ROLE safedba_terminator
    SET lock_timeout = '3s';
ALTER ROLE safedba_terminator
    SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE safedba_terminator
    SET search_path = pg_catalog, public;
ALTER ROLE safedba_terminator
    SET temp_file_limit = '16MB';
ALTER ROLE safedba_terminator
    SET work_mem = '4MB';

SET ROLE safedba_executor;

CREATE TABLE safedba_benchmark_marker (
    marker TEXT PRIMARY KEY,
    created_at TIMESTAMP NOT NULL
);

INSERT INTO safedba_benchmark_marker (
    marker,
    created_at
) VALUES (
    'SAFE_TO_RUN_DESTRUCTIVE_BENCHMARKS',
    TIMESTAMP '2026-01-01 00:00:00'
);

CREATE TABLE customers (
    id BIGINT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE orders (
    id BIGINT PRIMARY KEY,
    customer_id BIGINT NOT NULL
        REFERENCES customers(id),
    amount NUMERIC(10, 2) NOT NULL
        CHECK (amount >= 0),
    created_at TIMESTAMP NOT NULL
);

INSERT INTO customers
SELECT
    i,
    'customer_' || i
FROM generate_series(1, 100000) AS i;

INSERT INTO orders
SELECT
    i,
    (((i::BIGINT * 48271) % 100000) + 1),
    (((i::BIGINT * 7919) % 100000)::NUMERIC / 100),
    TIMESTAMP '2026-01-01 00:00:00'
        + ((i - 1) % 365) * INTERVAL '1 day'
        + ((i * 37) % 86400) * INTERVAL '1 second'
FROM generate_series(1, 1000000) AS i;

ANALYZE customers;
ANALYZE orders;

GRANT SELECT ON ALL TABLES IN SCHEMA public
    TO safedba_observer;

ALTER DEFAULT PRIVILEGES FOR ROLE safedba_executor
    IN SCHEMA public
    GRANT SELECT ON TABLES TO safedba_observer;

RESET ROLE;
