"""HTTP API + static frontend.

Identity comes from `app.auth`: a verified Google account when deployed
behind Identity-Aware Proxy, or a self-declared name when running locally.
"""
import csv
import io
import json
import os

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import auth, db, queues, rules, sources

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = FastAPI(title="SampleQueue", docs_url="/api/docs")


@app.on_event("startup")
def _startup():
    db.init()


def actor(request: Request) -> str:
    return auth.identify(request)


@app.get("/healthz")
def healthz():
    """Unauthenticated liveness probe for the load balancer."""
    return {"ok": True, "auth_mode": auth.MODE, "postgres": db.IS_POSTGRES}


@app.middleware("http")
async def require_identity(request: Request, call_next):
    """Reject unauthenticated traffic everywhere except the health probe.

    IAP already does this upstream; this is the backstop for anything that
    reaches the container directly.
    """
    if auth.MODE != "open" and request.url.path not in ("/healthz",):
        try:
            auth.identify(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return await call_next(request)


# --------------------------------------------------------------------------
# Metadata the rule builder runs on
# --------------------------------------------------------------------------

@app.get("/api/meta")
def meta(request: Request):
    conn = db.connect()
    fields = [{
        "name": r["name"], "kind": r["kind"],
        "distinct_count": r["distinct_count"], "values": db.jload(r["values_json"], []),
    } for r in conn.execute("SELECT * FROM fields ORDER BY position")]
    last = conn.execute(
        "SELECT * FROM source_runs ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "fields": fields,
        "operators": rules.OPERATORS,
        "sample_count": conn.execute("SELECT COUNT(*) c FROM samples").fetchone()["c"],
        "sources": [{"name": s["name"], "type": s["type"], "description": s.get("description", "")}
                    for s in sources.load_config()["sources"]],
        "last_run": dict(last) if last else None,
        "identity": auth.describe(request),
    }


@app.post("/api/sources/{name}/ingest")
def run_ingest(name: str):
    try:
        result = sources.ingest(name)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))
    # Rules are re-applied to every process so the boards reflect new data.
    refreshed = []
    for row in db.connect().execute("SELECT id, name FROM processes"):
        summary = queues.reconcile(row["id"], actor="ingest")
        refreshed.append({"process": row["name"], **summary})
    return {"ingest": result, "processes": refreshed}


@app.get("/api/runs")
def runs():
    return [dict(r) for r in db.connect().execute(
        "SELECT * FROM source_runs ORDER BY id DESC LIMIT 25")]


# --------------------------------------------------------------------------
# Processes and their status rules
# --------------------------------------------------------------------------

@app.get("/api/processes")
def list_processes():
    conn = db.connect()
    out = []
    for row in conn.execute("SELECT * FROM processes ORDER BY name"):
        counts = conn.execute(
            "SELECT COUNT(*) c FROM queue_items WHERE process_id=?", (row["id"],)).fetchone()["c"]
        statuses = conn.execute(
            "SELECT COUNT(*) c FROM statuses WHERE process_id=?", (row["id"],)).fetchone()["c"]
        out.append({
            "id": row["id"], "name": row["name"], "description": row["description"],
            "owner": row["owner"], "updated_at": row["updated_at"],
            "queued": counts, "status_count": statuses,
        })
    return out


@app.post("/api/processes")
def create_process(request: Request, payload: dict = Body(...)):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    conn = db.connect()
    stamp = db.now()
    try:
        with conn:
            new_id = db.insert_returning_id(
                conn,
                "INSERT INTO processes (name, description, owner, scope, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (name, payload.get("description", ""), payload.get("owner") or actor(request),
                 json.dumps(payload.get("scope") or {}), stamp, stamp))
    except Exception:
        conn.rollback()
        raise HTTPException(409, "a process named %r already exists" % name)
    return queues.get_process(new_id)


