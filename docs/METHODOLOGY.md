# Methodology: The Rate Index

## The Problem with Naive Comparisons

If you want to know whether Hospital A is more or less expensive than Hospital B for a given insurer, the obvious approach is:

```
avg(negotiated_rate at Hospital A for Aetna)
─────────────────────────────────────────────  =  rate_index
avg(negotiated_rate at Hospital B for Aetna)
```

**This is wrong.** It's wrong because it doesn't control for what the two hospitals actually do.

### Example

- Hospital A is a community hospital that does mostly outpatient visits, immunizations, simple imaging. Average negotiated rate across all codes: **$340**
- Hospital B is an academic medical center that does open-heart surgery, transplants, NICU. Average negotiated rate across all codes: **$2,840**

Hospital B looks "8x more expensive" but that's almost entirely because their service mix is different. The actual contracts with Aetna might give Hospital A a *better* deal on the procedures they both perform.

This is called **code-mix bias** and it's the same problem actuaries face when comparing provider panels.

## The Fix: Discount Rate Ratio

Every line item in an MRF has both a **gross charge** (the chargemaster "list price") and a **negotiated rate** (what the insurer actually pays). The ratio between them tells you how aggressive the negotiated discount is:

```
discount_rate = negotiated_rate / gross_charge
```

This number — often called the "discount off chargemaster" in healthcare finance — is roughly comparable across procedures because both numerator and denominator scale with code complexity. Open-heart surgery has a high charge AND a high negotiated rate; an office visit has a low charge AND a low negotiated rate. The ratio normalizes both.

The **rate index** is the ratio of two discount rates:

```
                avg(discount_rate at Hospital A for Aetna)
rate_index =  ──────────────────────────────────────────────────
              avg(discount_rate at peer hospitals for Aetna)
```

A `rate_index = 1.18` means: this hospital's negotiated rates with Aetna are **18% higher** (relative to chargemaster) than the average of peer hospitals — even after controlling for what services each hospital provides.

## Display

The frontend shows `rate_index_pct = round((rate_index - 1) × 100)`:

| `rate_index_pct` | Display |
|---|---|
| `+25%` | Hospital pays 25% more than peer market |
| `+5%` | Slightly above market |
| `0%` | At market |
| `-12%` | Below market — possible undervalued contract |
| `-30%` | Significantly below market |

## Fallback: Raw Rate Ratio

When `gross_charge` is missing or zero (rare but it happens — some hospitals don't publish chargemaster prices for certain code types), we fall back to:

```
rate_index = avg(negotiated_rate at A) / avg(negotiated_rate at peers)
```

The API returns a `comparison_basis` field (`"discount_rate"` or `"raw_rate"`) so consumers know which method was used.

## Peer Definition

Peers are defined as **all other hospitals in the same state** that have per-payer rate data on file for the same payer. Future versions could refine this with:

- Bed count tier (CAH vs. <100 vs. 100-300 vs. 300+)
- Teaching status (COTH membership, residency program)
- Case mix index (CMS publishes this in HCRIS)
- Urban vs. rural designation
- System affiliation (independent vs. IDN-owned)

For now, state-level is the practical floor — finer cuts produce too few peers per cohort to be statistically meaningful.

## Aggregation

`avg_rate_index_vs_peers` (the headline number on the executive dashboard) is the **simple mean of per-payer rate indices**, not weighted by contract volume. This is intentional:

- Volume-weighting would let one giant payer dominate the headline
- A CEO wants to know "are my contracts above or below market on average?" — every payer relationship matters strategically, not just the biggest one

A volume-weighted variant could be exposed as a separate field if needed.

## What This Methodology Cannot Tell You

Honesty matters. The rate index has limitations:

1. **It doesn't account for case-mix complexity within a single CPT code.** CPT 99285 (high-complexity ED visit) at an academic trauma center treats sicker patients than the same code at a critical access hospital. The dollars per code are comparable; the underlying patient acuity is not.
2. **It doesn't capture quality differences.** A hospital may "deserve" higher rates because of better outcomes. The dashboard shows rate index *alongside* CMS Hospital Compare quality scores so users can see both.
3. **It's a snapshot, not a trend.** MRF data is updated annually at most. Multi-year trend analysis would require versioned snapshots.
4. **Outlier sensitivity.** A payer with only one peer hospital in the comparison can produce extreme indices (Workers' Compensation often shows ±200% swings). The API exposes `peer_hospital_count` so the UI can de-emphasize low-N comparisons.

## Why This Matters

A hospital CFO with this data can answer:
- "Are we leaving money on the table with UnitedHealthcare?"
- "Why is Aetna paying us 30% less than competitors? Should we renegotiate or terminate?"
- "Which payers are our most profitable per service?"

A health insurance plan sponsor can answer:
- "Which hospitals in the network charge above-market rates?"
- "Are we steering members to expensive facilities?"

A regulator can answer:
- "Which hospitals are publishing data that doesn't satisfy the per-payer disclosure requirement?"

That's the value commercial firms charge $50K-$500K/year for. The methodology is open here.
