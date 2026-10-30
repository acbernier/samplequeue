"""Synthetic sample feed.

Deliberately contains **no status column**. The feed carries only things a
lab actually records -- when each step happened, which identifiers have been
assigned, what was measured -- and leaves "what status is this sample in?"
entirely to the rules a supervisor writes.

That is the point. A status column in the feed is someone else's guess
baked into the data, and every process then inherits that guess. Here, a
sample is in library construction because it has an LCSET and no pooling
date, and a supervisor decided that is what that means.

The two rollup fields (`Last Event`, `Last Event Date`) are facts about
which recorded timestamp is most recent, not inferences about workflow
state. They exist so "nothing has happened to this in two weeks" is one
condition instead of fourteen.

Deterministic by default (SEED), so a refresh does not reshuffle the world
underneath you while you are working.
"""
import random
from datetime import datetime, timedelta

PRODUCTS = [
    # (product name, product group, material, TAT target days, relative weight)
    ("PCR-Free Human WGS 30x", "WGS", "DNA", 28, 26),
    ("PCR-Free Human WGS 30x (High Volume)", "WGS", "DNA", 35, 14),
    ("Clinical Genome Sequencing - Technical Only", "cWGS", "DNA", 21, 10),
    ("Blended Genome Exome v2", "BGE", "DNA", 28, 22),
    ("Clinical Blended Genome Exome", "cBGE", "DNA", 21, 7),
    ("Custom Panel Sample Prep", "Panel", "DNA", 42, 6),
    ("Total RNASeq 50M reads", "RNA", "RNA", 35, 5),
    ("Transcriptome Capture v6", "RNA", "RNA", 35, 3),
    ("Somatic Tumor/Normal WGS", "Somatic", "DNA", 42, 4),
    ("Blood Biopsy ULP WGS QC", "ULP", "cfDNA", 14, 3),
]

STEPS = [
    ("Received DNA from Blood/Tissue/Cells", 58),
    ("In House XTR from Saliva", 18),
    ("Received DNA from Saliva", 10),
    ("In House XTR from Blood", 11),
    ("Batch Extraction", 3),
]

# The recorded milestones, in the order they happen, with the typical gap in
# days from the previous one. A sample's progress is simulated forward from
# its order date; whatever has not happened yet is simply blank.
MILESTONES = [
    ("Receipt Date", 6),
    ("Accessioning Date", 2),
    ("Extraction Date", 4),
    ("Initial Quant Date", 3),
    ("LCSET Creation Date", 5),
    ("Pooling Date", 3),
    ("Designation Date", 2),
    ("Sequencing Start Date", 3),
    ("Sequencing End Date", 2),
    ("Demultiplex Date", 1),
    ("Analysis Start Date", 1),
    ("Analysis Complete Date", 2),
    ("Primary Review Date", 2),
    ("Delivery Date", 2),
]
MILESTONE_FIELDS = [name for name, _ in MILESTONES]

COLLABORATORS = [
    ("Broad Neuro Initiative", "S. Okonkwo"), ("MGH Cardiology", "L. Ferreira"),
    ("All of Us Research Program", "D. Whitfield"), ("Rare Disease Consortium", "A. Nakamura"),
    ("Pediatric Oncology Network", "M. Castellanos"), ("Population Genomics Core", "R. Ahluwalia"),
    ("Infectious Disease Program", "T. Bergstrom"), ("Cancer Dependency Map", "J. Oyelaran"),
]

SITES = ["Boston, MA", "Cambridge, MA", "Rochester, MN", "Houston, TX", "Seattle, WA", "Durham, NC"]
PRIORITIES = [("Standard", 78), ("Rush", 14), ("Clinical - STAT", 8)]
MATERIAL_SOURCES = ["Whole Blood", "Saliva", "Buffy Coat", "Fresh Frozen Tissue", "Cell Pellet", "Plasma"]

# Only ever set on a sample that has genuinely stopped moving.
HOLD_REASONS = [
    "Awaiting collaborator consent form",
    "Low concentration - requesting re-send",
    "Shipment delayed in transit",
    "Quant failed, repeating",
    "Manifest mismatch with accessioning",
    "Billing hold - quote expired",
    "Instrument down, rescheduling",
]
STUCK_AFTER_DAYS = 14

SEED = 20260426
DEFAULT_COUNT = 6000


def _weighted(rng, pairs):
    total = sum(w for _, w in pairs)
    roll = rng.uniform(0, total)
    for value, weight in pairs:
        roll -= weight
        if roll <= 0:
            return value
    return pairs[-1][0]


def _sm_id(rng):
    return "SM-" + "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(5))


def _fmt(when):
    return when.strftime("%Y-%m-%d %H:%M:%S")


