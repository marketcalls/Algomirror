# Design Decisions

Each entry records what was decided, why, and what was given up. The rejected
option matters as much as the chosen one — without it a future reader cannot
tell a decision from an accident.

## D1 — The database is written before the broker is called

**Decided.** The parent order and every split are committed before the first
broker call.

**Why.** If the application dies between the two, the recoverable failure is an
order row with no broker order id. The unrecoverable one is money at a broker
with no record of it.

**Given up.** A brief window where an order row exists that will turn out never
to have been sent. That is what the `SKIPPED` and `INDETERMINATE` statuses are
for.

## D2 — INDETERMINATE is never retried automatically

**Decided.** A placement whose outcome is unknown is terminal for every automated
path. Only a definite refusal may be sent again.

**Why.** A retry after an unknown outcome can place a second real order. There
is no amount of convenience worth that.

**Given up.** Automatic recovery from a timeout. Reconciliation or a person
resolves it instead.

## D3 — The commit is the exit claim

**Decided.** Lock the row, re-check, set pending, commit, and only then call the
broker.

**Why.** An uncommitted status change is invisible to the other worker. Two
workers reading a holding of 40 and both selling it leaves the account short.

**Given up.** A database round trip on every exit. Correctness is worth it.

## D4 — A broker read never touches AlgoMirror's own state

**Decided.** The holdings sync writes quantity, average cost and pledged
quantity, and nothing else.

**Why.** A routine price refresh must never disarm a stop loss, reopen an exit
claim, or overwrite a trade nature you chose.

**Given up.** Nothing.

## D5 — Trade nature is inherited only when every share is accounted for

**Decided.** A holding starts with a tag only when tagged AlgoMirror buys cover
its whole quantity and agree on one nature.

**Why.** Claiming that 101 shares are Swing when only 100 came from a Swing order
is a small lie that compounds. Unassigned is the honest answer for shares that
arrived from somewhere the application never saw.

**Rejected.** Taking the nature of the most recent matching buy, which was the
first implementation. It would have tagged the whole holding from a part of it.

**Given up.** Some holdings need a manual tag. Setting one is two clicks and it
is never overwritten afterwards.

## D6 — Adoption requires exactly one candidate

**Decided.** An indeterminate split is matched to a broker order only when one
candidate matches on symbol, exchange, side, quantity and time, and that order id
is not already claimed.

**Why.** A wrong adoption attaches your order to someone else's fill and every
number downstream is then wrong with full confidence.

**Given up.** Some genuine matches go unadopted when two identical orders were
placed close together. A miss is recoverable; a wrong match is not.

## D7 — Both timezone readings are tried

**Decided.** The broker's unmarked timestamp is compared with the stored time
both as UTC and as IST, and the smaller gap wins.

**Why.** Indian brokers return IST with no marker; AlgoMirror stores UTC. Reading
one as the other is out by five and a half hours, which turned a 64-second match
into a 19,864-second miss.

**Rejected.** Assuming IST. It would be right for Dhan and wrong for the next
broker, and the failure would be silent.

## D8 — Prices for selling come only from the pushed feed

**Decided.** The exit monitor uses WebSocket prices only, and skips a symbol
with no fresh price.

**Why.** Selling on a stale price is worse than not selling. A skipped symbol is
evaluated again in thirty seconds.

**Given up.** Coverage during a feed outage. That is the correct trade.

## D9 — The equity books keep their own record, and the broker overrules it

**Decided.** Order Book and Trade Book merge the broker's live book with
AlgoMirror's stored record, rather than being a pure mirror like F&O.

**Why.** F&O can mirror because it shows only today and carries nothing the
broker does not know. Equity carries the trade nature, the account split, the
levels set at order time, and orders that failed before reaching the broker. A
pure mirror loses all of that. A pure store, which is what exists today, cannot
show an order placed at your terminal and cannot self-correct.

**Rejected.** Copying F&O exactly. Rejected by the product owner: *"It cannot be
totally like F&O."*

**Given up.** Complexity. Two sources have to be joined and three row types
displayed.

## D10 — A broker-only order is shown but never adopted

**Decided.** An order the broker reports and AlgoMirror did not place appears in
the books, marked, with no trade nature guessed, no levels inferred and no
allocation applied.

**Why.** The module records intent. It will not invent intent it was never given.

## D10a — A delivery sale is shown the way brokers show it

**Decided.** Selling out of a holding leaves a negative delivery line in the
position book and reduces the holding straight away. Both are shown as the
broker reports them; the negative line is labelled *Sold from holding* so it
cannot be misread as a short.

**Why.** It is what every Indian broker does. Sell one of eighty and Dhan shows
a -1 position marked "From Portfolio" and a holding of 79. An admin who trades
elsewhere already reads this fluently, and a tidier invention would only be
tidier here.

**Rejected, and briefly built.** Netting the sale off the broker's holdings
figure so the holding read zero immediately, and hiding the negative line
altogether. Both were wrong for the same reason: they were written against
OpenAlgo's *sandbox*, which settles holdings on T+1 and therefore lags for the
rest of the day. A real broker has already netted, so netting again would
subtract the same sale twice and report a holding smaller than it is.

**The lesson, which is the general one.** `01-ARCHITECTURE.md` says the broker
owns what happened. That rule was departed from to paper over a simulator
artifact, and the result would have been an application that disagreed with
every real broker it connected to. When the simulator and the market differ, the
market is right and the simulator is the thing to note.

## D11 — The sandbox is not the market

**Decided.** Analyzer-mode prices are treated as simulated and never used to
judge price-dependent behaviour.

**Why.** Time was lost investigating a "wrong" price that was the simulator
working correctly.

## D12 — Nothing on the operator's machine is deleted

**Decided.** Scripts back up before they change anything and rename superseded
files rather than removing them.

**Why.** The operator is not a developer and cannot inspect what a script did.
A rename is reversible; a delete is not.

## D13 — A broker's timestamp is converted, never assumed

**Decided.** An unmarked broker timestamp is resolved to UTC at the point it
enters the application. With an anchor — the moment AlgoMirror itself placed the
order, which is genuine UTC — the nearer of the two readings wins. Without one,
a time that would be in the future read as UTC cannot be UTC, so it is IST.

**Why.** Indian brokers report IST and say nothing about it. Writing those
digits into a UTC column does not merely mislabel the value: every screen then
converts UTC to IST for display and *adds* five and a half hours on top. A fill
at 09:44 was shown at 15:14.

