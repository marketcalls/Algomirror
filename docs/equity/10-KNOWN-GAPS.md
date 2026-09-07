# Known Gaps

An honest list. Anything here is either not built, built wrongly, or risky. It
is ordered by consequence, not by effort.

## 1. The background monitor sold on an unverified quantity — FIXED

**What it was.** Arming a level and pressing Sell both refreshed the holding
quantity from the broker first. The background exit monitor did not: on a breach
in Auto Sell mode it claimed and sold using whatever quantity was last stored.

**What it would have done.** Sell 60 of 100 shares at your broker terminal, and
if the stop loss fired before any screen refreshed that row, AlgoMirror would
send a sell for 100 against a holding of 40.

**Fixed.** Verification now sits inside the single helper every equity sell goes
through, so no path can skip it. A shortfall resizes the sell; an unreadable
broker withholds it and records why. Rule 1 in
`06-EXTERNAL-BROKER-ACTIVITY.md` has the detail.

**Proven live, 1 September.** A TARGET breach in Auto Sell mode placed exits on
both accounts three seconds later, with no `equity_auto_exit_withheld` — the
verification ran against the real broker, got an answer, and passed.

## 2. External activity — detected, and now told — FIXED

Quantity drift is detected against AlgoMirror's own net filled position, a
holding reaching zero has its levels and breach markers cleared, and shares
returning are treated as a new position. Rules 2 to 5 in
`06-EXTERNAL-BROKER-ACTIVITY.md`.

Notices now travel the same feed as watch list price alerts: the same pop-up,
the same navigation badge, the same log. Each row carries a `source` so the two
are told apart, and the browser keys its de-duplication on source **and** id —
the two tables number their rows independently, so an id alone would let a
price alert and a holding notice hide one another.

**Proven live, 1 September.** At 11:27:42 the drift detector caught ten shares
returning after an exit and raised a notice per account. Both reached the Alerts
log, sitting alongside a price alert that fired at 11:59, with neither hiding
the other. That is the arrangement the source-keyed de-duplication exists for,
against real data rather than a fixture.

**Still owed:** the falling-count case. OpenAlgo's sandbox does not settle
holdings until T+1, so a share count cannot be made to drop there on the same
day. It needs a real broker or a settled day.

## 4. The books read from the broker — FIXED

Today's Order Book and Trade Book merge each account's live broker book with the
stored record. An order placed at your terminal now appears, marked **Outside
AlgoMirror**; a stored order the broker has no record of is marked **NOT AT
BROKER** rather than left looking live.

An account that cannot be read is reported as unverified and never treated as
empty — the case that would otherwise tell you your orders had vanished.

**Proven live, 1 September.** Orders placed on OpenAlgo's own screen appeared
badged *Outside AlgoMirror* (T3), and with port 5001 closed the unreadable
account was reported while nothing was marked NOT AT BROKER (T4).

**A gap found doing it.** Neither book said on screen that an account could not
be read: the server put `unverified_accounts` in the payload and both templates
ignored it, so the Order Book showed an incomplete picture while looking
complete. Both now carry a banner naming the account. **Written, not yet seen
firing.**

## 5. The exit monitor — PROVEN LIVE, 1 September

Both modes exercised on real prices, both accounts, in one morning.

**To Confirm**, 09:15:02 — TARGET breach recorded on both holdings, price 1286.0
through level 1260.0. The holdings parked in the confirm queue. **Nothing was
sold.**

**Auto Sell**, 09:44:55 — TARGET breach at 1298.0 through 1290.0. **09:44:58,
exits placed on both.** Three seconds, and no withheld exit, so the quantity
verification reached the broker and was satisfied.

**What this exposed, and what was done about it.** Between the two tests the
mode was switched to Auto Sell on a holding still parked in the confirm queue,
and nothing happened — correctly, because the monitor only evaluates rows whose
status is ACTIVE, but *silently*, which is the part that was wrong. The Holdings
row now says so: a parked holding carries a line reading that it is not being
watched and that changing the mode will not act until the decision is made. The
same line covers a sell already in flight, which removes a row from the monitor
for the same reason.

## 5a. An exit claim could never close — FIXED

**What it was.** Nothing in the codebase called `mark_exit_completed`. A sell
was claimed, submitted and filled, the fill appeared in the Trade Book — and the
holding stayed EXIT_SUBMITTED for ever. From there it is invisible to the exit
monitor, its quantity is never reduced, and the holdings sync will not correct
it either, because that sync refuses to touch a row with an exit in flight.

