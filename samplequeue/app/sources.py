"""Source adapters: pull open orders in, normalise them, store a snapshot.

Adapters only have to yield dicts of {column name: value}. Everything
downstream -- the field catalogue, the rule builder, the queues -- is
driven off whatever columns show up, so adding a source does not mean
touching the rest of the app.
"""
import csv
import json
import os
import sqlite3
import sys

from . import db, rules

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sources.json")

# Columns worth promoting out of the JSON blob, tried in order.
KEY_CANDIDATES = [
    ["Order ID", "Sample ID"],
    ["MC ORDER  SAMPLE (OrderID)", "MC ORDER  SAMPLE (SampleID)"],
    ["Pdo Name", "Pdo Sample Id"],
    ["order_id", "sample_id"],
]
ORDER_ALIASES = ["Order ID", "MC ORDER  SAMPLE (OrderID)", "Pdo Name", "order_id"]
SAMPLE_ALIASES = ["Sample ID", "MC ORDER  SAMPLE (SampleID)", "Pdo Sample Id", "sample_id"]
PRODUCT_ALIASES = ["Product Name", "Product", "product_name"]

MAX_CATALOG_VALUES = 250


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as handle:
            return json.load(handle)
    return {"sources": []}


def save_config(config):
    with open(CONFIG_PATH, "w") as handle:
        json.dump(config, handle, indent=2)


# --------------------------------------------------------------------------
# Readers
# --------------------------------------------------------------------------

def _sniff_text(path):
    """Tableau and LIMS exports are frequently UTF-16 TSV, not UTF-8 CSV."""
    with open(path, "rb") as handle:
        head = handle.read(4)
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "utf-16", "\t"
    with open(path, encoding="utf-8-sig", errors="replace") as handle:
        first = handle.readline()
    return "utf-8-sig", ("\t" if first.count("\t") > first.count(",") else ",")


def read_delimited(path, **_):
    encoding, delimiter = _sniff_text(path)
    with open(path, encoding=encoding, errors="replace", newline="") as handle:
        for row in csv.DictReader(handle, delimiter=delimiter):
            yield {k: v for k, v in row.items() if k}


def read_xlsx(path, sheet=None, **_):
    try:
        from openpyxl import load_workbook
    except ImportError:  # pragma: no cover - dependency is in requirements.txt
        raise RuntimeError("openpyxl is required to read .xlsx sources")
    book = load_workbook(path, read_only=True, data_only=True)
    sheets = [sheet] if sheet else [book.sheetnames[0]]
    for name in sheets:
        grid = book[name].iter_rows(values_only=True)
        header = [str(c).strip() if c is not None else "" for c in next(grid)]
        for row in grid:
            if all(c is None for c in row):
                continue
            record = {}
            for col, value in zip(header, row):
                if col:
                    record[col] = "" if value is None else value
            if sheet is None and len(sheets) > 1:
                record["_sheet"] = name
            yield record
    book.close()


def read_bigquery(query=None, project=None, **_):
    """Mission Control (or any BigQuery) adapter.

    Left as a thin wrapper deliberately: when you point this at MC, the
    only thing that changes elsewhere is the column names the rule
    builder offers.
    """
    try:
        from google.cloud import bigquery
    except ImportError:
        raise RuntimeError(
            "google-cloud-bigquery is not installed. "
            "pip install google-cloud-bigquery, then set the source's `query` and `project`."
        )
    if not query:
        raise RuntimeError("BigQuery source needs a `query`")
    client = bigquery.Client(project=project)
    for row in client.query(query).result():
        yield dict(row.items())


def read_synthetic(count=None, seed=None, **_):
    from . import synth
    return synth.generate(count=count or synth.DEFAULT_COUNT, seed=seed or synth.SEED)


READERS = {
    "synthetic": read_synthetic,
    "csv": read_delimited, "tsv": read_delimited,
    "xlsx": read_xlsx, "bigquery": read_bigquery,
}


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

def _first(record, names):
    for name in names:
        value = record.get(name)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def build_key(record, key_fields=None):
    if key_fields:
        parts = [str(record.get(f, "")).strip() for f in key_fields]
        if any(parts):
            return "::".join(parts)
    for candidate in KEY_CANDIDATES:
        if all(c in record for c in candidate):
            parts = [str(record.get(c, "")).strip() for c in candidate]
            if any(parts):
                return "::".join(parts)
    # Last resort: hash the whole row so ingest still works, but queue
    # memory will not survive a column change. Surfaced as a warning.
    return "row::%d" % hash(json.dumps(record, sort_keys=True, default=str))