**Rejected.** Assuming IST at the boundary. It would be right for Dhan and wrong
for the next broker, and the failure would be silent — which is exactly the
reasoning already recorded in D7 for adoption matching. The same rule now
decides both.

**Given up.** Nothing. A broker that genuinely reports UTC is left alone,
because its reading is the one nearer the anchor.

## D14 — An exit claim is closed from the holding, not from the split

**Decided.** The pass that closes a filled exit starts from every holding with a
sell in flight and looks at the split it points at, rather than from the splits
the reconciler is chasing.

**Why.** The reconciler chases splits that are PENDING, PARTIAL or
INDETERMINATE, because a COMPLETED split has nothing left to ask the broker
about. That is true of the split and false of the holding behind it. A sell that
filled leaves a COMPLETED split and a holding still claimed, so a repair
attached to the chase query can never see the case it exists to repair. The
first attempt at this fix was attached to exactly that query and was dead code
from the moment it shipped.

**Also.** Starting from the holding needs no broker call and no readable
account, so a row stranded weeks ago is repaired as readily as one filling now.

**Deliberately not included.** A *rejected* sell also leaves the holding
claimed. Releasing a claim and settling one are different acts, and a pass that
guessed between them could reopen a holding whose sell is genuinely live. That
is recorded as owed rather than solved in passing.

## D15 — Warn about a repeated sale; never net it

**Decided, and built 1 September.** Where AlgoMirror has already sold a stock
today and the broker is still reporting the shares, the Holdings row says so. It
blocks nothing and adjusts no number.

**Why.** The broker owns what is held; that rule is not negotiable and D10a
records what happened when it was departed from. But AlgoMirror knows what it
sold today, and saying so costs nothing and changes nothing.

**Rejected, permanently.** Subtracting AlgoMirror's own sales from the broker's
figure. Against a real broker that subtracts the same sale twice.


## The button convention (agreed 4 September 2026, NOT yet built)

Agreed with the product owner after a screen-by-screen look at Place Order. The
decisions are settled; the work is not started. Anyone picking this up should
build exactly this rather than re-litigate it.

### Colour

- **A commit button is green.** In this theme `--p` is a green hue in light and
  a cyan one in dark, so `btn-primary` already IS the green: Add Stock, New
  Order, Add, Save all use it and are correct today.
- **A SELL commit is red**, and that is deliberate, asked for explicitly. The
  owner's words: *"Sell side final is Red."* Colour is the last signal before a
  live order goes out and the only thing on that row saying which way the trade
  runs. Uniformity does not get to take it.
- **Filled green carries BLACK lettering, not white.** The theme sets
  `--pc: 100% 0 0`, which is white, and the owner asked for black. Red keeps
  white: black on that red is unreadable.
- **A selected toggle** - Account Split and Depth when their panel is open -
  takes the same green fill with black lettering. Selection and commitment look
  alike on purpose; what separates them is position, not colour.

### Words

Three jobs were wearing the same labels. The split, and the owner's choice:

| Job | Word | Where |
|---|---|---|
| Abandons something you were about to commit | **Cancel** | Confirm order, Modify order, Edit stock, Remove stock, Save levels, Approve exit, Resolve exit |
| Closes something you only opened to look at | **Back** | Account-wise split (Order Book, Trade Book, Place Order), Fill detail, Account-wise position |
| Empties what you typed, no dialog involved | **Reset** / **Clear** | Reset Form on Place Order; **Watch List's Add Stock row moves from Cancel to Reset**; Clear on the search box; Clear Filters on the books |

*Keep the Order*, on the cancel-an-order dialog, stays as it is. It is the one
place where the plain word would be ambiguous - "Cancel" on a dialog about
cancelling an order says nothing at all.

### Size

Every button on an action row is the same size and the same width, set by the
longest label on that row. A shared `min-width` class, not per-screen values.

### Where the CSS lives

One block scoped to `.module-equity`, in the shared `layout.html`, beside the
violet table-heading rules that are already there. That scope is what makes a
shared-file edit safe: the class is on the page content div only when the
blueprint is equity, so F&O is untouched. **Flag it to the owner before
editing** - his standing rule - as was done for the headings.

Doing it per-template instead would mean ten copies of one rule, which is a
worse answer to a question whose whole point is uniformity.

## Intraday shorts (agreed 4-5 September 2026)

Selling shares the account does not own was refused outright, because as CNC it
is a short delivery - an auction and a penalty. The owner asked for the other
road: warn, then let it through as intraday, bought back before the close.

### What was agreed

| Decision | Answer |
|---|---|
| When does MIS appear? | Only as a fallback, where a sell has run out of shares. Never a product the admin picks. |
| Sell 100, hold 60? | 60 CNC and 40 MIS, as two orders. Smallest possible short. |
| Stop loss | **Required.** Validated above entry - a short's stop is above, not below. |
| Target | Optional. A stop is a safety device; a target is a preference, and forcing one gets a number typed to satisfy the form. |
| Exit mode | **Auto, always.** On a holding, waiting keeps the option not to sell. On a short there is no such option - it closes today regardless - so waiting can only move the forced buy-back to a worse price. |
| Square-off | **15:12**, a setting, clamped to 15:20. |
| New shorts after | **15:00** - refused. Two minutes is a coin toss with costs. |
| Monitor switched off | Cannot OPEN a short. Does not get to leave one open: an existing short is still squared off. |

### Why 15:12 and not 15:20

15:20 was the first answer. Then OpenAlgo's own `sandbox/squareoff_manager.py`
was read: it squares MIS off at **15:15** on NSE and BSE. At 15:20 the sandbox
wins every time, our square-off never runs, and the mechanism could not be
proven in testing. A live broker has its own cut-off near there too. Being
second means the broker does it, at market, at a price nobody chose.

### The stop lives at the broker

Raised by the owner: what happens on a power or wifi cut?

The square-off survives it - the monitor acts on everything still open PAST the
square-off minute, so a machine returning at 15:14 covers at 15:14, and if it
never returns the broker's own cut-off closes the position. But **the stop loss
did not survive it.** AlgoMirror's stop is AlgoMirror's own, never sent to the
broker, alive only while the monitor is running. A two hour outage left a short
with no stop at all.

