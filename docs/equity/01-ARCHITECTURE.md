# Equity Architecture

## The three layers

```
   Your broker (Dhan, and others through the same interface)
        |
        |   one broker account per OpenAlgo instance
        v
   OpenAlgo                      normalises every broker into one API
        |
        |   HTTP, one instance per trading account
        v
   AlgoMirror equity module      intent, multi-account, purpose, levels
```

Each trading account gets its **own OpenAlgo instance**, because an OpenAlgo
instance holds exactly one broker session. Two family accounts therefore run two
instances on two ports, each with its own API key stored encrypted against the
account row in AlgoMirror.

AlgoMirror never talks to a broker directly. Every order, every book, every
quote goes through OpenAlgo. That is what makes the module broker-agnostic: to
support a new broker, only OpenAlgo has to know about it.

## Who owns which truth

This is the single most important rule in the module, and everything else
follows from it.

**The broker owns what happened.** Which orders exist, their status, how much
filled, at what price, how many shares are held, how many are pledged, and what
anything is currently worth. If AlgoMirror's stored copy disagrees with the
broker, the broker is right and AlgoMirror's copy is stale.

**AlgoMirror owns what you intended.** Why a stock was bought (the trade
nature), which accounts one instruction was split across and in what ratio, the
stop loss and target you set, whether a breach should sell or ask you first, and
the record of an order that never reached the broker at all.

The broker cannot know any of the second list. AlgoMirror must not assert any of
the first.

**The operating rule:** where both hold an opinion about the same fact, the
broker wins. Where the broker holds no opinion, AlgoMirror is the only record
there is.

## What this means in practice

| Screen | Source | Label |
|---|---|---|
| Positions | Live from the broker on every load. Nothing stored. | BUILT |
| Holdings | Live from the broker on every load; AlgoMirror's own row supplies trade nature, levels and exit mode | BUILT |
| Order Book | AlgoMirror's own tables only. The broker is never called. | GAP — see `04-BOOKS.md` |
| Trade Book | AlgoMirror's own tables only. The broker is never called. | GAP — see `04-BOOKS.md` |

The F&O module reads all four books live from OpenAlgo and stores nothing. The
equity module was built differently for the two books, because equity carries
information F&O does not — the trade nature, the account split, and orders that
failed before reaching the broker. Reconciling those two facts is the subject of
`04-BOOKS.md`.

## The one constraint that shapes everything

**A broker's order book and trade book only ever return today.** No broker will
serve you last Tuesday's book. F&O can be a pure live mirror because F&O only
ever shows today. The equity Order Book has date filters and shows history, so
its design has to split by date:

- **Today** — the broker is authoritative, and its rows are the spine.
- **Earlier days** — AlgoMirror's own store is the only record that exists.

## Background workers

Three workers run inside the application process on the shared scheduler. All
three are equity-only and none of them touches F&O.

| Worker | File | Cadence | Job |
|---|---|---|---|
| Exit monitor | `app/utils/equity_exit_monitor.py` | ticks every 5s; each user evaluated on their own interval, default 30s | Watches stop loss and target against the live price feed and acts on a breach |
| Alert monitor | `app/utils/equity_alert_monitor.py` | every 10s | Watches watch-list price alerts and raises events |
| Fill reconciler | `app/utils/equity_fill_reconciler.py` | every 20s | Reads the broker's order and trade books and writes fills and statuses back onto splits |

The alert monitor and the fill reconciler are invoked as riders from the exit
monitor's tick, each isolated in its own error boundary, so a failure in one
cannot stop the other two.

## Prices

Prices come from a shared WebSocket feed (`equity_price_feed.py`), primed with
the set of symbols anything currently cares about. The exit monitor uses **only**
pushed prices: a symbol with no fresh price is skipped rather than sold against a
stale figure. REST quotes are used for screen loads and for the single-symbol
quote shown when you pick a stock in Place Order.

## Separation from F&O

- Equity code lives in `app/equity/` and `app/utils/equity_*.py`.
- Equity tables are all prefixed `equity_`.
- `app/models.py` is shared. Equity work only ever **adds** to it.
- `app/templates/layout.html` is shared. Equity work only ever adds links inside
  the equity navigation group.
- OpenAlgo is not modified for equity. If a change appears to be needed there,
  it is raised with the product owner rather than made.

## An account has no segment, and both modules take all of them

Equity and F&O share one table, `trading_accounts`, and **nothing on that record
says which module an account belongs to.** Both modules simply read every active
account:

- F&O: `app/trading/routes.py` → `get_selected_accounts()` → `get_active_accounts()`
- Equity: `app/equity/routes.py` → `_active_accounts()`

Same shape, same result. So on 1 September the F&O Positions screen listed the
equity accounts' CNC stock — not because anything in F&O changed, but because
the equity work added two accounts and F&O reads them all.

**It is symmetrical.** The day an F&O account is registered on an instance, the
equity screens will show F&O positions in exactly the same way.

**Not fixed, deliberately.** The fix belongs in F&O as much as in equity, and
the product owner's standing rule is that F&O is operational and not to be
touched. Their decision on 1 September: equity data is served by the equity
module being built here, and F&O takes its data from the already-built
deployment, which does not carry these accounts. So nothing live is affected and
the separation is a question for whenever the two are consolidated.

**When it is addressed**, the honest fix is a segment on the account — Equity,
F&O, or both — set once per account, with each module filtering on it. Filtering
instead on product (hiding CNC from F&O) would be guessing at the symptom: the
gap is that an account has no segment, not that CNC is unwelcome.