def _clean(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return value


def infer_kind(values):
    seen = [v for v in values if v not in (None, "")]
    if not seen:
        return "text"
    sample = seen[:400]
    lowered = {str(v).strip().lower() for v in sample}
    if lowered and lowered <= (rules.TRUE_STRINGS | rules.FALSE_STRINGS):
        return "bool"
    if sum(1 for v in sample if rules.parse_number(v) is not None) >= len(sample) * 0.9:
        return "number"
    if sum(1 for v in sample if rules.parse_date(v) is not None) >= len(sample) * 0.9:
        return "date"
    return "text"


# --------------------------------------------------------------------------
# Ingest
# --------------------------------------------------------------------------

def ingest(source_name, progress=None):
    """Run one source end to end and replace the sample snapshot."""
    config = load_config()
    source = next((s for s in config["sources"] if s["name"] == source_name), None)
    if source is None:
        raise KeyError("no source named %r" % source_name)

    reader = READERS.get(source["type"])
    if reader is None:
        raise RuntimeError("unknown source type %r" % source["type"])

    conn = db.connect()
    run_id = db.insert_returning_id(
        conn,
        "INSERT INTO source_runs (source, started_at, status) VALUES (?,?,?)",
        (source_name, db.now(), "running"))
    conn.commit()

    try:
        open_filter = source.get("open_filter") or {}
        key_fields = source.get("key_fields")
        options = source.get("options") or {}
        if source["type"] in ("csv", "tsv", "xlsx"):
            options.setdefault("path", source.get("path"))
        location = options.pop("path", None)

        kept = []
        scanned = 0
        column_values = {}
        column_order = {}

        stream = reader(location, **options) if location else reader(**options)
        for record in stream:
            scanned += 1
            record = {k: _clean(v) for k, v in record.items()}
            for name in record:
                column_order.setdefault(name, len(column_order))
            if not rules.evaluate(open_filter, record):
                continue
            for name, value in record.items():
                bucket = column_values.setdefault(name, [])
                if len(bucket) < 4000:
                    bucket.append(value)
            kept.append((build_key(record, key_fields), record))
            if progress and scanned % 20000 == 0:
                progress("scanned %d rows, kept %d" % (scanned, len(kept)))

        stamp = db.now()
        with conn:
            conn.execute("DELETE FROM fields")
            for name, index in sorted(column_order.items(), key=lambda kv: kv[1]):
                values = column_values.get(name, [])
                distinct = sorted({str(v) for v in values if v not in (None, "")})
                conn.execute(
                    "INSERT INTO fields (name, kind, distinct_count, values_json, position) VALUES (?,?,?,?,?)",
                    (name, infer_kind(values), len(distinct),
                     json.dumps(distinct[:MAX_CATALOG_VALUES]), index),
                )

            seen_keys = set()
            rows = []
            for key, record in kept:
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                rows.append((
                    key,
                    _first(record, ORDER_ALIASES),
                    _first(record, SAMPLE_ALIASES),
                    _first(record, PRODUCT_ALIASES),
                    json.dumps({k: v for k, v in record.items() if v not in (None, "")}, default=str),
                    stamp, stamp,
                ))
            conn.execute("DELETE FROM samples")
            conn.executemany(
                "INSERT INTO samples (sample_key, order_id, sample_id, product_name, attrs,"
                " first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?)", rows)
            conn.execute(
                "UPDATE source_runs SET finished_at=?, row_count=?, status=?, message=? WHERE id=?",
                (db.now(), len(rows), "ok",
                 "scanned %d rows, %d open" % (scanned, len(rows)), run_id))
        return {"run_id": run_id, "scanned": scanned, "open": len(rows), "columns": len(column_order)}
    except Exception as exc:  # surface the failure instead of leaving a stuck run
        with conn:
            conn.execute(
                "UPDATE source_runs SET finished_at=?, status=?, message=? WHERE id=?",
                (db.now(), "error", str(exc)[:1000], run_id))
        raise


def load_samples():
    """All open samples as (key, attrs dict) -- small enough to hold in memory."""
    conn = db.connect()
    out = []
    for row in conn.execute("SELECT sample_key, attrs FROM samples"):
        out.append((row["sample_key"], json.loads(row["attrs"])))
    return out


if __name__ == "__main__":  # python -m app.sources <source name>
    db.init()
    name = sys.argv[1] if len(sys.argv) > 1 else load_config()["sources"][0]["name"]
    print(ingest(name, progress=lambda m: print(" ", m, flush=True)))
