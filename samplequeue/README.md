# SampleQueue

A shared tool for organising and prioritising samples across lab processes.

1. **One source of open orders**, so nobody downloads from five places.
2. **Each process owner defines their own statuses** with a point-and-click rule
   builder — no two processes have to agree on what a status means.
3. **Each status is a worksheet**: the metadata columns you choose, a default
   sort you configure, and a priority order you set by hand and keep.

That last point is the design constraint. Re-running the rules against fresh
data never reshuffles a queue a human already ordered. Samples still in the same
status keep their position, their note and their pin. Only new arrivals
(appended), rule-moved samples (appended to their new status) and departed
samples change, and each is written to a change log you can read in the UI.

## Run it

```bash
./run.sh
```

First run creates `.venv` and installs dependencies, then serves
<http://127.0.0.1:8787> with auto-reload. To let the rest of the lab reach it
(auto-reload is off in this mode):

```bash
HOST=0.0.0.0 PORT=8787 ./run.sh
```

To load data and three example processes:

```bash
.venv/bin/python -m app.sources "Synthetic feed"
.venv/bin/python seed_demo.py
```

## The three views

### Processes

One card per lab workflow. A process owns its own statuses and its own queues.
Duplicating a working process is the usual way to start a new one.

### Board — a histogram

Opening a process shows one bar per status, **in rule order**, with the count at
each. This is the shape of the backlog at a glance: where work is piling up,
which queues are over their WIP limit, how old the oldest sample in each is.
Click any bar to open that status.

### Worksheet — one status

Everything in that status as a spreadsheet:

- **Columns** — choose which of the source's fields are shown, and in what
  order. Saved per status, so everyone opening it sees the same layout.
- **Default sort** — an ordered list of sort keys (Priority, then soonest due,
  then …). Blanks always sort last. Two ways to use it:
  - *Save & apply now* renumbers the queue immediately.
  - *Re-apply on every data refresh* (per status) keeps it permanently sorted.
    Leave it off for queues where hand-set priority is the point; turn it on
    for queues that genuinely are always "oldest first".
- **Batches** — how many samples the team runs together. Batches are counted
  down from the top of the priority order, so batch 1 is always the next run.
  A short final batch is flagged as partial, and each header has a
  *Select batch* button.
- **Ideal batch composition** (optional) — instead of taking the next N in
  order, describe what a good batch looks like and let the queue build one.
  Each slot is a rule plus an amount:

  | Mode | Means |
  | --- | --- |
  | exactly N | take N samples matching this rule |
  | N% of what is left | share out the capacity the fixed counts did not use |
  | the rest | take whatever remains that matches |

  Slots claim samples in the order listed, so put the must-haves first;
  priority order decides *which* matching sample gets claimed. "24 clinical,
  the rest research" is two slots. "Clinical first, then reworks, then an
  80/20 split between two projects" is four.

  Nothing is papered over: a slot that cannot be filled shows its shortfall
  on the batch header, and capacity topped up with samples matching no slot
  is labelled *filler*. A queue tool that silently drops samples to honour a
  spec is worse than one that shows you the compromise.
- **Colour rows** — tint each row by the value of one field (product, material,
  priority, …) to tell kinds of sample apart at a glance, with a legend showing
  the mix. Only fields with a small set of repeating values are offered, so
  nothing numeric like mass or concentration, where every row would get its own
  colour and the tinting would be noise.
- **Manual priority** — drag a row by its grip (⠿), or use ⤒ to send it to the
  top, then **Save priority order**. Pinned rows stay on top of their status.
- **Copying out** — click and drag across cells to select a range, shift-click
  to extend it, or click a column header to take the whole column. ⌘C / Ctrl+C
  copies it tab-separated, so one column pastes as a plain list of ids and a
  block pastes straight into a spreadsheet. Escape clears.
- **Selection** — tick rows to pin/unpin in bulk, move the selection to the top,
  or **send them to another status**. Shift-click a second checkbox to tick
  everything in between.
- **Search** filters the status; **Export CSV** exports it with exactly the
  columns the worksheet shows.

Sending samples to another status is a manual override: it is recorded, but the
next refresh puts them back where the rules say they belong. The UI says so
before it does it. Overriding the same thing repeatedly is the signal to change
the rule instead.

## Concepts

| Thing | What it is |
| --- | --- |
| **Source** | Where open orders come from. Configured in `sources.json`. |
| **Process** | One lab workflow. Owns its statuses and its queues. |
| **Scope** | A rule picking which samples belong to a process. |
| **Status** | A named bucket with a rule, a column layout, a default sort, and optional batching, composition and colouring. |
| **Queue** | The saved human priority order inside one status. |

