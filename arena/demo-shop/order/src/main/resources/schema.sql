CREATE TABLE IF NOT EXISTS product (
    sku   TEXT PRIMARY KEY,
    name  TEXT NOT NULL,
    price NUMERIC(12, 2) NOT NULL
);

CREATE TABLE IF NOT EXISTS coupon (
    code      TEXT PRIMARY KEY,
    type      TEXT NOT NULL CHECK (type IN ('AMOUNT', 'PERCENT')),
    value     NUMERIC(12, 2) NOT NULL,
    threshold NUMERIC(12, 2) NOT NULL DEFAULT 0,
    active    BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS orders (
    id          BIGSERIAL PRIMARY KEY,
    customer    TEXT NOT NULL,
    subtotal    NUMERIC(12, 2) NOT NULL,
    discount    NUMERIC(12, 2) NOT NULL,
    total       NUMERIC(12, 2) NOT NULL,
    coupon_code TEXT,
    status      TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS order_line (
    id         BIGSERIAL PRIMARY KEY,
    order_id   BIGINT NOT NULL REFERENCES orders (id),
    sku        TEXT NOT NULL,
    quantity   INT NOT NULL,
    unit_price NUMERIC(12, 2) NOT NULL
);
