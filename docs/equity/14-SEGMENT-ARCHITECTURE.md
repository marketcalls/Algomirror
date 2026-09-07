# Segment Architecture — PLANNED

**Status: PLANNED. Nothing here is built.** It exists to be agreed before code is
written, because it changes the F&O module, which is operational and under a
standing rule not to be touched without notice.

This is the second draft. The first was reviewed against the code and was wrong
in three ways that mattered; what it got wrong is kept below, because the
corrections are the useful part.

## The agreed position

Settled with the product owner, 1 September 2026. The rest of this document is
the reasoning; this is the decision.

1. **Accounts and cash are common.** Both modules, one copy. No change.
2. **Transactions split by exchange.** NSE and BSE to equity; NFO, BFO, MCX and
   CDS to F&O. Anything unrecognised is shown in both, never hidden.
3. **F&O Holdings needs nothing.** There is no such screen.
4. **P&L is two numbers that add up, in both modules.** *Unrealised P&L* is
   profit up to yesterday's close; *Today's P&L* is the movement since. Together
   they are the total. They do not overlap.
5. **A position opened today attributes nothing to yesterday.** The whole figure
   sits in Today's P&L. No prior-day number is ever invented.
6. **The broker's account M2M stays**, labelled account-wide, all segments.
7. **The F&O change is confined to hiding equity rows on three screens** —
   Positions, Order Book, Trade Book. No order logic, no strategy code, no risk
   monitor.

## The instruction

> *"Database and all the requirements need to be worked out common and only
> transaction details will go to respective modules."* — 1 September 2026

> *"Accounts, Cash are common for both."*

This simplifies the problem rather than complicating it. What was being treated
as one defect — *"the F&O screens show the equity accounts"* — splits in two,
and only half is a defect:

- Both modules seeing the same **accounts and cash** is **correct by design**.
  No fix. This also withdraws the earlier proposal of a per-account segment
  column: marking accounts by segment would contradict the instruction.
- Both modules seeing each other's **transactions** is the defect.

## The three layers

| Layer | Contents | Status |
|---|---|---|
| **Shared** | Users, accounts, credentials, connection state, funds and cash, the broker payload cache, trading hours | Common by instruction. **Has one known defect — see below.** |
| **Segment-routed** | Orders, trades, positions, holdings read live from the broker | The work. |
| **Module-private** | Equity's orders, splits, fills, holdings, levels, natures, alerts, rates; F&O's strategies, legs, executions, risk events | Already clean. Verified: each set is queried only from its own module. |

### The shared layer is not already correct

The first draft called it right. It is not. `last_data_update` is **one
timestamp for three different payloads**. A positions or holdings read advances
it (`trading/routes.py:284`, `:332`), and the F&O funds screen then reads it as
the age of `last_funds_data` and serves cash up to thirty seconds stale without
asking the broker (`:81`). The equity module already had to work around this
with its own process-local freshness dictionaries.

This is not in scope here, but "common" must not be read as "sound". It is
recorded so the next person does not inherit the first draft's confidence.

## The segment rule

The routing key is the **exchange**:

> *"NFO is exchange for Derivatives. NSE is Equity exchange."*

**There is precedent in a money path.** F&O's panic-close already scopes itself
with `PANIC_CLOSE_EXCHANGES = {'NFO', 'BFO'}` (`accounts/routes.py:459`), and
close-all and reconcile do the same. The exchange field is already trusted to
decide what gets closed at a broker. Using it to decide what gets *displayed* is
a weaker claim on the same field.

| Exchange | Segment | Equity shows | F&O shows |
|---|---|---|---|
| NSE, BSE | Equity cash | yes | no |
| NSE_INDEX, BSE_INDEX | Cash index | yes | no |
| NFO, BFO | Equity derivatives | no | yes |
| CDS, BCD | Currency derivatives | no | yes |
| MCX, NCDEX, MCX_INDEX | Commodity | no | yes |
| **anything else** | unknown | **yes** | **yes** |

`BCD` and the `*_INDEX` pseudo-exchanges were missing from the first draft. Under
a hide-list they would have defaulted to showing on both — tolerable for F&O,
a genuine leak on the equity side.

