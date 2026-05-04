# Hospital Price Transparency Intelligence Platform

> Actuarial-grade analysis of hospital negotiated rates from CMS machine-readable files.
> Streaming ingestion, peer benchmarking, and compliance detection — built independently as part of [JanusNW Research LLC](https://github.com/businessclub2050).

**🌐 Live API:** [hospital-rates-api.businessclub2050.workers.dev](https://hospital-rates-api.businessclub2050.workers.dev)
**📊 Try it:** [`/v1/hospitals/adventist-tillamook/payer-intel`](https://hospital-rates-api.businessclub2050.workers.dev/v1/hospitals/adventist-tillamook/payer-intel)

---

## The Problem

Since 2021, [45 CFR §180.50](https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-E/part-180) has required every U.S. hospital to publish a machine-readable file (MRF) listing standard charges and negotiated rates **for each payer and plan**. In practice:

- Files are 100 MB – 1+ GB of streaming JSON or CSV
- Schemas vary across systems (HCA, Providence, Kaiser, Legacy, Adventist all use different layouts)
- Many hospitals deliberately publish a single all-payers blended rate instead of per-insurer breakdowns — likely non-compliant
- The data is technically public but practically inaccessible without serious engineering

Commercial firms like Turquoise Health, Clarify Health, and Dais are building businesses on this dataset. This repo is the public-facing case study of an independent end-to-end implementation: ingestion → normalization → actuarial analysis → executive-facing API.

---

## What's In This Repo

This is a **public reference implementation** showing methodology and architecture. The full ingestion pipeline, hospital registry, frontend, and live data are kept in private repos.

| Path | Description |
|------|-------------|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | System design, data flow, infrastructure |
| [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) | The actuarial rate-index methodology — why discount-rate ratios beat raw averages |
| [`docs/COMPLIANCE.md`](docs/COMPLIANCE.md) | How the platform detects likely 45 CFR §180.50 violations |
| [`schema/d1-schema.sql`](schema/d1-schema.sql) | Cloudflare D1 schema (sanitized) |
| [`ingest/parse_large_mrf.py`](ingest/parse_large_mrf.py) | Streaming MRF parser (DuckDB + Python) for files too large for serverless memory limits |
| [`api/payer_intel_handler.ts`](api/payer_intel_handler.ts) | The Cloudflare Workers handler powering the live `/payer-intel` endpoint |
| [`screenshots/`](screenshots/) | UI screenshots from the executive dashboard |

---

## Highlights

### Pipeline & Ingestion
- **Streaming MRF parser** (DuckDB + Python) capable of processing 400–670 MB gzip/zip JSON files without loading into memory
- Handles non-standard ZIP variants including Deflate64
- **Ingest queue** via Cloudflare Workers + R2 object storage; per-hospital run tracking in D1 with retry/resume on partial failures
- **HCRIS FY2023 cost report integration** for 711 hospitals: cost-charge ratios, markup multiples, charity care as % of total costs

### Actuarial Analysis
- **Rate index = `negotiated_avg ÷ gross_charge`** (discount rate) — eliminates code-mix bias when comparing hospitals with different service portfolios. Same normalization technique actuaries use for provider-panel comparisons.
- **Peer comparison** across same-state hospitals — `+18% vs peers` means this insurer pays 18% more per service at this hospital than at comparable facilities, after controlling for service mix
- **Automatic compliance detection** — flags hospitals publishing single-payer rollups as likely non-compliant with the per-payer disclosure requirement; meaningful signal for contract negotiators and regulators

### Scale (current snapshot)
- 2.3M+ negotiated rate rows across 22 hospitals in OR, KS, MO
- 21-hospital Oregon peer cohort
- HCRIS cost data for 711 of 723 target KS/MO hospitals (FY2023)

### Stack
Python · DuckDB · TypeScript · Cloudflare Workers · D1 (serverless SQLite) · R2 · Queues · SvelteKit · Tailwind CSS · CMS MRF schema · HCRIS

---

## Try the Live API

```bash
# Hospital with rich per-payer data (Adventist Health Tillamook, OR)
curl https://hospital-rates-api.businessclub2050.workers.dev/v1/hospitals/adventist-tillamook/payer-intel | jq

# Hospital with rollup-only filing (Lafayette Regional, MO — likely non-compliant)
curl https://hospital-rates-api.businessclub2050.workers.dev/v1/hospitals/h-mo-261320/payer-intel | jq
```

The first returns 13 payers with rate-index comparisons against 21 Oregon peer hospitals. The second returns `has_per_payer_data: false` and a rollup summary — the API correctly identifies that the hospital's MRF doesn't satisfy the per-payer disclosure requirement.

---

## About

Built by **Gerald January** ([@businessclub2050](https://github.com/businessclub2050)) under JanusNW Research LLC.

17+ years in healthcare data engineering — Cerner Millennium, SAS Enterprise Data Warehousing, hospital revenue cycle, CMS reporting. CDC Charles C. Shepard Award nominee (2021). Three peer-reviewed public health publications.

Available for senior data engineering roles in healthcare analytics, payer-provider intelligence, and price transparency platforms.

📬 businessclub2050@gmail.com · [LinkedIn](https://linkedin.com/in/gerald-january)

---

## License

Source-available for review. © 2026 JanusNW Research LLC. All rights reserved.