A sample sits in exactly one status per process, and can be in several processes
at once.

### Finding a sample

The search box in the header takes a sample id, an order id, or part of either,
and answers "where is this?" — every queue the sample sits in, across every
process, with its rank in each. ⌘K focuses it. One tube ordered three times
shows as three rows, because they are tracked separately and can be in
different places.

### Rules

**There is no status column in the feed, on purpose.** A status column is
someone else's guess about what stage a sample is at, and every process that
reads it inherits that guess. Instead the feed carries what a lab actually
records — when each step happened, which identifiers exist, what was measured —
and each supervisor decides what those facts mean for their process.

In practice most statuses come out as "step X is recorded and step Y is not":

| Status | Rule |
| --- | --- |
| Awaiting accessioning | `Receipt Date` is not blank AND `Accessioning Date` is blank |
| In library construction | `LCSET Creation Date` is not blank AND `Pooling Date` is blank |
| On sequencer | `Sequencing Start Date` is not blank AND `Sequencing End Date` is blank |

`Last Event` and `Last Event Date` are factual rollups — which recorded
timestamp is the most recent — so "nothing has happened to this in two weeks"
is one condition instead of fourteen. They are not inferred statuses.

A rule is a tree of conditions joined by **match ALL** / **match ANY**, nestable
to any depth. Each condition is a field, an operator and a value, with operators
offered per field type:

- **text** — is / is not / is any of / contains / starts with / matches regex / is blank
- **number** — = ≠ > ≥ < ≤ / between
- **date** — is before / is after / within the last N days / more than N days ago
- **yes-no** — is yes / is no

Field lists and value dropdowns are built from the columns the source actually
returned, so there is nothing to type from memory and nothing to keep in sync
when the feed changes.

Status order matters:

- Put escalations first. *Status is one of [active] AND (past due OR more than
  14 days in status)* at the top pulls stuck samples out of whatever stage
  column they would otherwise hide in.
- Put a catch-all last. A status with no conditions matches everything left.
  Without one, leftovers land in **Unassigned**, which is deliberately visible.

The editor shows a live count beside every status while you edit, plus how many
samples match no status at all.

## Sources

`sources.json` is a list. Each entry has a `type`, a location, the columns that
identify a sample, and an `open_filter` — a rule (same format as everything
else) deciding which rows count as open.

The default source is **synthetic**: a generated population of 6,000 samples
across 52 columns, regenerated in under a second, with dates anchored to today.
It exists so UI work does not wait on a 322 MB export, and so date rules behave
sensibly. Change `count` or `seed` in `sources.json` to vary the population; the
same seed always produces the same world, so a refresh does not shuffle things
underneath you while you work.

`type` can be `synthetic`, `csv`, `tsv`, `xlsx` or `bigquery`. CSV reading
auto-detects UTF-16 and tab delimiters, which is what Tableau and most LIMS
exports produce.

**`key_fields` is the one setting that matters.** It is how a sample is
recognised as the same sample tomorrow. Get it wrong and every refresh looks
like a brand new population, and the saved queues are lost.

### Pointing at Mission Control

Switch the active source to the `bigquery` entry, fill in `project` and `query`,
and install the client:

```bash
.venv/bin/pip install google-cloud-bigquery
```

Nothing else changes. The field catalogue, the rule builder, the histogram and
the worksheets are all driven by whatever columns the query returns.

## Deploying for the lab

The app is built to run as a normal website: a container on Cloud Run, a
Cloud SQL Postgres database, and Google sign-in through Identity-Aware Proxy.
Nothing depends on anyone's laptop.

```bash
PROJECT=your-broad-project ./deploy/deploy.sh
```

**What it does.** Enables the APIs, creates a Cloud SQL Postgres instance,
stores the connection string in Secret Manager, builds and deploys the
container, attaches IAP to the Cloud Run service, and grants your domain
access. It prints the service URL at the end.

IAP attaches **directly to Cloud Run** (`gcloud run deploy --iap`), so there is
no load balancer to build and no serverless NEG to wire up.

**Two manual steps afterwards**, both printed by the script:

1. Click **Refresh data** once. The app starts with no data and no field
   catalogue, so the rule builder has nothing to offer until the first ingest.
2. Switch from trusted-header mode to verified mode. The app shows a yellow
   banner containing the exact `IAP_AUDIENCE` string IAP is sending — copy it
   into a `gcloud run services update` and the banner disappears. The audience
   format differs between IAP-on-Cloud-Run and IAP-behind-a-load-balancer, so
   the app reads it off a real request rather than making you guess.

### Storage

