# Reliable Distributed Transaction Processing Service

A backend service that processes financial account transactions safely under high
concurrency. It remains correct when requests are duplicated, workers are killed
mid-operation, queue messages are redelivered, reversals arrive before the
transactions they reverse, and Redis or PostgreSQL becomes unavailable.

This is a correctness and reliability system rather than a CRUD service. The API
surface is small; the design work is in the seams between the components.

**Stack:** Python 3.13, FastAPI, PostgreSQL 17, Redis 7, Docker Compose.

**Design record:** [`docs/DESIGN.md`](docs/DESIGN.md) — the design as argued before any code
was written, with a numbered decision log (D1–D11) giving the alternatives that were rejected
and why. This README says what the system does; that document says why it is shaped this way.

---

## Getting started

Everything below is copy-pasteable. Commands are shown for bash; where Windows
PowerShell differs, the PowerShell form follows on the next line.

### Prerequisites

| | |
| --- | --- |
| Docker | Docker Desktop, or Docker Engine with Compose v2 |
| Git | any recent version |
| Python 3.13 | **only** to run the tests, scripts and load generator from the host. Not needed to run the service. |

Host ports **8000** (API), **5433** (PostgreSQL) and **6379** (Redis) must be free.
PostgreSQL is deliberately published on 5433 rather than 5432 so the stack cannot
collide with a PostgreSQL already installed on the host — a collision that
presents as an authentication failure rather than a port error, which is a
confusing hour if you do not know to look for it.

### 1. Clone and configure

```bash
git clone https://github.com/Risvey-Hasan/banking_api.git
cd banking_api
cp .env.example .env
```

```powershell
Copy-Item .env.example .env
```

**Copy the file, do not retype it.** `.env.example` is normalised to LF by
`.gitattributes`; a CRLF `.env` is parsed differently by Docker Compose and by
Python's dotenv reader, and the resulting failure looks like a wrong password.

The shipped `POSTGRES_PASSWORD` is a local-development value. It must be
non-empty: the postgres image refuses to initialise a fresh data directory
without one.

### 2. Start the stack

```bash
docker compose up --build
```

That single command brings up everything, in dependency order:

1. **postgres** and **redis**, both gated on healthchecks
2. **migrate** — runs `alembic upgrade head` to completion and exits 0
3. **api** (uvicorn, 2 worker processes), **two worker containers**, **sweeper**

No manual table creation, no seed step, no ordering for you to get right. The
`migrate` service exists so that schema creation is a one-shot job with an exit
code rather than something the API does on startup — an API that migrates on boot
races itself when you run more than one instance of it.

Detached, which is what you want for everything that follows:

```bash
docker compose up --build -d
docker compose ps
```

Expected: `postgres` and `redis` healthy, `migrate` **Exited (0)**, and
`txnsvc-api-1`, `txnsvc-worker-1`, `txnsvc-worker-2`, `txnsvc-sweeper-1` up. Two
workers by default is deliberate — correctness across independent workers is a
core requirement, so the default stack demonstrates it rather than requiring a
flag.

### 3. Verify

```bash
curl http://localhost:8000/health
curl http://localhost:8000/metrics
```

`/health` reports `healthy`, `degraded` (Redis unreachable — the service still
accepts work) or `unhealthy` (PostgreSQL unreachable — the service refuses it).
Interactive OpenAPI documentation is at **http://localhost:8000/docs**.

A one-command smoke test of the happy path and the error contract:

```bash
python scripts/smoke.py
```

### If `migrate` exits 1 with `InvalidPasswordError`

```
FATAL: password authentication failed for user "txnsvc"
DETAIL: Connection matched ... "host all all all scram-sha-256"
```

You have a PostgreSQL volume left over from an earlier run of this stack, initialised
with a different password. `POSTGRES_PASSWORD` is an **initialisation** parameter: the
postgres image reads it once, when `initdb` runs against an empty data directory, and
ignores it forever after. The password then lives inside the database, not in the
environment.

Removing containers and images does not help — volumes outlive both, by design. Delete
them explicitly:

```bash
docker compose down -v
docker compose up --build
```

A successful fresh start prints `Volume txnsvc_pgdata Created`. If it does not, the old
volume is still in use. This stack sets `name: txnsvc` in `docker-compose.yml`, so its
identity is the project name rather than the directory — a second clone elsewhere on the
same machine adopts the same volumes. That is a deliberate trade for stable container
names (`txnsvc-postgres`, which the scripts and log instructions rely on) and it is
listed under **Known limitations**.

### 4. Run the automated tests

Two ways. Both need the stack from step 2 already running.

**In Docker — no Python on the host:**

```bash
docker compose --profile test run --rm tests
```

This builds a test image, joins the compose network and runs
`pytest -v -m "not slow"`. The `not slow` is not a convenience: the excluded test
(`test_worker_killed_mid_flight_loses_nothing`) kills a worker *container* by
driving `docker compose` itself, and this container has no Docker socket. Run that
one from the host.

**From the host — the full suite including the crash test:**

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest -v
```

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt
pytest -v
```

**Both** requirements files are needed. The tests assert against PostgreSQL
directly with `asyncpg` (a runtime dependency) as well as over HTTP with `httpx`
(a dev dependency) — because a service must not be the only witness to its own
correctness.

```bash
pytest -v                  # 29 tests, including the container-killing one
pytest -v -m "not slow"    # 28 tests, no container manipulation
```

The suite talks to the service over real HTTP rather than through an in-process
test client, and asserts the ledger invariant once after the whole run. Point it
elsewhere with `TXNSVC_API=http://host:port`.

### 5. Reproduce the failure demonstrations

Each script proves one numbered requirement and prints PASS/FAIL. Run them from
the host with the venv active:

