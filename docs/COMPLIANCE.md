# Compliance Detection: 45 CFR §180.50

## The Regulation

The Hospital Price Transparency final rule, codified at [45 CFR §180.50](https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-E/part-180/subpart-B/section-180.50), requires every U.S. hospital to publish a single machine-readable file containing standard charges for all items and services. Effective January 1, 2021. Updated and clarified in 2024 to require a CMS template format.

**The key requirement** (§180.50(b)(2)):

> The hospital shall include all of the following standard charges for each item or service:
>
> (i) Gross charge.
> (ii) Discounted cash price.
> (iii) **Payer-specific negotiated charge** (associated with the **name of the third party payer and plan**).
> (iv) De-identified minimum negotiated charge.
> (v) De-identified maximum negotiated charge.

The phrase "payer-specific" is unambiguous. The 2024 amendments make it more so by mandating the CMS template, which has explicit columns for `payer_name`, `plan_name`, and `standard_charge_negotiated_dollar` per row.

## What Some Hospitals Do Instead

A subset of hospitals — disproportionately those owned by large for-profit chains — publish files that contain a single blended rate per code, with no payer breakdown. In the data, these appear as one row per code with either:

- A `payer_name` field set to "All Payers", "Standard", or blank
- Or a structure that simply omits payer-level rows entirely

Whether this satisfies §180.50 is a regulatory question the hospitals' counsel have apparently answered "yes" but on plain reading of the rule, no. CMS has issued [warning letters](https://www.cms.gov/files/document/cms-10822-warning-letter-hpt.pdf) and assessed civil monetary penalties to hospitals with deficient filings.

## How the Platform Detects This

In our `rate_aggregates` table, the `payer_id` column is:
- A canonical payer ID (`aetna`, `unitedhealthcare`, `bcbs-or`, etc.) when the hospital published per-payer data
- An empty string `''` for the all-payers rollup

The detection logic is simple SQL:

```sql
SELECT
  hospital_id,
  COUNT(CASE WHEN payer_id != '' THEN 1 END) as per_payer_rows,
  COUNT(CASE WHEN payer_id  = '' THEN 1 END) as rollup_rows
FROM rate_aggregates
GROUP BY hospital_id;
```

A hospital with `per_payer_rows = 0` and `rollup_rows > 0` published only the rollup. The API surfaces this via:

```json
{
  "has_per_payer_data": false,
  "rollup_summary": {
    "code_count": 16651,
    "avg_negotiated": 1247.30,
    "avg_gross": 4892.10
  }
}
```

The frontend renders an amber-bordered warning banner:

> ⚠ **Insurer-level rates not disclosed**
>
> Federal price transparency rules (45 CFR §180.50) require hospitals to publish a standard charge for **each payer and plan**. This hospital filed a single blended rate for all insurers — which likely does not satisfy that requirement. Per-insurer comparison is unavailable.

## Current Detection Stats

From a snapshot of 22 hospitals across OR, KS, and MO:

| State | Hospitals Ingested | With Per-Payer Data | Rollup-Only (likely non-compliant) |
|-------|---|---|---|
| OR | 22 | 21 | 1 |
| MO | 14 | 0 | 14 |
| KS | 4 | 0 | 4 |

Every Missouri and Kansas hospital we've ingested — all of them either HCA Midwest or Saint Luke's — files only the rollup. Oregon hospitals (Legacy, Providence, Adventist, OHSU) overwhelmingly file proper per-payer data.

## Why This Detection Matters

For **regulators**: identifies enforcement targets at scale, automatically.

For **employer health plan sponsors**: knowing your network includes hospitals that won't disclose what they charge your insurer is itself a contract negotiation lever.

For **patients and price-comparison sites**: a "we couldn't compare prices because the hospital didn't disclose" message is more honest and more useful than silently presenting an averaged number.

For **the hospital itself** (if they're using this kind of platform internally): publishing a rollup is not zero-cost. It signals to sophisticated counterparties that you're hiding something, which strengthens insurer leverage in renegotiation.

## What This Detection Does NOT Claim

- It does **not** assert legal non-compliance — that determination is for CMS and HHS Office of Inspector General. The UI uses "likely does not satisfy" deliberately.
- It does **not** account for hospitals that publish a *separate* per-payer file alongside the rollup. The platform currently ingests one MRF per hospital; if the per-payer data exists at a different URL, we'd need to discover and ingest it.
- It does **not** distinguish between deliberate non-compliance and technical filing errors. Some small hospitals with limited IT resources may simply not understand the format requirements.

## Future Work

- Cross-reference with CMS's published [warning letter recipients](https://www.cms.gov/medicare/regulations-guidance/promoting-interoperability-programs/hospital-price-transparency) to validate detection
- Track filing changes over time — hospitals that improve disclosure should be credited
- Surface the regulatory text in the UI tooltip so users understand why the warning is shown