@app.get("/api/processes/{process_id}")
def read_process(process_id: int):
    try:
        process = queues.get_process(process_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    process["statuses"] = queues.get_statuses(process_id)
    return process


@app.put("/api/processes/{process_id}")
def update_process(process_id: int, request: Request, payload: dict = Body(...)):
    """Replaces the scope and the whole ordered status list in one shot.

    Status rows are matched by id so saved queues survive a rename or a
    reorder of the columns; only statuses the user actually deleted lose
    their queue, and those samples fall back to Unassigned on reconcile.
    """
    conn = db.connect()
    try:
        queues.get_process(process_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc))

    stamp = db.now()
    incoming = payload.get("statuses")
    with conn:
        conn.execute(
            "UPDATE processes SET name=COALESCE(?,name), description=COALESCE(?,description),"
            " owner=COALESCE(?,owner), scope=COALESCE(?,scope), updated_at=? WHERE id=?",
            (payload.get("name"), payload.get("description"), payload.get("owner"),
             json.dumps(payload["scope"]) if "scope" in payload else None, stamp, process_id))

        if incoming is not None:
            keep = []
            for index, status in enumerate(incoming):
                fields = (status.get("name") or "Untitled", status.get("color") or "#64748b",
                          index, json.dumps(status.get("conditions") or {}),
                          status.get("wip_limit"), status.get("batch_size"))
                if status.get("id"):
                    conn.execute(
                        "UPDATE statuses SET name=?, color=?, position=?, conditions=?,"
                        " wip_limit=?, batch_size=? WHERE id=? AND process_id=?",
                        fields + (status["id"], process_id))
                    keep.append(status["id"])
                else:
                    keep.append(db.insert_returning_id(
                        conn,
                        "INSERT INTO statuses (process_id, name, color, position, conditions,"
                        " wip_limit, batch_size) VALUES (?,?,?,?,?,?,?)", (process_id,) + fields))
            placeholders = ",".join("?" * len(keep)) or "NULL"
            conn.execute(
                "DELETE FROM statuses WHERE process_id=? AND id NOT IN (%s)" % placeholders,
                [process_id] + keep)

    summary = queues.reconcile(process_id, actor=actor(request) or "rules-update")
    return {"process": read_process(process_id), "reconcile": summary}


@app.delete("/api/processes/{process_id}")
def delete_process(process_id: int):
    conn = db.connect()
    with conn:
        conn.execute("DELETE FROM queue_items WHERE process_id=?", (process_id,))
        conn.execute("DELETE FROM queue_events WHERE process_id=?", (process_id,))
        conn.execute("DELETE FROM statuses WHERE process_id=?", (process_id,))
        conn.execute("DELETE FROM processes WHERE id=?", (process_id,))
    return {"deleted": process_id}


@app.post("/api/processes/{process_id}/duplicate")
def duplicate_process(process_id: int, payload: dict = Body(default={})):
    """Copying a working process is how a new one usually starts here."""
    source = read_process(process_id)
    name = payload.get("name") or (source["name"] + " (copy)")
    conn = db.connect()
    stamp = db.now()
    with conn:
        new_id = db.insert_returning_id(
            conn,
            "INSERT INTO processes (name, description, owner, scope, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (name, source["description"], source["owner"], json.dumps(source["scope"]), stamp, stamp))
        for status in source["statuses"]:
            conn.execute(
                "INSERT INTO statuses (process_id, name, color, position, conditions,"
                " wip_limit, batch_size, batch_composition, color_by,"
                " sort_spec, auto_sort, columns)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id, status["name"], status["color"], status["position"],
                 json.dumps(status["conditions"]), status["wip_limit"], status["batch_size"],
                 json.dumps(status["batch_composition"]), status["color_by"],
                 json.dumps(status["sort_spec"]), 1 if status["auto_sort"] else 0,
                 json.dumps(status["columns"])))
    queues.reconcile(new_id, actor="duplicate")
    return read_process(new_id)


@app.post("/api/preview")
def preview(payload: dict = Body(...)):
    """Live match counts while the user is still editing the rules."""
    return queues.preview(payload.get("scope") or {}, payload.get("statuses") or [])


# --------------------------------------------------------------------------
# Queues
# --------------------------------------------------------------------------

@app.get("/api/samples/lookup")
def sample_lookup(q: str = "", limit: int = 25):
    """Which queues is this sample in? Matches a sample id or an order id."""
    return queues.lookup(q, limit=limit)


@app.get("/api/processes/{process_id}/overview")
def get_overview(process_id: int):
    """Histogram data: one bar per status, in rule order."""
    try:
        return queues.overview(process_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc))


def _status_arg(raw):
    """Status ids travel as strings so "unassigned" can mean NULL."""
    if raw in (None, "", "null", "unassigned"):
        return None
    return int(raw)


@app.get("/api/processes/{process_id}/statuses/{status_id}/page")
def get_status_page(process_id: int, status_id: str, search: str = ""):
    try:
        return queues.status_page(process_id, _status_arg(status_id), search=search)
    except KeyError as exc:
        raise HTTPException(404, str(exc))