**What it looked like.** A holding sitting at its pre-sale quantity with a sale
that had visibly completed, and a row that could never be exited again.

**Fixed, and proven live 1 September.** Driven from the holding rather than the
split, for the reason recorded in D14. Both stranded RELIANCE rows closed at
11:22:41.

## 5b. A rejected exit left the holding claimed — FIXED

**What it was.** The close fired only when the sell filled. A sell the broker
*rejected* left the claim open, and that holding stayed out of the monitor's set
until someone cleared it by hand.

**Fixed.** A sell that was rejected or cancelled **with nothing filled** returns
the holding to ACTIVE, carrying the broker's own reason on the row.

**The case that needed care.** A cancellation or rejection *after* part of the
sell filled is a settlement, not a release — those shares are gone. Releasing it
would restore the full quantity and lose them. Anything with a fill therefore
goes to the settle and shrinks the row; only a sell with nothing filled is
released. Ten cancelled at four leaves six.

**Breach markers are left set on purpose**, so the monitor does not immediately
re-place a sell the broker has just refused. Re-arming is the admin's decision,
which is D2 applied to the exit path.

## 5c. A second sale is possible while the broker still reports the shares — NAMED

**What.** Every sell is sized against the broker's own quantity, which is right.
A broker whose holdings lag will keep reporting shares that have already gone,
and a second sell would then be permitted — correctly, on the information
available.

**Not possible against a real broker.** Dhan reduces the holding the moment a
delivery sale goes through, so the verification comes back at the reduced figure.
OpenAlgo's sandbox settles on T+1 and lags for the rest of the day.

**What was done.** The Holdings row now says what AlgoMirror sold today when the
broker is still reporting the shares. It blocks nothing and adjusts nothing: the
quantity stays exactly as the broker reports it, because the broker owns what is
held. See D15, and D10a for why netting was refused.

## 5d. Equity accounts appear on the F&O screens — OPEN, by decision

**What.** ALL FIVE F&O data screens list the equity accounts — Funds, Order
Book, Trade Book, Positions and Holdings. Each calls `get_selected_accounts()`,
which returns every active account, and the account record carries no segment
marker, so each module sees the other's. Symmetrical: an F&O account registered
here would show up on the equity screens the same way.

**It is a side effect of the equity work, and it cannot be undone from the
equity side.** Nothing in F&O changed; adding two accounts is what made them
visible there. Removing them from F&O's view means changing F&O.

**Consequence, today: none that is live.** The F&O the product owner trades runs
on a separate deployment which does not hold these accounts. On this instance
the F&O module is the same code with no F&O accounts, so the bleed is visual.

**Not fixed, by instruction.** Raised twice on 1 September and declined both
times: *"We are building equity module now. This should not affect F & O
module."* F&O is operational and under a standing rule not to be changed, and
the only fix touches it.

**The fix, when it is wanted:** a segment on the account, set once, with each
module filtering on it. `01-ARCHITECTURE.md` has the detail and says why
filtering on product instead would be the wrong shape.

## 5e. GTT — a fired trigger is invisible (FIXED), and a failed modify is saved anyway — PART HELD

**Observed live, 4 September 2026.** A BUY GTT on HDFCBANK, trigger 703 rising,
limit 702, 40 shares on Account 1 and 20 on Account 2, placed 2 September at 21:51
and accepted by both accounts.

At 11:02 on 4 September the price crossed 703 and **both GTTs fired.** Each
released an ordinary LIMIT BUY at 702 — orders 26090421489282 and
26090402117664 — which are still open and unfilled, the stock having moved on to
713. At 11:17 the owner tried to change the GTT to trigger 712.85 / limit 712.50
and it failed: a GTT that has already fired cannot be modified.

Three separate faults, in two systems.

**(a) AlgoMirror never learns that a GTT fired.** Reconciliation matches on
broker ORDER ids. A GTT split carries only a trigger id, and the order the
trigger releases has a brand new order id that AlgoMirror has never seen. The
reconciler skips any split with no `broker_order_id`, and adoption is restricted
to INDETERMINATE splits inside a 15 minute window, so a GTT never qualifies.
The split therefore sits at PENDING for ever while the order it created lives a
life of its own. The shares do eventually appear through the holdings drift
detector, so nothing is lost — but the Order Book reads wrong in the meantime.