### Each module hides; neither module shows-only

Written as *"hide NSE and BSE"*, an unrecognised exchange is **shown**. Written
as *"show only NFO and BFO"*, an unrecognised exchange is **hidden**.

Showing a row that belongs to the other module is an annoyance. **Hiding a live
position from the person responsible for it is a trading risk.** The failure
direction is chosen deliberately, on both sides.

### One key, not several

The first draft proposed reading `exchange`, `exch` and `exchange_segment`,
"the way the equity module already does for prices". That rationale was false.
**Neither `exch` nor `exchange_segment` appears anywhere in this application or
in the OpenAlgo SDK**; every book documents `exchange`, the live cached payloads
use `exchange`, and every existing money path reads that single key. Adding
`exchange_segment` would invite `NSE_EQ` / `NSE_FNO` style values that neither
hide-list matches.

Read `exchange`. Treat missing or blank as unknown, and therefore show it.

## The invariant

The first draft carried two invariants — *the cache holds the broker's
unfiltered payload*, and *filter at display, never at fetch*. **Both were aimed
at the wrong mechanism, and the first was already false.**

The live database shows `last_positions_data` containing `account_name`,
`invested_value` and `pnl_percentage` — fields injected by `enrich_positions()`,
which mutates the list in place before it is cached (`trading/routes.py:277`,
`:283`). The cache has never held a pristine broker payload.

And the real hazard is not fetch-versus-display at all. In the F&O holdings
route, `holding_list = data.get('holdings', [])` **is** `data['holdings']` — the
same object — and `account.last_holdings_data = data` runs *afterwards*
(`:322`, `:331`). A filter written at "display time" as `holding_list[:] = [...]`
would blank the shared cache exactly as thoroughly as one in the fetch helper.
Both of the first draft's invariants would have permitted it.

**The invariant, restated mechanically:**

> A filter produces a **new list bound to a new name**. No route may mutate,
> reslice or reassign any part of the payload it is about to cache. The cache
> write must be provably independent of the display list.

The correct shape already exists in the F&O positions route:

- `account.last_positions_data = pos_list` — the untouched list
- `positions_data.extend([p for p in pos_list if ...])` — a new list

That is the template for all four edits. It satisfies the invariant by
construction rather than by discipline.

## Where it is enforced

| Module | Screen | Action |
|---|---|---|
| F&O | Positions, Order Book, Trade Book | Hide equity exchanges when building the **display list** |
| F&O | Holdings | **Nothing to do — no such screen** (see below) |
| Equity | Order Book, **Dashboard** | Hide derivative exchanges on external broker rows |
| Equity | Positions | Hide derivative exchanges; also fix the feed subscription, below |
| Equity | Holdings | Already filtered — `_normalise_broker_holdings` drops non-CNC rows |
| Equity | Trade Book | Nothing needed — stored rows only, no broker merge |
| Both | Funds, Accounts, account cards | **No filter.** Common by instruction. |

Three corrections to the first draft here:

**The equity Dashboard is not exempt.** It listed dashboards under "no filter,
accounts and cash are common". But the equity dashboard's *Today's Orders* is
transactions — it delegates to the same order book builder with the broker merge
on. It needs the filter like any other book.

**The equity side is not "not urgent".** The first draft deferred it because
*"no F&O account is registered on this instance"*. There is no such thing as
registration per module — accounts are common, by this very instruction. The
equity Order Book merges **every** broker order for the day and shows anything
AlgoMirror did not place as an external row, with no exchange test. The moment
an F&O order exists on a shared account it appears on the equity Order Book and
in its totals. The exposure is symmetrical and so is the urgency.

**The equity Positions screen also feeds the price subscription.** It builds its
watch set from the raw broker position book with no exchange filter, and those
symbols count against a 500-symbol cap on a WebSocket manager **shared with the
F&O option chain and position monitor**. Filtering only at render leaves NFO
contracts subscribed. Filter before the subscription set is built, and do not
add pruning to that path until the unsubscribe path has been checked against the
other consumers — it has no per-consumer reference counting.

## Totals must come from the filtered list

