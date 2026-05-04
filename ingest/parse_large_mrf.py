#!/usr/bin/env python3
"""
Offline DuckDB parser for hospital MRF files that exceed Worker memory limits.

When a Worker fetch records `status='partial'` with a "deferred to offline
DuckDB job" warning, this tool picks up where it left off:

    1. Find deferred ingest_runs in remote D1.
    2. For each one, pull the MRF blob from R2 via `wrangler r2 object get`.
    3. Parse with DuckDB → tidy rows matching the `rates` table schema.
    4. Emit chunked `INSERT INTO rates …` SQL files.
    5. Apply each chunk via `wrangler d1 execute --remote --file=…`.
    6. Update the ingest_run row to status='ok' (or 'failed').

CSV-tall is the dominant Portland format (Adventist, Kaiser, Legacy, Unity,
Shriners). JSON is used by Providence (200 MB files). PeaceHealth ships ZIP.

Usage:
    # Process every deferred run in D1:
    python tools/parse_large_mrf.py --all-deferred

    # Process one hospital from R2 (uses the most recent snapshot):
    python tools/parse_large_mrf.py --hospital-id providence-portland

    # Process a local file (skip R2 download):
    python tools/parse_large_mrf.py --hospital-id kaiser-westside \
        --input ./mrf.csv

    # Dry run — write SQL chunks to ./out/ but don't touch D1:
    python tools/parse_large_mrf.py --all-deferred --dry-run

Requires:  pip install duckdb
           wrangler CLI (logged in, account configured in wrangler.jsonc)
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

try:
    import duckdb
except ImportError:
    print("Install duckdb:  pip install duckdb", file=sys.stderr)
    sys.exit(1)


# ─── config ─────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
D1_DB = "hospital-rates"
R2_BUCKET = "hospital-mrfs-raw"
OUT_DIR = REPO_ROOT / "out"

# D1 has a 100 KB-ish per-statement size cap and ~50 MB body cap on
# `d1 execute --file`. ~2000 rows per multi-VALUES INSERT keeps each
# statement small while still being fast.
# D1 caps individual SQL statements at 100 KB. Pack rows into INSERTs by byte
# size, not by count, so a few long descriptions don't blow up a chunk.
MAX_INSERT_BYTES = 80_000
MAX_FILE_BYTES   = 20_000_000  # ~20 MB per chunk file (well under wrangler upload limits)

# Columns the parser emits, in order. MUST match the `rates` table.
RATES_COLUMNS = [
    "hospital_id", "mrf_date", "code", "code_type", "modifiers", "description",
    "setting", "drug_unit", "drug_type", "gross_charge", "discounted_cash",
    "deid_min", "deid_max", "payer_id", "payer_name_raw", "plan_name_raw",
    "method", "negotiated_dollar", "negotiated_pct", "negotiated_algo",
    "estimated_amount", "median_allowed", "p10_allowed", "p90_allowed",
    "count_allowed", "additional_notes",
]


# ─── shell helpers ──────────────────────────────────────────────────────────

def wrangler(*args: str, capture: bool = True, check: bool = True) -> str:
    """Run `npx wrangler ...` from the repo root and return stdout."""
    cmd = ["npx", "wrangler", *args]
    res = subprocess.run(
        cmd, cwd=REPO_ROOT,
        capture_output=capture, text=True,
        check=False,
    )
    if check and res.returncode != 0:
        sys.stderr.write(res.stdout)
        sys.stderr.write(res.stderr)
        raise RuntimeError(f"wrangler failed: {' '.join(cmd)}")
    return res.stdout


def d1_query(sql: str) -> list[dict]:
    out = wrangler("d1", "execute", D1_DB, "--remote", "--json", "--command", sql)
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        # wrangler sometimes prepends a banner; strip to first '['
        idx = out.find("[")
        data = json.loads(out[idx:]) if idx >= 0 else []
    return data[0]["results"] if data else []


def d1_apply_file(path: Path, *, retries: int = 10) -> None:
    import time
    last_err = None
    for attempt in range(1, retries + 1):
        cmd = ["npx", "wrangler", "d1", "execute", D1_DB, "--remote", "--file", str(path)]
        res = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
        if res.returncode == 0:
            return
        combined = res.stdout + res.stderr
        # SQLITE_TOOBIG is non-recoverable; bail immediately.
        if "SQLITE_TOOBIG" in combined:
            raise RuntimeError(f"SQLITE_TOOBIG applying {path.name} (statement too long)")
        # D1 long-running import lock: wait longer before retry
        if "7500" in combined or "long-running import" in combined.lower():
            wait = 60
            print(f"  D1 busy (code 7500), waiting {wait}s before retry {attempt}/{retries}...")
        else:
            wait = 5 * attempt
        last_err = combined
        if attempt < retries:
            time.sleep(wait)
    sys.stderr.write(last_err or "")
    raise RuntimeError(f"d1 execute failed after {retries} attempts for {path.name}")


# ─── R2 fetch ───────────────────────────────────────────────────────────────

def r2_latest_key(hospital_id: str) -> str | None:
    """Return the most-recent non-.dot R2 key for a hospital from D1 ingest_runs."""
    rows = d1_query(
        "SELECT r2_key FROM ingest_runs WHERE hospital_id='"
        + hospital_id.replace("'", "''")
        + "' AND r2_key IS NOT NULL AND r2_key != ''"
        + " AND r2_key NOT LIKE '%.dot'"
        + " ORDER BY id DESC LIMIT 1"
    )
    if rows and rows[0].get("r2_key"):
        return rows[0]["r2_key"]
    return None


def r2_download(key: str, dest: Path) -> None:
    wrangler("r2", "object", "get", f"{R2_BUCKET}/{key}", "--remote", "--file", str(dest))


def r2_upload(key: str, src: Path) -> None:
    wrangler("r2", "object", "put", f"{R2_BUCKET}/{key}", "--remote", "--file", str(src))


def http_download(url: str, dest: Path) -> None:
    """Stream a large MRF directly from upstream to disk."""
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (HospitalRatesIngest/1.0)",
        "Accept": "*/*",
    })
    with urllib.request.urlopen(req, timeout=300) as r, dest.open("wb") as f:
        shutil.copyfileobj(r, f, length=1024 * 1024)


def maybe_unzip(path: Path) -> Path:
    """If the file is a ZIP, extract the largest member and return its path."""
    if not zipfile.is_zipfile(path):
        return path
    extract_to = path.parent / (path.stem + "_unzipped")
    extract_to.mkdir(exist_ok=True)
    with zipfile.ZipFile(path) as zf:
        members = sorted(zf.infolist(), key=lambda m: m.file_size, reverse=True)
        if not members:
            raise RuntimeError(f"ZIP {path} is empty")
        target = members[0]
        zf.extract(target, extract_to)
    return extract_to / target.filename


# ─── format detection ───────────────────────────────────────────────────────

def detect_format(path: Path) -> str:
    """Return 'json' | 'csv-tall' | 'csv-wide'."""
    with path.open("rb") as f:
        head = f.read(8192).decode("utf-8", errors="replace")
    head = head.lstrip("\ufeff \t\r\n")
    if head.startswith(("{", "[")):
        return "json"
    # CSV: csv-wide has a header that includes "standard_charge|<payer>|<plan>".
    first_line = head.splitlines()[0] if head else ""
    if "standard_charge|" in first_line.lower() and first_line.lower().count("|") > 4:
        return "csv-wide"
    return "csv-tall"


# ─── parsing ────────────────────────────────────────────────────────────────

# DuckDB SQL pieces for csv-tall. Run as separate statements so we can bind
# parameters; the final COPY writes parquet.
CSV_TALL_LOAD = (
    "CREATE OR REPLACE TABLE raw AS "
    "SELECT * FROM read_csv_auto(?, header=true, sample_size=-1, "
    "ignore_errors=true, all_varchar=true, skip=?)"
)

CSV_TALL_SELECT = r"""
SELECT
  ? AS hospital_id,
  COALESCE(?, CAST(current_date AS VARCHAR)) AS mrf_date,
  COALESCE("code|1", "code|2", "code|3", "code|4") AS code,
  COALESCE("code|1|type", "code|2|type", "code|3|type", "code|4|type") AS code_type,
  "modifiers" AS modifiers,
  "description" AS description,
  CASE LOWER(COALESCE("setting", "billing_class"))
    WHEN 'inpatient' THEN 'inpatient'
    WHEN 'outpatient' THEN 'outpatient'
    WHEN 'both' THEN 'both' ELSE NULL END AS setting,
  "drug_unit_of_measurement" AS drug_unit,
  "drug_type_of_measurement" AS drug_type,
  TRY_CAST("standard_charge|gross" AS DOUBLE) AS gross_charge,
  TRY_CAST("standard_charge|discounted_cash" AS DOUBLE) AS discounted_cash,
  TRY_CAST("standard_charge|min" AS DOUBLE) AS deid_min,
  TRY_CAST("standard_charge|max" AS DOUBLE) AS deid_max,
  CAST(NULL AS VARCHAR) AS payer_id,
  "payer_name" AS payer_name_raw,
  "plan_name" AS plan_name_raw,
  "standard_charge|methodology" AS method,
  TRY_CAST("standard_charge|negotiated_dollar" AS DOUBLE) AS negotiated_dollar,
  TRY_CAST("standard_charge|negotiated_percentage" AS DOUBLE) AS negotiated_pct,
  "standard_charge|negotiated_algorithm" AS negotiated_algo,
  TRY_CAST("estimated_amount" AS DOUBLE) AS estimated_amount,
  NULL AS median_allowed, NULL AS p10_allowed, NULL AS p90_allowed,
  NULL AS count_allowed,
  TRY_CAST("additional_generic_notes" AS VARCHAR) AS additional_notes
