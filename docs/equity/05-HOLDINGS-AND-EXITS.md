# Holdings, Levels and Exits

## Holdings versus Positions

They are different screens on purpose. A CNC delivery buy placed today is a
**position** until it settles; after settlement it is a **holding**. The
distinction decides what can be sold and what the stop loss monitor may act on,
so blurring the two would hide something that matters.

**Positions** is read live from the broker on every load and nothing is stored.
**Holdings** is read live from the broker too, with AlgoMirror's own row supplying
the things the broker cannot know.

## What the broker owns and what AlgoMirror owns — BUILT

The holdings sync writes only three things from the broker: **quantity**,
**average cost** and **pledged quantity**.

It never touches: stop loss, target, exit mode, trade nature, the exit claim, or
the breach records. Those belong to AlgoMirror, and a routine price refresh must
never be able to disarm a stop loss or undo a decision you made.

## Trade nature — BUILT

Why a stock is held: Swing, Long Term, and whatever else you define.

A holding inherits its nature the moment its row is **first created**, and only
when AlgoMirror's own tagged buys account for **every share of it**:

| Situation | Result |
|---|---|
| 100 shares bought as Swing, 100 held | **Swing** |
| 100 shares bought as Swing across two accounts, 100 held in each | **Swing** in both |
| 100 bought as Swing, 101 held | **Unassigned** — one share came from elsewhere |
| 100 bought as Swing, later 60 sold, 40 held | **Swing** |
| Bought partly as Swing and partly as Long Term | **Unassigned** — no single honest answer |
| Bought for one account only | Tagged for that account; the other stays **Unassigned** |
| Order placed but never filled | **Unassigned** — only genuine fills count |

Two rules hold this together. **The account must have taken part** — a stock
bought for one family member never tags another member's holding of the same
stock. And **once set, nothing overwrites it**: change a holding from Swing to
Long Term and no broker read will undo you.

## Stop loss, target and exit mode — BUILT

These are AlgoMirror's own levels. Your broker knows nothing about them; the
background monitor watches them and acts.

**Auto Sell** — a breach sells immediately, with no further confirmation.
**To Confirm** — a breach raises an alert and waits for you.

Saving a level from the main Holdings row applies it to every account holding
that symbol. The **Accounts** button opens the per-account breakdown, where one
account can be armed on its own.

Pledged shares are excluded from everything. The sellable quantity is always
quantity minus pledged, and a holding with nothing sellable is not monitored.

## The exit claim — BUILT

Two things can decide to sell the same shares: the background monitor and you
pressing Sell. Without a guard both read a holding of 40, both place a sell, and
the account ends up short 40 shares it never owned.

The claim is that guard, and the protocol is exact:

1. Lock the holding row.
2. Re-check under the lock that it is still claimable and carries no broker
   order id.
3. Set the status to pending and record the claimed quantity.
4. **Commit.**
5. Only then call the broker.

**The commit is the claim.** An uncommitted status change is invisible to the
other worker, so committing before the broker call is not an optimisation, it is
the whole mechanism.

Every equity sell in the module goes through one helper. Nothing else is
permitted to place a sell.

### Exit statuses

| Status | Meaning |
|---|---|
| `ACTIVE` | Held, nothing in flight |
| `AWAITING_CONFIRM` | A level was breached in To Confirm mode; waiting for you |
| `EXIT_PENDING` | Claimed, about to be sent |
| `EXIT_SUBMITTED` | At the broker, with an order id |
| `EXIT_INDETERMINATE` | The broker gave no usable answer. Terminal for every automated path — only a person reopens it |
| `EXITED` | Sold out |

When a sell is submitted, the broker's order id is written **unconditionally**,
even if the row's state looks wrong. Losing an order id is the worst failure
available: it leaves a real order at a broker that AlgoMirror cannot track,
modify or cancel.

## The monitor — BUILT

The scheduler ticks every 5 seconds and evaluates each user on their own
interval, 30 seconds by default and settable between 1 and 300.

Prices come **only** from the pushed WebSocket feed. A symbol with no fresh price
is skipped rather than judged on a stale one — nothing is ever sold against a
price that might be minutes old.

On a breach in **Auto Sell**, the breach is recorded once, an activity log entry
is written, and the sell is dispatched on a bounded worker pool. A definite
failure is retried up to five times, ten seconds apart, and then left for a
person. On a breach in **To Confirm**, the holding moves to awaiting-confirmation
and **nothing is placed**.

## Quantity refresh before selling

**Every** equity sell now verifies the quantity with the broker before it
claims — BUILT. The verification sits inside the one helper that all three
paths go through, so none of them can skip it.

| Path | Verified before claiming |
|---|---|
| Arming a stop loss or target | Yes |
| Pressing Sell manually | Yes |
| The background monitor acting on a breach | Yes |

A shortfall resizes the sell rather than stopping it: if the broker holds 40 of
the 100 on the row, 40 are sold. A broker that cannot be read stops the sell
altogether and the attempt is recorded as withheld — see
`06-EXTERNAL-BROKER-ACTIVITY.md`.

## Costs

Estimated exit cost is the cost of getting out: side SELL, one order and one
scrip per contributing account, priced at the current LTP. Net P&L is gross P&L
minus that estimate.

Costs are computed from the brokerage rates you enter in Equity Settings →
Charges. **With no rates set, estimated cost is ₹0.00 and Net P&L is simply
Gross P&L wearing a different name.** Setting the rates is not optional if the
Net column is to mean anything.
