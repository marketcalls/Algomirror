# Order Book and Trade Book

## How they are built — BUILT

**Today's rows merge the broker's live book with AlgoMirror's stored record.**
On every Order Book load, each account's order book and trade book are read
from OpenAlgo in parallel, and three kinds of row result:

| Kind | Shown as |
|---|---|
| Placed by AlgoMirror, confirmed by the broker | Normally |
| Reported by the broker, never placed here | **Outside AlgoMirror** badge; no trade nature, no split, no levels, because none were ever set |
| Placed by AlgoMirror, unknown to the broker | **NOT AT BROKER**, in red, outranking every stored status |

**Earlier days are not merged.** No broker serves last Tuesday's book, so an
explicit date range reads the stored record alone and does not pay for a broker
call that cannot answer.

**The Order Status list does not merge.** It is polled, and two broker calls per
account per poll is not a price worth paying there. The Order Book screen opts
in; the polled list stays cheap.

**An account that cannot be read is reported, never assumed empty.** This is the
rule the whole design turns on: an unreadable account and an account with
nothing in it look identical in a dictionary and mean opposite things. A stored
order is only ever marked *not at broker* when the account that would have it
actually answered. The response carries `unverified_accounts` so the screen can
say which accounts it could not check.

**Status is not rewritten here.** The background reconciler owns that, runs
every 20 seconds, and Check With Broker forces it. Duplicating its status
mapping on the read path would give two implementations to keep in step, and
the freshness gained is at most 20 seconds.

## How they were built before — the gap this closed

Both books read **only** AlgoMirror's own tables. Neither ever calls the broker.
They are as fresh as the last reconciler pass, which runs every 20 seconds, or
as fresh as the last press of **Check With Broker**.

- **Order Book** reads `equity_orders` joined to `equity_order_splits`. One row
  per parent order, showing how many accounts were selected, reached the broker
  and filled.
- **Trade Book** reads `equity_trades` joined back through splits to their parent
  order, so every fill carries its account, its trade nature and a link to the
  order that caused it.

**The default window is today**, plus any GTT placed earlier that is still
resting — an explicit date range switches that off, because an explicit question
deserves an exact answer.

"Today" is computed in UTC. That is safe rather than lucky: NSE hours of 9:15 to
15:30 IST fall between 03:45 and 10:00 UTC on the same calendar date, so a
trading day never straddles the boundary. An order placed between midnight and
5:30 a.m. IST would fall on the previous UTC date, but the market is shut then.

### The gap

Because the books read only AlgoMirror's tables:

- An order placed at your broker terminal **never appears**. F&O shows it; equity
  does not.
- An order AlgoMirror recorded that the broker never received keeps showing as
  though it were live, until reconciliation catches it.
- After an OpenAlgo sandbox reset, both books keep showing orders against a
  broker that has never heard of them.

## How they will be built — PLANNED

The design principle is in `01-ARCHITECTURE.md`: the broker owns what happened,
AlgoMirror owns what you intended. The books join the two rather than choosing
one.

### Today's rows

On every load, AlgoMirror calls OpenAlgo for each selected account in parallel,
exactly as F&O does. Each broker order is matched to an AlgoMirror split by
broker order id. Three kinds of row result:

| Kind | What is shown | Marked |
|---|---|---|
| Placed by AlgoMirror, confirmed by the broker | Full detail. Status, filled quantity and price come from the **broker**, never from the stored copy. Trade nature, the account split and the levels come from AlgoMirror. | — |
| Reported by the broker, not placed by AlgoMirror | Everything the broker gives. No trade nature, no split, no levels — none were ever set. | **Placed outside AlgoMirror** |
| Placed by AlgoMirror, unknown to the broker | The instruction as recorded, and the reason it went no further. | **Not at broker** |

The third row type is what today's UNCONFIRMED badge is reaching for. It covers
a failed placement, a skipped account and an indeterminate call, and it must
never be displayed as if the order were working.

