"""Seed three example processes against the synthetic feed.

Illustrative, not authoritative -- rewrite or delete them.

Every status here is defined from raw recorded facts, not from a status
column, because the feed has no status column. The recurring shape is
"step X is recorded and step Y is not", which is how a supervisor
actually describes where a sample sits:

    In library construction  =  LCSET exists  AND  no pooling date yet

Safe to re-run; it reuses process and status rows by name so bookmarked
worksheet URLs keep working.
"""
import json

from app import db, queues

AND = lambda *kids: {"type": "group", "op": "AND", "children": list(kids)}
OR = lambda *kids: {"type": "group", "op": "OR", "children": list(kids)}
ANY = {"type": "group", "op": "AND", "children": []}


def cond(field, op, value=None, kind="text"):
    return {"type": "cond", "field": field, "kind": kind, "op": op, "value": value}


def done(field):
    return cond(field, "not_empty", kind="date")


def not_done(field):
    return cond(field, "empty", kind="date")


NUMERIC = {"Days To Due", "Days Since Last Event", "Days Since Order", "Order Size",
           "Concentration (ng/uL)", "Volume (uL)", "Total Mass (ng)", "A260/280",
           "Lane Fraction", "TAT Target (Days)", "Top Off Counter"}
DATEY = {"Order Placed Date", "Due Date", "Last Event Date", "Receipt Date",
         "Accessioning Date", "Extraction Date", "Initial Quant Date",
         "LCSET Creation Date", "Pooling Date", "Designation Date",
         "Sequencing Start Date", "Sequencing End Date", "Demultiplex Date",
         "Analysis Start Date", "Analysis Complete Date", "Primary Review Date",
         "Delivery Date"}


def sort(*keys):
    """sort("Days To Due", "Priority desc") -> sort spec with inferred kinds."""
    spec = []
    for key in keys:
        direction = "asc"
        if key.endswith(" desc"):
            key, direction = key[:-5], "desc"
        kind = "number" if key in NUMERIC else ("date" if key in DATEY else "text")
        spec.append({"field": key, "dir": direction, "kind": kind})
    return spec


IDENT = ["Sample ID", "Order ID", "Product Name", "Priority"]
AGE = ["Days Since Last Event", "Days To Due"]

# A sample that has gone quiet, or blown its date, and is not finished.
STALLED = AND(
    not_done("Delivery Date"),
    OR(cond("Past Due", "is_true", kind="bool"),
       cond("Days Since Last Event", "gt", 14, "number")))

# name, colour, conditions, wip, batch, default sort, worksheet columns, auto-sort
WGS = [
    ("Escalate", "#a4342c", STALLED, None, None,
     sort("Days To Due", "Days Since Last Event desc"),
     IDENT + AGE + ["Last Event", "Hold Reason", "Collaborator"], True),

    ("Awaiting receipt", "#9a6b00", not_done("Receipt Date"), None, None,
     sort("Days Since Order desc"),
     IDENT + ["Days Since Order", "Order Placed Date", "Collaborator", "Hold Reason"], False),

    ("Awaiting accessioning", "#b45309",
     AND(done("Receipt Date"), not_done("Accessioning Date")), None, 96,
     sort("Receipt Date"),
     IDENT + ["Receipt Date", "Original Material", "Days Since Last Event"], False),

    ("Needs in-house extraction", "#c2410c",
     AND(cond("Extraction Required", "is_true", kind="bool"),
         done("Accessioning Date"), not_done("Extraction Date")), 150, 24,
     sort("Priority", "Days To Due"),
     IDENT + AGE + ["Original Material", "Processing Steps", "Plate Barcode", "Well"], False),

    ("Ready for initial quant", "#2f6f4f",
     AND(done("Accessioning Date"), not_done("Initial Quant Date")), 400, 96,
     sort("Priority", "Days To Due"),
     IDENT + AGE + ["Plate Barcode", "Well", "Extraction Date"], False),

    ("Ready for library prep", "#0f766e",
     AND(done("Initial Quant Date"), not_done("LCSET Creation Date")), None, 96,
     sort("Priority", "Concentration (ng/uL)"),
     IDENT + ["Concentration (ng/uL)", "Volume (uL)", "Total Mass (ng)", "A260/280",
              "Plate Barcode", "Well"], False),

    ("In library construction", "#1d4ed8",
     AND(done("LCSET Creation Date"), not_done("Pooling Date")), 300, 96,
     sort("LCSET", "Well"),
     IDENT + ["LCSET", "LCSET Creation Date", "Plate Barcode", "Well",
              "Is Rework", "Days Since Last Event"], False),

    ("Pooled, awaiting designation", "#6d28d9",
     AND(done("Pooling Date"), not_done("Designation Date")), None, None,
     sort("Pooling Date", "LCSET"),
     IDENT + ["LCSET", "Pooling Date", "Days Since Last Event"], False),

    ("Designated for sequencing", "#7c3aed",
     AND(done("Designation Date"), not_done("Sequencing Start Date")), None, 8,
     sort("Designation Date", "Lane Fraction desc"),
     IDENT + ["LCSET", "Lane Fraction", "Designation Date", "Days To Due"], False),

    ("On sequencer", "#be185d",
     AND(done("Sequencing Start Date"), not_done("Sequencing End Date")), None, None,
     sort("Flowcell", "Sample ID"),
     IDENT + ["Flowcell", "LCSET", "Lane Fraction", "Sequencing Start Date"], False),

    ("In analysis", "#0d9488",
     AND(done("Sequencing End Date"), not_done("Analysis Complete Date")), None, None,
     sort("Days Since Last Event desc"),
     IDENT + ["Flowcell", "Sequencing End Date", "Demultiplex Date",
              "Analysis Start Date", "Top Off Counter"], False),

    ("Awaiting review", "#475569",
     AND(done("Analysis Complete Date"), not_done("Delivery Date")), None, None,
     sort("Analysis Complete Date"),
     IDENT + ["Analysis Complete Date", "Primary Review Date", "Is Clinical"], False),

    ("Delivered", "#64748b", done("Delivery Date"), None, None,
     sort("Delivery Date desc"),
     IDENT + ["Delivery Date", "Days Since Order", "Collaborator"], False),
]

