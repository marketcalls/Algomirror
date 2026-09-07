# Watch List Alerts

Price alerts on watch list stocks, watched by a background worker and delivered
whether or not the watch list is open. They are separate from holding exits:
an alert never sells anything.

## Watch lists — BUILT

A stock belongs to a named watch list. **Key Stocks** is the default; you can
create others and switch between them with the selector above the table. Adding
a stock searches the broker's symbol master through OpenAlgo, and the results are
filtered by instrument kind — Stock, ETF or Mutual Fund — and ranked so the
obvious answer sits at the top. Selecting one shows its current market price
before you commit, so a near-miss on the symbol is caught at the point of
choosing.

Removing a stock removes its alerts with it. It does not touch holdings or
orders, and it does not keep a price alert history for a stock you no longer
watch — a dialog that promised otherwise was promising something false.

## Alert directions — BUILT

| Direction | Fires when |
|---|---|
| At or below | The traded price reaches or falls under your figure — a stop-watch level |
| At or above | The traded price reaches or exceeds your figure — a target level |

## The monitor — BUILT

A background worker runs every 10 seconds and evaluates armed alerts against the
live feed, capped per pass so a large watch list cannot monopolise a tick. When
an alert fires it writes an event carrying the alert price and the price at the
moment it fired — the two are rarely identical and the difference matters.

An alert fires **once** and then disarms itself. Re-arm it from the Alerts
screen when you want it live again. Pausing an alert leaves it configured but
silent.

## Delivery — BUILT

Alerts arrive as a pop-up at the bottom right of the screen and as a count on the
**Alerts** item in the navigation, on **every** equity screen, not only the watch
list. As long as AlgoMirror is open they reach you.

When you open the equity module for the first time in a day, anything that fired
while you were away is delivered as a burst rather than being lost. WhatsApp
delivery is a later phase.

## The Alerts screen — BUILT

Two tabs. **Alerts** lists what is armed, with re-arm and pause. **Log** is the
history of what fired and when, with a seen marker so the badge clears.

## Holding notices share this feed

A share count that moves without an AlgoMirror order behind it — an emergency
sell from the broker's own app, most obviously — raises a **holding notice**.
It is not a price alert and it is not an error, but it arrives the same way: the
same pop-up at the bottom right, the same count on the navigation badge, the
same Log.

That is deliberate. The admin should have one place to look, not two.

In the Log a notice is marked with a badge — *Shares left*, *Shares arrived*,
*New holding*, *Holding closed* — and its sentence takes the width the alert
price and condition columns would have used, because with a notice the sentence
*is* the content.

`06-EXTERNAL-BROKER-ACTIVITY.md` has the rules that decide when one is raised.

## A note on sandbox prices

In OpenAlgo's analyzer mode the prices are simulated. An alert that fires at a
figure nothing like the real market is the sandbox behaving normally, not a
fault. Judge alert behaviour on the live feed.


---

# 2 September — what changed

## A fired alert switches itself off

The monitor now writes `price_alert_enabled = False` alongside
`alert_triggered_at` when a level is crossed.

It could never have fired twice either way — the timestamp is the de-duplication
guard — but the switch on the row read ON for an alert that would never speak
again. Off is the truth, and turning it back on is already the re-arm, so the
switch now means something.

Two consequences, both deliberate:

- **The row shows `Alert at 14:04:36 02/09/26`** — time then date, IST. Without
  the date, an alert from last Thursday read identically to one from this
  morning, and a fired row can sit unnoticed for days.
- **The display checks *fired* before *off*.** Testing "off" first would replace
  the one thing worth reading with the word Off.

## The switch is repainted by the poll

`equityWriteLiveCells` now corrects the switch as well as the text.

This was a real defect. The ten second poll rewrote only the price cells and the
alert sentence; the switch was written once, when the row was built, and never
again. So a fired alert updated its text to `Alert at …` while the switch beside
it still read ON — two halves of one cell disagreeing, because only one of them
was being refreshed. The switch is server state, not a local control, so the poll
owns it.

## The Edit dialog does NOT default the switch on

It was tried, and reverted the same afternoon.

Defaulting it on made re-arming one action instead of two. It also meant that
opening Edit on a row whose alert had been switched off ON PURPOSE, and saving
any unrelated field, switched it back on. An edit screen shows what is stored;
it does not decide. Add Stock keeps its default of on, which is right, because a
new row has no stored state to contradict.

## Stop loss and target breaches now reach this feed

Until today a breach was written to the activity log and, in CONFIRM mode,
parked in the Holdings confirm queue — **and nowhere else**. Someone on the Watch
List or the Dashboard when a stop loss was hit was told nothing, and found out
whenever they next opened Holdings. That is the wrong silence for the most
urgent event the module produces.

`EquityExitMonitor._raise_breach_notice` now writes an `EquityHoldingNotice` on
every recorded breach, in two new kinds:

| Kind | Badge on the Alerts log |
|---|---|
| `STOP_LOSS_HIT` | Stop loss hit |
| `TARGET_HIT` | Target hit |

The sentence says what was done as well as what happened — *"the exit order has
been placed automatically"* for AUTO, *"waiting for your confirmation on the
Holdings screen, nothing has been placed"* for CONFIRM.

Three notes on the implementation:

- **No schema change.** It rides in the existing holding-notice table, which the
  feed, the pop-up, the badge and the log already read. No migration, no `.bat`.
- **The level and the traded price are in the sentence, not in columns**, because
  the columns that exist hold share counts. A real limitation: they cannot be
  sorted or reformatted later. Two price columns are the obvious next step if
  breaches are ever charted.
- **It sits behind `record_breach`**, so it is written once per armed level, not
  once per tick, and it never raises — a notice that cannot be written must not
  stop an exit.

## The Holdings menu has its own badge

`/api/alerts/unread` now answers `confirm_pending` as well as `unread`, and
`equity_alerts.js` paints both badges from the one request.

The two counts answer different questions and are independent by design:

- **Alerts (amber)** — what fired and has not been read. Reading it clears it.
- **Holdings (red)** — holdings whose level was hit and that are waiting for a
  decision. Not time limited, unlike the alert counts: an unread alert stops
  being news, a breached holding waiting for an answer does not. It stays until
  the decision is actually made.