FROM raw
WHERE COALESCE("code|1", "code|2", "code|3", "code|4") IS NOT NULL
  AND COALESCE("code|1", "code|2", "code|3", "code|4") != ''
  AND (
    TRY_CAST("standard_charge|negotiated_dollar" AS DOUBLE) IS NOT NULL
    OR TRY_CAST("standard_charge|gross" AS DOUBLE) IS NOT NULL
    OR TRY_CAST("standard_charge|discounted_cash" AS DOUBLE) IS NOT NULL
    OR TRY_CAST("estimated_amount" AS DOUBLE) IS NOT NULL
  )
"""


# CMS hospital MRF CSVs prefix the actual header with a metadata row pair —
# the real header is line 3 (0-indexed line 2). Detect by sniffing.
def detect_skip_rows(path: Path) -> int:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if i > 5:
                return 0
            low = line.lower()
            if "code|1" in low or "code_type" in low or low.startswith("description,"):
                return i
    return 0


def _parse_json_streaming(path: Path, hospital_id: str, mrf_date: str | None,
                          parquet: Path) -> Path:
    """
    Memory-efficient streaming parser for large CMS MRF JSON files using ijson.
    Handles files that would OOM with DuckDB's read_json_auto (typically >500MB).
    Writes directly to Parquet via pyarrow in batches.
    """
    import ijson
    import pyarrow as pa
    import pyarrow.parquet as pq

    PARQUET_SCHEMA = pa.schema([
        ("hospital_id", pa.string()),
        ("mrf_date", pa.string()),
        ("code", pa.string()),
        ("code_type", pa.string()),
        ("modifiers", pa.string()),
        ("description", pa.string()),
        ("setting", pa.string()),
        ("drug_unit", pa.string()),
        ("drug_type", pa.string()),
        ("gross_charge", pa.float64()),
        ("discounted_cash", pa.float64()),
        ("deid_min", pa.float64()),
        ("deid_max", pa.float64()),
        ("payer_id", pa.string()),
        ("payer_name_raw", pa.string()),
        ("plan_name_raw", pa.string()),
        ("method", pa.string()),
        ("negotiated_dollar", pa.float64()),
        ("negotiated_pct", pa.float64()),
        ("negotiated_algo", pa.string()),
        ("estimated_amount", pa.float64()),
        ("median_allowed", pa.float64()),
        ("p10_allowed", pa.float64()),
        ("p90_allowed", pa.float64()),
        ("count_allowed", pa.int32()),
        ("additional_notes", pa.string()),
    ])

    def _setting(sc: dict) -> str | None:
        raw = sc.get("setting") or sc.get("billing_class")
        if not raw:
            return None
        v = raw.lower()
        if v in ("inpatient", "outpatient", "both"):
            return v
        return None

    def _modifiers(sc: dict) -> str | None:
        if "modifier_code" in sc:
            lst = sc["modifier_code"]
            return ",".join(str(x) for x in lst) if lst else None
        return sc.get("modifiers")

    def _try_float(v) -> float | None:
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _try_int(v) -> int | None:
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    BATCH = 50_000
    rows: list[dict] = []
    writer: pq.ParquetWriter | None = None
    total_rows = 0

    # First pass: detect last_updated_on from top-level keys (if mrf_date not given)
    detected_date = mrf_date
    if not detected_date:
        try:
            with open(path, "rb") as f:
                for k, v in ijson.kvitems(f, ""):
                    if k == "last_updated_on":
                        detected_date = str(v)
                        break
        except Exception:
            pass
        if not detected_date:
            detected_date = date.today().isoformat()

    def _flush():
        nonlocal writer, total_rows
        if not rows:
            return
        col: dict[str, list] = {f.name: [] for f in PARQUET_SCHEMA}
        for r in rows:
            for f in PARQUET_SCHEMA:
                col[f.name].append(r.get(f.name))
        arrays = [pa.array(col[f.name], type=f.type) for f in PARQUET_SCHEMA]
        batch = pa.record_batch(arrays, schema=PARQUET_SCHEMA)
        if writer is None:
            writer = pq.ParquetWriter(str(parquet), PARQUET_SCHEMA,
                                      compression="zstd")
        writer.write_batch(batch)
        total_rows += len(rows)
        rows.clear()

    with open(path, "rb") as f:
        # Strip UTF-8 BOM if present
        hdr = f.read(3)
        if hdr != b'\xef\xbb\xbf':
            f.seek(0)
        # Stream standard_charge_information items
        for sci in ijson.items(f, "standard_charge_information.item"):
            codes = sci.get("code_information") or []
            code = codes[0].get("code") if codes else None
            code_type = codes[0].get("type") if codes else None
            if not code:
                continue
            description = sci.get("description")
            drug_info = sci.get("drug_information") or {}
            drug_unit = str(drug_info.get("unit", "") or "") or None
            drug_type = str(drug_info.get("type", "") or "") or None

            for sc in (sci.get("standard_charges") or []):
                setting = _setting(sc)
                gross = _try_float(sc.get("gross_charge"))
                disc = _try_float(sc.get("discounted_cash"))
                deid_min = _try_float(sc.get("minimum"))
                deid_max = _try_float(sc.get("maximum"))
                mods = _modifiers(sc)
                add_notes = sc.get("additional_generic_notes")

                for p in (sc.get("payers_information") or []):
                    neg_dollar = _try_float(p.get("standard_charge_dollar"))
                    neg_algo = p.get("standard_charge_algorithm")
                    if (neg_dollar is None and neg_algo is None
                            and gross is None and disc is None):
                        continue
                    rows.append({
                        "hospital_id": hospital_id,
                        "mrf_date": detected_date,
                        "code": code,
                        "code_type": code_type,
                        "modifiers": mods,
                        "description": description,
                        "setting": setting,
                        "drug_unit": drug_unit,
                        "drug_type": drug_type,
                        "gross_charge": gross,
                        "discounted_cash": disc,
                        "deid_min": deid_min,
                        "deid_max": deid_max,
                        "payer_id": None,
                        "payer_name_raw": p.get("payer_name"),
                        "plan_name_raw": p.get("plan_name"),
                        "method": p.get("methodology"),
                        "negotiated_dollar": neg_dollar,
                        "negotiated_pct": _try_float(p.get("standard_charge_percentage")),
                        "negotiated_algo": neg_algo,
                        "estimated_amount": None,
                        "median_allowed": _try_float(p.get("median_amount")),
                        "p10_allowed": _try_float(p.get("10th_percentile")),
                        "p90_allowed": _try_float(p.get("90th_percentile")),
                        "count_allowed": _try_int(p.get("count")),
                        "additional_notes": add_notes or p.get("additional_payer_notes"),
                    })
                    if len(rows) >= BATCH:
                        _flush()

    _flush()
    if writer:
        writer.close()
    if total_rows == 0:
        raise RuntimeError("ijson streaming parsed 0 rows — check JSON schema")
    return parquet


def parse_with_duckdb(path: Path, hospital_id: str, mrf_date: str | None,
                      fmt: str) -> Path:
    """Parse `path` and write a Parquet file with `rates`-shaped columns. Returns the parquet path."""
    OUT_DIR.mkdir(exist_ok=True)
    parquet = OUT_DIR / f"{hospital_id}-{date.today().isoformat()}.parquet"

    con = duckdb.connect()
    # Allow spilling to disk for large JSON files (e.g. ~1GB Saint Luke's files).
    # Without a temp_directory DuckDB cannot spill and OOMs instead.
    con.execute("PRAGMA threads=4; PRAGMA memory_limit='4GB';")
    con.execute("SET temp_directory='/tmp/duckdb_spill'; SET preserve_insertion_order=false;")

    if fmt == "csv-tall":
        skip = detect_skip_rows(path)
        con.execute(CSV_TALL_LOAD, [str(path), skip])
        # CSV_TALL_SELECT references many CMS-template columns. Real MRFs often
        # omit optional ones (e.g. estimated_amount, drug_*, additional_generic_notes).
        # Pad the table with NULL columns so the SELECT binds cleanly.
        existing = {r[0].lower() for r in con.execute("DESCRIBE raw").fetchall()}
        expected = [
            "code|1", "code|2", "code|3", "code|4",
            "code|1|type", "code|2|type", "code|3|type", "code|4|type",
            "modifiers", "description", "setting", "billing_class",
            "drug_unit_of_measurement", "drug_type_of_measurement",
            "standard_charge|gross", "standard_charge|discounted_cash",
            "standard_charge|min", "standard_charge|max",
            "payer_name", "plan_name", "standard_charge|methodology",
            "standard_charge|negotiated_dollar",
            "standard_charge|negotiated_percentage",
            "standard_charge|negotiated_algorithm",
            "estimated_amount", "additional_generic_notes",
        ]
        for col in expected:
            if col.lower() not in existing:
                con.execute(f'ALTER TABLE raw ADD COLUMN "{col}" VARCHAR DEFAULT NULL')
        con.execute(
            f"COPY ({CSV_TALL_SELECT}) TO '{parquet}' "
            f"(FORMAT 'parquet', COMPRESSION 'zstd');",
            [hospital_id, mrf_date],
        )
    elif fmt == "json":
        # CMS MRF JSON: top-level wrapper with standard_charge_information[].
        # For large files (>500MB) use ijson streaming to avoid OOM;
        # for smaller files use DuckDB read_json_auto (faster).
        _file_size = path.stat().st_size
        if _file_size > 500 * 1024 * 1024:
            parquet = _parse_json_streaming(path, hospital_id, mrf_date, parquet)
        else:
            # DuckDB EINVAL on macOS: maximum_object_size must be <= actual file size.
            con.execute("INSTALL json; LOAD json;")
            _obj_size = _file_size
            con.execute(
                f"CREATE TABLE raw AS SELECT * FROM read_json_auto('{path}', "
                f"maximum_object_size={_obj_size}, ignore_errors=true)"
            )
            sc_struct = con.execute(
                "SELECT column_type FROM (DESCRIBE raw) "
                "WHERE column_name='standard_charge_information'"
            ).fetchone()[0]
            has_modifier_code = "modifier_code" in sc_struct
            has_pct = "standard_charge_percentage" in sc_struct
            has_billing_class = "billing_class" in sc_struct
            has_add_notes = "additional_generic_notes" in sc_struct
            has_min_max = "minimum" in sc_struct and "maximum" in sc_struct
            # Check if 'modifiers' is in the standard_charges sub-struct specifically.
            # We inspect a sample row to get the actual standard_charges struct type
            # so we don't misidentify 'modifiers' on the sci (top-level) struct.
            _sc_type_row = con.execute(
                "SELECT pg_typeof(standard_charge_information[1].standard_charges[1]) "
                "FROM raw WHERE standard_charge_information IS NOT NULL LIMIT 1"
            ).fetchone()
            _sc_sub_type = (_sc_type_row[0] if _sc_type_row else "") or ""
            has_modifiers = "modifiers" in _sc_sub_type
            if has_modifier_code:
                modifiers_expr = "array_to_string(sc.modifier_code, ',')"
            elif has_modifiers:
                modifiers_expr = "sc.modifiers"
            else:
                modifiers_expr = "CAST(NULL AS VARCHAR)"
            pct_expr = "p.standard_charge_percentage" if has_pct else "NULL"
            billing_class_expr = "sc.billing_class" if has_billing_class else "CAST(NULL AS VARCHAR)"
            add_notes_expr = "sc.additional_generic_notes" if has_add_notes else "CAST(NULL AS VARCHAR)"
            deid_min_expr = "sc.minimum" if has_min_max else "CAST(NULL AS DOUBLE)"
            deid_max_expr = "sc.maximum" if has_min_max else "CAST(NULL AS DOUBLE)"
            con.execute(f"""
            COPY (
              WITH flat AS (
                SELECT
                  ? AS hospital_id,
                  COALESCE(?, CAST(last_updated_on AS VARCHAR), CAST(current_date AS VARCHAR)) AS mrf_date,
                  unnest(standard_charge_information) AS sci
                FROM raw
              ),
              coded AS (
                SELECT hospital_id, mrf_date,
                  sci.description AS description,
                  sci.code_information[1].code AS code,
                  sci.code_information[1].type AS code_type,
                  TRY_CAST(sci.drug_information.unit AS VARCHAR) AS drug_unit,
                  CAST(sci.drug_information.type AS VARCHAR) AS drug_type,
                  unnest(sci.standard_charges) AS sc
                FROM flat
              ),
              payered AS (
                SELECT hospital_id, mrf_date, code, code_type, description,
                  drug_unit, drug_type,
                  sc.setting AS setting,
                  {billing_class_expr} AS billing_class,
                  sc.gross_charge, sc.discounted_cash,
                  {deid_min_expr} AS deid_min, {deid_max_expr} AS deid_max,
                  {modifiers_expr} AS modifiers,
                  {add_notes_expr} AS additional_notes,
                  unnest(sc.payers_information) AS p
                FROM coded
              )
              SELECT hospital_id, mrf_date, code, code_type,
                modifiers, description,
                CASE LOWER(COALESCE(setting, billing_class))
                  WHEN 'inpatient' THEN 'inpatient'
                  WHEN 'outpatient' THEN 'outpatient'
                  WHEN 'both' THEN 'both' ELSE NULL END AS setting,
                drug_unit, drug_type,
                gross_charge, discounted_cash, deid_min, deid_max,
                CAST(NULL AS VARCHAR) AS payer_id,
                p.payer_name AS payer_name_raw,
                p.plan_name AS plan_name_raw,
                p.methodology AS method,
                p.standard_charge_dollar AS negotiated_dollar,
                {pct_expr} AS negotiated_pct,
                p.standard_charge_algorithm AS negotiated_algo,
                NULL AS estimated_amount,
                p.median_amount AS median_allowed,
                p."10th_percentile" AS p10_allowed,
                p."90th_percentile" AS p90_allowed,
                TRY_CAST(p.count AS INTEGER) AS count_allowed,
                COALESCE(additional_notes, p.additional_payer_notes) AS additional_notes
              FROM payered
              WHERE code IS NOT NULL
                AND (p.standard_charge_dollar IS NOT NULL
                     OR p.standard_charge_algorithm IS NOT NULL
                     OR gross_charge IS NOT NULL
                     OR discounted_cash IS NOT NULL)
            ) TO '{parquet}' (FORMAT 'parquet', COMPRESSION 'zstd');
            """, [hospital_id, mrf_date])
    elif fmt == "csv-wide":
        # csv-wide pivots payers across columns. Out of scope for v1 — use the
        # in-Worker parser for any csv-wide files small enough to fit, or write
        # a per-hospital handler when one shows up.
        raise NotImplementedError(
            "csv-wide is not yet supported in the DuckDB tool. "
            "All deferred Portland files seen so far are csv-tall or json."
        )
    else:
        raise RuntimeError(f"Unknown format {fmt}")

    return parquet


# ─── SQL emission ───────────────────────────────────────────────────────────

def _sql_lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        if v != v:  # NaN
            return "NULL"
        return repr(v)
    s = str(v).replace("'", "''")
    return f"'{s}'"


def emit_sql_chunks(parquet: Path, hospital_id: str) -> list[Path]:
    """Aggregate the parquet into rate_aggregates rows (with full quantile
    stats — DuckDB has quantile_cont, SQLite doesn't) and emit chunked
    INSERT OR REPLACE statements targeting `rate_aggregates`.

    Two passes: per-payer rollup (payer_id from row), then all-payers rollup
    (payer_id='') so /v1/compare/ranked works without a payer filter.
    """
    sql_dir = OUT_DIR / "sql" / hospital_id
    if sql_dir.exists():
        shutil.rmtree(sql_dir)
    sql_dir.mkdir(parents=True)

    agg_cols = [
        "hospital_id", "code", "code_type", "setting", "payer_id",
        "description", "mrf_date", "n",
        "negotiated_min", "negotiated_p25", "negotiated_median",
        "negotiated_p75", "negotiated_max", "negotiated_avg",
        "gross_charge", "discounted_cash", "deid_min", "deid_max",
    ]
    cols_sql = ", ".join(agg_cols)
    insert_prefix = f"INSERT OR REPLACE INTO rate_aggregates ({cols_sql}) VALUES\n"

    # Build the two aggregate queries.
    # Use COALESCE(negotiated_dollar, gross_charge, discounted_cash) so that
    # hospitals that only publish chargemaster data (no negotiated rates) still
    # produce aggregate rows and appear on the map.
    dollar_expr = "COALESCE(negotiated_dollar, gross_charge, discounted_cash)"
    per_payer_sql = f"""
        SELECT hospital_id, code, code_type,
               COALESCE(LOWER(setting), '')                 AS setting,
               COALESCE(payer_id, '')                       AS payer_id,
               MIN(description)                             AS description,
               MAX(mrf_date)                                AS mrf_date,
               COUNT(*)                                     AS n,
               MIN({dollar_expr})                           AS negotiated_min,
               quantile_cont({dollar_expr}, 0.25)           AS negotiated_p25,
               quantile_cont({dollar_expr}, 0.50)           AS negotiated_median,
               quantile_cont({dollar_expr}, 0.75)           AS negotiated_p75,
               MAX({dollar_expr})                           AS negotiated_max,
               AVG({dollar_expr})                           AS negotiated_avg,
               MIN(gross_charge)                            AS gross_charge,
               MIN(discounted_cash)                         AS discounted_cash,
               MIN(deid_min)                                AS deid_min,
               MAX(deid_max)                                AS deid_max
        FROM read_parquet('{parquet}')
        WHERE {dollar_expr} IS NOT NULL
        GROUP BY hospital_id, code, code_type,
                 COALESCE(LOWER(setting), ''), COALESCE(payer_id, '')
    """
    all_payers_sql = f"""
        SELECT hospital_id, code, code_type,
               COALESCE(LOWER(setting), '')                 AS setting,
               ''                                           AS payer_id,
               MIN(description)                             AS description,
               MAX(mrf_date)                                AS mrf_date,
               COUNT(*)                                     AS n,
               MIN({dollar_expr})                           AS negotiated_min,
               quantile_cont({dollar_expr}, 0.25)           AS negotiated_p25,
               quantile_cont({dollar_expr}, 0.50)           AS negotiated_median,
               quantile_cont({dollar_expr}, 0.75)           AS negotiated_p75,
               MAX({dollar_expr})                           AS negotiated_max,
               AVG({dollar_expr})                           AS negotiated_avg,
               MIN(gross_charge)                            AS gross_charge,
               MIN(discounted_cash)                         AS discounted_cash,
               MIN(deid_min)                                AS deid_min,
               MAX(deid_max)                                AS deid_max
        FROM read_parquet('{parquet}')
        WHERE {dollar_expr} IS NOT NULL
        GROUP BY hospital_id, code, code_type, COALESCE(LOWER(setting), '')
    """

    files: list[Path] = []
    chunk_idx = 0
    fp = None
    file_bytes = 0
    insert_bytes = 0
    rows_in_insert = 0

    def open_chunk():
        nonlocal fp, chunk_idx, file_bytes
        chunk_idx += 1
        file_bytes = 0
        fpath = sql_dir / f"chunk-{chunk_idx:04d}.sql"
        files.append(fpath)
        fp = fpath.open("w")

    def close_chunk():
        nonlocal fp, insert_bytes, rows_in_insert
        if fp is not None:
            if rows_in_insert > 0:
                fp.write(";\n")
            fp.close()
            fp = None
            insert_bytes = 0
            rows_in_insert = 0

    open_chunk()
    con = duckdb.connect()
    for q in (per_payer_sql, all_payers_sql):
        cur = con.execute(q)
        while True:
            rows = cur.fetchmany(500)
            if not rows:
                break
            for row in rows:
                values = "(" + ", ".join(_sql_lit(v) for v in row) + ")"
                line_bytes = len(values) + 2

                if rows_in_insert > 0 and insert_bytes + line_bytes > MAX_INSERT_BYTES:
                    fp.write(";\n")
                    file_bytes += 2
                    insert_bytes = 0
                    rows_in_insert = 0

                if file_bytes + len(insert_prefix) + line_bytes > MAX_FILE_BYTES and rows_in_insert == 0:
                    close_chunk()
                    open_chunk()

                if rows_in_insert == 0:
                    fp.write(insert_prefix)
                    file_bytes += len(insert_prefix)
                    insert_bytes = len(insert_prefix)
                    fp.write(values)
                    file_bytes += len(values)
                    insert_bytes += len(values)
                else:
                    fp.write(",\n")
                    fp.write(values)
                    file_bytes += line_bytes
                    insert_bytes += line_bytes
                rows_in_insert += 1

    close_chunk()
    return files


# ─── orchestration ──────────────────────────────────────────────────────────

@dataclass
class Job:
    hospital_id: str
    run_id: int | None
    r2_key: str | None
    fmt_hint: str | None
    file_bytes: int | None
    mrf_url: str | None = None


def list_deferred_jobs() -> list[Job]:
    rows = d1_query("""
        SELECT ir.id, ir.hospital_id, ir.r2_key, ir.file_format, ir.file_bytes,
               ir.warnings, h.mrf_url
        FROM ingest_runs ir
        JOIN hospitals h ON h.hospital_id = ir.hospital_id
        WHERE ir.id IN (SELECT MAX(id) FROM ingest_runs GROUP BY hospital_id)
          AND ir.status = 'partial'
          AND (ir.warnings LIKE '%DuckDB%'
               OR ir.warnings LIKE '%stream ceiling%'
               OR ir.warnings LIKE '%body exceeded%')
        ORDER BY ir.file_bytes ASC
    """)
    return [
        Job(
            hospital_id=r["hospital_id"],
            run_id=r["id"],
            r2_key=r.get("r2_key") or None,
            fmt_hint=r.get("file_format"),
            file_bytes=r.get("file_bytes"),
            mrf_url=r.get("mrf_url") or None,
        )
        for r in rows
    ]


def update_run_status(run_id: int, status: str, rows_written: int,
                      error: str | None = None) -> None:
    err_sql = f", error={_sql_lit(error)}" if error else ""
    d1_query(
        f"UPDATE ingest_runs SET status='{status}', "
        f"rows_written={rows_written}, finished_at=datetime('now'){err_sql} "
        f"WHERE id={run_id};"
    )


def process_job(job: Job, *, dry_run: bool, local_input: Path | None = None) -> None:
    print(f"\n=== {job.hospital_id} (run_id={job.run_id}, {job.file_bytes} bytes) ===")

    with tempfile.TemporaryDirectory(prefix=f"mrf-{job.hospital_id}-") as tmpdir:
        tmp = Path(tmpdir)
        if local_input:
            src = local_input
        else:
            key = (job.r2_key or "").strip() or r2_latest_key(job.hospital_id)
            if key:
                src = tmp / Path(key).name
                print(f"  downloading r2://{R2_BUCKET}/{key}")
                r2_download(key, src)
            elif job.mrf_url:
                src = tmp / (Path(job.mrf_url.split('?')[0]).name or 'mrf.bin')
                print(f"  downloading upstream {job.mrf_url}")
                http_download(job.mrf_url, src)
            else:
                print(f"  no R2 object or upstream URL, skipping")
                return

        src = maybe_unzip(src)
        fmt = detect_format(src)
        if job.fmt_hint and job.fmt_hint != "unknown" and job.fmt_hint != fmt:
            print(f"  format hint {job.fmt_hint!r} but detected {fmt!r}; using {fmt!r}")
        print(f"  parsing as {fmt}")

        try:
            parquet = parse_with_duckdb(src, job.hospital_id, mrf_date=None, fmt=fmt)
        except Exception as e:
            print(f"  parse failed: {e}")
            if job.run_id and not dry_run:
                update_run_status(job.run_id, "failed", 0, error=str(e)[:500])
            return

        rows = duckdb.connect().execute(
            f"SELECT count(*) FROM read_parquet('{parquet}')"
        ).fetchone()[0]
        print(f"  parsed {rows} rows → {parquet.name}")

        # Determine the snapshot date for the R2 key. Prefer the parquet's
        # MAX(mrf_date); fall back to today.
        mrf_date_row = duckdb.connect().execute(
            f"SELECT MAX(mrf_date) FROM read_parquet('{parquet}')"
        ).fetchone()
        mrf_date = (mrf_date_row[0] if mrf_date_row else None) or date.today().isoformat()

        chunks = emit_sql_chunks(parquet, job.hospital_id)
        print(f"  emitted {len(chunks)} aggregate SQL chunk(s)")

        if dry_run:
            print(f"  [dry-run] skipping D1 apply + R2 upload")
            return

        # Upload the raw parquet to R2 so we can re-aggregate later without
        # re-fetching the upstream MRF (option-3 architecture).
        r2_key = f"rates/{job.hospital_id}/{mrf_date}.parquet"
        try:
            print(f"  uploading parquet to r2://{R2_BUCKET}/{r2_key} ({parquet.stat().st_size} bytes)")
            r2_upload(r2_key, parquet)
            d1_query(
                "INSERT OR REPLACE INTO rate_parquet_exports "
                "(hospital_id, mrf_date, r2_key, row_count, bytes, exported_at) "
                f"VALUES ({_sql_lit(job.hospital_id)}, {_sql_lit(mrf_date)}, "
                f"{_sql_lit(r2_key)}, {rows}, {parquet.stat().st_size}, "
                f"datetime('now'));"
            )
        except Exception as e:
            print(f"  WARN: parquet upload failed: {e} (continuing — aggregates will still load)")

        for i, ch in enumerate(chunks, 1):
            print(f"  applying chunk {i}/{len(chunks)} ({ch.name}, {ch.stat().st_size} bytes)")
            try:
                d1_apply_file(ch)
            except Exception as e:
                if job.run_id:
                    update_run_status(job.run_id, "failed", 0,
                                      error=f"chunk {i} failed: {str(e)[:300]}")
                raise

        if job.run_id:
            if rows == 0:
                update_run_status(job.run_id, "failed", 0,
                                  error="DuckDB parsed 0 rows (likely csv-wide or unsupported schema)")
                print(f"  WARN: 0 rows parsed; marked failed for review")
            else:
                update_run_status(job.run_id, "ok", rows)
                print(f"  done: {rows} raw rows → aggregates + parquet, run {job.run_id} marked ok")


# ─── cli ────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--all-deferred", action="store_true",
                   help="Process every deferred run in remote D1.")
    p.add_argument("--hospital-id", help="Process a single hospital.")
    p.add_argument("--input", type=Path,
                   help="Local file to parse instead of R2 download.")
    p.add_argument("--dry-run", action="store_true",
                   help="Parse and emit SQL chunks but skip D1 apply.")
    p.add_argument("--limit", type=int, default=None,
                   help="When --all-deferred, only process the first N (smallest first).")
    args = p.parse_args()

    if not (args.all_deferred or args.hospital_id):
        p.error("Need either --all-deferred or --hospital-id")

    if args.all_deferred:
        jobs = list_deferred_jobs()
        if args.limit:
            jobs = jobs[: args.limit]
        print(f"{len(jobs)} deferred job(s)")
    else:
        # Synthesize a job; query D1 for the latest deferred run if it exists.
        rows = d1_query(
            f"SELECT ir.id, ir.r2_key, ir.file_format, ir.file_bytes, h.mrf_url "
            f"FROM ingest_runs ir "
            f"JOIN hospitals h ON h.hospital_id = ir.hospital_id "
            f"WHERE ir.hospital_id='{args.hospital_id}' "
            f"ORDER BY ir.id DESC LIMIT 1"
        )
        meta = rows[0] if rows else {}
        jobs = [Job(
            hospital_id=args.hospital_id,
            run_id=meta.get("id"),
            r2_key=meta.get("r2_key"),
            fmt_hint=meta.get("file_format"),
            file_bytes=meta.get("file_bytes"),
            mrf_url=meta.get("mrf_url"),
        )]

    failed = 0
    for j in jobs:
        try:
            process_job(j, dry_run=args.dry_run, local_input=args.input)
        except Exception as e:
            failed += 1
            print(f"  ERROR {j.hospital_id}: {e}")

    print(f"\nDone. {len(jobs) - failed}/{len(jobs)} succeeded.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