@app.put("/api/processes/{process_id}/statuses/{status_id}/view")
def put_status_view(process_id: int, status_id: str, payload: dict = Body(...)):
    """Save the worksheet layout: visible columns and the default sort."""
    try:
        return queues.save_view(
            process_id, _status_arg(status_id),
            columns=payload.get("columns"), sort_spec=payload.get("sort_spec"),
            auto_sort=payload.get("auto_sort"),
            batch_size=payload.get("batch_size"),
            batch_given="batch_size" in payload,
            batch_composition=payload.get("batch_composition"),
            color_by=payload.get("color_by"))
    except KeyError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/processes/{process_id}/statuses/{status_id}/resort")
def post_resort(process_id: int, status_id: str, request: Request):
    try:
        return queues.resort(process_id, _status_arg(status_id), actor=actor(request))
    except KeyError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/processes/{process_id}/bulk")
def post_bulk(process_id: int, request: Request, payload: dict = Body(...)):
    """Act on a selection: move to a status, or pin/unpin."""
    keys = payload.get("sample_keys") or []
    if not keys:
        raise HTTPException(400, "no samples selected")
    action = payload.get("action")
    if action == "move":
        return queues.bulk_move(process_id, keys, _status_arg(payload.get("status_id")),
                                actor=actor(request))
    if action == "pin":
        return queues.bulk_pin(process_id, keys, payload.get("pinned", True), actor=actor(request))
    raise HTTPException(400, "unknown action %r" % action)


@app.post("/api/processes/{process_id}/reconcile")
def post_reconcile(process_id: int, request: Request):
    return queues.reconcile(process_id, actor=actor(request) or "manual")


@app.post("/api/processes/{process_id}/order")
def post_order(process_id: int, request: Request, payload: dict = Body(...)):
    keys = payload.get("sample_keys") or []
    return queues.save_order(process_id, _status_arg(payload.get("status_id")), keys, actor(request))


@app.post("/api/processes/{process_id}/items/{sample_key:path}")
def post_item(process_id: int, sample_key: str, request: Request, payload: dict = Body(...)):
    try:
        return queues.set_item(
            process_id, sample_key,
            note=payload.get("note"), pinned=payload.get("pinned"),
            status_id=_status_arg(payload.get("status_id")),
            status_given="status_id" in payload, actor=actor(request))
    except KeyError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/processes/{process_id}/events")
def get_events(process_id: int, limit: int = 200):
    return [dict(r) for r in db.connect().execute(
        "SELECT * FROM queue_events WHERE process_id=? ORDER BY id DESC LIMIT ?",
        (process_id, limit))]


@app.get("/api/processes/{process_id}/export.csv")
def export_csv(process_id: int, status_id: str = ""):
    """The spreadsheet people still want, generated from the saved state.

    Each status exports the columns its worksheet is configured to show.
    """
    process = queues.get_process(process_id)
    only = _status_arg(status_id) if status_id else "all"

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    header_written = None
    for sid, name, rank, row, columns in queues.export_rows(process_id):
        if only != "all" and sid != only:
            continue
        head = ["Status", "Priority", "Pinned", "Note"] + list(columns)
        if head != header_written:
            writer.writerow(head)
            header_written = head
        writer.writerow([name, rank, "yes" if row["pinned"] else "", row["note"]]
                        + [row["attrs"].get(c, "") for c in columns])
    buffer.seek(0)
    filename = "%s-queues.csv" % process["name"].replace(" ", "-").lower()
    return StreamingResponse(
        iter([buffer.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="%s"' % filename})


# --------------------------------------------------------------------------
# Static frontend
# --------------------------------------------------------------------------

@app.get("/")
def index():
    """Serve the shell with the asset URLs stamped by file mtime.

    Without this, editing app.js only takes effect after a hard reload --
    hash navigation reuses the cached script, so you end up debugging code
    that is no longer on disk.
    """
    path = os.path.join(STATIC_DIR, "index.html")
    with open(path, encoding="utf-8") as handle:
        html = handle.read()
    for asset in ("app.js", "style.css"):
        stamp = int(os.path.getmtime(os.path.join(STATIC_DIR, asset)))
        html = html.replace("/static/%s" % asset, "/static/%s?v=%d" % (asset, stamp))
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(Exception)
def unhandled(request: Request, exc: Exception):  # pragma: no cover
    return JSONResponse({"detail": str(exc)}, status_code=500)