**Correction, 5 September - the note that used to sit here was wrong.** It said
OpenAlgo's `gttorderbook` endpoint "would resolve a trigger id to its released
order id and close this". It does not, and saying so made the fix sound like
one call away when it is not. The evidence, read rather than assumed:

- `sandbox/gtt_manager.py`, `list_gtts(status_filter="active")` - the default,
  and what the service calls. **A fired trigger is not in the book at all.**
- `_serialize()` in the same file returns trigger id, type, status, symbol,
  exchange, trigger prices, last price, legs, timestamps, strategy and margin.
  **There is no released-order id in it.** The link exists in the sandbox's own
  table - `sandbox_gtt_legs.triggered_order_id` - and no API exposes it.

So the endpoint answers a different question than the one that needed asking.
What it CAN say is negative and still useful: a trigger id absent from the
active book has **stopped resting**. It fired, or was cancelled, or expired.

**Seen a second time, 5 September 2026.** A BUY GTT on WOCKPHARMA, trigger 2000
rising, limit 2090, 386 shares on Account 1 and 214 on Account 2, placed 17:09.
It fired the same minute. Account 1's leg released order 26090520718913 for 386
shares, which completed. AlgoMirror still shows the GTT as PENDING, and the
order it released appears in the day's list badged *Outside AlgoMirror* -
AlgoMirror reporting its own trade as a stranger's.

**The AlgoMirror-only fix, designed and not built.** The reconciler already
adopts an order that lost its reference: it matches an unclaimed broker order on
stock, exchange, side, quantity and time and adopts it **only when exactly one
candidate fits**. A fired GTT is the same shape of problem. Extending that to a
split holding a trigger id but no order id would close both halves - the stale
PENDING and the phantom row - without touching OpenAlgo.

Two guards make it safe enough to be worth building:

1. **Adopt only after the trigger has left the active GTT book.** While a GTT is
   still resting nothing can be adopted at all, so a manual buy placed at the
   terminal during that time cannot be captured.
2. **The child order must be timestamped at or after the GTT was placed**, and
   still exactly one candidate, or none is taken.

Residual risk, stated plainly: a GTT fires AND the owner separately places an
order in the same stock, same side, same exact quantity, and that order is also
unclaimed. Then either two candidates survive and nothing is adopted, which is
the safe outcome, or only the manual order survives and it is wrongly attributed
to the GTT. Unlikely, given the quantity is an allocation split rather than a
round number - Account 1's leg was 386 - but not impossible.

**What WAS fixed, step 60, 5 September.** The GTT row no longer wears a red
NOT AT BROKER badge. That badge came from looking for a TRIGGER id among ORDER
ids and not finding it, which proves nothing: a GTT is held in the broker's GTT
book, not its order book. A leg carrying a trigger id is now excluded from that
judgement; a leg carrying neither is still reported, because that one is true.
The badge was a separate falsehood layered on top of this gap, and it is gone.
The gap itself is untouched.

**(b) A failed modify was written to AlgoMirror anyway — FIXED, step 63,
6 September.** `modify_order` wrote the new price, trigger and quantities onto
the parent order after the broker calls returned, without testing whether any of
them had succeeded. Both accounts refused the 4 September modify and AlgoMirror
still showed 712.85 / 712.50 — a price no broker has ever held, on an order
still live at the old one, for days.

The parent is now written only when at least one account accepted. A PARTIAL is
written: one account holding the new price is a real change, and the accounts
that refused carry their own `error_message`, which is where a per-account
failure belongs. The per-split writes were always correct — only the parent was
written blind.

Fixed when it was, because step 63 put Modify on the Order Book as well as
Order Status, and a fault that shows a false price is not something to make
reachable from a second screen.

**(c) OpenAlgo returns HTTP 500 instead of the refusal.** In Analyzer Mode a
refused GTT modify goes down the failure path in
`services/modify_gtt_order_service.py`, which publishes `GTTModifyFailedEvent`
with an `exchange` keyword the event class does not accept. The publish raises,
the endpoint's catch-all turns it into *"An unexpected error occurred"*, and the
real reason never reaches the caller. **This is OpenAlgo, not AlgoMirror.**
Nothing was changed there. Raised with the owner on 4 September; he is taking it
up with the OpenAlgo side, who may not have completed GTT support.