def generate(count=DEFAULT_COUNT, seed=SEED, **_):
    """Yield `count` sample dicts. Signature matches the other readers."""
    rng = random.Random(seed)
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    # Build a pool of orders first, so samples cluster into orders the way
    # real work does -- a supervisor almost always prioritises by order.
    orders = []
    for _ in range(max(1, count // 24)):
        product, group, material, tat = _weighted(
            rng, [((p, g, m, t), w) for p, g, m, t, w in PRODUCTS])
        collaborator, pi = rng.choice(COLLABORATORS)
        # Skewed recent: most orders are young, with a long tail of
        # stragglers. A flat distribution would put almost everything past
        # due and make every escalation rule useless.
        age = min(int(rng.expovariate(1 / 18.0)) + 1, 140)
        orders.append({
            "id": "PDO-%05d" % rng.randint(10000, 99999),
            "product": product, "group": group, "material": material, "tat": tat,
            "collaborator": collaborator, "pi": pi,
            "placed": today - timedelta(days=age),
            "size": rng.choice([8, 24, 48, 94, 96, 192, 384, 768]),
            "priority": _weighted(rng, PRIORITIES),
            "site": rng.choice(SITES),
            "clinical": group.startswith("c") or rng.random() < 0.08,
        })

    for index in range(count):
        order = rng.choice(orders)
        extracts_in_house = False
        step = _weighted(rng, STEPS)
        if "In House XTR" in step or step == "Batch Extraction":
            extracts_in_house = True

        # Walk the milestones forward from the order date. Anything whose
        # date would land in the future simply has not happened yet.
        dates = {}
        cursor = order["placed"]
        # A minority of samples stall somewhere; that is what hold reasons
        # and escalation rules are for.
        stall_at = rng.randint(0, len(MILESTONES) - 1) if rng.random() < 0.18 else None
        for position, (field, mean_gap) in enumerate(MILESTONES):
            if stall_at is not None and position > stall_at:
                break
            if field == "Extraction Date" and not extracts_in_house:
                continue  # nothing to record; the sample arrived as DNA
            gap = rng.expovariate(1.0 / mean_gap) + 0.25
            cursor = cursor + timedelta(days=gap)
            if cursor > today:
                break
            dates[field] = cursor

        reached = [f for f in MILESTONE_FIELDS if f in dates]
        last_event = reached[-1] if reached else ""
        last_event_date = dates[last_event] if last_event else None
        idle_days = (today - last_event_date).days if last_event_date else \
                    (today - order["placed"]).days

        has = dates.__contains__
        quant = round(rng.uniform(1.2, 180.0), 1) if has("Initial Quant Date") else ""
        volume = round(rng.uniform(15, 220), 1) if has("Initial Quant Date") else ""
        due = order["placed"] + timedelta(days=order["tat"])
        delivered = has("Delivery Date")

        record = {
            "Sample ID": _sm_id(rng),
            "Order ID": order["id"],
            "Collaborator Sample ID": "%s-%04d" % (
                order["collaborator"].split()[0].upper()[:4], index % 9999),

            "Product Name": order["product"],
            "Product Group": order["group"],
            "Processing Steps": step,
            "Material Type": order["material"],
            "Original Material": rng.choice(MATERIAL_SOURCES),
            "Extraction Required": "T" if extracts_in_house else "F",

            "Order Placed Date": _fmt(order["placed"]),
            "Order Size": order["size"],
            "Priority": order["priority"],
            "Collaborator": order["collaborator"],
            "Principal Investigator": order["pi"],
            "Collection Site": order["site"],
            "Is Clinical": "T" if order["clinical"] else "F",

            "Due Date": _fmt(due),
            "TAT Target (Days)": order["tat"],
            "Days To Due": (due - today).days,
            "Past Due": "T" if due < today and not delivered else "F",
            "Days Since Order": (today - order["placed"]).days,

            # Factual rollups over the milestone dates below -- which
            # timestamp is newest, and how long ago. Not a status.
            "Last Event": last_event,
            "Last Event Date": _fmt(last_event_date) if last_event_date else "",
            "Days Since Last Event": idle_days,

            # Identifiers, assigned as the sample progresses.
            "Plate Barcode": "PL-%06d" % rng.randint(1, 999999) if has("Accessioning Date") else "",
            "Well": "%s%02d" % (rng.choice("ABCDEFGH"), rng.randint(1, 12)) if has("Accessioning Date") else "",
            "LCSET": "LCSET-%d" % rng.randint(20000, 29999) if has("LCSET Creation Date") else "",
            "Flowcell": "FC-%s" % "".join(rng.choice("ABCDEFGH0123456789") for _ in range(9))
                        if has("Sequencing Start Date") else "",
            "Lane Fraction": round(rng.uniform(0.05, 1.0), 3) if has("Designation Date") else "",

            # Measurements, recorded at initial quant.
            "Concentration (ng/uL)": quant,
            "Volume (uL)": volume,
            "Total Mass (ng)": round(quant * volume, 1) if quant != "" else "",
            "A260/280": round(rng.uniform(1.55, 2.15), 2) if has("Initial Quant Date") else "",

            "Is Rework": "T" if rng.random() < 0.05 else "F",
            "Top Off Counter": rng.choice([0, 0, 0, 0, 1, 1, 2]) if has("Sequencing End Date") else 0,
            "Hold Reason": rng.choice(HOLD_REASONS)
                           if (idle_days > STUCK_AFTER_DAYS and not delivered and rng.random() < 0.55)
                           else "",

            # Whether the order is still live. Gates what enters the tool at
            # all (see sources.json open_filter); not a workflow status.
            "Order Is Open": "T",
            "Is Abandoned": "F",
        }
        for field in MILESTONE_FIELDS:
            record[field] = _fmt(dates[field]) if field in dates else ""
        yield record
