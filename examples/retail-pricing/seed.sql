CREATE TABLE IF NOT EXISTS pricing_rules (
    sku TEXT PRIMARY KEY,
    brand TEXT NOT NULL,
    market TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    classification TEXT NOT NULL,
    allowed_purposes TEXT[] NOT NULL,
    source_version BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

INSERT INTO pricing_rules (
    sku, brand, market, title, content, classification, allowed_purposes,
    source_version, updated_at
) VALUES
    (
        'NORTHSTAR-104', 'NORTHSTAR', 'US', 'Promotional floor for NORTHSTAR-104',
        'SKU NORTHSTAR-104 has a promotional price floor of 20 percent below list price.',
        'internal', ARRAY['pricing-analysis'], 1, now()
    ),
    (
        'NORTHSTAR-205', 'NORTHSTAR', 'US', 'Premium footwear margin guidance',
        'Protect contribution margin on premium footwear; discounts above 15 percent require approval.',
        'internal', ARRAY['pricing-analysis'], 1, now()
    ),
    (
        'NORTHSTAR-SECRET', 'NORTHSTAR', 'US', 'Restricted acquisition plan',
        'This synthetic record exists solely to prove candidate-set authorization.',
        'restricted', ARRAY['restricted-data'], 1, now()
    )
ON CONFLICT (sku) DO UPDATE SET
    brand = EXCLUDED.brand,
    market = EXCLUDED.market,
    title = EXCLUDED.title,
    content = EXCLUDED.content,
    classification = EXCLUDED.classification,
    allowed_purposes = EXCLUDED.allowed_purposes,
    source_version = pricing_rules.source_version + 1,
    updated_at = now();
