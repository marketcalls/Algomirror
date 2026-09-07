# Operations

## The setup on this machine

| Piece | Where | Port |
|---|---|---|
| AlgoMirror | `E:\Web App\AlgoMirror` | 8000 |
| OpenAlgo, account 1 | `E:\Web App\OpenAlgo` | 5000, WebSocket 8765 |
| OpenAlgo, account 2 | `E:\Web App\OpenAlgo2` | 5001, WebSocket 8766 |

Broker is `dhan_sandbox` with Analyzer Mode on. AlgoMirror runs on a signed
Python 3.12 from python.org, not on uv's build — see below.

## Starting and stopping

| Script | What it does |
|---|---|
| `00_START_EVERYTHING.bat` | Starts both OpenAlgo instances and AlgoMirror |
| `00_RESTART_ALGOMIRROR.bat` | Restarts AlgoMirror alone. Needed after any Python change |
| `22_check_all.bat` | Health check across all three |
| `40_preflight_check.bat` | Can an order reach real money right now? Run it first, every session |
| `41_fix_fill_times.bat` | One-off repair of fills stored with an IST timestamp. AlgoMirror closed |

**A template change needs a RESTART, not just a refresh.** Flask compiles a
template once and caches it; it only re-reads on change when auto-reload or
debug is on, and neither is set here. Learned the hard way on 1 September: three
rounds of "it should be visible now" were spent hard-refreshing a page the
server was rebuilding from a cached copy each time. Ctrl+F5 clears the browser,
not the server.

A change to a Python file needs a restart too. In practice: **restart for
everything.**

**A Tailwind class that is not in `compiled.css` renders as nothing.** The
stylesheet is built, not loaded from a CDN, so only utilities already used
somewhere in the app exist in it. `bg-primary/5` and `border-r-primary/30` are
not in the build; using them produced a change that measured *identical* to no
change at all — RGB 250,250,250 on both panels. Check a class against
`app/static/css/compiled.css` before relying on it, or write an explicit rule
with the theme's own `--p` / `--su` variables, which is what the module toggle
tint does.

## Settings that matter

**Equity Settings → Charges.** Brokerage, STT, exchange charges, GST, SEBI fees
and stamp duty per account. Until these are set, estimated cost is ₹0.00 and
the Net P&L column carries no information.

**Equity Settings → Order timeout.** How long to wait for a broker reply,
between 10 and 180 seconds. Currently 120 because sandbox replies have been slow.
**Put this back to about 30 before going live**, so a genuinely dead call is not
waited on for two minutes.

**Stop loss monitor interval.** How often each user's holdings are evaluated,
between 1 and 300 seconds, 30 by default.

## Simulated orders, real prices

Two switches decide what is real, and **they are independent**. Confusing them
costs days.

**Analyzer Mode** is OpenAlgo's execution switch. On, every order — and every
read of the order book, trade book, positions, holdings and funds — is routed to
OpenAlgo's own sandbox database, and nothing reaches the broker. Off, orders are
real. This is the only thing standing between a stray order and real money.

**The broker adapter** decides where prices come from, and Analyzer Mode has no
effect on it whatsoever. Market data is served by whichever adapter the account
is connected to, whether Analyzer Mode is on or off.

The `dhan_sandbox` adapter does not serve market data. Its own source says so:
depth returns five hardcoded rows of zeros, last traded quantity is zero,
previous close is zero, and there is no quote endpoint at all — the price is
*derived* from the most recent one-minute candle with synthetic noise added. Its
WebSocket adapter is a mock that generates prices with a random walk. Two
different mock paths produce two different numbers for the same stock on the
same screen, which is exactly what was seen.

**So `dhan_sandbox` plus Analyzer Mode is the worst pairing available**: neither
real execution nor real data. The combination that works is a **live broker
connection with Analyzer Mode on**. OpenAlgo's own user guide states the intent:
*"Market prices still come from the active broker data services."* Analyzer Mode
is an execution sandbox, not a market simulator.

The consequence worth knowing: OpenAlgo's sandbox fill engine prices its
simulated fills through the same quotes service that serves live quotes. It has
no pricing model of its own. So on a live connection with Analyzer Mode on,
simulated orders fill **at real market prices** — which is what makes the whole
flow worth testing.