### Earlier days

The broker cannot serve them. AlgoMirror's own store is the whole record and
stands as written, marked as such so nobody mistakes it for a broker statement.

### Reconciliation as a side effect

The broker's book is in hand at load time, so what it says is written back to
the splits. **Opening the Order Book reconciles it.** The background reconciler
stays for when no tab is open, and Check With Broker stays as a force button,
but the drift you can see on screen stops being possible.

### Trade Book

The same treatment. Every fill the broker reports for today appears, joined to
its order where AlgoMirror placed it, and marked **Placed outside AlgoMirror**
where it did not. `equity_trades` becomes the historical record for days the
broker will no longer serve, rather than the primary source for today.

## Consequences worth knowing

- After an OpenAlgo reset, today's books clear themselves. Old rows in
  AlgoMirror's history remain, correctly, because they did happen.
- When you go live, an emergency trade at your broker terminal shows up in the
  books beside everything else.
- A broker that is unreachable degrades the books to AlgoMirror's own record,
  clearly marked as unverified, rather than showing an empty screen.

## (D) — Direct

An order the broker reports that AlgoMirror never placed is marked **(D)**,
beside the account count. **D is for Direct: placed at the broker terminal, not
through this application.**

It sits with the account count because that column already answers "where did
this come from", and two characters do not crowd a table the way a badge does.

A (D) row is deliberately hollow where AlgoMirror would have had something to
say: no trade nature, no allocation ratio, no levels. Nobody told this
application why the order existed, and inventing an answer would be worse than
leaving it blank. D10 carries the reasoning.

## The dashboard shares this builder

Today's Orders on the dashboard is the same book, filtered to today with no
carry-over of an older resting GTT.

It used to build its own list, and that list quietly behaved differently:
two exits placed a second apart on two accounts showed as two rows where the
Order Book showed one, and an order placed at the broker terminal did not
appear at all - so on 1 September the dashboard reported three orders on a day
with five. One builder, one behaviour, and the (D) marking, the NOT AT BROKER
marking and the unverified-account reporting all come with it.

## Price is what it filled at

The Price column shows the **quantity-weighted average execution price** across
every account, taken from the trade rows.

Not from the split's stored `avg_fill_price`, which comes from the broker's
ORDER book: that carries the instruction price rather than the execution, so
for a limit order it is the limit and for a market order it is empty. A market
order that had filled therefore used to read Rs 0.00 - a price of zero, which
is not what happened.

Until an order fills there is nothing to average, so a limit order shows its
limit and a market order shows *Market*.

## The two P&L figures add up

On the dashboard, **Unrealised P&L** and **Today's P&L** do not overlap. They
are two halves of the same profit:

| Figure | Measured from | Buy 1200, yesterday closed 1250, now 1280 |
|---|---|---|
| Unrealised P&L | average cost **to yesterday's close** | 500 |
| Today's P&L | yesterday's close **to now** | 300 |
| Together | | **800** — the whole profit since purchase |

They used to report 800 and 300, where the 800 already contained the 300, so
they could neither be added nor compared. Decided by the product owner:

> *"Let Unrealised P&L show profit till yesterday i.e Rs20 and Today's P&L
> shows 5."*

**The label carries the meaning.** Read as "total profit", 500 is short by
exactly today's move, so the card says *To previous close. Add Today's P&L for
the total.*

**Where a previous close is missing**, the split is impossible and the card
falls back to the whole profit since purchase, saying *Total since purchase. No
previous close reported.* A partial split is never shown: if one row in a total
cannot be split, the whole total reverts, because a mixture of two measures
must not be labelled as either one.

**The same-day problem does not arise here.** These figures are computed over
holdings, and a holding exists only after T+1 settlement, so every row was
necessarily held at yesterday's close. On a screen that showed positions opened
today, attributing anything to yesterday would be inventing history — the rule
there is that a position opened today puts its whole profit in Today's P&L and
nothing in Unrealised.
