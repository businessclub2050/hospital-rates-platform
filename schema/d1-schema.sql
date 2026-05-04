-- Hospital Rates Platform — Cloudflare D1 Schema (sanitized excerpt)
--
-- This is the public-facing schema definition. The full production schema
-- includes additional tables for ingest run audit trail, API key management,
-- and discovered MRF URL tracking that are omitted here.

PRAGMA foreign_keys = ON;

-- ─── Hospitals ──────────────────────────────────────────────────────────────
-- One row per hospital. Identified by a stable internal ID; CCN is the CMS
-- Certification Number (Medicare provider ID), useful for joining to HCRIS,
-- Hospital Compare, and other federal datasets.
CREATE TABLE hospitals (
    hospital_id      TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    system           TEXT NOT NULL,           -- e.g. "Legacy Health", "HCA Midwest"
    ccn              TEXT,                    -- 6-digit CMS Certification Number
    npi_type2        TEXT,                    -- 10-digit organizational NPI
    city             TEXT NOT NULL,
    state            TEXT NOT NULL,           -- 2-letter postal code
    county           TEXT NOT NULL,
    mrf_url          TEXT NOT NULL,           -- Public CMS price transparency URL
    mrf_format       TEXT NOT NULL,           -- 'json' | 'csv-tall' | 'csv-wide' | 'zip'
    last_ingested_at TEXT,
    last_status      TEXT
);

-- ─── Canonical Payer Dictionary ─────────────────────────────────────────────
-- Hospitals publish payer names in dozens of variants ("BCBS of Oregon",
-- "Blue Cross Blue Shield Oregon", "BCBSOR"). The application normalizes
-- to canonical IDs at ingest time and stores the raw name alongside.
CREATE TABLE payers (
    payer_id       TEXT PRIMARY KEY,          -- e.g. 'aetna', 'unitedhealthcare', 'bcbs-or'
    canonical_name TEXT NOT NULL              -- e.g. 'Aetna', 'UnitedHealthcare', 'BCBS Oregon'
);

-- ─── Procedure Code Dictionary ──────────────────────────────────────────────
-- Populated lazily as new codes are encountered during ingestion.
CREATE TABLE codes (
    code        TEXT NOT NULL,                -- e.g. '99213', '0001A', 'J0135'
    code_type   TEXT NOT NULL,                -- 'CPT' | 'HCPCS' | 'MS-DRG' | 'APR-DRG' | 'NDC' | 'REV'
    description TEXT,
    PRIMARY KEY (code, code_type)
);
CREATE INDEX codes_type_idx ON codes(code_type);

-- ─── Raw Rates ──────────────────────────────────────────────────────────────
-- One row per (hospital, code, payer/plan, MRF date). The unique index is the
-- dedupe key for re-ingesting the same MRF without creating duplicate rows.
CREATE TABLE rates (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    hospital_id       TEXT NOT NULL REFERENCES hospitals(hospital_id),
    mrf_date          TEXT NOT NULL,          -- ISO date the MRF was published
    code              TEXT NOT NULL,
    code_type         TEXT NOT NULL,
    modifiers         TEXT,
    description       TEXT,
    setting           TEXT,                   -- 'inpatient' | 'outpatient' | NULL
    drug_unit         TEXT,
    drug_type         TEXT,
    gross_charge      REAL,                   -- The chargemaster "list price"
    discounted_cash   REAL,
    deid_min          REAL,                   -- De-identified minimum across payers
    deid_max          REAL,                   -- De-identified maximum across payers
    payer_id          TEXT,                   -- '' = all-payers rollup, NULL = unknown
    payer_name_raw    TEXT,                   -- Original payer name from the MRF
    plan_name_raw     TEXT,
    method            TEXT,                   -- 'fee schedule' | 'percentage' | 'algorithm' | etc.
    negotiated_dollar REAL,                   -- Dollar amount, when method = fee schedule
    negotiated_pct    REAL,                   -- Percentage, when method = percentage
    negotiated_algo   TEXT,                   -- Free-text algorithm description
    estimated_amount  REAL,
    additional_notes  TEXT
);

