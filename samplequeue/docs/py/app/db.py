"""Storage layer. SQLite for local work, PostgreSQL when deployed.

Everything the tool remembers between refreshes lives here: process
definitions, the status rules users author, and the saved queue order.
Sample rows themselves are disposable -- they get replaced on every
ingest from the upstream source.

Which engine is used depends on one environment variable:

    DATABASE_URL unset                 -> SQLite at data/samplequeue.db
    DATABASE_URL=postgresql://...      -> PostgreSQL

The rest of the app writes one dialect of SQL (SQLite's, with `?`
placeholders) and this module translates. The two differences worth
knowing about are handled by `translate()`:

  * `?` becomes `%s`
  * `col IS ?` becomes `col IS NOT DISTINCT FROM %s`, because Postgres
    `=` is never true for NULL and several queries key on a NULL
    status_id (the "Unassigned" bucket)
"""
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
IS_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))

DB_PATH = os.environ.get(
    "SAMPLEQUEUE_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "data", "samplequeue.db"),
)

_local = threading.local()
_pool = None

# `SERIAL` in Postgres, `INTEGER ... AUTOINCREMENT` in SQLite.
_PK = "BIGSERIAL PRIMARY KEY" if IS_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_runs (
    id            @PK@,
    source        TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    row_count     INTEGER,
    status        TEXT NOT NULL,
    message       TEXT
);

