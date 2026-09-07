# Live Validation Checklist

**RESULT — 1 September 2026. All eight resolved in one morning, on real prices.**

| Test | Result |
|---|---|
| T1 breach that asks | **PASSED** 09:15:02 |
| T2 clear it down | **PASSED** |
| T3 external order appears | **PASSED** |
| T4 unreadable account | **PASSED**, with a display gap found and fixed |
| T5 shares that leave | **Not runnable** in the sandbox; mechanism proven in the arrival direction 11:27:42 |
| T6 breach that sells | **PASSED** 09:44:55 breach, 09:44:58 exits placed |
| T7 retirement | **PASSED** |
| T8 price alert | **PASSED** 11:59, level 1438, fired at 1437.65 |

`11-WHERE-WE-ARE.md` carries what each result exposed and what is still owed.
The checklist below is kept as written, because it is the procedure to repeat
after any significant change - not a record of one morning.

---

Eight tests. Each one exercises a path that has only ever been proven against a
fixture. Work through them in order — some consume the test holding, so the
order is not arbitrary.

**Before anything:** run `40_preflight_check.bat`. Both accounts must read
**Analyzer Mode ON** and market data **REAL**. Nothing below is safe to run
otherwise.

**Nothing here touches `live-fno` or your F&O session.** Every order is
simulated into OpenAlgo's own sandbox. The Dhan key rotation is *not* on this
list on purpose — it touches `live-fno` and belongs to a day with no open F&O
positions.

**The trick that makes most of this possible:** OpenAlgo has its own Trading
screen. An order placed *there* goes to the same sandbox but never passes
through AlgoMirror — which is precisely what an order placed at your broker
terminal looks like from AlgoMirror's side.

---

## T1 — A breach that asks, and sells nothing

**Needs:** the settled RELIANCE holding. Does not consume it.

On Holdings, set a **Target just below the current price** so it is already
breached. Leave Exit Mode on **To Confirm**. Save Levels.

**Right:** within about 30 seconds the holding moves to awaiting-confirmation
and an alert appears. **Nothing is sold.** Order Book gains no row.

**Wrong:** a sell appears. Stop and say so — that is To Confirm behaving as
Auto Sell, and it is the most serious thing that could be wrong here.

**Note:** before 9:15 nothing will happen at all. The monitor uses only pushed
prices and skips a symbol with no fresh tick, rather than judging a level
against a stale figure. That is correct, not broken.

---

## T2 — Clear it down

Dismiss the confirmation and clear the target.

**Right:** the holding returns to active, no order was ever placed.

---

## T3 — An order placed outside AlgoMirror appears

Open OpenAlgo instance 1 at `127.0.0.1:5000` → its own **Trading** screen.
Place a small **BUY**, any liquid stock, CNC.

Then open AlgoMirror's **Order Book**.

**Right:** the order appears, badged **Outside AlgoMirror**, with no trade
nature and no account split — because none were ever set. Its fill appears in
**Trade Book**, badged the same way, with the nature reading Unassigned.

**Wrong:** it does not appear at all. That is the books not reading the broker.

---

## T4 — An unreadable account says so

Close the OpenAlgo console window for **port 5001**. Load AlgoMirror's Order
Book.

**Right:** Account 1's orders still show. Account 2's account is reported as
unverified. **No order is marked NOT AT BROKER purely because its account was
unreachable.**

**Wrong:** existing orders start reading NOT AT BROKER. That would mean an
unreadable account is being treated as an empty one — the failure this design
exists to prevent.

Restart with `17_run_openalgo2.bat` afterwards, then run step 40 again:
**Analyzer Mode does not survive a restart.**

---

## T5 — Shares that leave without an order

From OpenAlgo instance 1's own Trading screen, **SELL a few RELIANCE shares** —
fewer than the holding. This never passes through AlgoMirror.

**Right:** within a refresh or two, a notice appears — in the pop-up at the
bottom right, on the Alerts badge, and in the Alerts log — reading something
like *"RELIANCE went from 10 to 7 shares. 3 left this account without an order
from AlgoMirror."* Holdings shows the corrected quantity.

**Wrong:** the quantity changes silently. That is the drift detection not
firing, and it is the whole point of yesterday's work.

---

## T6 — A breach that sells

**Consumes the holding.** Do this one last among the holding tests.

Arm a **Target below the current price** again, this time with Exit Mode on
**Auto Sell**. Save Levels.

**Right:** the monitor records the breach, verifies the quantity against the
broker, and places a sell for what is actually held — which after T5 is the
*reduced* number, not the original. The sell appears in Order Book and Trade
Book.

**Wrong, and important:** a sell for the pre-T5 quantity. That would mean the
verification added yesterday is not running, and it is the exact bug it was
built to prevent.

Also wrong: `equity_auto_exit_withheld` in the log. That means the broker could
not be read and the sale was deliberately withheld — safe, but I would want to
know why the read failed.

---

## T7 — A holding that goes to zero is retired

After T6 sells the position out.

**Right:** the holding's stop loss and target are cleared, and its breach
markers with them. The trade nature and exit mode survive as defaults.

**Wrong:** the old target is still sitting on the row. That would mean a level
from a closed position could govern shares bought months later.

---

## T8 — A price alert on a real price

Any time during market hours. On a watch list stock, set an alert a few paise
the wrong side of the live price.

**Right:** it fires within about ten seconds, pops up wherever you are in the
equity module, and disarms itself. The message names both the alert price and
the price at the moment it fired — those differ, and the difference matters.

---

## What is checked in the log afterwards

`[EQUITY_EXIT]` breach and dispatch lines for T1 and T6.
`[EQUITY_HOLDING]` for T5.
`[EQUITY_BOOKS]` warnings for T4 — silence there means the reads succeeded.
`equity_auto_exit_withheld` anywhere is worth investigating.

Until T1 and T6 have run, the only part of this module that can sell shares has
never been exercised. Everything else is secondary to those two.
