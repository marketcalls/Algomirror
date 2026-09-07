# Order Lifecycle

One instruction from you becomes one **parent order** and one **split per
account**. The parent records what you asked for; each split records what
happened at one broker.

## Placement, step by step — BUILT

1. **Validation.** Symbol, exchange, side, quantity and price are checked. An
   invalid instruction is refused before anything is written.
2. **Accounts loaded.** Only accounts you own and that are active. An unknown or
   inactive account aborts the whole instruction before any row exists.
3. **The split plan.** Each account's quantity is computed from the allocation
   ratio, taken across the *participating* accounts only — not across every
   active account. You may override any account's quantity by hand; both numbers
   are kept, the ratio's answer and the one actually sent.
4. **Funds check.** For a buy, each account's cash is checked. An account that
   cannot afford its share is handled by the insufficient-funds policy: `SKIP`
   (the default) sends the others, `ABORT` sends nothing.
5. **The parent order row is written and committed.**
6. **Every split row is written and committed.**
7. **Only then is the broker called**, one thread per account, in parallel.
8. Results are written back on the main thread and the parent status is
   recomputed from its splits.

**The rule that matters: nothing reaches a broker before the database knows the
order exists.** If the application dies between the commit and the broker call,
you have an order row with no broker order id — recoverable. If it were the
other way round you would have money at a broker and no record of it.

Product is always CNC. There is no product parameter anywhere in the equity
order engine.

## Order statuses — BUILT

The parent status is rolled up from its splits and is never set directly.

| Status | Meaning | Terminal |
|---|---|---|
| `PENDING` | Every account still open, none has diverged | No |
| `PARTIAL` | Accounts have diverged — some open or filled, some failed, skipped or cancelled | No |
| `COMPLETED` | Every account filled | Yes |
| `CANCELLED` | Nothing filled anywhere and every account cancelled or never sent | Yes |

`PARTIAL` on the Order Book does **not** mean a partial fill. It means the
accounts no longer agree. The short reason beside it (`1 failed`, and so on)
says which way.

## Split fill statuses — BUILT

| Status | Meaning | Terminal | Safe to resend |
|---|---|---|---|
| `PENDING` | Working at the broker | No | — |
| `PARTIAL` | Partly filled, still working | No | — |
| `COMPLETED` | Fully filled | Yes | No |
| `CANCELLED` | Cancelled | Yes | No |
| `FAILED` | Broker definitely refused | Yes | **Yes** |
| `REJECTED` | Broker explicitly rejected | Yes | **Yes** |
| `INDETERMINATE` | The broker never gave a usable answer | Yes | **Never** |
| `SKIPPED` | Failed the funds check, never sent | Yes | No |
| `UNSUPPORTED` | This broker has no GTT capability | Yes | No |

## INDETERMINATE — the most important state — BUILT

A split becomes `INDETERMINATE` when AlgoMirror **cannot know** whether the
order reached the broker:

- The call raised before returning anything.
- A timeout, a connection error, or an unparseable response.
- An HTTP 5xx, or a status code that cannot be read.
- A response that says *success* but carries **no order id** — because an order
  with no id cannot afterwards be tracked, modified or cancelled.

A definite refusal is different: an `api_error`, or an HTTP 4xx, means the
broker answered and said no. Those are `FAILED` or `REJECTED` and may be sent
again.

**`INDETERMINATE` is never retried automatically, ever.** The whole point of the
state is that a retry might place a second real order. It is resolved by the
reconciler finding the order at the broker, or by a person.

`attempt_count` records how many placement requests were actually sent. It is
evidence for reconciliation, not a retry gate — the status is the gate.

## Reconciliation — BUILT

Every 20 seconds, and on demand from **Check With Broker**, the reconciler reads
each account's order book and trade book from OpenAlgo and writes what it learns
back onto the splits.

**Direct match.** A split that already has a broker order id is looked up by
that id. Status, filled quantity and average price are **set** from the broker's
figures, never incremented.

**Adoption.** A split that is `INDETERMINATE` with no broker order id is matched
against the broker's book on five things at once: order id not already claimed
by another split on that account, same symbol, same exchange, same side, same
quantity, and placed close enough in time. **Adoption happens only when exactly
one candidate survives.** Zero is logged as a miss with the search criteria.
Two or more is recorded as ambiguous and neither is adopted — a wrong adoption
would attach your order to somebody else's fill.

**The timestamp problem.** An Indian broker returns IST with no timezone marker;
AlgoMirror stores UTC. Read one as the other and every comparison is out by five
and a half hours — which is how a match 64 seconds apart once measured as 19,864
seconds and failed. The window check therefore computes the gap **both ways** and
takes the smaller. The window is 15 minutes.

**Fill de-duplication.** OpenAlgo's trade book carries no trade id, so the same
fill returns on every poll. Each fill gets a fingerprint built from order id,
timestamp, quantity and price, and a unique index on split plus fingerprint stops
the same fill being booked twice. Known limitation, stated rather than hidden:
two genuinely separate fills identical in all four fields collapse into one row.

## What reconciliation does not do — GAP

The reconciler only ever explains orders **AlgoMirror placed**. It never creates
a row from a broker order it does not recognise. An order placed at your broker
terminal is invisible to it. See `06-EXTERNAL-BROKER-ACTIVITY.md`.

## Order timeouts — BUILT

The broker read timeout is a setting (Equity Settings → Order timeout), clamped
between 10 and 180 seconds. It exists because a sandbox reply once took 63
seconds against a 30-second timeout: the order succeeded and AlgoMirror reported
it failed. Raise it when the broker is slow; lower it before going live so a
genuinely dead call is not waited on for two minutes.