```bash
python scripts/duplicate_hammer.py        # §5  1000 concurrent duplicates -> one effect
python scripts/multi_instance_hammer.py   # §5  the same across 3 API containers
python scripts/concurrency_check.py       # §6  100 concurrent debits -> exactly 0.0000
python scripts/apply_check.py             # §5  redelivery causes no second effect
python scripts/out_of_order_check.py      # §8  the five-message sequence -> net 0
python scripts/redis_outage_check.py      # §10 Redis stopped and restored, nothing lost
python scripts/postgres_outage_check.py   # §10 503s, and no false acceptance
python scripts/pg_waits.py --seconds 30   #     where PostgreSQL's time goes under load
```

`multi_instance_hammer.py` needs three API containers, which is an explicit
opt-in because it requires a port range:

```bash
API_PORTS=8000-8002 docker compose up --build -d --scale api=3
docker compose ps          # read the assigned 8000/8001/8002 mapping
```

```powershell
$env:API_PORTS = "8000-8002"
docker compose up --build -d --scale api=3
```

The base compose file publishes the API on **one fixed port** on purpose. A port
range lets Docker choose, and an API that lands on 8001 breaks every hardcoded
URL in this repository — intermittently, which is worse than breaking outright.

The §7 crash demonstration is deliberately manual, because a reviewer will want to
watch it happen. It is described in **Testing → Reproducing the failure
demonstrations** below.

### 6. Run the load test

