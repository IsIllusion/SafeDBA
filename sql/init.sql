CREATE TABLE customers (
    id BIGINT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE orders (
    id BIGINT PRIMARY KEY,
    customer_id BIGINT NOT NULL,
    amount NUMERIC(10, 2) NOT NULL,
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
    (random() * 99999 + 1)::BIGINT,
    (random() * 1000)::NUMERIC(10, 2),
    NOW() - random() * INTERVAL '365 days'
FROM generate_series(1, 1000000) AS i;

ANALYZE;