BGE = [
    ("Escalate", "#a4342c", STALLED, None, None,
     sort("Days To Due"),
     IDENT + AGE + ["Last Event", "Hold Reason"], True),

    ("Waiting on sample", "#9a6b00", not_done("Accessioning Date"), None, None,
     sort("Days Since Order desc"),
     IDENT + ["Days Since Order", "Receipt Date", "Original Material", "Hold Reason"], False),

    ("Ready for prep", "#2f6f4f",
     AND(done("Accessioning Date"), not_done("LCSET Creation Date")), 500, 96,
     sort("Priority", "Days To Due"),
     IDENT + AGE + ["Initial Quant Date", "Concentration (ng/uL)",
                    "Total Mass (ng)", "Plate Barcode", "Well"], False),

    ("In library construction", "#1d4ed8",
     AND(done("LCSET Creation Date"), not_done("Pooling Date")), None, 96,
     sort("LCSET", "Well"),
     IDENT + ["LCSET", "Plate Barcode", "Well", "Is Rework"], False),

    ("Sequencing & analysis", "#6d28d9",
     AND(done("Pooling Date"), not_done("Analysis Complete Date")), None, None,
     sort("Flowcell", "LCSET"),
     IDENT + ["LCSET", "Flowcell", "Designation Date", "Sequencing Start Date",
              "Days Since Last Event"], False),

    ("Awaiting review", "#475569",
     AND(done("Analysis Complete Date"), not_done("Delivery Date")), None, None,
     sort("Analysis Complete Date"),
     IDENT + ["Analysis Complete Date", "Is Clinical"], False),

    ("Delivered", "#64748b", done("Delivery Date"), None, None,
     sort("Delivery Date desc"),
     IDENT + ["Delivery Date", "Collaborator"], False),
]

EXTRACTION = [
    ("Not yet received", "#9a6b00", not_done("Receipt Date"), None, None,
     sort("Days Since Order desc"),
     IDENT + ["Original Material", "Days Since Order", "Collection Site", "Hold Reason"], False),

    ("Awaiting accessioning", "#b45309",
     AND(done("Receipt Date"), not_done("Accessioning Date")), None, 48,
     sort("Receipt Date"),
     IDENT + ["Receipt Date", "Original Material", "Days Since Last Event"], False),

    ("Ready to extract", "#2f6f4f",
     AND(done("Accessioning Date"), not_done("Extraction Date")), 250, 24,
     sort("Priority", "Days To Due"),
     IDENT + AGE + ["Original Material", "Processing Steps", "Plate Barcode", "Well"], True),

    ("Extracted, awaiting quant", "#0f766e",
     AND(done("Extraction Date"), not_done("Initial Quant Date")), None, 96,
     sort("Extraction Date"),
     IDENT + ["Extraction Date", "Plate Barcode", "Well"], False),

    ("Past extraction", "#64748b", ANY, None, None,
     sort("Last Event Date desc"),
     IDENT + ["Last Event", "Last Event Date"], False),
]