So a resting **SL-M buy** is placed at the broker the moment the short fills,
triggered at the stop the admin typed. It sits in the exchange's stop-loss book
and fires whether or not this machine is on. Verified as supported: OpenAlgo
accepts `SL` and `SL-M`, and the sandbox implements the resting trigger-pending
phase, firing a BUY when the price rises to the trigger.

### Two things can now close the same short, so the order of operations is law

The owner chose to keep AlgoMirror watching the stop as well, with the broker
order as the backstop. That is faster, and it means two independent things can
decide to buy the same shares - and a double buy-back leaves you LONG a stock
you never wanted.

Every AlgoMirror-initiated cover - stop, target or square-off - therefore runs
in exactly this order, and no other:

1. **VERIFY** against the position book. If nothing is owed, the broker's own
   stop already fired: record it closed and place nothing.
2. **CLAIM** the short. One conditional UPDATE decides the winner.
3. **CANCEL** the resting SL-M, and confirm the cancellation.
4. **PLACE** the buy-back.

**If the cancel cannot be confirmed, nothing is placed.** The resting order will
do the job, and the broker's own square-off sits behind that. Placing anyway is
the one move that turns a covered short into an unwanted long.

### A GTT cannot open a short (added 5 September, when placement was built)

A fifth gate, alongside the four agreed above. A GTT waits for a price and can
rest for days; a short is an obligation that has to be closed this afternoon.
Waiting is the one thing it cannot do, so the two do not go together. The sell
is refused rather than quietly downgraded to a market order.

### The obligation is written down before anything protects it

`_place_short_legs` commits the `EquityIntradayShort` row on its own, and only
then places the resting stop. If the stop fails, the short is still on disk and
the square-off monitor will find it.

The reverse order — protect first, record after — reads tidier and is wrong. A
failure between the two would leave shares owed with nothing watching them, and
the square-off works off the record, not off the broker's position book.

For the same reason a short is recorded even when the broker's answer was
**never confirmed**. An unconfirmed short may still be a short, and the
square-off verifies against the position book before it acts — so a row that
turns out to be nothing is closed harmlessly, while a row that was never
written is a real short nobody is watching.

### A short is not "Sold from holding", and the position book cannot tell them apart

Both are a negative line. Sell one of eighty and the position book carries a
−1 while the holding reads 79; sell forty you never owned and it carries a −40.
The broker's data is identical in shape and opposite in meaning: one is money
already off the table, the other is an obligation.

So Positions decides from AlgoMirror's own short record first, and falls back to
the product only when there is none — MIS being the only thing this module ever
sends intraday. Calling a short "Sold from holding" would be exactly backwards
on the one row where being wrong costs the most.

### The two square-off times are validated as a pair

A cut-off at or after the square-off would let a short be opened that nothing
would ever close. Neither number is wrong on its own, so neither can be
validated on its own — they are read together, in the browser and again on the
server, and a save that would invert them is refused with both times named.

Nothing past 15:14 is accepted either, for the reason in "Why 15:12 and not
15:20" above.

## One heading per screen (agreed 5 September 2026, BUILT)

Every equity page carried a title twice: once in the top bar, from
`{% block page_title %}`, and again as a big `<h1>` at the top of the content.

The reason the top bar won: it does not scroll. `<main>` is
`overflow-y-auto` and the 64px `<header>` above it is not, so scroll down a
long Holdings table and the top bar is the only thing left naming the page.
Keeping the big heading and removing the top bar would have left the page
nameless exactly when a long table makes you forget which one you are on.

So the in-page `<h1>` went, on all ten screens, and the explanatory sentence
under it stayed and moved up - that sentence was never a duplicate, it was the
only line saying what the screen is for. Watch List and Place Order had no such
sentence, so those two now open straight onto their controls.

The top bar then took the owner's wording, which was better than the
`page_title` values it had: **Manage Accounts**, **Watch List**, **Place
Order**, **Order Book**, **Trade Book**, **Positions**, **Holdings**,
**Alerts**. Equity Dashboard and Equity Settings were left as they were, on the
owner's call.

Because the top bar is now the ONLY heading, it carries the weight the h1 used
to: `text-3xl font-bold` instead of `text-lg font-semibold`. That is in the
shared `layout.html` and could not live anywhere else, so it is sized from the
blueprint - `request.blueprint == 'equity'` - rather than from a CSS class:
`.module-equity` sits on the content div and cannot reach up into the header.
F&O renders exactly as it did.

Column headings went up one step at the same time, 0.75rem to 0.875rem, in the
same scoped block. They were already `font-weight: 700`, so weight had nowhere
left to go and size was the axis remaining.

## The investment note (agreed 5 September 2026, BUILT)

The first thing in this module that is not a fact somebody else reported. Every
other row is a price, a fill or a balance; this is why the owner bought and what
would make him wrong. No broker API returns it and it is the first thing
forgotten.

### It belongs to the stock

Not to a watch list entry, not to a holding. A thesis is about the company, so
it has to survive the stock being dropped from a list, sold out of entirely, and
bought back months later. Attached to a holding it would have been destroyed at
the exact moment it became interesting - when you sold, and later wondered why
you had ever bought.

One current note per `(user, symbol, exchange)`, enforced by a unique
constraint.

### Two boxes, not one

Thesis and risk. The owner named both, and written as one paragraph in a hurry
the second half is the half that stops getting written. Either may be empty;
both empty means there is no note, and that is how one is deleted.

### The history is the point

`equity_stock_note_versions`, written on every save that actually changes
something, before the current row is touched. Nothing in it is ever edited: a
version you can change is not a record.

A thesis quietly rewritten to match what happened is worth nothing. The value
of writing one down is being able to read, in September, what you actually
believed in March - including the parts that turned out wrong, which are the
parts worth reading.

Two consequences, both deliberate:

- A save that changes nothing writes no version. History is a record of
  thinking, not a click count.
- Clearing both boxes still writes a version. Deleting the note does not delete
  what it said.

`symbol` and `exchange` are repeated on each version rather than only reached
through the note, so a history stays readable if the note it belongs to is ever
removed. A history that depends on the thing it outlives is not a history.

### Reachable from two screens, so the dialog is a file

`app/static/js/equity_notes.js`, injected into the page on first open. Two
copies of the same markup in two templates is the thing that drifts.

The button is filled when a note exists and outlined when it does not, so the
table says at a glance which stocks have been written about. Without that you
would open every row to find out, which is the same as not having the feature.