A related one seen the same morning: at 11:01:51 the sandbox GTT manager
rejected the first trigger attempt with *Symbol HDFCBANK not found on NSE*, then
succeeded on a retry twenty seconds later.

**Status: (a) and (b) FIXED, (c) HELD.** (c) is OpenAlgo's and nothing has been
changed there. The visible consequence of (c) is only a poor error message on a
refused GTT modify: AlgoMirror no longer SAVES the refused value, which was the
part that mattered.

**(a) BUILT, 5 September, step 61** - the owner saw the two tables side by side,
one saying COMPLETED and the other PENDING about the same instruction, and asked
for it. The design above is what was built: adopt only after the trigger has
LEFT the active GTT book, only an order timestamped at or after the GTT was
placed, only when exactly one candidate survives, and nothing at all when the
GTT book cannot be read. One further change was needed that the design had
missed - the seven day chase cutoff would have hidden any GTT that fired later
than a week after placement, so a leg carrying a trigger id is now exempt from
it.

**(b) and (c) remain HELD.** Originally re-parked 5 September with (a) declined;
reopened and built the same evening. The right home for (a) is OpenAlgo: return the released order id on the
GTT book, and let the book optionally include fired triggers. Until then the
Order Book will keep showing a fired GTT as pending and its released order as
*Outside AlgoMirror*. **Nothing is lost** - the shares still arrive through the
holdings drift detector - but the Order Book reads wrong in the meantime, and
that is now a known and accepted state rather than a surprise.

## 5f. A same-day sale is announced as shares arriving from nowhere — HELD

**Observed live, 4 September 2026, 12:16.** LT was sold through AlgoMirror,
67 shares on Account 1 and 33 on Account 2, both filled. Forty seconds later the
drift detector raised two notices: *"LT went from 0 to 67 shares. 67 arrived
without an order from AlgoMirror."* Both were false. GOLDBEES did the same a
minute later.

**What happened.** The detector works out *external = the broker's count minus
what AlgoMirror's own orders explain*. After the sale AlgoMirror's net for LT is
zero - bought 100 on 2 September, sold 100 today. OpenAlgo's sandbox does not
remove sold shares from its holdings book until T+1, so it still answered 67.
Zero from sixty-seven leaves 67 shares that could not be accounted for, and the
detector reported them as having arrived.

Checked at 12:48, half an hour after the sale: sandbox **holdings** still LT 67
and GOLDBEES 333; sandbox **positions** LT -67 and GOLDBEES -333. Both true at
once, and that is how settlement works.

The wording compounds it. The sentence quotes the ROW's quantity ("from 0 to
67") while the share count comes from the external figure. The row read zero
because AlgoMirror wrote it down after the sale and the lagging broker book then
put it back, so even that half describes bookkeeping rather than shares.

**The fix is small and the ingredient already exists.** `_algomirror_sold_today`
is computed for the Holdings screen and says exactly what AlgoMirror sold today
per account and stock. `_reconcile_external_quantity` never consults it. Asking
it first - and absorbing a difference fully explained by our own same-day
trading - closes this.

**Not sandbox-only.** A live broker drops the shares on fill, but not
instantly. Read the holdings inside that window and the same false notice
appears.

**Why it matters more than a wrong message.** These notices share a channel with
stop loss and target alerts: same pop-up, same badge, same log. A channel that
cries wolf on every sale is a channel that stops being read, and the message
eventually ignored will be a real one.

**Status: HELD** at the owner's instruction, to be addressed during the live
check and full-flow run.

## 6. Order timeout is at 120 seconds — MEDIUM

**What.** Raised from 30 to survive slow sandbox replies.

**Consequence.** In live trading a dead call is waited on for two minutes.

**Fix.** Put it back to about 30 before the live switch.

## 7. Estimated costs are zero — FIXED, 1 September

Rates are entered for both accounts and Est. Costs now carries information.

**Two things settled while entering them.** GST applies to the service charges —
brokerage, exchange transaction charge, SEBI turnover fee and the DP charge —
and not to STT or stamp duty, which are taxes in their own right. DP/AMC is
entered **net of GST**; the formula adds it. Leaving the DP charge out of the
base had put Est. Costs about ₹2.43 under the real figure on every sell, per
scrip — small, but wrong in the same direction every time.

**Checked and corrected.** The exchange transaction rate had been entered as
0.000297 on one account against 0.00297 on the other — a statutory charge that
does not vary by broker, so one of them was out by a factor of ten. The product
owner confirmed **0.00297%** for both and the rates were re-saved effective
1 September.

