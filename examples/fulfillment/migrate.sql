-- Safe on an existing named local volume; backfills required conservative defaults only.
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

-- An existing local volume predates the synthetic fulfillment scenario. Normalize only its fixed
-- showcase record so the deliberate start action has a safe, visible happy path.
UPDATE fulfillment_rules
SET title = 'Fulfillment promise for NORTHSTAR-104',
    content = 'SKU NORTHSTAR-104 has capacity for its current fulfillment promise.',
    classification = 'internal',
    allowed_purposes = ARRAY['fulfillment-analysis'],
    available_to_promise = 8,
    carrier_cutoff_open = true,
    address_hold = false,
    risk_hold = false
WHERE sku = 'NORTHSTAR-104';

CREATE TABLE IF NOT EXISTS showcase_runs (
    run_id TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    source_system TEXT,
    record_id TEXT,
    source_version BIGINT
);
