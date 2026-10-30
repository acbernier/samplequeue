"""Queue reconciliation, status overview, and the per-status worksheet.

The ordering model has exactly three layers, applied in this order:

  1. **Pinned** samples float to the top of their status. Always.
  2. **Saved position** -- what a human dragged. This is the durable layer,
     and a data refresh never disturbs it.
  3. **Default sort** -- an ordered list of fields a supervisor configures
     per status. It decides where *new arrivals* land, and it can be
     applied to the whole queue on demand ("Apply default sort").

If a status has `auto_sort` on, layer 3 wins on every refresh and manual
order is not kept for that status. That is a deliberate per-status choice:
some queues are genuinely "always oldest-first" and re-dragging them daily
is the thing we are trying to abolish.
"""
import json

from . import db, rules

APPEND_GAP = 1024.0
MAX_WORKSHEET_ROWS = 2000


# --------------------------------------------------------------------------
# Reading definitions
# --------------------------------------------------------------------------

def get_process(process_id):
    conn = db.connect()
    row = conn.execute("SELECT * FROM processes WHERE id=?", (process_id,)).fetchone()
    if row is None:
        raise KeyError("no process %s" % process_id)
    return {
        "id": row["id"], "name": row["name"], "description": row["description"],
        "owner": row["owner"], "scope": db.jload(row["scope"], {}),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def get_statuses(process_id):
    conn = db.connect()
    rows = conn.execute(
        "SELECT * FROM statuses WHERE process_id=? ORDER BY position, id", (process_id,))
    return [{
        "id": r["id"], "process_id": r["process_id"], "name": r["name"],
        "color": r["color"], "position": r["position"],
        "conditions": db.jload(r["conditions"], {}), "wip_limit": r["wip_limit"],
        "batch_size": r["batch_size"],
        "batch_composition": db.jload(r["batch_composition"], {}) or {},
        "color_by": r["color_by"] or "",
        "sort_spec": db.jload(r["sort_spec"], []) or [],
        "auto_sort": bool(r["auto_sort"]),
        "columns": db.jload(r["columns"], []) or [],
    } for r in rows]


def get_status(process_id, status_id):
    for status in get_statuses(process_id):
        if status["id"] == status_id:
            return status
    raise KeyError("no status %s in process %s" % (status_id, process_id))


# --------------------------------------------------------------------------
# Sorting
# --------------------------------------------------------------------------

def _sort_value(attrs, field, kind):
    """Comparable value, or None for blank. None always sorts last."""
    raw = attrs.get(field)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if kind == "number":
        return rules.parse_number(raw)
    if kind == "date":
        parsed = rules.parse_date(raw)
        return parsed.timestamp() if parsed else None
    if kind == "bool":
        parsed = rules.parse_bool(raw)
        return None if parsed is None else (1.0 if parsed else 0.0)
    return str(raw).strip().lower()


def apply_sort(items, spec, attrs_of=lambda item: item["attrs"]):
    """Stable multi-key sort. Blanks last in both directions, pinned first."""
    for key in reversed(spec or []):
        field = key.get("field")
        kind = key.get("kind", "text")
        if not field:
            continue
        descending = str(key.get("dir", "asc")).lower() == "desc"
        blank = "" if kind not in ("number", "date", "bool") else 0.0
        items.sort(key=lambda it: (lambda v: blank if v is None else v)(
            _sort_value(attrs_of(it), field, kind)), reverse=descending)
        # Re-stabilise so blanks stay at the bottom whichever way we sorted.
        items.sort(key=lambda it: _sort_value(attrs_of(it), field, kind) is None)
    items.sort(key=lambda it: not item_pinned(it))
    return items


def item_pinned(item):
    value = item.get("pinned")
    return bool(value)


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------

def reconcile(process_id, actor="system"):
    """Apply the current rules to the current snapshot, preserving order."""
    conn = db.connect()
    process = get_process(process_id)
    statuses = get_statuses(process_id)
    by_id = {s["id"]: s for s in statuses}
    status_names = {s["id"]: s["name"] for s in statuses}

    samples = conn.execute("SELECT sample_key, attrs FROM samples").fetchall()
    existing = {
        r["sample_key"]: r for r in
        conn.execute("SELECT * FROM queue_items WHERE process_id=?", (process_id,))
    }

    tail = {}
    for item in existing.values():
        bucket = item["status_id"]
        tail[bucket] = max(tail.get(bucket, 0.0), item["position"])

    def append_position(status_id):
        nxt = tail.get(status_id, 0.0) + APPEND_GAP
        tail[status_id] = nxt
        return nxt

    stamp = db.now()
    entered, moved, left = [], [], []
    inserts, updates, events = [], [], []
    in_scope = set()
    touched_statuses = set()
    attrs_by_key = {}

    for row in samples:
        key = row["sample_key"]
        attrs = json.loads(row["attrs"])
        if not rules.evaluate(process["scope"], attrs):
            continue
        in_scope.add(key)
        attrs_by_key[key] = attrs
        match = rules.classify(statuses, attrs)
        target = match["id"] if match else None

        prior = existing.get(key)
        if prior is None:
            inserts.append((process_id, key, target, append_position(target), stamp, stamp, actor))
            events.append((process_id, key, "entered", None, status_names.get(target), actor, stamp))
            entered.append(key)
            touched_statuses.add(target)
        elif prior["status_id"] != target:
            updates.append((target, append_position(target), stamp, stamp, actor, process_id, key))
            events.append((process_id, key, "moved", status_names.get(prior["status_id"]),
                           status_names.get(target), actor, stamp))
            moved.append(key)
            touched_statuses.add(prior["status_id"])
            touched_statuses.add(target)
        # else: untouched on purpose -- this is the saved human ordering.

    for key, item in existing.items():
        if key not in in_scope:
            left.append(key)
            touched_statuses.add(item["status_id"])
            events.append((process_id, key, "left", status_names.get(item["status_id"]), None, actor, stamp))

    with conn:
        if inserts:
            conn.executemany(
                "INSERT INTO queue_items (process_id, sample_key, status_id, position,"
                " entered_status_at, updated_at, updated_by) VALUES (?,?,?,?,?,?,?)", inserts)
        if updates:
            conn.executemany(
                "UPDATE queue_items SET status_id=?, position=?, entered_status_at=?,"
                " updated_at=?, updated_by=? WHERE process_id=? AND sample_key=?", updates)
        if left:
            conn.executemany(
                "DELETE FROM queue_items WHERE process_id=? AND sample_key=?",
                [(process_id, k) for k in left])
        if events:
            conn.executemany(
                "INSERT INTO queue_events (process_id, sample_key, kind, from_status,"
                " to_status, actor, at) VALUES (?,?,?,?,?,?,?)", events)
        conn.execute("UPDATE processes SET updated_at=? WHERE id=?", (stamp, process_id))

    # Statuses that asked for it get renumbered by their default sort.
    resorted = []
    for status_id in touched_statuses:
        status = by_id.get(status_id)
        if status and status["auto_sort"] and status["sort_spec"]:
            resort(process_id, status_id, actor="auto-sort")
            resorted.append(status["name"])

    return {
        "entered": len(entered), "moved": len(moved), "left": len(left),
        "total": len(in_scope), "resorted": resorted, "at": stamp,
    }


def resort(process_id, status_id, actor=""):
    """Renumber one queue by its saved default sort."""
    status = get_status(process_id, status_id)
    items = _queue_rows(process_id, status_id)
    if not status["sort_spec"]:
        return {"sorted": 0, "reason": "no default sort configured"}
    apply_sort(items, status["sort_spec"])
    save_order(process_id, status_id, [i["sample_key"] for i in items],
               actor=actor or "default sort", kind="sorted")
    return {"sorted": len(items), "by": status["sort_spec"]}


def _queue_rows(process_id, status_id):
    conn = db.connect()
    sql = (
        "SELECT q.sample_key, q.status_id, q.position, q.pinned, q.note,"
        "       q.entered_status_at, q.updated_at, q.updated_by,"
        "       s.order_id, s.sample_id, s.product_name, s.attrs"
        "  FROM queue_items q JOIN samples s USING (sample_key)"
        " WHERE q.process_id=? AND q.status_id IS ?"
        " ORDER BY q.pinned DESC, q.position")
    return [{
        "sample_key": r["sample_key"], "order_id": r["order_id"],
        "sample_id": r["sample_id"], "product_name": r["product_name"],
        "position": r["position"], "pinned": bool(r["pinned"]), "note": r["note"],
        "entered_status_at": r["entered_status_at"], "updated_at": r["updated_at"],
        "updated_by": r["updated_by"], "attrs": json.loads(r["attrs"]),
    } for r in conn.execute(sql, (process_id, status_id))]


# --------------------------------------------------------------------------
# Batch composition
# --------------------------------------------------------------------------

def compose(rows, batch_size, composition):
    """Assign every row a batch number, and report how each batch turned out.

    Without a composition a batch is just the next `batch_size` rows in
    saved priority order. With one, each batch is *built* from the pool:
    slots claim samples in the order they are listed, and priority order
    is the tiebreaker for which matching sample gets claimed first.

    Slot modes:
      count   -- take exactly N matching samples
      percent -- take N% of whatever capacity the count slots left over
      fill    -- take whatever is left that matches

    Two things are reported rather than silently papered over: a slot that
    could not be filled (`short`), and capacity topped up with samples
    matching no slot (`filler`). A queue tool that quietly drops samples
    to honour a spec is worse than one that shows you the compromise.
    """
    slots = (composition or {}).get("slots") or []
    plan = []

    if not batch_size:
        for row in rows:
            row["batch"] = None
            row["slot"] = ""
        return plan

    if not slots:
        for index, row in enumerate(rows):
            row["batch"] = index // batch_size + 1
            row["slot"] = ""
        total = -(-len(rows) // batch_size) if rows else 0
        for number in range(1, total + 1):
            size = min(batch_size, len(rows) - (number - 1) * batch_size)
            plan.append({"number": number, "size": size, "capacity": batch_size,
                         "slots": [], "filler": 0})
        return plan

    # Capacity left for percentage slots after the fixed counts are met.
    fixed = sum(int(s.get("count") or 0) for s in slots if s.get("mode") == "count")
    share = max(0, batch_size - fixed)

    pool = list(rows)
    number = 0
    while pool:
        number += 1
        claimed = []
        taken_keys = set()
        report = []

        for slot in slots:
            mode = slot.get("mode", "count")
            if mode == "count":
                want = int(slot.get("count") or 0)
            elif mode == "percent":
                want = int(round(share * float(slot.get("percent") or 0) / 100.0))
            else:
                want = None  # fill slots are resolved after the sized ones
            if want is None:
                continue
            got = 0
            for row in pool:
                if len(claimed) >= batch_size or got >= want:
                    break
                if row["sample_key"] in taken_keys:
                    continue
                if rules.evaluate(slot.get("rule") or {}, row["attrs"]):
                    row["batch"] = number
                    row["slot"] = slot.get("label", "")
                    claimed.append(row)
                    taken_keys.add(row["sample_key"])
                    got += 1
            report.append({"label": slot.get("label", ""), "want": want, "got": got,
                           "short": max(0, want - got), "mode": mode})

        # Fill slots take what is left that matches them.
        for slot in slots:
            if slot.get("mode") != "fill":
                continue
            got = 0
            for row in pool:
                if len(claimed) >= batch_size:
                    break
                if row["sample_key"] in taken_keys:
                    continue
                if rules.evaluate(slot.get("rule") or {}, row["attrs"]):
                    row["batch"] = number
                    row["slot"] = slot.get("label", "")
                    claimed.append(row)
                    taken_keys.add(row["sample_key"])
                    got += 1
            report.append({"label": slot.get("label", ""), "want": None, "got": got,
                           "short": 0, "mode": "fill"})

        # Anything still unclaimed tops the batch up, in priority order, and
        # is flagged so the compromise is visible rather than hidden.
        filler = 0
        for row in pool:
            if len(claimed) >= batch_size:
                break
            if row["sample_key"] in taken_keys:
                continue
            row["batch"] = number
            row["slot"] = ""
            claimed.append(row)
            taken_keys.add(row["sample_key"])
            filler += 1

        if not claimed:  # nothing matched anything: stop rather than spin
            break
        plan.append({"number": number, "size": len(claimed), "capacity": batch_size,
                     "slots": report, "filler": filler})
        pool = [r for r in pool if r["sample_key"] not in taken_keys]

    # Any row the loop could not place still needs to render somewhere.
    for row in rows:
        row.setdefault("batch", None)
        row.setdefault("slot", "")
    return plan


# --------------------------------------------------------------------------
# Overview (the histogram)
# --------------------------------------------------------------------------

def overview(process_id):
    """Counts and health per status, in rule order -- what the board shows."""
    conn = db.connect()
    process = get_process(process_id)
    statuses = get_statuses(process_id)

    counts = {}
    oldest = {}
    fresh = {}
    pinned = {}
    for row in conn.execute(
            "SELECT status_id, COUNT(*) n, MIN(entered_status_at) oldest,"
            "       SUM(pinned) pins FROM queue_items WHERE process_id=? GROUP BY status_id",
            (process_id,)):
        counts[row["status_id"]] = row["n"]
        oldest[row["status_id"]] = row["oldest"]
        pinned[row["status_id"]] = row["pins"] or 0

    cutoff = db.now()[:10]
    for row in conn.execute(
            "SELECT status_id, COUNT(*) n FROM queue_items"
            " WHERE process_id=? AND entered_status_at >= ? GROUP BY status_id",
            (process_id, cutoff)):
        fresh[row["status_id"]] = row["n"]

    bars = []
    for status in statuses + [{"id": None, "name": "Unassigned", "color": "#94a3b8",
                               "wip_limit": None, "batch_size": None,
                               "batch_composition": {}, "color_by": "",
                               "sort_spec": [], "auto_sort": False}]:
        sid = status["id"]
        bars.append({
            "id": sid, "name": status["name"], "color": status["color"],
            "count": counts.get(sid, 0), "wip_limit": status["wip_limit"],
            "batch_size": status["batch_size"],
            "batches": (-(-counts.get(sid, 0) // status["batch_size"])
                        if status["batch_size"] else None),
            "over_limit": bool(status["wip_limit"] and counts.get(sid, 0) > status["wip_limit"]),
            "pinned": pinned.get(sid, 0), "new_today": fresh.get(sid, 0),
            "oldest_entered": oldest.get(sid),
            "auto_sort": status["auto_sort"],
            "sort_summary": summarise_sort(status["sort_spec"]),
        })
    return {
        "process": process, "bars": bars,
        "total": sum(counts.values()),
        "last_run": _last_run(),
    }


def summarise_sort(spec):
    if not spec:
        return ""
    return ", ".join("%s %s" % (k.get("field"), "↓" if str(k.get("dir")) == "desc" else "↑")
                     for k in spec if k.get("field"))


def _last_run():
    row = db.connect().execute("SELECT * FROM source_runs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------
# Worksheet (one status)
# --------------------------------------------------------------------------

def status_page(process_id, status_id, search=""):
    """Everything the spreadsheet view needs for one status."""
    process = get_process(process_id)
    statuses = get_statuses(process_id)
    if status_id is None:
        status = {"id": None, "name": "Unassigned", "color": "#94a3b8", "wip_limit": None,
                  "batch_size": None, "batch_composition": {}, "color_by": "",
                  "sort_spec": [], "auto_sort": False, "columns": [], "conditions": {}}
    else:
        status = get_status(process_id, status_id)

    rows = _queue_rows(process_id, status_id)
    total = len(rows)

    needle = (search or "").strip().lower()
    if needle:
        rows = [r for r in rows if needle in json.dumps(r["attrs"], default=str).lower()]

    truncated = len(rows) > MAX_WORKSHEET_ROWS
    rows = rows[:MAX_WORKSHEET_ROWS]

    columns = status["columns"] or default_columns()
    plan = compose(rows, status["batch_size"], status["batch_composition"])

    color_by = status["color_by"]
    for index, row in enumerate(rows, start=1):
        row["rank"] = index
        row["cells"] = {c: row["attrs"].get(c, "") for c in columns}
        # The colouring field need not be one of the visible columns.
        row["color_value"] = str(row["attrs"].get(color_by, "")) if color_by else ""
        row.pop("attrs")

    return {
        "process": {"id": process["id"], "name": process["name"]},
        "status": {
            "id": status["id"], "name": status["name"], "color": status["color"],
            "wip_limit": status["wip_limit"], "batch_size": status["batch_size"],
            "batch_composition": status["batch_composition"],
            "color_by": status["color_by"],
            "sort_spec": status["sort_spec"],
            "auto_sort": status["auto_sort"], "columns": columns,
        },
        "batch_plan": plan,
        "siblings": [{"id": s["id"], "name": s["name"], "color": s["color"]} for s in statuses]
                    + [{"id": None, "name": "Unassigned", "color": "#94a3b8"}],
        "rows": rows, "total": total, "shown": len(rows), "truncated": truncated,
        "available_columns": available_columns(),
    }


def available_columns():
    return [{"name": r["name"], "kind": r["kind"]}
            for r in db.connect().execute("SELECT name, kind FROM fields ORDER BY position")]


def default_columns():
    """A sensible opening view when a supervisor has not chosen columns yet."""
    have = [c["name"] for c in available_columns()]
    preferred = ["Sample ID", "Order ID", "Product Name", "Priority", "Last Event",
                 "Days Since Last Event", "Days To Due", "Collaborator"]
    picked = [c for c in preferred if c in have]
    return picked or have[:8]


def save_view(process_id, status_id, columns=None, sort_spec=None, auto_sort=None,
              batch_size=None, batch_given=False, batch_composition=None,
              color_by=None):
    """Persist the worksheet layout and default ordering for one status."""
    if status_id is None:
        raise KeyError("the Unassigned bucket has no saved view")
    sets, args = [], []
    if columns is not None:
        sets.append("columns=?")
        args.append(json.dumps(columns))
    if sort_spec is not None:
        sets.append("sort_spec=?")
        args.append(json.dumps(sort_spec))
    if auto_sort is not None:
        sets.append("auto_sort=?")
        args.append(1 if auto_sort else 0)
    if batch_given:
        sets.append("batch_size=?")
        args.append(int(batch_size) if batch_size else None)
    if batch_composition is not None:
        sets.append("batch_composition=?")
        args.append(json.dumps(batch_composition))
    if color_by is not None:
        sets.append("color_by=?")
        args.append(str(color_by))
    if not sets:
        return {"changed": False}
    conn = db.connect()
    with conn:
        conn.execute("UPDATE statuses SET %s WHERE id=? AND process_id=?" % ", ".join(sets),
                     args + [status_id, process_id])
    return {"changed": True}


# --------------------------------------------------------------------------
# Writing order and moving samples
# --------------------------------------------------------------------------

def save_order(process_id, status_id, ordered_keys, actor="", kind="reordered"):
    """Persist an explicit order as dense integer positions."""
    conn = db.connect()
    stamp = db.now()
    payload = [(float(i), stamp, actor, process_id, key) for i, key in enumerate(ordered_keys)]
    with conn:
        conn.executemany(
            "UPDATE queue_items SET position=?, updated_at=?, updated_by=?"
            " WHERE process_id=? AND sample_key=?", payload)
        conn.execute(
            "INSERT INTO queue_events (process_id, sample_key, kind, to_status, actor, at)"
            " VALUES (?,?,?,?,?,?)",
            (process_id, "%d samples" % len(ordered_keys), kind,
             _status_name(status_id), actor or "someone", stamp))
    return {"saved": len(ordered_keys), "at": stamp}


def _status_name(status_id):
    if status_id in (None, "", "null"):
        return "Unassigned"
    row = db.connect().execute("SELECT name FROM statuses WHERE id=?", (status_id,)).fetchone()
    return row["name"] if row else "Unassigned"


def set_item(process_id, sample_key, note=None, pinned=None, status_id=None,
             status_given=False, actor=""):
    """Manual overrides on one sample: a note, a pin, or a different status."""
    conn = db.connect()
    stamp = db.now()
    current = conn.execute(
        "SELECT * FROM queue_items WHERE process_id=? AND sample_key=?",
        (process_id, sample_key)).fetchone()
    if current is None:
        raise KeyError("sample %s is not in this process" % sample_key)

    sets, args, events = [], [], []
    if note is not None:
        sets.append("note=?")
        args.append(note)
        events.append(("noted", None, None))
    if pinned is not None:
        sets.append("pinned=?")
        args.append(1 if pinned else 0)
    if status_given and status_id != current["status_id"]:
        tail = conn.execute(
            "SELECT COALESCE(MAX(position),0) AS p FROM queue_items"
            " WHERE process_id=? AND status_id IS ?", (process_id, status_id)).fetchone()["p"]
        sets += ["status_id=?", "position=?", "entered_status_at=?"]
        args += [status_id, tail + APPEND_GAP, stamp]
        events.append(("moved", _status_name(current["status_id"]), _status_name(status_id)))
    if not sets:
        return {"changed": False}

    sets += ["updated_at=?", "updated_by=?"]
    args += [stamp, actor]
    args += [process_id, sample_key]
    with conn:
        conn.execute("UPDATE queue_items SET %s WHERE process_id=? AND sample_key=?"
                     % ", ".join(sets), args)
        for kind, src, dst in events:
            conn.execute(
                "INSERT INTO queue_events (process_id, sample_key, kind, from_status,"
                " to_status, actor, at) VALUES (?,?,?,?,?,?,?)",
                (process_id, sample_key, kind, src, dst, actor or "someone", stamp))
    return {"changed": True, "at": stamp}


def bulk_move(process_id, sample_keys, status_id, actor=""):
    """Send a selection to another status, appended in the order given."""
    conn = db.connect()
    stamp = db.now()
    tail = conn.execute(
        "SELECT COALESCE(MAX(position),0) AS p FROM queue_items"
        " WHERE process_id=? AND status_id IS ?", (process_id, status_id)).fetchone()["p"]

    current = {
        r["sample_key"]: r["status_id"] for r in conn.execute(
            "SELECT sample_key, status_id FROM queue_items WHERE process_id=?", (process_id,))
    }
    target_name = _status_name(status_id)

    updates, events = [], []
    for offset, key in enumerate(sample_keys, start=1):
        if key not in current or current[key] == status_id:
            continue
        updates.append((status_id, tail + offset * APPEND_GAP, stamp, stamp, actor, process_id, key))
        events.append((process_id, key, "moved", _status_name(current[key]),
                       target_name, actor or "someone", stamp))
    if not updates:
        return {"moved": 0}

    with conn:
        conn.executemany(
            "UPDATE queue_items SET status_id=?, position=?, entered_status_at=?,"
            " updated_at=?, updated_by=? WHERE process_id=? AND sample_key=?", updates)
        conn.executemany(
            "INSERT INTO queue_events (process_id, sample_key, kind, from_status,"
            " to_status, actor, at) VALUES (?,?,?,?,?,?,?)", events)
    return {"moved": len(updates), "to": target_name, "at": stamp}


def bulk_pin(process_id, sample_keys, pinned, actor=""):
    conn = db.connect()
    stamp = db.now()
    with conn:
        conn.executemany(
            "UPDATE queue_items SET pinned=?, updated_at=?, updated_by=?"
            " WHERE process_id=? AND sample_key=?",
            [(1 if pinned else 0, stamp, actor, process_id, k) for k in sample_keys])
    return {"pinned": len(sample_keys), "value": bool(pinned)}


# --------------------------------------------------------------------------
# Rule preview (unchanged behaviour, used by the editor)
# --------------------------------------------------------------------------

def preview(scope, statuses, limit=5):
    """Dry-run the rules against the snapshot so the editor can show counts."""
    conn = db.connect()
    samples = [(r["sample_key"], json.loads(r["attrs"]))
               for r in conn.execute("SELECT sample_key, attrs FROM samples")]

    prepared = [{"id": i, "name": s.get("name", "Status %d" % (i + 1)),
                 "conditions": s.get("conditions") or {}} for i, s in enumerate(statuses)]
    counts = [0] * len(prepared)
    examples = [[] for _ in prepared]
    unmatched, unmatched_examples, in_scope = 0, [], 0

    for key, attrs in samples:
        if not rules.evaluate(scope or {}, attrs):
            continue
        in_scope += 1
        match = rules.classify(prepared, attrs)
        if match is None:
            unmatched += 1
            if len(unmatched_examples) < limit:
                unmatched_examples.append(_thumbnail(key, attrs))
        else:
            counts[match["id"]] += 1
            if len(examples[match["id"]]) < limit:
                examples[match["id"]].append(_thumbnail(key, attrs))

    return {
        "total_samples": len(samples), "in_scope": in_scope,
        "statuses": [{"name": prepared[i]["name"], "count": counts[i], "examples": examples[i]}
                     for i in range(len(prepared))],
        "unmatched": {"count": unmatched, "examples": unmatched_examples},
    }


def _thumbnail(key, attrs):
    return {
        "sample_key": key,
        "order_id": attrs.get("Order ID", ""),
        "sample_id": attrs.get("Sample ID", ""),
        "product_name": attrs.get("Product Name", ""),
    }


def lookup(term, limit=25):
    """Find a sample and report every queue it is sitting in.

    Matches a sample id, an order id, or the combined key. One physical
    tube ordered three times is three rows here, which is the honest
    answer -- they are tracked separately and can be in different places.
    """
    needle = (term or "").strip()
    if not needle:
        return {"term": "", "samples": []}
    like = "%" + needle.lower() + "%"
    conn = db.connect()

    rows = conn.execute(
        "SELECT sample_key, order_id, sample_id, product_name, attrs FROM samples"
        " WHERE LOWER(sample_id)=? OR LOWER(order_id)=? OR LOWER(sample_key)=?"
        " OR LOWER(sample_id) LIKE ? OR LOWER(order_id) LIKE ?"
        " ORDER BY CASE WHEN LOWER(sample_id)=? THEN 0 ELSE 1 END, sample_key"
        " LIMIT ?",
        (needle.lower(), needle.lower(), needle.lower(), like, like, needle.lower(), limit),
    ).fetchall()
    if not rows:
        return {"term": needle, "samples": []}

    process_names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM processes")}
    status_info = {r["id"]: (r["process_id"], r["name"], r["color"])
                   for r in conn.execute("SELECT id, process_id, name, color FROM statuses")}

    out = []
    for row in rows:
        placements = []
        for item in conn.execute(
                "SELECT process_id, status_id, position, pinned, note, entered_status_at"
                "  FROM queue_items WHERE sample_key=?", (row["sample_key"],)):
            sid = item["status_id"]
            name, color = ("Unassigned", "#94a3b8")
            if sid is not None and sid in status_info:
                _, name, color = status_info[sid]
            # Rank within that queue, counting the same way the board does.
            rank = conn.execute(
                "SELECT COUNT(*) AS n FROM queue_items"
                " WHERE process_id=? AND status_id IS ?"
                "   AND (pinned > ? OR (pinned = ? AND position <= ?))",
                (item["process_id"], sid, item["pinned"], item["pinned"], item["position"]),
            ).fetchone()["n"]
            placements.append({
                "process_id": item["process_id"],
                "process": process_names.get(item["process_id"], "?"),
                "status_id": sid, "status": name, "color": color,
                "rank": rank, "pinned": bool(item["pinned"]), "note": item["note"],
                "entered_status_at": item["entered_status_at"],
            })
        placements.sort(key=lambda p: p["process"])
        attrs = json.loads(row["attrs"])
        out.append({
            "sample_key": row["sample_key"], "sample_id": row["sample_id"],
            "order_id": row["order_id"], "product_name": row["product_name"],
            "placements": placements,
            "highlights": {k: attrs.get(k, "") for k in
                           ("Priority", "Last Event", "Last Event Date",
                            "Days To Due", "Hold Reason") if attrs.get(k, "") != ""},
        })
    return {"term": needle, "samples": out}


def export_rows(process_id):
    """Every queued sample, in status order then priority order."""
    statuses = get_statuses(process_id) + [{"id": None, "name": "Unassigned", "columns": []}]
    for status in statuses:
        columns = status.get("columns") or default_columns()
        for index, row in enumerate(_queue_rows(process_id, status["id"]), start=1):
            yield status["id"], status["name"], index, row, columns