## 8. Fills identical in every field collapse into one — LOW, documented

**What.** OpenAlgo's trade book carries no trade id, so fills are de-duplicated
on a fingerprint of order id, timestamp, quantity and price.

**Consequence.** Two genuinely separate fills identical in all four are recorded
once.

**Fix.** None available without a trade id from the broker. Stated rather than
hidden.

## 9. Leftovers to tidy — LOW

- Allocation rules are still computed for a template that no longer renders them.
- `.venv-old-py313` can be removed after a few clean sessions.
- The `CE` / `AE` exit-mode tag is still produced server-side for the CSV export
  although the screen no longer shows it.

## 10. Broker timestamps were stored as UTC without being converted — FIXED

**What it was.** A broker's unmarked IST timestamp was written straight into a
UTC column. Every screen then converted UTC to IST for display and added five
and a half hours on top, so a fill at 09:44 read 15:14.

**Fixed** at the point of entry, for recorded fills and for the external order
and trade rows alike. D13 has the rule.

**Residue cleared.** `41_fix_fill_times.bat` repaired the four fills already on
file, on 1 September, using the same rule in reverse. Safe to re-run: a corrected
row is then the nearer reading and is left alone.

## 11. A fired price alert logged nothing — FIXED

**What it was.** The exit monitor logs every breach. The alert monitor logged
nothing when an alert fired, so it could only ever be confirmed from a screen
somebody happened to be watching. The durable record in `equity_alert_events`
existed; the operational trace did not.

**Fixed.** The level and the price at firing are both logged.

## Corrected: the UTC "today" window is not a bug

An earlier note in this project claimed the Order Book's "today" window was
broken because it is computed in UTC while orders arrive in IST. On re-reading
the code, that is wrong. NSE hours of 9:15 to 15:30 IST fall between 03:45 and
10:00 UTC on the same calendar date, so a trading day never straddles the
boundary. Only an order placed between midnight and 5:30 a.m. IST would land on
the previous UTC date, and the market is shut then. Recorded here so the wrong
claim is not inherited by a future reader.


## 12. The Watch List upload: a stale file silently reverts recent work — OPEN

**Found on 6 September**, while the owner was reviewing the Holdings upload
that was built and removed the same day. It applies to the WATCH LIST upload,
which is still live.

**What it is.** A file downloaded on Monday and uploaded on Friday carries
Monday's values. Applying it writes Monday's targets, alert prices, alert
states and notes over anything changed on the screen in between. Nothing warns
about it, because nothing knows how old the file is.

The import fingerprint does NOT cover this. It guards the preview against the
apply — if the list moved in the seconds between the two, the apply refuses —
and it says nothing about the age of the FILE.

**Why it is worth fixing and was not fixed today.** It is one line in the
download and one check in the preview: put the download timestamp in the file,
read it back, and say *"this file is 4 days old; anything changed since will be
put back."* It was offered to the owner and is his call; the review moved on.

**What it can and cannot cost.** A reverted target price or a re-armed alert.
Notes are versioned — every write keeps the previous text, an upload's included
— so reverted prose is recoverable. Nothing here can sell anything: a watch
list alert alerts and never places an order.

## 13. Blank clears, and Excel is very good at making blanks — NAMED, by design

The rule on every upload in this module: a column that is NOT in the file
leaves that field alone; a column that IS there and blank CLEARS it.

That is the right rule and it is deliberate — without it a field could never be
emptied from a file — but it is the sharpest edge in the feature. Deleting a
column's CONTENTS to mean "do not touch this" clears every value in it. The
preview names each such row and the summary counts them, so it is guarded by
reading rather than by refusing.

Recorded rather than changed, because the alternative (blank means leave alone)
makes clearing impossible and would surprise a different person just as badly.

## 14. A cleared stop loss cannot be recovered — NAMED

Notes keep versions: every write to `equity_stock_notes` writes the previous
text to `equity_stock_note_versions` first, whether it came from the dialog or
from an upload. So a note cleared by accident can be read back.

Levels keep nothing. A stop loss or target that is cleared — from the Edit
dialog, or by any future bulk path — leaves no record of what it was. This is
why the Holdings upload was removed on 6 September rather than tightened: the
blank-clears rule and an unrecoverable field are a bad pair when one press
covers a hundred rows.

If a bulk level path is ever built again, versioning the level is the guard
that makes it safe, and it should come first.

