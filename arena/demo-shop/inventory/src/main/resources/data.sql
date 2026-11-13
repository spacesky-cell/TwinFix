INSERT INTO stock (sku, available) VALUES
    ('SKU-1', 100),
    ('SKU-2', 500),
    ('SKU-3', 50)
ON CONFLICT (sku) DO NOTHING;
