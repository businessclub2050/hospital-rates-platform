# API Reference

The live Cloudflare Worker exposes a public REST API. Selected endpoints:

## `GET /v1/hospitals/:id/payer-intel`

Returns per-payer rate intelligence with peer-state benchmarking.

**Live examples:**
- [Adventist Health Tillamook (OR) — 13 payers, full per-payer data](https://hospital-rates-api.businessclub2050.workers.dev/v1/hospitals/adventist-tillamook/payer-intel)
- [Lafayette Regional (MO) — rollup-only, likely non-compliant](https://hospital-rates-api.businessclub2050.workers.dev/v1/hospitals/h-mo-261320/payer-intel)
- [Legacy Emanuel Medical Center (OR)](https://hospital-rates-api.businessclub2050.workers.dev/v1/hospitals/legacy-emanuel/payer-intel)

**Response shape:**

```json
{
  "hospital_id": "adventist-tillamook",
  "state": "OR",
  "has_per_payer_data": true,
  "total_payers": 13,
  "total_rate_rows": 814170,
  "avg_rate_index_vs_peers": 0.738,
  "peer_hospitals_in_state": 21,
  "payers": [
    {
      "payer_id": "unitedhealthcare",
      "payer_name": "UnitedHealthcare",
      "code_count": 8421,
      "total_rate_rows": 142003,
      "avg_negotiated": 1247.30,
      "peer_avg_negotiated": 932.40,
      "peer_hospital_count": 18,
      "rate_index": 1.34,
      "rate_index_pct": 34,
      "comparison_basis": "discount_rate"
    }
    /* ... */
  ],
  "rollup_summary": null
}
```

**Compliance fallback (rollup-only hospital):**

```json
{
  "hospital_id": "h-mo-261320",
  "state": "MO",
  "has_per_payer_data": false,
  "total_payers": 0,
  "total_rate_rows": 0,
  "avg_rate_index_vs_peers": null,
  "peer_hospitals_in_state": 0,
  "payers": [],
  "rollup_summary": {
    "code_count": 16651,
    "avg_negotiated": 1247.30,
    "avg_gross": 4892.10
  }
}
```

The implementation is in [`payer_intel_handler.ts`](payer_intel_handler.ts).

## Other Endpoints (Not Detailed Here)

The full API also exposes:

- `GET /v1/hospitals` — registry browse with state/system filters
- `GET /v1/hospitals/:id` — hospital profile with quality + cost summary
- `GET /v1/hospitals/:id/cost-context` — HCRIS-derived cost ratios and markup
- `GET /v1/codes/:code/spread?state=OR` — per-code price spread across hospitals
- `GET /v1/quality/ranked?measure_id=OVERALL_RATING` — quality leaderboards
- `GET /v1/ascs/:id` — ambulatory surgery center profiles
- `GET /v1/providers/:npi` — physician lookup with affiliated facilities

These are kept private to limit scraping pressure on the free Cloudflare tier and to preserve the value of the data for future product development.
