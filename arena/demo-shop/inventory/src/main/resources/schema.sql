CREATE TABLE IF NOT EXISTS stock (
    sku       TEXT PRIMARY KEY,
    available INT NOT NULL CHECK (available >= 0)
);