Place Order was offered and declined: the owner wants that screen about the
order.

### One refusal worth naming

If the READ fails, Save sends nothing and says so. What is in the boxes at that
moment is not this stock's note, and saving it would overwrite a note nobody
has seen.

### The note, as it settled after the first look

The owner used it and asked for four changes, all made:

- **Earlier versions off the screen.** The block is gone from the dialog. The
  versions are still WRITTEN, and that was a deliberate split: a record costs
  nothing to keep and cannot be recovered once it stops being kept, so showing
  them again is a change to one file rather than a gap in the history. He was
  told, and can ask for the writing to stop too.
- **The heading is one line** - `GENUSPOWER - Investment Note - last saved
  05 Sep 2026, 07:20 pm` - with only the first half carrying the heading's
  weight. The saved time is small and grey: it is a footnote to a heading, not
  part of one.
- **A third box, "To Watch".** Not folded into Risk, because they are different
  sentences: a risk is what could go wrong, and this is the number, date or
  trigger that would tell you it IS going wrong.
- **50 / 25 / 25 by HEIGHT**, rows 10 / 5 / 5. Read as height rather than
  width, and said so: side by side in a `max-w-2xl` dialog the two quarter
  boxes would be about 160px, too narrow to write a sentence in.

## A naive UTC timestamp is read as LOCAL time (bitten twice)

The server sends timestamps as `datetime.utcnow()` through `_iso()`, which
produces no timezone marker. A marker-less value handed to `new Date()` is
parsed by the browser as LOCAL time, so on an Indian machine every clock reads
**5 hours 30 minutes early**.

`EquityFormat.clockTime` and `EquityFormat.dateTime` already handle it, and
carry a comment saying it once "put every clock on this screen 5h30m behind".
The note dialog then shipped with its own date formatter that did not, and the
owner spotted a note saved at 19:20 IST displayed as 1:50 pm.

**Any new date formatter in this module must do both halves:**

```
if (!/(Z|[+-]\d{2}:?\d{2})$/.test(text)) { text += 'Z'; }
new Date(text).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' })
```

A note on testing it, because the first attempt was worthless: a harness run in
a UTC container passes with the bug present, since both readings land on the
same instant. The check only bites with `TZ=Asia/Kolkata`, which is the
condition on the owner's machine. Run it that way or do not claim it is tested.

## The equity styling scope is one wrapper, and a stray tag can close it

The layout drops every equity screen into a single wrapper:

```
<div class="p-6 {{ 'module-equity' if request.blueprint == 'equity' }}">
    {% block content %}{% endblock %}
</div>
```

Everything that makes an equity screen look like an equity screen hangs off
that one class: the violet bold column headings, the button colour overrides,
the top padding, the sort affordances.

A screen that writes one closing `</div>` more than it opened does not fail,
does not warn and does not look obviously broken. It closes that wrapper early,
and every element BELOW the break becomes a sibling of the wrapper rather than
a descendant. The `.module-equity ...` rules can no longer reach it, so it
quietly falls back to the DaisyUI default.

`place_order.html` carried two such tags. They sat at the foot of the Place
Order tab, so the entire Order Status tab, the split-result table and all four
dialogs were outside the scope. The symptom the owner reported was that the
Order Status column headings were grey and small while the Watch List's were
violet and larger. Measured in a real browser: `insideModuleEquity: false`,
`rgb(120,123,134)` at `12px/500` instead of the violet at `14px/700`.

Two things are worth recording about how this was found, because both cost
time:

- The stylesheet was blamed first and changed twice. It was correct throughout.
  The owner said so, twice, and was right both times. A styling rule that looks
  correct and does not apply is a *scope* question, not a *specification*
  question - check where the element actually sits before touching the CSS.
- The 110-open / 112-close imbalance had been noticed earlier in the session
  and dismissed as pre-existing and harmless. It was neither.

`57_verify_template_scope.bat` now counts the tags on all ten equity screens
and names the file and line where the count first goes negative. A line-by-line
counter is not enough on its own: a `<div>` whose attributes wrap across lines
is invisible to a per-line scan, which produced four false alarms before the
counter was moved to the whole file.

## A harness must call the thing that talks to the broker

Three times in one session a check passed while the code was wrong. The
failures look different and are the same failure.

1. **A stub that returned where the real function raises.** `_owned_watchlist`
   raises on an unknown id; the stub returned None. The test went green and the
   live screen said "Watch list not found".
2. **A deletion proof that broke the wrong function.** The proof that a check
   bites is to delete the guard and watch it fail. The guard's line -
   `if len(candidates) != 1:` - exists in TWO functions in
   `equity_fill_reconciler.py`, a `str.replace(..., 1)` hit the first one, and
   the check reported a clean all-clear on an untouched code path.
3. **A suite that tested every piece around the call and never the call.** The
   fired-GTT fix had 27 checks over `_adopt_fired_gtts`, `_released_after`,
   `_has_resting_gtt` and `_extract_gtt_rows`. `_read_active_triggers` - the
   only one of the five that posts to a broker - had none. It sent the request
   with an empty body, OpenAlgo answered *"apikey: Missing data for required
   field"*, and the whole feature did nothing at all while the suite stayed
   green.

**The rule that comes out of it.** In a group of new functions, the one that
crosses a boundary - a broker call, a database write, a request - is the one
most worth a test and the one most likely to be skipped, because it is the one
that needs a fake to test at all. Write that test first. A fake client that
RECORDS what it was asked and lets the test assert on the endpoint and the
payload costs ten lines and catches the whole class.

And two mechanical habits that would have caught two of the three:

- **Assert the count before replacing**, in test scaffolding as strictly as in
  the code itself. `assert s.count(old) == 1` is not a formality.
- **Every deletion proof must show its failure**, and the failure must name the
  check you expected to break. A deletion that changes nothing visible has not
  proved the check bites; it has proved you deleted the wrong thing.

## `_make_request` posts the payload verbatim

`ExtendedOpenAlgoAPI._make_request(endpoint, payload)` is AlgoMirror's own
override. It posts `payload` exactly as given and **adds nothing** - not the api
key, not anything else. The SDK's wrapped methods (`orderbook`, `tradebook`)
build their own payloads and so carry the key; a raw endpoint posted through
`_make_request` does not.

Every raw endpoint in this module therefore passes it itself:

```
{'apikey': api_key, ...}
```

`placegttorder`, `modifygttorder`, `cancelgttorder`, `gttorderbook` and `ping`
all do. An endpoint called without it comes back HTTP 400 with
*"apikey: Missing data for required field"*, which - because a refused read is
correctly treated as "could not ask" - fails silently and completely.

## The apps being up is not the brokers being authenticated

`00_START_EVERYTHING.bat` reports the three ports listening. That says the
processes are running; it says nothing about whether either broker session is
alive. Indian brokers force a fresh login each trading day, and once that
session lapses OpenAlgo cannot resolve the api key: every read comes back
`HTTP 403: Invalid openalgo apikey` from an instance that is plainly up.

So the morning sequence is three steps, not one: start everything, log in at
`localhost:5000` AND `localhost:5001`, then `40_preflight_check.bat` - because
Analyzer Mode survives neither a restart nor a reconnect, and preflight is the
only thing that states in plain words whether an order can reach real money.

## Order-time levels and armed levels are different things

A stop loss appears in two places and they are not the same number.

- **On the ORDER** (`EquityOrder.stop_loss`) - what was asked for when the order
  was placed. The Order Book and the Trade Book show this, because they are
  lists of instructions.
- **On the HOLDING** (`EquityHolding.stop_loss`) - what is actually ARMED.
  Positions and Holdings show this, and **the exit monitor acts on it and
  nothing else.**

Only the second one can sell your shares. A screen showing the first is
describing an intention; a screen showing the second is describing a live
instruction to the machine.

**The rule, settled 6 September: the newest instruction wins.** When a BUY
fills, its stop loss and target become the armed levels on every account that
filled. A holding can hold exactly one stop and one target, so something has to
win, and the oldest buy winning - which is what happened until this date - is
indefensible: an admin naming a level on a new buy of a stock he already holds
is stating his view of the STOCK, not of those particular shares.

What the rule refuses to do, each for its own reason:

- **A SELL arms nothing.** It takes shares out; it is not a view on what is
  left.
- **A buy naming NO level does not clear the levels already there.** Silence is
  not an instruction to disarm.
- **A buy naming only a stop leaves the target alone**, and the reverse.
- **An account whose leg was rejected is not moved.** It bought nothing.
- **It fires on the FIRST transition into a filled state only.** The function
  runs wherever a split changes, which is often. Re-applying on every pass would
  overwrite a level the admin had since set by hand on the Holdings screen. The
  first transition happens once.
- **Moving a level RE-ARMS it** through `EquityHolding.clear_breach`. A level
  that has already fired stays silent for good otherwise - so a stop moved after
  it fired would never fire again at its new price.

The remaining edge, stated rather than solved: place an order, edit the level by
hand before it fills, and the order's level wins when it does. The window is
small and the order carrying an explicit level is the stronger signal, but it is
not free of doubt.

## A repair script must prove it changed only what it said

`fix_armed_levels.py` writes to the live database. Three habits made that
defensible and they are the pattern for any repair script here:

1. **Preview every row before writing**, with the old value, the new value, and
   the reason - the order id and date that justify it.
2. **Back up first**, timestamped, beside the database.
3. **Re-run the detection after writing** and report failure if anything is
   still out of step. A script that says "3 updated" without checking has not
   verified anything.

And to test it here: run it against a COPY of the owner's real database, then
diff the copy against its own backup column by column. That proved only
`stop_loss` and `target` moved and that `equity_orders`, `equity_order_splits`
and `equity_trades` were byte-identical - which no amount of reading the script
could have proved. Running it twice proved it is a no-op the second time.

Note on reading the database from here: AlgoMirror runs SQLite in WAL mode, so
`algomirror.db` alone can be STALE by hours. Stage `algomirror.db-wal` with it
or the answer is silently wrong - which happened once while confirming this very
repair, and the old values looked like a failed write.

## One look per module - the owner's rule, 6 September

> "Equity and F&O module can have different layout. But inside each module
> layout and usage should be uniform wherever possible."

Two rules, and the second is the one that gets broken.

**Across modules, difference is allowed.** F&O and Equity may look unlike each
other. They are different products with different screens, and forcing them
together buys nothing.

**Inside a module, one thing is done ONE way.** One word for one action, one
colour for one meaning, one shape for one kind of panel. A reader who has
learned a screen should not have to re-learn it on the next one.

What this caught when it was applied:

- Holdings said **"Export CSV" in green**; the Watch List said **"Download CSV"
  outlined**. Same action, different word AND different colour - and green on
  these screens already means *this commits something*, which downloading a copy
  of what is on screen does not. All four screens now read Download CSV,
  outlined.
- Five summary strips existed in two shapes. All five now close with the same
  rule.

**Uniformity is checkable, so check it.** `verify_book_exports.py` walks EVERY
equity template and fails if any download button uses a different word or
colour, or any summary strip is missing its closing rule. A convention nobody
enforces is a convention that lasts until the next screen.

Note the direction of the fix: the odd one out was changed to match the many,
not the other way round. When there is no majority, the rule is whichever
choice already carries meaning elsewhere - here, outlined, because green was
already spoken for.

## A figure on a tile is not a button

`.module-equity .equity-accent` paints a money figure in the module's violet -
the same colour as the column headings, so the two accents on an equity screen
are one colour rather than two.

Deliberately NOT `text-primary`. This theme's `--p` is GREEN, and green on these
screens means "this commits something". A total commits nothing. Reaching for
the theme's primary because it is the obvious class would have put a button
colour on a number.

## Green Buy, red Sell, hollow everything else

The colour convention, stated once because it was applied inconsistently three
times in one day:

- **Green** - Buy. Or, on a dialog, the button that actually sends.
- **Red** - Sell.
- **Hollow** - opens a panel, a dialog or another screen. Commits nothing.

The subtle one is a button that OPENS a commit rather than being one. Cover Now
on Positions was briefly made red. It is wrong twice: pressing it only opens a
dialog, and the button inside that dialog is what sends the order; and Cover Now
is a BUY - buying back a short - so red said the opposite of what it does, next
to a red Sell that meant something else.

**Buy and Sell keep their colours even when they are links.** On the Watch List,
Holdings and Positions they navigate to Place Order rather than sending
anything, and they are still green and red. The colour names the ACTION the user
is heading towards; hollow is for buttons that reveal rather than lead.