## 15. Note History exists in the database and nowhere on screen — FIXED, step 85

**BUILT on 6 September**, hours after it was written up here. A History button
inside the Notes dialog, one row per version with its time and its first words,
and Restore on each row.

Restore fills the boxes and does not write; Save is the only button on that
dialog that writes, and Save is the one path that copies the current text into
the history first. So a restore cannot lose what it replaced.

Ten versions shown, at the owner's instruction, down from twenty-five. The rest
stay on disk and are never deleted.

The original entry follows, because the half that mattered had been working all
along and that is worth remembering.

### As written that morning

Agreed with the owner on 6 September as point 3 of his "one stock, one home"
design: *all previous note contents are saved in the background and can be
restored to the previous status on request.*

**Half of it is built and has been since 5 September.** Every write to
`equity_stock_notes` copies the previous text into
`equity_stock_note_versions` first, whichever path made the change. So nothing
has been lost and nothing is being lost now.

**The missing half is the way in.** There is no History button inside the Notes
dialog and no Restore. The versions accumulate where only a database client can
read them.

What it needs when it is built: a History button in the Notes dialog listing
the versions with their timestamps, and a Restore that **saves the current text
as a version first** — restoring is itself a write, and a restore that
overwrites without versioning is the same accident it is meant to undo.

This is owed to the owner, not merely noticed. It matters more now than it did
before 6 September, because a note is now edited from one screen only and the
Holdings upload that would have carried a bad note across a hundred rows is
gone — so the realistic loss is one person overwriting one note by hand, which
is exactly the case Restore covers.

## 16. Cancel is solid red and opens a dialog — RECORDED, the owner's call

The module's button convention, walked screen by screen on 6 September: a
button that ACTS carries its colour; a button that opens a dialog is hollow,
and the colour goes on the dialog's confirm.

Cancel on the Order Book and on Place Order is solid red and opens a dialog. By
the rule it should be outlined red, the way Delete on the Watch List is.

Left as it is, deliberately. Making the one destructive row action less
prominent than it is today is a judgement about how visible a cancel should be,
not a styling slip, and it is the owner's to make. Everything else in the
module now follows the rule — no amber button is left anywhere — so this is the
single known exception rather than a loose end among many.

## 17. Three equity screens never loaded the alerts script — FIXED, step 84

**Found by the owner on 6 September**, reported as "the Watch List badge
disappears when I open Holdings".

**What it was.** `equity_alerts.js` paints every count in the menu and raises
the price-alert popup. It was a script tag pasted at the bottom of each equity
screen, and three screens never got one: **Holdings, Dashboard and Accounts.**

**What it did.** On those three, no menu count was ever painted - so the count
did not disappear, it was never drawn. And, far worse, **a price alert firing
while the owner sat on one of those screens showed him nothing.** It was written
to the alert feed and to the log; it simply never announced itself.

**How long.** Since the counts were built. It surfaced only because step 83 put
two new counts on the Holdings menu item - counts that could never have appeared
while he was on Holdings.

**Fixed.** Loaded once from `layout.html`, guarded on `equity_module_active`,
seven pasted copies removed. A check fails if any screen pastes its own copy
back, because two copies would mean two pollers and two popups for one alert.

**What to take from it.** Per-screen includes hide their own gaps: six correct
copies make the seventh invisible. It was found by a user noticing a difference
between two pages, not by anything in the code saying so.

## 18. A stop loss cannot fire on a fully pledged holding — NAMED

The monitor evaluates a holding only when it is ACTIVE and has shares it can
actually sell. Pledged shares cannot be delivered, so a holding whose entire
quantity is pledged has nothing sellable and is skipped - **with its stop loss
and target still on the row, looking armed.**

This is correct behaviour: selling pledged shares would fail at the broker or,
worse, go through and leave the pledge short. It is listed here because the
consequence is not obvious and it can last for weeks, arriving from the broker
without anything being done inside AlgoMirror.

**It is now visible.** This is one of the four causes of the Uncompleted state,
and the Uncompleted chip appears on Holdings whenever the count is not zero. So
the state is announced rather than merely true.

**The word does not fit this case**, and the owner was told so: a sell in
flight or an exit awaiting a decision is "uncompleted", but a pledged holding is
BLOCKED. "Not Watched" was offered as the word covering all four causes. He kept
Uncompleted. Recorded so the mismatch is a decision and not an oversight.
