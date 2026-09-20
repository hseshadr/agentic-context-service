CREATE TABLE IF NOT EXISTS fulfillment_rules (
    sku TEXT PRIMARY KEY,
    brand TEXT NOT NULL,
    market TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    classification TEXT NOT NULL,
    allowed_purposes TEXT[] NOT NULL,
    available_to_promise INTEGER NOT NULL CHECK (available_to_promise >= 0),
    carrier_cutoff_open BOOLEAN NOT NULL,
    address_hold BOOLEAN NOT NULL,
    risk_hold BOOLEAN NOT NULL,
    source_version BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    showcase_run_id TEXT,
    showcase_correlation_id TEXT
);

ALTER TABLE fulfillment_rules
    ADD COLUMN IF NOT EXISTS showcase_run_id TEXT;
ALTER TABLE fulfillment_rules
    ADD COLUMN IF NOT EXISTS showcase_correlation_id TEXT;
ALTER TABLE fulfillment_rules
    ADD COLUMN IF NOT EXISTS available_to_promise INTEGER;
ALTER TABLE fulfillment_rules
    ADD COLUMN IF NOT EXISTS carrier_cutoff_open BOOLEAN;
ALTER TABLE fulfillment_rules
    ADD COLUMN IF NOT EXISTS address_hold BOOLEAN;
ALTER TABLE fulfillment_rules
    ADD COLUMN IF NOT EXISTS risk_hold BOOLEAN;
UPDATE fulfillment_rules
SET available_to_promise = 0,
    carrier_cutoff_open = false,
    address_hold = false,
    risk_hold = false
WHERE available_to_promise IS NULL
   OR carrier_cutoff_open IS NULL
   OR address_hold IS NULL
   OR risk_hold IS NULL;
ALTER TABLE fulfillment_rules
    ALTER COLUMN available_to_promise SET NOT NULL,
    ALTER COLUMN carrier_cutoff_open SET NOT NULL,
    ALTER COLUMN address_hold SET NOT NULL,
    ALTER COLUMN risk_hold SET NOT NULL;

CREATE TABLE IF NOT EXISTS showcase_runs (
    run_id TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    source_system TEXT,
    record_id TEXT,
    source_version BIGINT
);

INSERT INTO fulfillment_rules (
    sku, brand, market, title, content, classification, allowed_purposes, available_to_promise,
    carrier_cutoff_open, address_hold, risk_hold,
    source_version, updated_at
) VALUES
    (
        'NORTHSTAR-104', 'NORTHSTAR', 'US', 'Fulfillment promise for NORTHSTAR-104',
        'SKU NORTHSTAR-104 has capacity for its current fulfillment promise.',
        'internal', ARRAY['fulfillment-analysis'], 8, true, false, false, 1, now()
    )
ON CONFLICT (sku) DO UPDATE SET
    brand = EXCLUDED.brand,
    market = EXCLUDED.market,
    title = EXCLUDED.title,
    content = EXCLUDED.content,
    classification = EXCLUDED.classification,
    allowed_purposes = EXCLUDED.allowed_purposes,
    available_to_promise = EXCLUDED.available_to_promise,
    carrier_cutoff_open = EXCLUDED.carrier_cutoff_open,
    address_hold = EXCLUDED.address_hold,
    risk_hold = EXCLUDED.risk_hold,
    source_version = fulfillment_rules.source_version + 1,
    updated_at = now(),
    showcase_run_id = NULL,
    showcase_correlation_id = NULL;