# Row colouring and ideal batch composition, for the handful of statuses
# where they illustrate something. Keyed by (process, status) so the status
# tables above stay readable.
IS_CLINICAL = cond("Is Clinical", "is_true", kind="bool")
IS_REWORK = cond("Is Rework", "is_true", kind="bool")

EXTRAS = {
    ("WGS Production", "Ready for library prep"): {
        "color_by": "Priority",
    },
    ("WGS Production", "In library construction"): {
        "color_by": "Product Name",
    },
    ("Exome / BGE Production", "Ready for prep"): {
        "color_by": "Product Name",
        "composition": {"slots": [
            {"label": "Clinical", "mode": "count", "count": 24, "rule": IS_CLINICAL},
            {"label": "Rework", "mode": "count", "count": 8, "rule": IS_REWORK},
            {"label": "Research", "mode": "fill", "rule": ANY},
        ]},
    },
    ("In-House Extraction", "Ready to extract"): {
        "color_by": "Original Material",
        "composition": {"slots": [
            {"label": "Clinical first", "mode": "count", "count": 6, "rule": IS_CLINICAL},
            {"label": "Saliva", "mode": "percent", "percent": 60,
             "rule": cond("Original Material", "eq", "Saliva")},
            {"label": "Everything else", "mode": "percent", "percent": 40, "rule": ANY},
        ]},
    },
}

PROCESSES = [
    {"name": "WGS Production",
     "description": "Whole genome sequencing, clinical and research.",
     "scope": AND(cond("Product Group", "in", ["WGS", "cWGS"])),
     "statuses": WGS},
    {"name": "Exome / BGE Production",
     "description": "Blended genome exome, clinical and research.",
     "scope": AND(cond("Product Group", "in", ["BGE", "cBGE"])),
     "statuses": BGE},
    {"name": "In-House Extraction",
     "description": "Samples the lab extracts itself, across every product.",
     "scope": AND(cond("Extraction Required", "is_true", kind="bool")),
     "statuses": EXTRACTION},
]


def seed():
    db.init()
    conn = db.connect()
    stamp = db.now()
    for spec in PROCESSES:
        old = conn.execute("SELECT id FROM processes WHERE name=?", (spec["name"],)).fetchone()
        with conn:
            if old:
                pid = old["id"]
                conn.execute(
                    "UPDATE processes SET description=?, scope=?, updated_at=? WHERE id=?",
                    (spec["description"], json.dumps(spec["scope"]), stamp, pid))
                conn.execute("DELETE FROM queue_items WHERE process_id=?", (pid,))
            else:
                pid = db.insert_returning_id(
                    conn,
                    "INSERT INTO processes (name, description, owner, scope, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (spec["name"], spec["description"], "demo", json.dumps(spec["scope"]),
                     stamp, stamp))

            # Match statuses by name so their ids -- and therefore bookmarked
            # worksheet URLs -- survive a re-seed.
            existing = {r["name"]: r["id"] for r in conn.execute(
                "SELECT id, name FROM statuses WHERE process_id=?", (pid,))}
            keep = []
            for index, row in enumerate(spec["statuses"]):
                name, color, conditions, wip, batch, sort_spec, columns, auto = row
                extra = EXTRAS.get((spec["name"], name), {})
                values = (color, index, json.dumps(conditions), wip, batch,
                          json.dumps(extra.get("composition", {})),
                          extra.get("color_by", ""),
                          json.dumps(sort_spec), 1 if auto else 0, json.dumps(columns))
                if name in existing:
                    conn.execute(
                        "UPDATE statuses SET color=?, position=?, conditions=?, wip_limit=?,"
                        " batch_size=?, batch_composition=?, color_by=?,"
                        " sort_spec=?, auto_sort=?, columns=? WHERE id=?",
                        values + (existing[name],))
                    keep.append(existing[name])
                else:
                    keep.append(db.insert_returning_id(
                        conn,
                        "INSERT INTO statuses (process_id, name, color, position, conditions,"
                        " wip_limit, batch_size, batch_composition, color_by,"
                        " sort_spec, auto_sort, columns)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (pid, name) + values))
            conn.execute(
                "DELETE FROM statuses WHERE process_id=? AND id NOT IN (%s)"
                % (",".join("?" * len(keep)) or "NULL"), [pid] + keep)

        summary = queues.reconcile(pid, actor="seed")
        # Nothing human to protect on a fresh seed, so start each queue in
        # its own default order rather than in whatever order it scanned.
        for status in queues.get_statuses(pid):
            if status["sort_spec"]:
                queues.resort(pid, status["id"], actor="seed")
        print("%-24s %5d samples sorted" % (spec["name"], summary["total"]))


if __name__ == "__main__":
    seed()