**In Docker** (recommended — the generator is CPU-hungry and this keeps it off
the host's network stack):

```bash
docker compose --profile loadtest run --rm loadgen \
    python -m loadtest.run --rate 1000 --duration 60 --accounts 200
```

**From the host:**

```bash
python -m loadtest.run --rate 1000 --duration 60 --accounts 200
```

The generator is **open-loop**: it offers `--rate` requests per second on 100 ms
ticks regardless of how fast the service answers, and records its own scheduling
lag so you can tell whether the harness kept up. A closed-loop generator that
waits for each response measures the service and the harness together and cannot
separate them.

| flag | default | |
| --- | --- | --- |
| `--rate` | 1000 | offered requests/second |
| `--duration` | 60 | seconds |
| `--accounts` | 200 | distinct accounts; `--accounts 1` makes one hot row |
| `--processes` | 4 | generator processes — one Python process saturates a core near 200 rps |
| `--concurrency` | 50 | in-flight requests per process |
| `--label` | none | names the report file |

It discovers live API instances by probing `/health`, writes a JSON report to
`loadtest/results/`, and reports throughput, P50/P95/P99 latency, error rate,
queue backlog, worker processing delay and the ledger-mismatch count. Measured
results are in **Benchmark results** below.

### 7. Stop

```bash
docker compose down        # keep the data
docker compose down -v     # also delete the PostgreSQL and Redis volumes
```

Use `-v` between benchmark runs. Table size and cache state change throughput, so
comparing a run on a warm database against one on a cold database measures the
database's history rather than your change.

---

## Architecture

```
                    ┌──────────────┐
  client ──POST──►  │   FastAPI    │ ──INSERT + COMMIT──►  ┌────────────┐
                    │  (N x 2 proc)│                       │ PostgreSQL │
                    └──────┬───────┘ ◄─────────────────────│  (record)  │
                           │                               └─────▲──────┘
                    XADD (best effort,                            │
                     allowed to fail)                             │
                           ▼                                      │
                    ┌──────────────┐    XREADGROUP      ┌─────────┴─────┐
                    │ Redis Stream │ ──────────────────►│  Worker x N   │
                    │ + consumer   │ ◄────XACK──────────│ CAS + money   │
                    │    group     │                    └───────────────┘
                    └──────▲───────┘
                           │  re-dispatch
                    ┌──────┴───────┐   scan for due work   ┌────────────┐
                    │   Sweeper    │ ─────────────────────►│ PostgreSQL │
                    └──────────────┘                       └────────────┘
```

| Component | Responsibility | Never does |
| --- | --- | --- |
| **API** | validate, write, commit, answer fast | decide anything requiring balance state |
| **PostgreSQL** | hold the truth; arbitrate every race | get bypassed by application-level checking |
| **Redis Streams** | carry pointers to work; make redelivery visible | act as a durable store |
| **Worker** (N) | move money inside one database transaction | hold state in memory between messages |
| **Sweeper** | re-dispatch work the nudge failed to deliver | move money itself |

### The durable-accept boundary

The most important line in the system runs through the API's `COMMIT`. Before it,
nothing is promised and any failure is the client's to retry. After it, the work
will happen even if every process dies.

This is the **transactional outbox** pattern. Its defining property is that the
queue is not the record — the database is, and Redis is a latency optimisation
over work that is already durable.

**Redis decides *when* a worker is woken. It never decides *whether* work
happens.** Every correctness question is answered inside a PostgreSQL transaction,
which is why Redis can be stopped, restarted or emptied without risking any
guarantee.

Two independent writes (PostgreSQL then Redis) cannot be made atomic. That gap is
not eliminated; it is **chosen**. The design fails on the side of *accepted but not
yet dispatched*, because that state is recoverable by scanning PostgreSQL. The
opposite ordering would produce queue messages for records that do not exist,
which is not recoverable.

---

## Assumptions

- **One PostgreSQL primary.** All correctness rests on a single node arbitrating
  races. Multi-region writes would invalidate most of this design.
- **Clients may retry anything, at any time, with the same `transaction_id`.** A
  client that reuses an id for different content is submitting a duplicate, not a
  new transaction; the first accepted version wins.
- **Reversal identity is derived, not supplied.** `POST /transactions/{id}/reverse`
  produces `REV-{id}`. A caller cannot mint two reversals for one transaction.
- **Amounts arrive as JSON strings.** JSON numbers are IEEE 754 doubles in most
  parsers, so `"250.75"` is quoted in both directions.
- **One currency per account, no conversion.** A currency mismatch is an error,
  never an implicit conversion.
- **Accounts are never deleted**, so a foreign key to `accounts` cannot dangle.
- **`POST /accounts` exists** although it is not in the required API list, because
  §6's test requires creating an account with a balance.

---

## Consistency guarantees

Six invariants. Every mechanism in the system exists to hold one of them.

| # | Guarantee | Enforced by |
| --- | --- | --- |
| I1 | A logical transaction's financial effect occurs **exactly once**, however many times it is submitted or delivered | `PRIMARY KEY (transaction_id)` at accept; compare-and-set on `status` at apply |
| I2 | Concurrent debits never lose an update and never drive a balance below zero | row lock on `accounts`; `WHERE balance >= amount`; `CHECK (balance >= 0)` |
| I3 | A `202` response means the request is **durably stored** and will eventually be processed | the PostgreSQL commit *is* the acceptance; nothing is promised before it |
| I4 | A worker may die at any instant without duplicating or losing a financial effect | claim, money movement and finalisation in one database transaction; queue acknowledgement strictly after commit |
| I5 | Originals and reversals converge to the correct net effect regardless of arrival order or duplication | derived `REV-` identity; compensating entries; park-and-retry |
| I6 | No monetary value is represented in binary floating point | `NUMERIC(20,4)` in PostgreSQL, `Decimal` in Python, strings on the wire |

Two are worth stating negatively, because that is how they are usually violated.

**I1 is not exactly-once *delivery*.** Exactly-once delivery is impossible across a
network. The guarantee is exactly-once *effect* under at-least-once delivery.

**I3 is a promise, not a status code.** Returning `202` and then losing the request
is the worst available failure, because nobody learns anything is wrong. A `503` is
preferable to a false `202` in every case.

### The ledger invariant

Because every movement of money is its own row, a balance is reconstructible:

```sql
SELECT COALESCE(SUM(CASE WHEN type = 'CREDIT' THEN amount ELSE -amount END), 0)
  FROM transactions
 WHERE account_id = $1 AND status IN ('COMPLETED', 'REVERSED');
```

`COMPLETED` and `REVERSED` both count — both mean the money moved; `REVERSED` says
a compensating row exists elsewhere, not that the original never happened.

**This must always equal `accounts.balance`.** It is exposed as
`txnsvc_balance_mismatches` in `/metrics` and asserted once after the whole test
suite. It found a real defect on its first scrape: opening balances were being
written straight into `accounts` with no corresponding entry, so seven accounts
held money the ledger could not explain. An opening balance is now a genesis
`CREDIT` transaction, `OPEN-{account_id}`, written in the same database
transaction as the account.

---

## Idempotency strategy

Duplicates arrive from **two independent directions**, and one guard cannot cover
both.

| Source | Example | Defence |
| --- | --- | --- |
| **Submission** | the client retries `POST /transactions` with the same id | `PRIMARY KEY (transaction_id)` with `INSERT ... ON CONFLICT DO NOTHING` |
| **Delivery** | the request was accepted once, but the queue message is delivered twice | compare-and-set on `status` inside the apply transaction |

### At the front door

```sql
INSERT INTO transactions (...) VALUES (...)
ON CONFLICT (transaction_id) DO NOTHING
RETURNING ...;
```

A check followed by an insert has a gap between them, and a thousand concurrent
requests live in that gap. No amount of careful Python closes it, because the
requests run in different processes on different machines. `ON CONFLICT` removes
the gap by making the check and the write one operation: PostgreSQL locks the index
entry, the second insert waits for the first to commit, then matches nothing.

`RETURNING` yields no row on conflict, which is how "created" is determined — so
the happy path is a single query. A duplicate returns `200` with the existing
state rather than `409`, so a retrying client converges on the truth instead of
receiving a failure it will retry again.

**This is an optimisation and a UX guarantee, not the safety guarantee.** It fires
once at accept time and has no opinion about how many times the message is
delivered afterwards.

### At the apply step

```sql
UPDATE transactions SET status = 'PROCESSING'
 WHERE transaction_id = $1 AND status = 'PENDING'
RETURNING account_id, type, amount;
```

Zero rows returned means another worker already has it, or it is already finished.
The worker logs a no-op and acknowledges. Reading the status into Python and then
deciding would reintroduce the same race in new clothing — two workers both read
`PENDING` before either writes.

**This is the safety guarantee.** At-least-once delivery is guaranteed by the
design itself: the sweeper that makes §10 work is also a duplicate generator.

Verified by `tests/test_concurrency.py` (1,000 concurrent identical submissions →
exactly one `202`, one row, one balance movement) and
`scripts/multi_instance_hammer.py` (the same across three API containers, six
independent processes).

---

## Concurrency strategy

No application-level locks anywhere. Every race is arbitrated by PostgreSQL.

**Balance movement:**

```sql
UPDATE accounts SET balance = balance - $2, updated_at = now()
 WHERE id = $1 AND balance >= $2
RETURNING balance;
```

PostgreSQL takes a row lock for the duration of each update, so concurrent debits
on one account serialise and each reads a balance that includes every commit
before it. Zero rows returned means the funds were not there.

Note that atomic arithmetic alone is *not* enough. `balance = balance - amount` is
immune to lost updates, and it does nothing about a transaction being applied
twice — that is a different problem, solved by the compare-and-set above.

**Sweeper claiming**, safe with several sweepers running:

```sql
UPDATE transactions SET next_attempt_at = now() + make_interval(secs => $2)
 WHERE transaction_id IN (
     SELECT transaction_id FROM transactions
      WHERE status = 'PENDING' AND next_attempt_at <= now()
      ORDER BY next_attempt_at LIMIT $1
      FOR UPDATE SKIP LOCKED
 ) RETURNING transaction_id;
```

`FOR UPDATE SKIP LOCKED` makes concurrent sweepers take disjoint batches without
blocking. Selecting and rescheduling must be one atomic statement, or the same row
is re-dispatched on every pass while a worker is still processing it — `PROCESSING`
is invisible outside the worker's transaction, so a mid-flight row still reads
`PENDING`.

Verified by `tests/test_concurrency.py`: an account with SAR 10,000 and 100
simultaneous debits of SAR 100 settles to exactly `0.0000`, all `COMPLETED`, and
the next debit is `REJECTED`.

---

## Transaction boundaries

| Path | Boundary | Why |
| --- | --- | --- |
| **Accept** | one statement, no explicit transaction | a single `INSERT` is atomic by itself, and *is* the durable-accept boundary. An explicit transaction would cost two round trips on the hottest path for nothing. |
| **Account creation** | one transaction: `accounts` row + genesis credit | an account can never exist holding money the ledger cannot explain |
| **Apply** | one transaction: claim → move money → finalise | the whole reason crash recovery is free |
| **Reversal apply** | one transaction: claim reversal → lock original → **move money** → mark original `REVERSED` → settle reversal | once `REV-{id}` is `COMPLETED`, the original is necessarily `REVERSED`, with no window between them. The order inside the transaction matters: the compensation is applied *before* the status changes, so a reversal that cannot complete leaves the original `COMPLETED`. A row marked `REVERSED` must mean the money actually came back. |
| **Sweeper** | one statement per batch | select-and-reschedule cannot be split |

The apply transaction is the whole correctness argument, and it is about twenty
lines (`app/worker/apply.py`):

```
BEGIN
  claim:     UPDATE transactions SET status='PROCESSING'
              WHERE transaction_id=$1 AND status='PENDING' RETURNING ...
             -- 0 rows -> already handled; commit, acknowledge, leave
  money:     UPDATE accounts SET balance = balance ± amount
              WHERE id=... AND balance >= amount   (debits)
  finalise:  status = COMPLETED | REJECTED
COMMIT
             then acknowledge the queue — always last
```

If the worker is killed anywhere inside that block, PostgreSQL rolls back the
claim and the balance change together. There is no half-applied state, ever.
Either the transaction committed — and a redelivery finds `COMPLETED` and no-ops —
or it did not, and a redelivery does the work properly. There is no third case.

**Acknowledge last.** If the acknowledgement is lost, the entry is redelivered and
the redelivery is harmless. The reverse ordering loses work silently.

---

## Retry and failure behaviour

### The failure model

| What fails | Behaviour | Guarantee held |
| --- | --- | --- |
| API dies mid-request | client retries; the retry hits the primary key and returns the existing state | I1, I3 |
| PostgreSQL unavailable during accept | `503` with `Retry-After`; **no `202`**, and no row written | I3 |
| PostgreSQL unavailable during apply | the transaction never commits; the entry stays pending and is redelivered | I4 |
| Redis unavailable during accept | the commit already happened, so the `202` stands; the nudge fails silently and the sweeper dispatches when Redis returns | I3 |
| Redis loses recent entries | rows still sit in PostgreSQL as `PENDING`; the sweeper re-dispatches | I3 |
| Worker killed after commit, before acknowledgement | the entry remains in the Pending Entries List; another worker claims it after an idle timeout; the CAS matches zero rows | I1, I4 |
| Worker killed mid-transaction | PostgreSQL rolls back claim and money together | I4 |
| Same message delivered twice | both workers attempt the CAS; the row lock serialises them; exactly one matches | I1 |
| Reversal arrives before its original | the reversal parks and retries until the original completes | I5 |
| Original never arrives | the reversal exhausts a 24-hour horizon and moves to `DEAD_LETTER`, visible in `/metrics` | I5 |

### Retry classification

Three situations reach a worker again, bounded by different things. Counting them
with one counter is the common mistake — a reversal parked for ten seconds at a
250 ms interval would burn 40 attempts and dead-letter a healthy row.

| Situation | Failure? | Bound | Column |
| --- | --- | --- | --- |
| Reversal parked, original not yet arrived | no — expected | 24 hours elapsed | `parked_since` |
| Transient error (connection lost, deadlock, serialisation failure) | yes, recoverable | 10 attempts, exponential backoff | `failed_attempts`, `next_attempt_at` |
| Permanent error (constraint violation, malformed row) | yes, unrecoverable | none — `DEAD_LETTER` on the first attempt | `last_error` |

With exponential backoff of 1, 2, 4 … 512 seconds, ten attempts span about
**seventeen minutes**. An outage longer than that is an incident, and
dead-lettering with a metric is the correct response. This also dissolves the
apparent choice between capping by count and capping by time: with backoff, an
attempt cap *is* a time cap.

### Health states

Three states, because they demand different operator responses.

| State | Meaning | HTTP |
| --- | --- | --- |
| `healthy` | PostgreSQL and Redis both reachable | 200 |
| `degraded` | PostgreSQL reachable, Redis not — accepting work, dispatch delayed | 200 |
| `unhealthy` | PostgreSQL unreachable — not accepting work | 503 |

`db.connect()` deliberately never raises. A process that refuses to start when its
database is down cannot report that its database is down.

Every health probe is wrapped in a 2-second timeout. An unbounded health check can
hang, and a hung check is worse than a failing one because it reports nothing. That
timeout is measured: under `SIGKILL` of PostgreSQL, `/health` returns `503` in
about two seconds instead of waiting on TCP retransmission.

---

## Data model

Three tables. Reversals live in `transactions` alongside originals, so one
compare-and-set pattern, one state machine and one endpoint serve both.

```sql
CREATE TABLE currencies (
    code        CHAR(3) PRIMARY KEY CHECK (code ~ '^[A-Z]{3}$'),
    minor_units SMALLINT NOT NULL CHECK (minor_units BETWEEN 0 AND 4)
);

CREATE TABLE accounts (
    id         BIGSERIAL PRIMARY KEY,
    currency   CHAR(3) NOT NULL REFERENCES currencies(code),
    balance    NUMERIC(20,4) NOT NULL DEFAULT 0 CHECK (balance >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE transactions (
    transaction_id  TEXT PRIMARY KEY,
    kind            tx_kind   NOT NULL DEFAULT 'TRANSACTION',   -- TRANSACTION | REVERSAL
    reverses_id     TEXT,                                       -- no FK; see below
    type            tx_type,                          -- DEBIT | CREDIT; NULL until
                                                      -- a reversal settles (D12)
    status          tx_status NOT NULL DEFAULT 'PENDING',

    reverses_type   tx_type,                          -- the direction a reversal
                                                      -- opposed; must differ (D12)
    account_id      BIGINT REFERENCES accounts(id),   -- NULL on a parked reversal
    amount          NUMERIC(20,4) CHECK (amount > 0), -- NULL on a parked reversal
    currency        CHAR(3) REFERENCES currencies(code),
    device_id       TEXT,
    client_ts       TIMESTAMPTZ,

    failed_attempts INT NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    parked_since    TIMESTAMPTZ,
    last_error      TEXT,
    accepted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at    TIMESTAMPTZ,
    ...
);
```

### Money

`NUMERIC(20,4)` in PostgreSQL, `Decimal` in Python, quoted strings in JSON.

Binary floating point cannot represent `0.1`, so error accumulates: subtracting
`0.1` from `10000.0` a thousand times in Python yields `9899.999999999636`. The
same loop in `Decimal` yields exactly `9900.00`.

`REAL` and `DOUBLE PRECISION` are the same disease made durable. PostgreSQL's
`MONEY` type is rejected because its output format depends on the server's
`lc_monetary` locale and its scale is fixed at database configuration time.

Scale 4 rather than 2 gives headroom for three-decimal currencies — KWD, BHD, OMR
— at no cost; the currency's real scale is enforced at the API boundary against
`currencies.minor_units`.

The system only ever **adds and subtracts amounts that arrived already valid**.
There is no multiplication or division anywhere, so no rounding policy is needed
and none is defined.

### Direction lives in `type`, never in the sign

Amounts are always positive. Signed amounts would let a `DEBIT` of `-250.75` act
as a credit, make `CHECK (amount > 0)` impossible, and force every aggregate query
to know the sign convention to be correct.

### Why a reversal row is thin, and why `reverses_id` has no foreign key

A reversal that arrives before its original cannot name an account or an amount:
`POST /transactions/{id}/reverse` carries no body, and the original does not exist
to copy from. A foreign key on `reverses_id` would reject the insert outright, and
`NOT NULL` on `account_id` or `amount` would too — §8's first message would fail at
the front door.

So a reversal has no money of its own. Its amount *is* the original's amount and
its account *is* the original's account, inherited when it applies rather than
asserted when it is accepted. Two `CHECK` constraints keep that safe, and
referential integrity comes from the state machine instead: nothing can be marked
`REVERSED` without a compare-and-set finding a real `COMPLETED` row.

### Transaction state machine

```
ORIGINAL (kind = 'TRANSACTION')
   PENDING ──claim──► PROCESSING ──┬──► COMPLETED ──reversal──► REVERSED
                                   └──► REJECTED  (insufficient funds)

REVERSAL (kind = 'REVERSAL')
   PENDING ──claim──► PROCESSING ──┬──► COMPLETED       (credit applied)
      ▲                            ├──► NOT_APPLICABLE  (original was REJECTED)
      └──── park (original not COMPLETED yet)
                                   └──► DEAD_LETTER     (horizon exhausted)
```

Three rules:

- **`REVERSED` is reachable only from `COMPLETED`.** Money can only be returned if
  it was taken.
- **A reversal of a `REJECTED` original terminates as `NOT_APPLICABLE`.** Nothing
  moved, so there is nothing to give back. Miss this and the reversal parks
  forever and dead-letters, which looks like a bug.
- **A reversal cannot be reversed.** `POST /transactions/REV-X/reverse` → `400`.
- **A reversal compensates in the opposite direction, derived from the original.**
  Reversing a `DEBIT` credits the account; reversing a `CREDIT` debits it. The
  direction is read from the original at settle time, never assumed, and the
  reversal row records both its own direction and the one it opposed so that
  `CHECK (type <> reverses_type)` can enforce it. A reversal is therefore the one
  row whose `type` is `NULL` while it waits — §8 requires accepting a reversal
  before its original exists, so the direction is genuinely unknown until then.
- **A compensating debit is guarded like any other debit.** If the credited money
  has since been spent, the reversal parks and retries rather than overdrawing the
  account, and dead-letters at the horizon with `REVERSAL_FUNDS_UNAVAILABLE`.
  Invariant I2 outranks the reversal: breaking "no negative balance" to satisfy a
  reversal would trade the more fundamental promise for the less.
  Otherwise the system produces `REV-REV-X` and an infinite regress with real
  money in it.

`PROCESSING` is written and replaced inside one transaction, so no other session
ever observes it. It is retained because it makes the worker's intent explicit in
the SQL, and because splitting claim from apply later would make it visible without
a schema change.

One row per **logical operation**, with `status` mutated in place; a reversal is a
separate operation and gets its own row. So a reversed debit is two rows and a
declined debit is one. **Financial history is append-only** — every movement has a
permanent row. **Processing history is not retained** — the path through
`PENDING → PROCESSING → COMPLETED → REVERSED` is overwritten, leaving
`accepted_at`, `processed_at`, `failed_attempts` and `last_error`.

---

## Constraints and indexes

Every constraint is load-bearing. A rule the database enforces cannot be bypassed
by a code path that forgot about it — all of these are tested by attacking the
database directly, with no API involved (`tests/test_constraints.py`).

| Constraint | Prevents | Invariant |
| --- | --- | --- |
| `transactions.transaction_id` PRIMARY KEY | duplicate submission creating a second row | I1 |
| `accounts.balance CHECK (>= 0)` | negative money, even if application logic fails | I2 |
| `transactions.amount CHECK (> 0)` | sign smuggling; keeps direction in `type` | I6 |
| `accounts.currency` FK → `currencies` | unknown or mistyped currency codes | — |
| `transactions.account_id` FK → `accounts` | transactions against accounts that do not exist | — |
| `original_is_complete` | an ordinary transaction accepted with no account or amount | I3 |
| `settled_is_complete` | any row reaching `COMPLETED`/`REVERSED` without knowing what it settled | I1, I5 |
| `reversal_has_target` | a reversal with no target, or an original carrying one | I5 |
| `reversal_compensates` | a settled reversal that moves money the **same** way as the transaction it reverses — which would apply that transaction's effect twice, creating money. Replaced `reversal_only_credits`, which required every reversal to be a `CREDIT` and therefore *mandated* the bug. See `docs/DESIGN.md` **D12**. | I1, I5 |
| `reverses_type_only_on_reversals` | an ordinary transaction claiming to oppose a direction | I5 |

```sql
-- Available balance: a few page reads instead of a table scan.
CREATE INDEX idx_tx_pending_debits_by_account
    ON transactions (account_id) INCLUDE (amount)
 WHERE status = 'PENDING' AND type = 'DEBIT';

-- The sweeper's scan: work that is due, in due order.
CREATE INDEX idx_tx_due
    ON transactions (next_attempt_at) WHERE status = 'PENDING';

-- GET /accounts/{id}/transactions, newest first.
CREATE INDEX idx_tx_by_account_time
    ON transactions (account_id, accepted_at DESC);

-- Finding a parked reversal by its target.
CREATE INDEX idx_tx_reversals_by_target
    ON transactions (reverses_id) WHERE kind = 'REVERSAL' AND status = 'PENDING';
```

**Why the first two are partial.** `PENDING` rows are a small, constantly draining
slice — a few thousand at most even when `transactions` holds tens of millions. A
partial index covers only rows matching its predicate, so it stays small enough to
remain resident in memory and does not grow with history. The `INCLUDE (amount)`
makes the available-balance query an index-only scan: PostgreSQL sums the pending
amounts without visiting the table.

This matters more than it looks. Without `idx_tx_pending_debits_by_account` the
available-balance query is a sequential scan and the service collapses under load.
With it, the query is near-free. The same design is excellent or unusable
depending on one index.

---

## API

| Method | Path | Success | Notes |
| --- | --- | --- | --- |
| POST | `/transactions` | `202` new, `200` duplicate | `202` means durably accepted, not applied |
| GET | `/transactions/{transaction_id}` | `200` | how a client learns an outcome |
| POST | `/transactions/{transaction_id}/reverse` | `202` new, `200` duplicate | `400` if the target is itself a reversal |
| GET | `/accounts/{account_id}` | `200` | returns `settled_balance` and `available_balance` |
| GET | `/accounts/{account_id}/transactions` | `200` | paginated (`limit` ≤ 200), newest first |
| POST | `/accounts` | `201` | addition, required by §6's test |
| GET | `/health` | `200` / `503` | `200` when healthy or degraded |
| GET | `/metrics` | `200` | Prometheus text format |

```json
{
  "transaction_id": "TX-938283",
  "device_id": "DEV-22",
  "account_id": 981,
  "type": "DEBIT",
  "amount": "250.75",
  "currency": "SAR",
  "timestamp": "2026-09-20T10:31:44Z"
}
```

One error shape everywhere, so clients need one parser:

```json
{"error": {"code": "CURRENCY_MISMATCH", "message": "...", "details": {}}}
```

| Code | Status |
| --- | --- |
| `VALIDATION_FAILED` | 422 |
| `UNKNOWN_CURRENCY` | 422 |
| `CURRENCY_MISMATCH` | 422 |
| `ACCOUNT_NOT_FOUND` | 404 |
| `TRANSACTION_NOT_FOUND` | 404 |
| `NOT_REVERSIBLE` | 400 |
| `SERVICE_UNAVAILABLE` | 503 (with `Retry-After`) |

### Insufficient funds is not in that table

`POST /transactions` never returns "insufficient funds". The API does not read
balances, so at accept time it cannot know. The client receives `202`, and
`GET /transactions/{id}` later shows `REJECTED` with reason `INSUFFICIENT_FUNDS`.

Answering synchronously would require reserving funds during accept — a balance
read and decrement inside the request path. That moves the core business decision
into the synchronous path of a system whose premise is asynchronous processing, and
puts a row lock on `accounts` in the hot path. **Insufficient funds is a
processing outcome, not a validation error**, and the API surface reflects that
rather than hiding it.

### Available balance

`accounts` stores `balance` only. `available_balance` is derived at read time as
`balance − SUM(pending debits)`, served by the partial index.

A stored `pending_outflow` counter was rejected: it is a second copy of a fact
that already exists, every state transition must maintain it or it drifts silently,
and it puts a row lock on `accounts` in the accept path — serialising the hot path
the load test measures. **Every stored aggregate is a consistency obligation.** A
derived value can be slow; it cannot be wrong.

A parked reversal whose original has not arrived does not appear in the account
listing, because its `account_id` is still `NULL` — the system genuinely does not
yet know which account it belongs to. It becomes listable the moment it applies.

---

## Observability

`GET /metrics` reports **state derived from PostgreSQL and Redis at scrape time**,
not in-process counters. Counters would live in whichever process incremented
them: the worker's count is invisible to the API serving `/metrics`, and with
three API instances and three workers you would have six partial pictures and no
way to reconcile them.

| Metric | Meaning |
| --- | --- |
| `txnsvc_transactions{kind,status}` | rows by kind and status |
| `txnsvc_oldest_pending_age_seconds` | age of the oldest `PENDING` transaction |
| `txnsvc_balance_mismatches` | **accounts whose balance disagrees with their ledger — must always be 0** |
| `txnsvc_stream_length` | Redis stream log length |
| `txnsvc_stream_pending_entries` | delivered but unacknowledged |
| `txnsvc_up{component}` | PostgreSQL and Redis reachability |

`txnsvc_balance_mismatches` is the one to watch. If the apply path ever
double-credits or loses an update, that gauge moves without anyone having to notice
a wrong number on a statement.

---

## Testing

See **Getting started → step 4** for setup. In short:

```bash
docker compose up --build -d                     # the stack
pip install -r requirements.txt -r requirements-dev.txt
pytest -v                                        # everything
pytest -v -m "not slow"                          # skips the container-killing test
docker compose --profile test run --rm tests      # or: no host Python at all
```

Run from the project root. Tests execute over real HTTP against the running stack —
an in-process test client shares a process, and sharing a process is precisely the
assumption this design refuses to make.

| §12 requirement | Test |
| --- | --- |
| normal debit / credit | `test_transactions.py::test_debit_applies`, `::test_credit_applies` |
| insufficient funds | `::test_insufficient_funds_is_rejected` |
| duplicate submission | `::test_duplicate_submission_returns_existing` |
| concurrent debits | `test_concurrency.py::test_hundred_concurrent_debits_settle_to_zero` |
| concurrent duplicate transactions | `::test_thousand_concurrent_duplicates_apply_once` |
| reversal | `test_reversals.py::test_reversal_restores_the_balance` |
| duplicate reversal | `::test_duplicate_reversal_credits_once` |
| reversing a credit debits (D12 regression) | `::test_reversing_a_credit_debits_and_creates_no_money` |
| a reversal never overdraws | `::test_reversing_a_credit_waits_rather_than_overdrawing` |
| out-of-order events | `::test_out_of_order_converges_to_zero_effect` |
| worker retry / recovery | `test_recovery.py::test_redelivered_entry_is_a_noop`, `::test_worker_killed_mid_flight_loses_nothing` |
| database constraints | `test_constraints.py` (10 tests, direct SQL) |

Three properties of these tests worth naming:

- **Every test gets its own account.** Shared fixtures across concurrency tests
  produce failures that depend on execution order and vanish when you look at them.
- **Assertions read the database, not only the API.** The service under test cannot
  be the sole witness.
- **The ledger invariant is asserted once after the entire suite.** Every test
  contributes; a test that double-credits and then asserts the wrong expected value
  would pass on its own and fail there.

### Reproducing the failure demonstrations

```bash
python scripts/duplicate_hammer.py        # §5  1000 concurrent duplicates
python scripts/multi_instance_hammer.py   # §5  the same across 3 API containers
python scripts/concurrency_check.py       # §6  100 concurrent debits → 0.0000
python scripts/out_of_order_check.py      # §8  the five-message sequence → net 0
python scripts/crash_check.py setup       # §7  see below
python scripts/redis_outage_check.py      # §10 Redis stopped and restored
python scripts/postgres_outage_check.py   # §10 no false acceptance
```

The §7 crash demonstration is deliberately manual and inspectable, because a
reviewer will want to watch it:

```bash
docker compose up -d --scale worker=0
python scripts/crash_check.py setup
docker compose run --rm -e CRASH_AFTER_COMMIT=true -e WORKER_NAME=crashy worker
# claims, commits, logs the crash, exits 1 — before acknowledging
python scripts/crash_check.py verify <tx> <account>   # COMPLETED, balance moved once
docker compose exec redis redis-cli XPENDING transactions processors - + 10
# the entry, owned by 'crashy', with a climbing idle time
docker compose up -d --scale worker=2
# XAUTOCLAIM hands it to a live worker, which logs 'no-op (already handled)'
```

`CRASH_AFTER_COMMIT` calls `os._exit(1)` immediately after `COMMIT` returns — no
cleanup handlers, no graceful shutdown, no chance for the acknowledgement to sneak
through. It is safe by default at three layers: `False` in `config.py`,
`${CRASH_AFTER_COMMIT:-false}` in Compose, and absent from `.env`.

---

## Benchmark results

60,000 transactions per run, three API containers (two uvicorn processes each),
three workers, one sweeper, on a single Docker Desktop host. **The load generator
itself consumed four of roughly nine committed cores**, so these figures are a
floor, not a ceiling.

```bash
docker compose --profile loadtest build
docker compose --profile loadtest run --rm loadgen \
  python -m loadtest.run --rate 1000 --duration 60 --accounts 200 --processes 4
```

| | 200 accounts | 1 account | Δ |
| --- | --- | --- | --- |
| accept throughput | **516.0 rps** | 368.1 rps | −29% |
| accept latency P50 | 219 ms | 325 ms | +48% |
| accept latency P95 | 1209 ms | 1649 ms | +36% |
| accept latency P99 | 1983 ms | 2701 ms | +36% |
| processing delay P50 | 47.6 s | 77.6 s | +63% |
| processing delay P99 | 60.8 s | 96.6 s | +59% |
| backlog peak | 27,165 | 38,275 | +41% |
| drain after submit | 45.6 s | 71.6 s | +57% |
| end-to-end rate | 370.6/s | 255.7/s | −31% |
| **error rate** | **0** | **0** | — |
| **balance mismatches** | **0** | **0** | — |

*Processing delay* is `processed_at − accepted_at`, computed by PostgreSQL over the
rows each run created — a domain measurement that a generic HTTP load tool cannot
produce, and the reason the harness is hand-written.

**First bottleneck: PostgreSQL CPU**, measured at 193% (≈ 2 cores) and the largest
single consumer in the stack. Every accept commits a row and every apply commits
three statements, each costing a WAL fsync.

**Second, quantified: per-account row-lock serialisation.** Concentrating all
traffic on one account costs 31% of end-to-end throughput and 63% more processing
delay. That serialisation *is* invariant I2 — a cost of correctness, not a defect.

**Third, quantified: table growth.** At fixed cardinality, 20,000 transactions
sustained 920 rps against 368 rps for 60,000 — a 60% loss as the table and its four
indexes outgrew the buffer cache. The headline 516 rps is therefore an average over
a run during which throughput was falling, not a steady state.

**Correctness never degraded.** Zero errors and zero ledger mismatches across every
run, including a 38,000-transaction backlog under deep overload, which drained
completely in 45.6 seconds once load stopped. Under load the system traded latency,
never correctness.

### Method note

The first attempt at this comparison varied account cardinality **and** total
volume at once, and reported the single-account profile as *faster* — the opposite
of the truth. Re-running both at identical volume from a freshly initialised
database separated the effects. A benchmark with two independent variables measures
neither.

The load generator also had to be rewritten as a multi-process tool. A single
Python process running httpx saturates one core at roughly 200 rps, which was the
apparent ceiling for two runs until `docker stats` showed the generator at 97.7%
CPU while the API containers idled at 16%. The instrument has to be scaled before
the subject can be.

---

## Known limitations

- **PostgreSQL CPU is the throughput ceiling** on a single host, at roughly 516
  accepts/second with the generator co-resident.
- **Per-account throughput is bounded by one row lock.** Costs 31% of end-to-end
  throughput when all traffic targets one account. This is I2 working as intended,
  not a defect, but it caps per-account rate.
- **Throughput degrades as `transactions` grows** — 60% between 20,000 and 60,000
  rows at fixed cardinality, as the working set outgrows the buffer cache.
- **A transient reversal window.** When a reversal arrives before its original, the
  settled balance reads low for up to one parked-sweeper interval (250 ms). The
  value is accurate — the debit really did apply — but a reviewer querying inside
  that window may read it as a failure. Closing it entirely is possible by having
  the debit's worker settle a parked reversal in its own transaction; rejected
  because the two workers would then take the same two row locks in opposite
  orders, creating a deadlock hazard.
- **No processing history.** `status` is overwritten at each transition, so the
  path a transaction took is not recoverable — only where it ended, plus four
  timestamps and counters. The financial trail is unaffected.
- **Dead-lettered work needs a human.** `DEAD_LETTER` rows are counted in
  `/metrics`; there is no automated remediation and no replay endpoint.
- **`txnsvc_balance_mismatches` is O(n)** over `transactions`. Fine at this scale;
  production would sample accounts or run it on a schedule rather than per scrape.
- **No authentication or authorisation.** Any caller can move money on any
  account. Out of scope, and the first thing to add.
- **The Compose project name is fixed** (`name: txnsvc`), so the stack's identity is
  the project name rather than the working directory. Two clones on one machine share
  containers and volumes, which is how you end up authenticating against a database
  initialised by a different `.env` — see the `InvalidPasswordError` note in
  **Getting started**. It is a deliberate trade for stable container names, which the
  `scripts/` and the log and crash instructions depend on. Removing it would mean
  every command that names `txnsvc-postgres` has to discover the name first.
- **A pending compensating debit is not reserved.** A reversal waiting to claw
  back a credit has no `account_id` until it settles, so `available_balance` does
  not subtract it. The same thin-row property (D9) is why a reversal accepted
  before its original cannot be authorised against an account at all — which is
  why "add authentication" is not on its own a sufficient answer there.
- **Single-region, single-writer.** All correctness rests on one PostgreSQL primary.
- **The sweeper polls.** `LISTEN`/`NOTIFY` would make the common case
  event-driven, with polling retained as the fallback. The partial index makes the
  scan cheap enough at this scale.

### Still genuinely open

- Redis Streams has no visibility-extension call. If a legitimate apply ever
  exceeded `min_idle_time`, the worker would have to `XCLAIM` its own entry to reset
  the timer. Measured apply latency is 3–16 ms, so this is far off, but it is not
  impossible.
- The 24-hour parking horizon for an orphaned reversal is a guess. Nothing in the
  requirement says how long an original may lag its reversal.

---

## What I would improve next

Each item is pointed at by a measurement, not a guess.

1. **Halve the accept path's database round trips.** It currently does a `SELECT`
   to validate the account and currency, then an `INSERT`. Fold them into one
   statement with a CTE. Directly targets the 516 rps ceiling.
2. **Partition `transactions` by time, or archive settled rows.** Keeps the hot
   working set small. Directly targets the 60% throughput loss from table growth.
3. **Shard workers by account, and batch applies across different accounts** in one
   database transaction to amortise the WAL fsync. Targets both the PostgreSQL CPU
   bound and the per-account serialisation cost. Batching couples the fate of the
   transactions in a batch, so it needs care.
4. **Move the load generator off the host.** The measurement itself is the first
   thing limiting what can be learned.
5. **`LISTEN`/`NOTIFY` for the sweeper**, so the common case is event-driven.

One thing deliberately **not** on this list: relaxing `synchronous_commit`. It
would buy throughput by making committed transactions losable on a crash, which
trades away invariant I3. For a financial ledger that is not a trade worth making,
at any price.
