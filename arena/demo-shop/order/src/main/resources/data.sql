INSERT INTO product (sku, name, price) VALUES
    ('SKU-1', 'Mechanical keyboard', 299.00),
    ('SKU-2', 'Wireless mouse', 89.90),
    ('SKU-3', 'USB-C hub', 129.50)
ON CONFLICT (sku) DO NOTHING;

INSERT INTO coupon (code, type, value, threshold) VALUES
    ('SAVE20', 'AMOUNT', 20.00, 100.00),
    ('OFF10', 'PERCENT', 10.00, 0.00)
ON CONFLICT (code) DO NOTHING;