### What this changes about risk

On `dhan_sandbox` the safety net is physical: the credentials point at a
different host, so even a bug could not reach real money. On a live connection
the net is a **setting**. Analyzer Mode being on is the only thing between a
stray order and the real account.

That is why `40_preflight_check.bat` exists. Run it at the start of every
session and after any OpenAlgo restart. It reports, per account, whether
Analyzer Mode is on, and whether the prices answering are real — and it decides
the second one by **asking for a quote and a depth book and looking at what
comes back**, not by reading a setting. A configuration string can be stale.
Zeros cannot lie. If it cannot establish Analyzer Mode, it reports the state as
unsafe rather than assuming.

## Reconnecting a broker CLEARS Analyzer Mode

Observed, not theorised: Analyzer Mode was on all morning, the Upstox broker
session was reconnected, and it came back **off** — on an instance now wired to
a live account. Any order placed in that window would have been real.

The switch resets at exactly the moment you are least likely to check it: right
after a reconfiguration, when your attention is on whether the connection
worked. Treat every reconnect, every restart and every credential change as
having turned Analyzer Mode off until `40_preflight_check.bat` says otherwise.

This is the whole reason the check exists and the reason it reports unknown as
unsafe rather than assuming.

## Where the sandbox differs from a real broker

Prices are the obvious one. **Settlement is the other, and it is easier to
mistake for a bug.**

A real broker reduces a holding the moment a delivery sale fills — sell one of
eighty and it reports seventy-nine immediately, alongside a -1 line in the
position book marked as sold from the portfolio. **OpenAlgo's sandbox settles
holdings on T+1 instead**, so after a simulated sale the holding keeps showing
the pre-sale quantity for the rest of the day.

AlgoMirror reports whatever the broker reports, so in the sandbox a sold holding
will appear to linger. That is the simulator's settlement model, not a fault
here, and it corrects itself overnight. Do not judge holdings behaviour after a
sale on the sandbox.

## Reading the books after market hours

Outside 9:15 to 15:30 IST a live feed still answers, but it answers with a
closed market. Expect last traded price to equal previous close, one side of the
book to read zero, and nothing to move. That is a real feed reporting a shut
exchange, not a broken one. `40_preflight_check.bat` judges data real on
previous close and bid being non-zero, which survives the close; depth alone
would not.

A stopped price is convenient for testing an exit: set a level the wrong side of
the last trade and it breaches on the next tick instead of making you wait for
the market to come to you.

## Two things that will waste your afternoon

**A setting and a session are different things.** `REDIRECT_URL` decides which
broker you can *log in as*. The stored `auth` record decides which adapter
*serves your market data*. They can disagree — the header badge can read `dhan`
while every quote still comes from the sandbox, because the broker session was
never re-established. After changing broker settings you must **Reconnect
Broker**, or the data keeps coming from wherever you last logged in.

**Settings are read once, at process start.** Editing `.env`, or saving through
the Profile page, changes nothing until that OpenAlgo instance is restarted.

A corollary worth knowing: when a broker session is broken, OpenAlgo may not let
you reach the Profile page you need in order to fix it. Editing `.env` in a text
editor and restarting is the way out of that loop.

## Reading a Dhan connection failure

`HTTP 401` on generate-consent means Dhan rejected the credentials before your
login page ever appeared. The failure is in `log/errors.jsonl`. The three things
that produce it, in order of likelihood:

- The **Client ID** is wrong. It is the numeric UCC from Dhan's Profile & Account
  Details, and it goes on the **left** of the `:::` separator. Anything
  non-numeric there is the bug.
- The API key or secret is wrong, or they have been swapped.
- The key has been revoked.

The banner reading *"Dhan requires an active Data API subscription"* on the
broker selection screen is **static**. It appears for every Dhan user and is not
a check on your account.

## Resetting to a clean state

Resetting OpenAlgo's sandbox alone is **not enough**, for reasons in
`04-BOOKS.md` and `06-EXTERNAL-BROKER-ACTIVITY.md`: the Order Book and Trade Book
read AlgoMirror's own tables, and a holding row survives at zero quantity
carrying its levels and tags.

The order is:

1. Close AlgoMirror.
2. Run `38_reset_equity_history.bat` and type `RESET`. It backs the database up
   to a timestamped file first, clears orders, splits, fills and tracked
   holdings, and keeps watch lists, alerts, trade natures, allocations,
   brokerage rates, settings, accounts and API keys.
3. Reset the sandbox in **both** OpenAlgo instances, 5000 and 5001. Missing one
   leaves that account's books half full.
4. Start everything.
5. Set the brokerage rates before placing anything.

Once the books read from the broker as planned in `04-BOOKS.md`, step 2 becomes
optional tidying rather than a requirement.

## Two environment lessons worth keeping

**Signed Python.** Windows Smart App Control blocks unsigned `.pyd` files. uv's
python-build-standalone is unsigned, so `_ssl` and other extensions were blocked
and AlgoMirror could not make HTTPS calls. The fix was to rebuild the virtual
environment on a signed python.org 3.12. Disabling Smart App Control is a
one-way switch and is not an acceptable fix.

**Tailwind purging.** `compiled.css` only contains classes it can see in the
templates. A class that exists only inside a JavaScript string is purged and the
element renders unstyled. Anything injected from JavaScript must carry its own
CSS.

## The numbered scripts

Every operation is a numbered `.bat` in `E:\Web App\`, run by double-clicking.
Each explains what it will do before it does it, writes a report file beside
itself, and waits for a keypress. Scripts that change anything take a backup
first and never delete a file — a superseded file is renamed, not removed.

| Script | What it does | AlgoMirror |
|---|---|---|
| `38_reset_equity_history.bat` | Clears AlgoMirror's orders, fills and holdings. Only alongside an OpenAlgo sandbox reset | must be closed |
| `39_add_holding_notices_db.bat` | Adds the holding-notice table and the external-quantity column | must be closed |
| `40_preflight_check.bat` | Reports Analyzer Mode and whether prices are real, per account | either |
| `41_fix_fill_times.bat` | Corrects fills stored with the broker's IST timestamp in a UTC column | must be closed |

**Step 41 in particular.** Fills recorded before 1 September took the broker's
timestamp exactly as it arrived, so they sit five and a half hours ahead — and
the screen, converting UTC to IST for display, adds the same again. The recorder
was fixed at the point of entry (D13), so nothing new is wrong; this repairs what
was already written. It compares each fill against the moment AlgoMirror placed
the order behind it, which is genuine UTC, and keeps the nearer of the two
readings. That makes it safe to run twice: a row it has corrected is then the
nearer reading and is left alone. Run on 1 September it corrected four fills.

## Rotating a broker API key

A key is rotated because it was exposed, or on a schedule. The whole procedure
turns on one rule.

**Revoke last.** The old key stays valid until the final step, so until then
every failure is reversible: put the old key back and nothing is lost. After
revocation, every system holding that key string stops working at the same
instant.

That rule was broken on 1 September and it is recorded here rather than tidied
away. Account 1's OpenAlgo instance and the live F&O connection `live-fno` shared one
Dhan key. Account 1 was moved to a new key and the old one revoked in the same
breath — and `live-fno` went down with it, with the next trading session the
following morning. Nothing was lost, but the outage was avoidable and the
sequence below exists to avoid it.

**Before starting.** Market closed. No open F&O position and no resting order.
The new key and secret go into a password manager — never a screenshot, which is
how the 31 August key was exposed in the first place. Establish **every** system
that holds the key: `live-fno`, each OpenAlgo instance, and any separate project
using the same broker's data.

1. **Generate the replacement** in the broker's API panel. Give it a name
   clearly different from the old one, so the wrong row cannot be revoked at the
   end.
2. **Move the lowest-stakes system first** — an instance running in Analyzer
   Mode, where a mistake costs nothing. Restart it (environment settings are
   read once at process start), reconnect the broker session, and put Analyzer
   Mode back ON, because reconnecting clears it.
3. **Move `live-fno` next**, and after reconnecting confirm Analyzer Mode there reads
   **OFF**. On the live F&O connection it must stay off.
4. **Verify both** — funds, positions and the order book load. Place no test
   order. Run `40_preflight_check.bat`.
5. **Only now revoke the old key.** Check the name twice.
6. **Reload everything once more.** A failure at this point means something else
   was quietly using that key, and tonight is a better time to learn it than at
   the open.

**Prefer one key per system.** A shared credential is what turns a routine
rotation into a coupled outage. Separate keys make step 5 harmless.

## The log silently drops `%`-style messages

AlgoMirror writes its log through `pythonjsonlogger.JsonFormatter`
(`app/__init__.py`). Only messages that are **already a finished string** come
out. A lazily formatted call is dropped with no error, no warning and no gap in
the file:

    current_app.logger.info(f'[X] value {value}')       # appears
    current_app.logger.info('[X] value %s', value)      # VANISHES

Every logging call in AlgoMirror's own code already uses f-strings, so nothing
in the product was ever affected. It matters only when someone adds new
diagnostics — which is exactly when a silent log is most expensive.

**On 2 September this cost most of a morning.** Six rounds of instrumentation
were added to find out why Today's P&L read n/a. Every one used `%`-style
arguments, so every one vanished, and the silence was read as evidence three
separate times:

1. that the previous close was never fetched,
2. that a broker read was blocking inside the account fan-out,
3. that the restart script was leaving a stale process on port 8000.

All three were wrong. The dashboard had been building correctly, in about a
second, the entire time.

Two rules follow.

**Write diagnostics as f-strings.** Same as the rest of the file.

**Prove the instrument before trusting the reading.** The mistake was not the
formatter; it was treating "the log is silent" as a fact about the application
before establishing that the log could speak at all. What broke the deadlock
was a build stamp in the message text (`BUILD 6`), which distinguishes "this
line is not running" from "this line is running and not being recorded" in one
read. Put a stamp on any new diagnostic that has to survive a restart.

When logging itself is the suspect, bypass it — a plain `open(path, 'a')`
append with no framework in the path settles the question immediately. That is
what finally showed the request completing normally end to end.

## Checking a template: loading it is not running it

A template's JavaScript can be verified three ways, and only the third catches
the fault that actually reaches the screen.

1. **`node --check`** on the script blocks, Jinja stripped. Proves it parses.
   Catches a stray bracket. Catches nothing else.
2. **Executing the file** against a stub DOM. Proves it LOADS - which caught a
   temporal-dead-zone bug that syntax checking could not see, where a `let`
   above the `const` it referenced killed the whole page.
3. **Calling the render function** against a realistic payload. Proves it RUNS.

The gap between 2 and 3 cost an afternoon on 4 September 2026. A new section on
the Holdings screen called `EquityFormat.signedPercent`, which exists on
Positions and the Trade Book but had never been defined on Holdings. The file
parsed. The file loaded. Nothing called the function until a browser did, and
then it threw - part way through the render, after the new section had drawn
its heading and before the holdings table had drawn a single row. The screen
sat on *"Loading holdings."* with a generic "live data unavailable" banner and
no indication of the real cause.

**Two rules follow.**

**Call the function you just wrote, with data shaped like the real thing** -
including the awkward cases: a null price, a null percentage, one account and
several. A stub DOM plus `eval` of the script blocks is enough; the harness
needs `getElementById` to return the same object each time so the code under
test can find what it appended.

**A supplementary section must not be able to take the main table down with
it.** Each independent block of a render gets its own try/catch. The holdings
table is what that page is for; a panel underneath it is not worth losing it
over.

A useful check on any such fix: delete the fix, re-run the harness, and confirm
it fails. A test that has never failed has not been shown to test anything.

## Today's P&L, and why an empty account made it n/a

The dashboard KPI takes `all()` across the account cards. A card marked its
Today's P&L "known" only after a row supplied a previous close, so an account
holding **nothing** reported it as unknown — and one empty account took the
whole strip to n/a no matter how well the other account was priced.

The same flag hid a worse bug. Because it was set by the *first* row with a
previous close while rows without one silently added zero, a card holding three
stocks with one priced would publish a Today's P&L covering that single row and
present it as complete. The figure would have been short, with nothing on the
screen to say so.

The flag now starts **true** and is falsified by any row that cannot be measured
against yesterday. Nothing is unknown about an account that holds nothing: its
Today's P&L is zero, and it is zero for certain.
