CREATE TABLE IF NOT EXISTS pricing_rules (
    sku TEXT PRIMARY KEY,
    brand TEXT NOT NULL,
    market TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    max_discount_percent SMALLINT NOT NULL CHECK (max_discount_percent BETWEEN 0 AND 100),
    classification TEXT NOT NULL,
    allowed_purposes TEXT[] NOT NULL,
    source_version BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

-- Keep the local showcase seed repeatable when an older named volume already exists.
ALTER TABLE pricing_rules
    ADD COLUMN IF NOT EXISTS max_discount_percent SMALLINT;
UPDATE pricing_rules
SET max_discount_percent = 0
WHERE max_discount_percent IS NULL;
ALTER TABLE pricing_rules
    ALTER COLUMN max_discount_percent SET NOT NULL;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'pricing_rules_max_discount_percent_range'
    ) THEN
        ALTER TABLE pricing_rules
            ADD CONSTRAINT pricing_rules_max_discount_percent_range
            CHECK (max_discount_percent BETWEEN 0 AND 100);
    END IF;
END $$;

INSERT INTO pricing_rules (
    sku, brand, market, title, content, max_discount_percent, classification, allowed_purposes,
    source_version, updated_at
) VALUES
    (
        'NORTHSTAR-104', 'NORTHSTAR', 'US', 'Promotional floor for NORTHSTAR-104',
        'SKU NORTHSTAR-104 has a promotional price floor of 20 percent below list price.', 20,
        'internal', ARRAY['pricing-analysis'], 1, now()
    ),
    (
        'NORTHSTAR-205', 'NORTHSTAR', 'US', 'Premium footwear margin guidance',
        'Protect contribution margin on premium footwear; discounts above 15 percent require approval.', 15,
        'internal', ARRAY['pricing-analysis'], 1, now()
    ),
    (
        'NORTHSTAR-SECRET', 'NORTHSTAR', 'US', 'Restricted acquisition plan',
        'This synthetic record exists solely to prove candidate-set authorization.', 0,
        'restricted', ARRAY['restricted-data'], 1, now()
    )
ON CONFLICT (sku) DO UPDATE SET
    brand = EXCLUDED.brand,
    market = EXCLUDED.market,
    title = EXCLUDED.title,
    content = EXCLUDED.content,
    max_discount_percent = EXCLUDED.max_discount_percent,
    classification = EXCLUDED.classification,
    allowed_purposes = EXCLUDED.allowed_purposes,
    source_version = pricing_rules.source_version + 1,
    updated_at = now();