`verify_positions.py` reads every equity screen and fails if any Buy or Sell
wears a different colour anywhere in the module - not just on the screen it was
written for. Colour conventions drift one screen at a time, so the check has to
look at all of them.

## Place Order is the only screen that can reach a broker

Buy and Sell on the Watch List, Holdings and Positions are LINKS. They open
Place Order with the stock, side, quantity and accounts filled into the query
string, and place nothing.

That is a safety decision, not a convenience one, and it was put to the owner as
a choice on 6 September before Positions got its buttons. Place Order shows the
account split, checks the cash, and raises a confirmation restating the stock,
the side and the quantity. A second screen that could send an order would be a
second way to sell the same shares twice - and a position bought today has NO
holding row yet, so the claim-before-sell guard that prevents a double sale does
not cover it at all.

A SHORT offers no Sell. The obligation on a short is to BUY the shares back,
which is what Cover Now does.

## A sortable table with action buttons: set the index before you sort

`_row_index` on a Positions row is set BEFORE the rows are sorted, so it keeps
pointing at the row in the array the actions look up. Cover Now and View Split
find their row by that index.

Sort first and number afterwards and every button on the screen fires at
whatever now sits in that position - Cover Now buying back a different short
than the one whose row was pressed. The serial number in the first column is the
opposite: it is computed from the POSITION after sorting, because a serial that
did not renumber when you sorted would be describing an order nobody is looking
at.

Two numbers on the same row that must be derived at opposite ends of the same
operation. Worth testing directly, which `verify_positions.py` does.


## A table that repaints is no place for a control

Holdings carried a stop loss box, a target box, an exit mode picker and a trade
nature picker in every row, with a Save Levels button beside them. On
6 September the owner asked for all of it to go, to the Watch List's pattern:
values in the table, an Edit button, a dialog.

The reason is not tidiness. This table repaints every thirty seconds with new
prices. A control living in a repainting row can be nudged by accident and then
redrawn under the hand that nudged it - and what these particular boxes arm is
what the background monitor uses to SELL SHARES. The Watch List had reached the
same conclusion for its own trade nature and target price and said so in its
code: *"a control in every row of a list that repaints every ten seconds is a
control that can be changed by accident."* The same sentence was true here and
the stakes were higher.

The screen used to defend itself with a focus guard: a background poll skipped
the redraw while any box in the table had focus. That guard came out with the
boxes. It was a workaround for a design fault, and it also meant a table could
sit stale for as long as a cursor rested in it.

**Removing the controls also let four columns start sorting.** Stop Loss,
Target, Exit Mode and Trade Nature were not sortable while they held widgets.
They are values now, so they sort like everything else - which is what made the
next decision possible.

## The trade-nature bands came off Holdings

The rows used to be banded under a grey divider per trade nature: SWING, ETF,
Long Term. A sort ran WITHIN each band rather than across the table, on the
argument that "my biggest loser in Long Term" was a more useful answer than one
mixed list.

The owner removed them on 6 September and the argument does not survive the
change above. Trade Nature is a column, and a column that now sorts. The bands
were saying a second time what the column already says, while costing the table
the one ordering that runs from top to bottom - so "my biggest loser" could
never be answered at all. The Trade Nature filter is still there for anyone who
wants one nature on its own.

The server still sends `groups` and `grouped`. Nothing reads them. They were
left in place rather than removed, because the CSV export and the API share the
builder and this was a template change, not a server one.

## One dialog, with the warning inside it, instead of two

Saving a level used to be two presses: Save Levels on the row gathered what the
boxes held and opened a confirmation dialog, which then had its own Save Levels
button. The confirmation carried the important sentence - what Auto Sell means
as against To Confirm, and which accounts it applies to.

The Edit dialog now carries that warning itself, above its own Save, and
re-writes it the moment the exit mode picker changes. So the warning is read on
the way past rather than after the fact, there is one press instead of two, and
the dialog can never describe the mode that was showing a moment ago.

**Validation happens before anything is disabled or closed.** A stop loss at or
above the target, a negative figure, a target that is not a number: the dialog
stays open with the figures still in it and says why. The old flow could only
refuse before the confirmation opened, because by then the boxes it read from
were in the table behind it.

Editing one account on its own survives: Accounts on a row, then Edit on that
account's line, arms that account alone. The trade nature picker is NOT offered
there. The nature belongs to the stock rather than to one member's parcel of it,
and an account-scoped save carries that account's own stored nature through
untouched - falling through to null would silently clear a tag while somebody
was only moving a stop loss.


## A table that repaints is no place for a control (6 September)

Recorded above. Holdings lost its stop loss box, target box, exit mode picker
and trade nature picker, and gained an Edit dialog, because this table repaints
every thirty seconds and what those boxes arm can SELL SHARES.

## The upload: built in a morning, removed in an afternoon

The owner asked for an Upload CSV on Holdings to match the Watch List's. It was
built: by stock, applied to every account holding it, changing stop loss,
target, exit mode, trade nature and the note; preview then apply; the apply
re-planning from the file and refusing if the holdings had moved; the levels
going through the same function the Edit dialog uses so the re-arm rule could
not diverge.

He then used it once, asked what concerns it carried, and removed it on the
answer. The answer was three points:

1. **Blank means clear, and Excel is very good at making blanks.** Deleting a
   column's contents to mean "leave this alone" clears every stop loss in the
   file. The rule is the opposite — delete the whole column — and it depends on
   the preview being read.
2. **A stale file silently reverts recent work.** Monday's file uploaded on
   Friday puts Monday's levels back. Never guarded.
3. **One press changes everything at once, with no undo on the server.**

**The decision worth keeping is not "no uploads".** It is that a bulk path is
only as safe as its worst accident, and the worst accident here was an
unrecoverable field cleared a hundred times. Notes are versioned; levels are
not. If a bulk level path is built again, version the level first.

The planner is still in `routes.py`, marked NOT REACHABLE with the reasoning,
so reversing costs two route functions. The routes themselves are deleted, so
the feature cannot be reached by a stale tab.

**What was kept from it.** The Holdings download gained the three note columns
and a better column order — the key, then AlgoMirror's own side of the stock,
then the figures. Worth having in a report whatever else changed. And
`_write_holding_levels` stayed factored out, because one function that writes
and re-arms a level is right even with one caller.

## A warning that fires on ordinary edits stops being read