Four aggregate blocks are computed in Python, not in the template: order book
statistics, trade book P&L, positions totals, holdings statistics. If the filter
is applied in the template, every one of them silently disagrees with the rows
above it. **The filter belongs in the Python list that both the rows and the
totals are computed from.**

## Resolved: F&O Holdings needs nothing

A derivative cannot sit in a demat account, so an F&O holdings screen can only
ever show equity. The product owner: *"Previously also holding section was
showing equity holdings only."*

**And there is no such screen to fix.** The F&O sidebar has never carried a
Holdings link; the only links to `trading.holdings` are in `navbar.html`, which
is included nowhere and is dead markup from an older layout. The route is
reachable only by typing its URL.

Decision: **leave the route alone.** Removing an already-unreachable route from
an operational module buys nothing.

**This removes the worst hazard in the plan.** The near-miss that produced the
invariant — F&O's Holdings screen writing a filtered, empty list into the cache
the equity module reads — cannot occur, because there is no F&O Holdings screen
to filter. The invariant still governs Positions, where the same aliasing
pattern exists.

## Segment P&L, and the two figures that must not be confused

The first draft proposed accepting that the F&O M2M P&L would keep including
equity, because it is a broker scalar on the common funds payload with no rows
to filter. The product owner rejected that and was right:

> *"NFO P&L should be reflected in F&O module and NSE P&L should be reflected
> in equity module."*

That is achievable, because **every position row carries its own exchange**. It
simply cannot come from the funds payload. It has to be computed from the
position book, per segment. Three things decide whether that computation is
right.

### P&L since entry is not today's M2M

An NFO position carried three days: entry 100, yesterday's close 120, trading
at 125.

| Figure | Basis | Value |
|---|---|---|
| P&L since entry | average price | 25 |
| **Today's M2M** | **previous close** | **5** |

The broker's M2M is the second. A row sum on `average_price` gives the first.
On a carried position, labelling one as the other is wrong by the whole of the
prior days' move.

The equity module already keeps these apart — *Unrealised P&L* against average
cost, *Today's P&L* against previous close. F&O needs the same pair, and today's
M2M needs a previous close per symbol, which the F&O module does not currently
fetch.

### A squared-off position still counts

Brokers keep a closed position in the book all day at quantity 0. The F&O screen
drops those rows before totalling — `_position_is_open` at
`trading/routes.py:245`, and `total_pnl` at `:296` sums only the survivors. So
anything opened *and* closed today contributes nothing.

A segment total must sum **every row of that segment, squared-off included** —
realised plus unrealised. That is what M2M means.

### Show both, label both

Each module shows its own segment figure, computed from its own rows. The
broker's account-wide figure stays visible, labelled as account-wide. Nothing is
invented and nothing is hidden, and when the two differ the difference is
visibly the other segment.

## Before step 2: enumerate every consumer

The first draft scoped this to four screens in one file. The application
registers eleven blueprints. Before any code, list every place that calls
`orderbook`, `tradebook`, `positionbook` or `holdings` — including the API
blueprint, margin, tradingview and the diagnose UI, none of which were examined.
A filter on four screens is not a boundary if a fifth route serves the same rows
unfiltered.

## Sequence

Each step is independently reversible and verifiable.

1. Agree this document and the two open decisions.
2. Enumerate every consumer, as above.
3. Write the exchange test once, in shared utility code, with tests for every
   exchange in the table and for missing, blank and unrecognised values.
4. Apply it to **F&O Positions only**.
5. Apply it to F&O Order Book and Trade Book.
6. Apply it to the equity Order Book, Dashboard and Positions, including the
   subscription set.
7. Decide F&O Holdings and the M2M note on the evidence of steps 4 to 6.

## Verification

Step 4 proves the design. The check is **not** "the cache is still full" — the
first draft's version, which would have passed while the invariant it tested was
already violated.

- After an F&O Positions load, the cached row count **equals the broker's row
  count**, and the cached rows still include NSE.
- After an F&O Holdings load, the equity module's holdings cache is complete.
  This is the specific near-miss and is not optional.
- No NFO or BFO row disappears from any F&O screen.
- An unrecognised exchange value is shown on both sides, not hidden.
- Every total agrees with the rows displayed above it.
- Equity screens are unchanged by steps 4 and 5.