CREATE UNIQUE INDEX rates_dedupe_idx
  ON rates(
    hospital_id, mrf_date, code, code_type,
    COALESCE(modifiers, ''),
    COALESCE(payer_name_raw, ''),
    COALESCE(plan_name_raw, ''),
    COALESCE(method, '')
  );
CREATE INDEX rates_code_idx          ON rates(code, code_type);
CREATE INDEX rates_hospital_code_idx ON rates(hospital_id, code, code_type);
CREATE INDEX rates_payer_code_idx    ON rates(payer_id, code, code_type);

-- ─── Materialized Aggregates ────────────────────────────────────────────────
-- Pre-computed per-(hospital, code, setting, payer) summary stats.
-- payer_id = '' represents the all-payers rollup for hospitals that don't
-- disclose per-payer rates. Refreshed by a scheduled cron job.
CREATE TABLE rate_aggregates (
    hospital_id       TEXT NOT NULL,
    code              TEXT NOT NULL,
    code_type         TEXT NOT NULL,
    setting           TEXT NOT NULL DEFAULT '',
    payer_id          TEXT NOT NULL DEFAULT '',
    n                 INTEGER NOT NULL,
    negotiated_min    REAL,
    negotiated_p25    REAL,
    negotiated_median REAL,
    negotiated_p75    REAL,
    negotiated_max    REAL,
    negotiated_avg    REAL,
    gross_charge      REAL,
    discounted_cash   REAL,
    deid_min          REAL,
    deid_max          REAL,
    PRIMARY KEY (hospital_id, code, code_type, setting, payer_id)
);
CREATE INDEX rate_agg_hospital_idx ON rate_aggregates(hospital_id);
CREATE INDEX rate_agg_code_idx     ON rate_aggregates(code, code_type);
CREATE INDEX rate_agg_payer_idx    ON rate_aggregates(payer_id);

-- ─── HCRIS Cost Reports ─────────────────────────────────────────────────────
-- CMS Healthcare Cost Report Information System — every Medicare-certified
-- hospital files an annual cost report. We extract the financial fields most
-- relevant to executive intelligence: cost-charge ratios, charity care, beds.
CREATE TABLE hospital_cost_ratios (
    hospital_id          TEXT NOT NULL,
    ccn                  TEXT NOT NULL,
    fiscal_year          INTEGER NOT NULL,
    fy_begin             TEXT,
    fy_end               TEXT,
    total_costs          REAL,
    inpatient_charges    REAL,
    outpatient_charges   REAL,
    combined_charges     REAL,
    ccr_hospital_wide    REAL,                -- = total_costs / combined_charges
    cost_of_charity_care REAL,
    cost_of_uncompensated REAL,
    number_of_beds       INTEGER,
    total_discharges     INTEGER,
    source_label         TEXT,                -- e.g. 'HCRIS FY2023'
    fetched_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (hospital_id, fiscal_year)
);

-- ─── CMS Hospital Quality (Care Compare) ────────────────────────────────────
CREATE TABLE hospital_quality (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    hospital_id   TEXT NOT NULL,
    source        TEXT NOT NULL,              -- 'cms_general' | 'cms_outcomes' | 'cms_hcahps' | etc.
    measure_id    TEXT NOT NULL,              -- e.g. 'OVERALL_RATING', 'MORT_30_AMI'
    measure_name  TEXT,
    score         REAL,
    score_text    TEXT,
    comparison    TEXT,                       -- 'BETTER' | 'WORSE' | 'SAME' | NULL
    period_end    TEXT,
    fetched_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX hq_hospital_measure_idx ON hospital_quality(hospital_id, measure_id);
CREATE INDEX hq_measure_idx          ON hospital_quality(measure_id);