The upload's red banner first warned about three things: Auto Sell being armed,
a level being cleared, and a note being written. The owner cut it to Auto Sell
alone and he was right.

A banner that appears on every ordinary edit is a banner the reader learns to
click past, and then it does not work on the day it matters. Auto Sell was the
only thing in a file that SOLD SHARES without asking again.

Nothing was lost by cutting it: a cleared level still reads in red on its own
row, and a cleared note still says so there. **The row says what happens; the
banner says what is dangerous.** That division is worth keeping wherever this
module warns about anything.

## The Updated line: movement, not the clock

Every equity screen carries "Updated ...". It was nine screens each writing
their own version, drifting; it is now one shared file,
`app/static/js/equity_updated.js`, loaded by ten.

It reads `Updated 06 Sep 2026, 12:47:05` — the date as well as the time,
because a screen left open overnight showed a time with no day against it and
read as current. No IST suffix: the whole application is Indian.

**The colour was first written from the clock** — a weekday between 09:15 and
15:30 — and the owner replaced it the same day with the better rule: if an LTP
on the screen has changed in the last two minutes, the market is trading.

That is evidence rather than a guess, and it is right for the case a clock is
wrong about: an exchange holiday. On a holiday nothing moves, and nothing
moving is exactly what amber means. It costs no server call and no new field —
the screen hands over the prices it just drew as one string, and the script
remembers whether that string changed.

Three states, and the third is the honest one: GREEN a price changed within two
minutes; AMBER two minutes with nothing moving; GREY read once so far, because
one reading is not a change. A screen with no prices on it passes nothing and
gets no colour, because a colour there would be a guess.

The two minutes are measured between two SERVER timestamps, never against the
browser's clock. The month name comes from a fixed table rather than Intl,
which renders September as "Sept" in some engines and "Sep" in others.

## Var Qty: the plan, less the fact

The per-account panel on Holdings gained Var Qty and Var Qty %. The plan is the
SAME split Place Order would make — the whole holding divided by each account's
Order Qty Ratio, rounded the same way — so the two screens can never disagree
about what a ratio means.

**An account holding NONE of the stock is listed**, with a quantity of zero and
the whole plan as its variance. That is the case the column exists to show and
it could not appear otherwise. Those lines offer Buy and nothing else: there is
nothing to sell and nothing to arm a level on.

**The percentage uses `signed_percent_of`, not `percent_of`.** `percent_of`
clamps a negative numerator to 0.0, so an account holding MORE than its ratio
calls for would have read as exactly on plan — the one answer that column must
never give. Caught by the harness before it shipped, and there is now a check
that fails if it is ever written the other way.

## Est. Costs is a download-only figure

Off the table, off the per-account panel and off the Net P&L tile, at the
owner's instruction. Still a column and a total in the CSV.

The tile now reads Gross only, and "After estimated exit costs" under Net P&L
says what the difference is without printing it. "Est. Costs Formula" on the
Settings screen is untouched: that card is where the rates are configured, not
a figure about the holdings.

## One stock, one home: a filter, not a state

The owner's rule, 6 September. A stock you HOLD does not also sit on a watch
list. Its stop loss, target, exit mode, trade nature and note are changed on
Holdings, one stock at a time; when the last share is sold it goes back.

**The watch list row is hidden, never deleted.** Holdings are read, the held
symbol keys are collected, and rows matching them are dropped at read time. So
the row comes back with its list, its trade nature, its target price and its
note exactly as they were.

**Given up.** A held stock cannot be edited in bulk from the Watch List upload,
which is the point: the bulk path over an unrecoverable field is the hazard
that removed the Holdings upload the same afternoon.

**Rejected: a `hidden` flag on the watch list row.** It would have needed
writing on every buy and clearing on every sale, from every path that can
create a holding — including a broker-side purchase AlgoMirror only discovers
later. A filter computed from the holdings cannot fall out of step with them,
because there is nothing to keep in step. Most of this feature therefore
carries no state at all.

The rule holds on the write paths too, or it would not be a rule: adding a held
stock by hand is refused and says why, and a watch list upload naming one skips
that row and reports it. The bulk path is not a way round.

## The price alert is cleared when the stock is held, not when it is sold

The owner's instruction, and it is the stronger of the two possible moments.

Clearing at hold-time closes the firing window: nothing can fire from a row
nobody can see. Clearing at sale-time would leave a live alert armed on an
invisible row for as long as the stock is held — the exact shape of a
notification the owner cannot trace to anything on screen.

Done in the alert monitor rather than in the buy path. The monitor is the one
place every alert must pass through to fire, so a purchase route that nobody
remembered still cannot leave an alert armed. `alert_price`,
`alert_direction`, `price_alert_enabled`, `alert_triggered_at` and
`alert_triggered_price` are all cleared together; a partial clear would leave a
row that looks fired.

**Given up.** The old alert level is gone, not parked. When the stock returns
to the watch list the box is empty and a new level is set deliberately —
which is what the owner asked for. An alert set months ago against a price the
stock has since doubled through is not a level anyone would have chosen.

**A read that fails returns an empty set.** If the holdings cannot be read, no
symbol is treated as held, so alerts stay armed. The failure mode of this
feature is an alert that fires when it need not, never one that is silently
disarmed.

## The one stored column, and the single case it exists for

`equity_holdings.returned_to_watchlist` — a shared-file edit to `models.py`,
flagged to the owner and approved, additive, migration 026, re-runnable.

Everything above is a read-time filter. This column is the one case a filter
cannot cover: a stock BOUGHT that was never on a watch list has no hidden row
to unhide, so a row must be CREATED when the last share is sold. Creation is
not idempotent. Without a marker the row would be recomputed on every refresh,
so a row the owner deleted would reappear within seconds — and there would be
no way to tell "never returned" from "returned, then deleted on purpose".

**Rejected: inferring it from the note or the watch list itself.** Both are
things the owner edits. A flag the owner can change by accident is not a flag.

`downgrade` deliberately leaves the column in place. Dropping it would make
every returned row eligible for re-creation on the next sale, which is a worse
outcome than a column nobody reads.

## The Alert column says what the levels are DOING

The owner's items 3 and 7. Holdings had a stop loss and a target and no way to
tell, at a glance, whether either was armed, breached, or waiting on him.