-- One row per open sample pulled from the upstream source.
CREATE TABLE IF NOT EXISTS samples (
    sample_key    TEXT PRIMARY KEY,
    order_id      TEXT,
    sample_id     TEXT,
    product_name  TEXT,
    attrs         TEXT NOT NULL,          -- JSON object of every source column
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_order ON samples(order_id);
CREATE INDEX IF NOT EXISTS idx_samples_product ON samples(product_name);

-- Catalogue of the columns the source provided, so the rule builder can
-- offer real field names and real value choices instead of free text.
CREATE TABLE IF NOT EXISTS fields (
    name           TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,         -- text | number | date | bool
    distinct_count INTEGER NOT NULL,
    values_json    TEXT NOT NULL,         -- JSON array of common values
    position       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS processes (
    id          @PK@,
    name        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    owner       TEXT NOT NULL DEFAULT '',
    scope       TEXT NOT NULL,            -- JSON condition group: which samples belong
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Ordered, first-match-wins status rules for one process.
CREATE TABLE IF NOT EXISTS statuses (
    id          @PK@,
    process_id  INTEGER NOT NULL REFERENCES processes(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    color       TEXT NOT NULL DEFAULT '#64748b',
    position    INTEGER NOT NULL,
    conditions  TEXT NOT NULL,            -- JSON condition group
    wip_limit   INTEGER,
    batch_size  INTEGER,                      -- chunk the queue into runs of this many
    batch_composition TEXT NOT NULL DEFAULT '{}', -- JSON {slots:[...]} ideal make-up of a batch
    color_by    TEXT NOT NULL DEFAULT '',     -- field whose value tints each row
    sort_spec   TEXT NOT NULL DEFAULT '[]',   -- JSON [{field, dir, kind}] default ordering
    auto_sort   INTEGER NOT NULL DEFAULT 0,   -- 1 = re-sort on every refresh
    columns     TEXT NOT NULL DEFAULT '[]'    -- JSON [field] visible in the worksheet
);
CREATE INDEX IF NOT EXISTS idx_statuses_process ON statuses(process_id, position);

-- The saved human decisions. Survives every refresh of `samples`.
CREATE TABLE IF NOT EXISTS queue_items (
    process_id        INTEGER NOT NULL REFERENCES processes(id) ON DELETE CASCADE,
    sample_key        TEXT NOT NULL,
    status_id         INTEGER,            -- NULL = matched no rule
    position          DOUBLE PRECISION NOT NULL,
    pinned            INTEGER NOT NULL DEFAULT 0,
    note              TEXT NOT NULL DEFAULT '',
    entered_status_at TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    updated_by        TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (process_id, sample_key)
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue_items(process_id, status_id, position);

-- Audit trail: how samples moved between statuses, and manual reorders.
CREATE TABLE IF NOT EXISTS queue_events (
    id          @PK@,
    process_id  INTEGER NOT NULL,
    sample_key  TEXT NOT NULL,
    kind        TEXT NOT NULL,            -- entered | moved | left | reordered | noted
    from_status TEXT,
    to_status   TEXT,
    actor       TEXT NOT NULL DEFAULT 'system',
    at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_process ON queue_events(process_id, at DESC);
""".replace("@PK@", _PK)

# Columns added after the first release; CREATE TABLE IF NOT EXISTS will not
# add them to a database that already exists.
MIGRATIONS = [
    ("statuses", "sort_spec", "TEXT NOT NULL DEFAULT '[]'"),
    ("statuses", "auto_sort", "INTEGER NOT NULL DEFAULT 0"),
    ("statuses", "columns", "TEXT NOT NULL DEFAULT '[]'"),
    ("statuses", "batch_size", "INTEGER"),
    ("statuses", "batch_composition", "TEXT NOT NULL DEFAULT '{}'"),
    ("statuses", "color_by", "TEXT NOT NULL DEFAULT ''"),
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def jload(raw, default=None):
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# Dialect translation
# --------------------------------------------------------------------------

_IS_PLACEHOLDER = re.compile(r"\bIS\s+\?", re.I)


def translate(sql: str) -> str:
    """Rewrite the app's SQLite-flavoured SQL for Postgres."""
    if not IS_POSTGRES:
        return sql
    sql = _IS_PLACEHOLDER.sub("IS NOT DISTINCT FROM ?", sql)
    return sql.replace("?", "%s")


class _PgConnection:
    """Enough of the sqlite3 connection surface for this app.

    Rows come back as mappings so `row["name"]` works the same either way,
    and `with conn:` commits on success and rolls back on failure, matching
    sqlite3's behaviour.
    """

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, args=()):
        cur = self._raw.cursor()
        cur.execute(translate(sql), tuple(args))
        return _PgCursor(cur)

    def executemany(self, sql, seq):
        rows = list(seq)
        cur = self._raw.cursor()
        if rows:
            cur.executemany(translate(sql), [tuple(r) for r in rows])
        return _PgCursor(cur)

    def executescript(self, script):
        cur = self._raw.cursor()
        cur.execute(script)
        self._raw.commit()
        return _PgCursor(cur)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._raw.commit()
        else:
            self._raw.rollback()
        return False


class _PgCursor:
    """Iterable cursor that also exposes sqlite3's `lastrowid`."""

    def __init__(self, cur):
        self._cur = cur

    def __iter__(self):
        return iter(self._cur.fetchall())

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def lastrowid(self):
        # Postgres has no lastrowid; callers that need the new id use
        # `insert_returning_id()` instead. Kept so attribute access does
        # not explode if something slips through.
        raise RuntimeError(
            "lastrowid is not available on PostgreSQL -- use db.insert_returning_id()")


def connect():
    """Thread-local connection; the server is threaded."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn

    if IS_POSTGRES:
        import psycopg
        from psycopg.rows import dict_row
        raw = psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=False)
        conn = _PgConnection(raw)
    else:
        directory = os.path.dirname(DB_PATH)
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # The browser-only build keeps the whole database in one file it
        # saves to the viewer's browser, so it asks for a rollback journal.
        conn.execute("PRAGMA journal_mode=%s" % os.environ.get("SAMPLEQUEUE_JOURNAL", "WAL"))
        conn.execute("PRAGMA foreign_keys=ON")
    _local.conn = conn
    return conn


def insert_returning_id(conn, sql, args):
    """INSERT and get the new primary key, on either engine."""
    if IS_POSTGRES:
        row = conn.execute(sql + " RETURNING id", args).fetchone()
        return row["id"]
    return conn.execute(sql, args).lastrowid


def existing_columns(conn, table):
    if IS_POSTGRES:
        rows = conn.execute(
            "SELECT column_name AS name FROM information_schema.columns WHERE table_name=?",
            (table,))
    else:
        rows = conn.execute("PRAGMA table_info(%s)" % table)
    return {r["name"] for r in rows}


def init() -> None:
    conn = connect()
    conn.executescript(SCHEMA)
    for table, column, spec in MIGRATIONS:
        if column not in existing_columns(conn, table):
            conn.execute('ALTER TABLE %s ADD COLUMN "%s" %s' % (table, column, spec))
    conn.commit()
