# Architecture

## High-Level Data Flow

```
┌─────────────────┐
│ Hospital MRF    │  Public URLs published per 45 CFR §180.50
│  (100MB - 1GB)  │  JSON, CSV, gzip, zip — heterogeneous schemas
└────────┬────────┘
         │
         ▼
┌────────────────────────────────────────────────────────────────┐
│ Cloudflare Worker — Ingest Producer                            │
│  • Cron trigger (weekly)                                       │
│  • Manual trigger via POST /v1/admin/ingest                    │
│  • Probes URL, captures content-length + ETag                  │
│  • Enqueues onto mrf-ingest queue with hospital_id + URL       │
└────────────────────────┬───────────────────────────────────────┘
                         │
                         ▼
            ┌──────────────────────────┐
            │   mrf-ingest (Queue)     │
            │   max_batch_size = 1     │
            │   max_retries = 3 → DLQ  │
            └──────────┬───────────────┘
                       │
                       ▼
┌────────────────────────────────────────────────────────────────┐
│ Cloudflare Worker — Ingest Consumer                            │
│  • Streams MRF in 50MB chunks → R2                             │
│  • If file < 50MB: parse inline, write `rates` rows to D1      │
│  • If file > 50MB: mark ingest_run status='partial' + warning  │
│  • Updates ingest_runs audit row                               │
└─────┬──────────────────────────────────────────┬───────────────┘
      │                                           │
      ▼                                           ▼
┌──────────────┐                    ┌──────────────────────────┐
│  R2 Storage  │                    │ D1 (SQLite, edge)        │
│  mrfs/{id}/  │                    │  • hospitals             │
│  {YYYY-MM-DD}│                    │  • rates                 │
│  /file.json  │                    │  • rate_aggregates       │
└──────┬───────┘                    │  • payers                │
       │                            │  • hcris_cost_ratios     │
       │ Files marked 'partial'     │  • hospital_quality      │
       │ (too big for Worker CPU)   │  • ingest_runs           │
       ▼                            └──────────┬───────────────┘
┌──────────────────────────────────┐           │
│ Offline DuckDB Worker (Python)   │           │
│  tools/parse_large_mrf.py        │           │
│  • Pulls from R2                 │           │
│  • DuckDB streaming JSON parse   │           │
│  • Chunks 80KB INSERT batches    │           │
│  • Writes back via wrangler d1   │ ──────────┘
│  • Updates ingest_runs to 'ok'   │
└──────────────────────────────────┘

                  ┌─────────────────────────────────────┐
                  │ Materialized View Refresh (cron)    │
                  │  rate_aggregates =                  │
                  │    GROUP BY hospital, code, payer   │
                  │    aggregate min/max/p25/p50/p75/   │
                  │              avg + n                │
                  └──────────────────┬──────────────────┘
                                     │
                                     ▼
        ┌────────────────────────────────────────────────┐
        │  Cloudflare Worker — Public REST API           │
        │  • GET /v1/hospitals/:id                       │
        │  • GET /v1/hospitals/:id/payer-intel  ◄─ key   │
        │  • GET /v1/hospitals/:id/cost-context          │
        │  • GET /v1/quality/ranked?measure_id=…         │
        │  • GET /v1/codes/:code/spread?state=OR         │
        └─────────────────┬──────────────────────────────┘
                          │
                          ▼
                ┌──────────────────────┐
                │  SvelteKit Frontend  │
                │  CEO Executive Dash  │
                │  /hospital/[id]      │
                └──────────────────────┘
```

## Why This Architecture

### Cloudflare Workers + D1 + R2
- **Edge deployment** — API responds in 30-80ms globally without managing servers
- **D1 = serverless SQLite** — perfect fit for our workload (100s of MB, read-heavy, point lookups + small aggregations)
- **R2 = S3-compatible object storage with no egress fees** — critical for storing raw MRF blobs we may need to re-parse
- **Queues** decouple ingestion producers from consumers; partial failures don't lose data, the DLQ catches anything that fails 3 retries

### Why offline DuckDB for large files
Cloudflare Workers have hard limits: 128 MB memory, 30 sec CPU on paid plan. A 670 MB MRF file from HCA's Research Medical Center can't be parsed inline — it would hit Error 1102 (CPU exceeded). Two options:

1. **Stream-parse in the Worker, write rows incrementally** — works for some files, but JSON files with deeply nested objects can't be streamed line-by-line
2. **Hand off to DuckDB** — DuckDB's `read_json_auto` and `read_csv_auto` functions are stream-capable and battle-tested on multi-GB files. Run it on a beefy box once per ingest, write back via D1 batched INSERTs.

The offline parser is idempotent: re-running it on the same R2 blob produces the same rows (deduped by the unique index on `rates`).

### Why a `rate_aggregates` materialized view
The raw `rates` table has 2.3M+ rows. Computing per-payer p25/p50/p75 on every API request would be slow. `rate_aggregates` pre-computes:

```sql
PRIMARY KEY (hospital_id, code, code_type, setting, payer_id)
n, negotiated_min, negotiated_p25, negotiated_median,
negotiated_p75, negotiated_max, negotiated_avg,
gross_charge, discounted_cash, deid_min, deid_max
```

A `payer_id = ''` row represents the all-payers rollup (used for hospitals that don't disclose per-payer rates).

## Repository Layout (full system, not just public repo)

```
hospital-rates-api/        # Cloudflare Worker — ingest + API (private)
├── src/
│   ├── api/handlers.ts    # All REST handlers
│   ├── ingest/            # MRF format parsers (json, csv-tall, peace-zip, etc.)
│   ├── lib/payers.ts      # Payer name normalization → canonical IDs
│   └── index.ts           # Itty router
├── tools/                 # Python helpers
│   ├── parse_large_mrf.py # DuckDB offline parser
│   ├── load_hcris.py      # HCRIS cost report loader
│   └── load_cms_quality.py # CMS Care Compare loader
├── migrations/            # D1 SQL schema migrations (sequential)
└── wrangler.jsonc         # CF deployment config

hospital-rates-web/        # SvelteKit frontend — CEO dashboard (private)
├── src/routes/hospital/[id]/+page.svelte  # The executive profile
└── src/lib/api.ts          # Typed API client
```