`DATABASE_URL` decides the engine. Unset means SQLite at `data/samplequeue.db`
(local work). Set to a `postgresql://` URL and it uses Postgres. The app writes
one dialect of SQL and `app/db.py` translates — the only two differences that
matter are placeholder style and `IS NULL` comparison, both handled in
`translate()`.

SQLite is not an option once deployed: Cloud Run containers have ephemeral
disks and can run several instances, so the file would vanish and the saved
queues with it. Hence Cloud SQL.

The smallest instance (`db-f1-micro`) is enough for a lab's worth of users and
is what `deploy.sh` creates. Override with `DB_TIER=`.

### Identity

`app/auth.py` has three modes, chosen by environment variable:

| Mode | When | Trust |
| --- | --- | --- |
| `IAP_AUDIENCE` set | production | Verifies the signed IAP JWT. A forged email header gets nowhere. |
| `TRUST_IAP_HEADER=1` | after deploy, before the LB exists | Takes the IAP email header at face value. Only safe because ingress is locked down. |
| neither | local development | Self-declared `X-Actor` name. Not trustworthy, and the UI shows a name box instead of a signed-in badge. |

`deploy.sh` starts you in `TRUST_IAP_HEADER` mode because the audience string
does not exist until the service does. **Switch to `IAP_AUDIENCE` as soon as
the app is up** — it shows you the exact value in a banner. `ALLOWED_DOMAINS`
(default `broadinstitute.org`) is a second gate on top of the IAP IAM policy.

Everything except `/healthz` requires identity; the health check stays open for
the load balancer probe.

### Compliance

The default source is synthetic, so the deployed app holds no real data. That
changes the moment you point it at Mission Control: real sample identifiers,
collaborator IDs and clinical flags land in that Postgres instance. Confirm with
whoever owns data governance for your group that the project and database are
inside the right boundary **before** switching sources.

### Scheduled refreshes

A Cloud Scheduler job hitting the ingest endpoint, which pulls the source and
re-sorts every process in one call:

```bash
curl -XPOST "https://your-service/api/sources/Synthetic%20feed/ingest"
```

Scheduler needs to get through IAP: add its service account to the IAP policy
and have the job send an OIDC token.

### Running the container locally

```bash
docker build -t samplequeue .
docker run -p 8080:8080 \
  -e DATABASE_URL="postgresql://user:pw@host.docker.internal:5432/samplequeue" \
  -e TRUST_IAP_HEADER=1 -e ALLOWED_DOMAINS=broadinstitute.org \
  samplequeue
curl -H 'X-Goog-Authenticated-User-Email: accounts.google.com:you@broadinstitute.org' \
  http://localhost:8080/api/processes
```

The Postgres role must **own** its database, otherwise schema creation fails
with `must be owner of table samples`. `createdb -O samplequeue samplequeue`.

## Layout

```
app/db.py        schema, migrations, SQLite/Postgres dialect shim
app/auth.py      identity: verified IAP, trusted header, or local name
app/rules.py     the condition engine (shared by scope, statuses and open_filter)
app/synth.py     the synthetic sample generator
app/sources.py   source adapters, field catalogue, ingest
app/queues.py    reconciliation, histogram, worksheet, sorting, saved order
app/main.py      HTTP API
app/static/      frontend, no build step
sources.json     source configuration
seed_demo.py     three example processes
Dockerfile       container image
deploy/deploy.sh one-shot Cloud Run + Cloud SQL deploy
```

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/meta` | field catalogue, operators, snapshot info |
| POST | `/api/sources/{name}/ingest` | pull the source, re-sort every process |
| GET POST | `/api/processes` | list / create |
| GET PUT DELETE | `/api/processes/{id}` | read / save rules / delete |
| POST | `/api/processes/{id}/duplicate` | copy a process and its rules |
| POST | `/api/preview` | dry-run rules, returns counts and examples |
| GET | `/api/processes/{id}/overview` | histogram: one bar per status |
| GET | `/api/processes/{id}/statuses/{sid}/page` | the worksheet for one status |
| PUT | `/api/processes/{id}/statuses/{sid}/view` | save columns, sort, batch size, composition, colouring |
| POST | `/api/processes/{id}/statuses/{sid}/resort` | apply the default sort now |
| POST | `/api/processes/{id}/reconcile` | re-apply rules, preserve order |
| POST | `/api/processes/{id}/order` | save a queue's order |
| POST | `/api/processes/{id}/bulk` | move or pin a selection |
| POST | `/api/processes/{id}/items/{key}` | note, pin, manual status override |
| GET | `/api/processes/{id}/events` | change log |
| GET | `/api/processes/{id}/export.csv` | export, honouring saved priority |

`{sid}` is a status id, or `unassigned` for the no-rule-matched bucket.
Interactive docs at `/api/docs`.