Five states, computed in one function so the chips and the cells can never
disagree: **SL Triggered** (red), **Target Reached** (green), **Active**,
**Paused**, **Not set**. They are tested in that order — a breach outranks an
arming state, because a breach is the thing that needs reading first.

Each cell also says what happens NEXT, which is the half a status word leaves
out: on Auto Sell the order has already gone, on To Confirm the holding is
parked and the cell names the card where that decision is made. A stock held
across accounts where only some breached reads "On 1 of 2 accounts" rather than
picking a side.

**The chips are counted BEFORE the filter is applied**, so a chip's number is
what that chip will give you. Counting after would make every unselected chip
read zero — a filter that erases its own way back.

**Two decisions taken on the owner's behalf, both told to him.** Approve and
Dismiss stay in the confirm card and are NOT repeated on the chip view: one
place makes a decision, and duplicating a commit action is how two paths drift
apart. And "Paused" is kept as his own word from the Watch List chips rather
than renamed to something more literal — the same state should not have two
names in one module.

## The Alert column came off, and the chips stayed

Built on 6 September as a column plus four chips; the column came off the same
day at the owner's instruction - "it does not add any value".

He was right, and the reason is worth keeping. The chips above the table say
the same five states AND filter to them. The column said them once per row and
filtered nothing, so it was the same information at fourteen times the width,
in the place where the row had least room for it.

**Given up.** The line that said what happens NEXT - "Waiting for you", "A sell
is in flight", "A breach sells immediately". Two of those three are still said
elsewhere on the screen: the confirm queue is the Exits Waiting On You card,
and the exit mode has a column of its own. The third, the auto-sell reading, is
gone.

**The state itself did not go anywhere.** One function computes it and the
chips read that function, so removing the column removed a view, not a fact.

## A chip you must be TOLD, not one you would ask for

Four chips are drawn always, even at zero: SL Triggered, Target Reached,
Active, Not Set. A fifth - Uncompleted, a level set with nothing watching it -
is drawn only when its count is not zero.

The rule that separates them: **the four are questions somebody might ask.**
"Which have no levels?" is worth being able to ask when the answer is none, so
a zero chip stays clickable. **Uncompleted is not a question anybody asks.** It
is a thing that needs to announce itself, and a chip permanently reading zero
is a chip nobody reads - so on the day it is not zero, it appears where nothing
was, which is the only way it gets noticed.

It is also kept on screen while it is the chip you are filtered TO, or the
filter could not be undone with the control that set it.

**"Not set" and "Uncompleted" are different states and neither is a rename of
the other.** Not set: no stop loss and no target, nothing armed, nothing
expected. Uncompleted: a level IS armed and cannot fire. Renaming one to the
other, which is what a literal reading of the instruction would have done,
would have put a wrong number on the chip.

**"Uncompleted" is the owner's word and it fits three of its four causes.** A
sell in flight, an exit gone indeterminate, a holding awaiting a decision - all
uncompleted. Every share pledged for margin is NOT: that stop loss is blocked,
not unfinished, and it stays that way for as long as the pledge does. He was
told, offered "Not Watched", and kept his word.

## The menu counts must count the way the chips count

Red for stop losses breached, green for targets reached, on the Holdings menu
item. Hidden at zero, red first.

The server counts them BY STOCK, not by account row, and lets a stop loss
outrank a target on the same stock. Both of those are what the chips do. If
they disagreed, the same fact would be quoted two ways on one screen and there
would be no way to tell which was wrong.

**They replaced a single red badge counting holdings awaiting a decision.** Not
a loss: such a holding has had a level breached, so it is still inside the red
count. Only the separation went, and it went into the red badge's hover text.
Three numbers on one menu line is not a menu line.

**Rejected: keeping all three.** Two red numbers side by side, and you would
have to remember which was which every time you looked.

## The screen script that three screens never got

`equity_alerts.js` paints every menu count and raises the price-alert popup. It
was a `<script>` tag pasted at the bottom of each equity screen - and Holdings,
Dashboard and Accounts never got one.

So on those three, no count was painted and **no price alert could appear on
screen.** It was recorded and logged; it never announced itself. True from the
day the counts were built until 6 September, and found only because two new
counts were put on the Holdings menu item - counts that could never have shown
while the owner was on Holdings.

**Now loaded once from `layout.html`**, guarded on `equity_module_active` so an
F&O page never loads it, with all seven pasted copies removed.

**The principle.** A thing that has to be remembered on every new screen will
be forgotten on some new screen. Per-screen includes look harmless because six
of them are correct; the seventh is invisible until somebody notices a
difference between two pages. Anything the whole module needs belongs to the
module, once.

**Given up.** The layout now carries an equity-only script tag, which is a
shared file carrying module-specific knowledge. The guard is the price of
that, and it is cheaper than the alternative.

## Restore fills the boxes; Save writes

Note History and Restore, 6 September. The versions had been recorded since
5 September and were already in the payload the dialog reads - only the way in
was missing.

**Restore does not write.** It puts the chosen version's text into the three
boxes, says in green that nothing has been saved, and leaves the writing to
Save.

**Why not write directly.** Exactly one place in the module copies the CURRENT
text into the history before overwriting it: the save path on the server. A
Restore that wrote directly would have needed its own copy of that rule - and a
rule written twice is a rule that will one day disagree with itself. With
Restore as a fill, there is nothing to disagree with, and restoring an old
version cannot lose the current one.

**Given up.** Two presses instead of one. That is also a reading step, which is
the right thing to spend a press on when the alternative overwrites prose
nobody can retype.

**Ten versions shown, at the owner's instruction, down from twenty-five. The
eleventh is out of sight, not deleted.** A record costs almost nothing to keep
and cannot be recovered once it stops being kept.

## One word for one thing, changed in one pass

"Member" became "Account" on nine headings across six screens, and
`equityMemberCell` became `equityAccountCell` on the three screens that had it.

**In one pass, deliberately.** A word changed on one screen and not the next is
worse than the word nobody liked, because then both are on screen at the same
time and the reader has to wonder whether they mean different things.

**The function was renamed with the heading.** A column headed Account drawn by
a function called Member is how the code and the screen start describing
different things, and the next person to read it believes the code.

**"The clearing member" was deliberately left alone.** It is the entity that
holds pledged shares, not an account, and renaming it would have made that
sentence untrue. A find-and-replace over the whole tree is exactly how it would
have gone, so there is a check that fails if it ever does.
