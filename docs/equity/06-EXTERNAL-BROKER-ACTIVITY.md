# Trades Made Outside AlgoMirror

A buy or sell placed straight at the broker — from the broker's own terminal or
mobile app, or directly in OpenAlgo — is a normal and necessary thing. In an
emergency you will not stop to open AlgoMirror. The module must therefore treat
external activity as expected, not as corruption.

This document states what happens today, what is wrong with it, and the rules
that fix it.

## The three cases

### 1. An external BUY

The broker reports a holding AlgoMirror never bought.

**Today — BUILT.** The holding appears on the Holdings screen with the broker's
quantity and cost, because that screen reads the broker. A tracked row is
created for it and, since no AlgoMirror order explains the shares, its trade
nature is left **Unassigned** — the honest answer. You can set the nature by
hand and nothing will overwrite it afterwards.

**Wrong with it — GAP.** Nothing tells you the holding appeared. It arrives
silently among the others, with no stop loss and no target, and it is easy to
miss that it is unprotected.

### 2. An external SELL, partial

You sell some of a holding at the broker. AlgoMirror still thinks you hold the
original quantity.

**Today — partly BUILT, partly GAP.** The tracked quantity is corrected the next
time holdings are read from the broker, silently. Two paths refresh from the
broker before selling — arming a stop loss or target, and pressing **Sell**
manually — so those are safe: the sell is sized to what the broker actually has.

**The background monitor does not.** When a stop loss or target is breached in
AUTO mode, the monitor claims and sells using the quantity on the stored row,
whatever it was when last synced. If you sold 60 of 100 shares at your terminal
and the stop loss fires before any screen refreshed that row, AlgoMirror will
send a sell for 100 shares against a holding of 40.

**This is the most serious gap in the module** and it is the one to fix before
going live. In CNC the broker will almost certainly reject an oversell, and the
error you would see is the broker's wording, not an explanation. But relying on
a broker's rejection as a safety mechanism is not a design.

### 3. An external SELL, complete

You exit the whole holding at the broker.

**Today — BUILT with a sting.** The tracked row's quantity is set to zero and the
row drops out of the monitor's scan, so nothing fires. But the row survives,
still carrying your old stop loss, target, exit mode and — critically — the
breach markers recording that a level was already hit.

**Wrong with it — GAP.** If you buy that stock again later, the old row is reused.
The old stop loss and target govern the new position without you setting them,
and a breach marker left over from the old position can suppress a genuine alert
on the new one. It is also why the trade nature is not re-inherited on a
re-purchase: inheritance only runs when a row is created, and this row already
exists.

## The rules — PLANNED

### Rule 1 — Nothing sells against an unverified quantity — BUILT

The verification sits inside the single helper every equity sell goes through,
so no path can skip it. Before a holding is claimed, its quantity and pledged
count are read from that account's broker and written to the row. The claim then
sizes itself against the broker's figure.

- **Broker holds fewer than the row says.** The row is corrected and the sell
  goes ahead for what is really there. Selling the 40 you still hold is right;
  selling 100 is not. The shortfall is logged with the observation that the
  difference did not come through AlgoMirror.
- **Broker holds none.** Nothing is sellable, so nothing is sent.
- **Broker cannot be read.** No claim is taken and no order is sent. The result
  is `unverified`, which is distinct from a failure: nothing happened, so trying
  again is safe. The level stays armed and the next tick tries again.
- **A withheld automatic exit is recorded** in the activity log as
  `equity_auto_exit_withheld`, carrying the symbol, the level, the breach price
  and the reason. A stop loss that fires without a sale following must never be
  silent.

An unreadable broker is treated as *unknown*, never as *zero*. An empty answer
and no answer are different things, and confusing them is how an application
sells shares it cannot see.

### Rule 2 — A quantity that moves without an order is an event

On every holdings read, the tracked quantity is compared with the broker's. When
they differ and no AlgoMirror order or in-flight exit explains the difference, an
alert is raised into the existing Alerts screen, in your words:

> RELIANCE fell from 101 to 40 at Account 1's broker and AlgoMirror did not sell
> it. The stop loss of ₹1,240 is still armed on the remaining 40 shares.

Silent absorption is what makes external trading dangerous. Naming it makes it
safe.

### Rule 3 — A new holding announces itself

A holding that appears with no AlgoMirror order behind it raises its own alert:

> NHPC appeared with 50 shares in Account 2's account. AlgoMirror did not buy it.
> It has no trade nature, no stop loss and no target.

### Rule 4 — A holding that goes to zero is retired

When a tracked holding reaches zero shares, its stop loss, target and breach
markers are cleared and the row is marked closed. Your exit mode preference and
the trade nature stay, as a reasonable default if you buy the stock again.

### Rule 5 — A re-purchase is a new position

When a retired holding's quantity goes from zero back to positive, it is treated
as newly created: the trade nature is inherited again from the order that bought
it, under the coverage rule in `05-HOLDINGS-AND-EXITS.md`. No level from the old
position is ever carried into a new one.

### Rule 6 — External orders appear in the books

Every order and fill the broker reports for today appears in the Order Book and
Trade Book, whether AlgoMirror placed it or not, marked **Placed outside
AlgoMirror**. See `04-BOOKS.md`.

## What is deliberately not done

**AlgoMirror does not adopt an external order as its own.** An order it did not
place gets no trade nature guessed for it, no stop loss inferred, and no
allocation ratio applied. It is shown, it is counted, and it is left alone. The
module records intent; it will not invent intent it was never given.

**AlgoMirror does not cancel or modify an external order.** It has no basis for
deciding that an order it did not place should not stand.
