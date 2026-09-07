# Equity Data Model

The equity module keeps its own set of database tables, all prefixed `equity_`, and does not share them with the F&O (futures and options) side of the application. Where the equity module needs information that already lives on an F&O table — a trading account's login details, its cached broker balances — it reads that table by its id and never adds columns to it or edits its rows. This separation is intentional and is called out repeatedly in the code: it exists so that building and changing the equity module can never break the live F&O module. The two systems currently share the same trading accounts and the same underlying database, but as far as data ownership goes, they are two separate applications.

## Equity Account Allocations

One row per trading account, holding the rupee amount that account has set aside for equity trading.

| Column | Type | What it holds |
|---|---|---|
| id | Integer (primary key) | Unique identifier for the allocation row. |
| account_id | Integer (foreign key to trading_accounts.id) | Which trading account this allocation belongs to. |
| user_id | Integer (foreign key to users.id) | Which user owns the account, so allocations can be filtered without joining through the account. |
| equity_fund_allocation | Float | The rupee amount of this account's funds earmarked for equity trading. |
| is_active | Boolean | Whether this allocation is currently in effect. |
| created_at | DateTime | When the allocation row was created. |
| updated_at | DateTime | When the allocation row was last changed. |

**Rules.** There is deliberately one allocation row per trading account (enforced by a unique constraint on `account_id`), rather than adding an allocation column onto the shared `trading_accounts` table — `trading_accounts` belongs to the live F&O module, and the equity module owns no columns on it at all, only a relationship back to it by id. The equity module is still allowed to refresh the broker payload cache fields that already exist on that shared row (`last_funds_data`, `last_holdings_data`, `last_data_update`), the same way the F&O blueprints do, but it must not let a holdings-only read make stale F&O cash data look fresh.

## Equity Trade Natures

An admin-defined tag describing why a trade is being taken, for example "Swing" or "Long Term".

| Column | Type | What it holds |
|---|---|---|
| id | Integer (primary key) | Unique identifier for the trade nature. |
| user_id | Integer (foreign key to users.id) | Which user this tag belongs to. |
| name | String(50) | The tag's label, for example "Swing" or "Long Term". |
| display_order | Integer | Controls the order the tags are listed in on screen. |
| is_active | Boolean | Whether the tag is currently offered for selection. |
| created_at | DateTime | When the tag was created. |
| updated_at | DateTime | When the tag was last changed. |

**Rules.** A user cannot create the same tag name twice (unique constraint on user and name). Every user is seeded with four defaults on first use: Swing, Short Term, Long Term, and Momentum, in that display order.

## Equity Watchlists

A named watch list of stocks. A user may keep several.

| Column | Type | What it holds |
|---|---|---|
| id | Integer (primary key) | Unique identifier for the watch list. |
| user_id | Integer (foreign key to users.id) | Which user owns this list. |
| name | String(60) | The list's display name. |
| is_default | Boolean | Whether this is the user's default list. |
| sort_order | Integer | Display order of the lists in the list selector; ties fall back to name. |
| created_at | DateTime | When the list was created. |
| updated_at | DateTime | When the list was last changed. |

**Rules.** A user cannot have two lists with the same name (unique constraint on user and name). Exactly one list per user should be marked default: it is the list the screen opens on, and the list a stock lands in when no list is named. Deleting the default list is refused, so a user can never be left with nowhere to put a stock. The default flag moves from one list to another only when a different list is explicitly made the default. The same stock is allowed to sit in more than one list at once, in the manner of Screener, which is why uniqueness on watch list items (below) is scoped to the list and not to the user.

## Equity Watchlist Items

One stock entry inside a watch list, with its own target price and its own price alert.

| Column | Type | What it holds |
|---|---|---|
| id | Integer (primary key) | Unique identifier for the entry. |
| user_id | Integer (foreign key to users.id) | Owner of the entry. |
| watchlist_id | Integer (foreign key to equity_watchlists.id) | Which watch list this entry sits in. |
| symbol | String(50) | The stock's trading symbol. |
| exchange | String(20), default 'NSE' | The exchange the symbol trades on. |
| trade_nature_id | Integer (foreign key to equity_trade_natures.id), optional | Which trade nature tag is attached to this entry, if any. |
| target_price | Float | The admin's target price for this stock, shown on screen. |
| alert_price | Float | The price level that should trigger a price alert. |
| price_alert_enabled | Boolean | Whether a price alert is active for this entry. |
| alert_direction | String(10) | Which way the price must cross `alert_price` to fire: 'ABOVE', 'BELOW', or NULL if no alert price is set. |
| alert_triggered_at | DateTime | When the alert last fired; NULL means the alert is armed and ready to fire again. |
| alert_triggered_price | Float | The price at which the alert last fired. |
| created_at | DateTime | When the entry was created. |
| updated_at | DateTime | When the entry was last changed. |

**Rules.** Uniqueness is on watch list, symbol, and exchange together — not on the user — so the same stock can appear in several of a user's lists, each with its own target price and its own alert. `alert_direction` is worked out and stored once, when the alert is saved, rather than guessed at the moment the alert fires, because an alert price alone is ambiguous: a stock at 100 with an alert at 110 needs 'ABOVE', the same stock with an alert at 90 needs 'BELOW'. `alert_triggered_at` is the alert's de-duplication guard: because the watch list refreshes its live price every few seconds, an alert is only allowed to fire while this column is NULL, and setting it is what marks the alert as delivered. Re-arming is explicit and must be done by the caller: any write that changes `alert_price`, `alert_direction`, or `price_alert_enabled` must also clear `alert_triggered_at` and `alert_triggered_price`, or the alert will stay silent forever.

## Equity Alert Events

A permanent record of one price alert that actually fired.

| Column | Type | What it holds |
|---|---|---|
| id | Integer (primary key) | Unique identifier for the event. |
| user_id | Integer (foreign key to users.id) | Owner of the event. |
| watchlist_item_id | Integer (foreign key to equity_watchlist_items.id, cascades on delete) | Which watch list entry raised this alert. |
| symbol | String(50) | The stock symbol, copied at the moment the alert fired. |
| exchange | String(20), default 'NSE' | The exchange, copied at the moment the alert fired. |
| alert_price | Float | The alert level that was crossed, copied at the moment the alert fired. |
| alert_direction | String(10) | 'ABOVE' or 'BELOW', copied at the moment the alert fired. |
| ltp | Float | The last traded price at the moment the alert fired. |
| message | String(255) | The human-readable alert text shown to the user. |
| created_at | DateTime | When the alert fired. |
| notified_at | DateTime | When the alert was shown to a screen; NULL means it has not been shown yet. |

**Rules.** This table exists because the background monitor evaluates alerts on its own schedule whether or not a browser is open, so a fired alert needs somewhere to wait until a screen is there to show it — and a later delivery channel (WhatsApp is named as an example) can read these same rows instead of needing its own alert store. The symbol, exchange, alert price, and alert direction are copied onto the event rather than read back through the watch list relationship, because an alert event is a statement about a specific moment and must keep reading correctly even if the underlying watch list entry is edited afterward. `notified_at` works exactly like `alert_triggered_at` on the watch list item: an event is offered to a screen only while it is NULL, and stamping it is what marks it shown, so a repeated poll cannot raise the same alert twice. Deleting a watch list entry deletes its alert events with it (cascade on delete) — removing a stock from a watch list is meant to remove the record of its alerts too.

## Equity Orders

The parent record of one admin order action. It is split into one Equity Order Split per participating trading account.

| Column | Type | What it holds |
|---|---|---|
| id | Integer (primary key) | Unique identifier for the parent order. |
| user_id | Integer (foreign key to users.id) | Who placed the order. |
| symbol | String(50) | The stock's trading symbol. |
| exchange | String(20), default 'NSE' | The exchange the symbol trades on. |
| side | String(10) | 'BUY' or 'SELL'. |
| order_type | String(20), default 'MARKET' | 'MARKET', 'LIMIT', or 'GTT' (good-till-triggered). |
| product | String(10), default 'CNC' | The broker product code; always CNC because the equity module is delivery only. |
| total_quantity | Integer | The total number of shares requested across all participating accounts. |
| price | Float | The limit price, used by LIMIT and GTT orders; NULL for MARKET. |
| trigger_price | Float | The GTT trigger level that activates the order; NULL for MARKET and LIMIT. |
| stop_loss | Float | The stop loss level to carry onto the resulting holding. |
| target | Float | The target level to carry onto the resulting holding. |
| trade_nature_id | Integer (foreign key to equity_trade_natures.id), optional | The trade nature tag to stamp onto the resulting holding when it fills. |
| source | String(20), default 'MANUAL' | What raised this order: 'MANUAL' (an admin action), 'STOP_LOSS', or 'TARGET' (raised by the monitor). |
| leftover_quantity | Integer | Shares that could not be given to any account because splitting rounds every account's share down to a whole share. |
| insufficient_funds_action | String(10), default 'SKIP' | The funds policy — SKIP or ABORT — that was in force when this order was placed. |
| status | String(20), default 'PENDING' | The order's overall status: PENDING, PARTIAL, COMPLETED, or CANCELLED. |
| placed_at | DateTime | When the admin submitted the order. |
| cancelled_at | DateTime | When the order was cancelled, if it was. |
| error_message | Text | A parent-level failure summary, for example why an ABORT-policy order placed nothing at all. |
| created_at | DateTime | When the row was created. |
| updated_at | DateTime | When the row was last changed. |

**Rules.** The parent order carries the instruction only and never carries a broker order id, because there is one broker order per account and those ids live on the splits. `status` is a roll-up of the splits: PARTIAL means some accounts got their order and some did not, which is a normal outcome and not an error. `leftover_quantity` is recorded once, at order time, from whatever whole-share rounding left over, and must never be recalculated afterward — Place Order shows it before submission and the order book shows it afterward, and the numbers must still add up when the order is reopened later. `insufficient_funds_action` is a snapshot of the Equity Setting in force when the order was placed, specifically so that changing the setting later can never rewrite the history of what this particular order actually did. Per-account error detail belongs on the split, not here.

## Equity Order Splits

One trading account's share of a parent equity order.

| Column | Type | What it holds |
|---|---|---|
| id | Integer (primary key) | Unique identifier for the split. |
| equity_order_id | Integer (foreign key to equity_orders.id) | The parent order this split belongs to. |
| account_id | Integer (foreign key to trading_accounts.id) | Which trading account this split was sent to. |
| qty_ratio_at_order | Float | This account's share of the total quantity, as a ratio, at order time. |
| quantity | Integer | The number of shares actually sent to the broker for this account. |
| est_value | Float | The estimated rupee value of this account's share, at order time. |
| cash_balance_at_order | Float | This account's cash balance at the moment the order was placed. |
| ratio_quantity | Integer | The quantity the ratio calculation produced before any admin override. |
| qty_overridden | Boolean | Whether the admin manually changed the quantity for this account away from the ratio result. |
| broker_order_id | String(100) | The order id the broker assigned, once known. |
| broker_gtt_id | String(100) | The GTT id assigned by the broker, kept separate from the resulting real order id. |
| fill_status | String(20), default 'PENDING' | This account's fill status: see Status vocabularies below. |
| filled_quantity | Integer | How many shares of this split have filled so far. |
| avg_fill_price | Float | The average price the filled shares executed at. |
| error_message | Text | A human-readable error for this account's leg, if there was one. |
| error_type | String(50) | The raw error category returned by the broker integration, for example 'timeout_error'. |
| broker_order_status | String(50) | The raw status string returned by the broker, for example 'open' or 'rejected'. |
| placed_at | DateTime | When this account's order actually reached the broker. |
| last_synced_at | DateTime | When this split was last refreshed from the broker. |
| attempt_count | Integer | How many times placement was attempted for this account. |
| created_at | DateTime | When the row was created. |
| updated_at | DateTime | When the row was last changed. |

**Rules.** Every snapshot value — `qty_ratio_at_order`, `est_value`, `cash_balance_at_order`, `ratio_quantity` — is captured once when the parent order was created and must never be recalculated later, so the order book always shows what was true at order time. `ratio_quantity` (what the ratio calculation produced) is kept separate from `quantity` (what was actually sent) because Place Order lets the admin override the quantity per account, and both numbers are worth keeping: one explains the default, the other is what was actually sent. `broker_gtt_id` is kept apart from `broker_order_id` because when a GTT triggers, the broker issues a brand-new order id for the resulting real order — overwriting the GTT id with it would lose the link back to the trigger that caused the trade. There is one split per account within a given parent order (unique constraint on order and account). `attempt_count` matters because a split that has gone INDETERMINATE must never show more than one attempt — it is never retried automatically. `error_type` is kept verbatim so a person reconciling the account later can tell exactly why a split ended up INDETERMINATE rather than trusting a status that was derived once and might be wrong. `placed_at` on the split is when this specific account's request reached the broker, which can differ across accounts even though they share one parent order — that difference is exactly what reconciliation is meant to read.

## Equity Trades

A single fill against an order split. One split can produce several trades when the broker fills it in parts.

| Column | Type | What it holds |
|---|---|---|
| id | Integer (primary key) | Unique identifier for the fill. |
| split_id | Integer (foreign key to equity_order_splits.id) | Which account split this fill belongs to. |
| execution_price | Float | The price this portion filled at. |
| executed_quantity | Integer | The number of shares filled in this event. |
| exchange | String(20) | The exchange the fill was reported on. |
| executed_at | DateTime | When the fill happened. |
| broker_trade_id | String(100) | The broker's own identifier for this fill, when it provides one. |
| created_at | DateTime | When the row was created. |
| updated_at | DateTime | When the row was last changed. |

**Rules.** Fills are discovered by polling the broker, and the same fill can come back on every poll. A unique index on split and broker trade id together is what stops a repeated poll from booking the same fill twice and inflating the reported quantity. It is implemented as a unique index rather than a table constraint on purpose, because SQLite cannot add a constraint to an existing table but can add a unique index — this keeps the object created by an in-place migration identical to the one produced by a fresh `db.create_all()` on both SQLite and PostgreSQL. Both databases allow repeated NULLs in a unique index, so a broker that returns no trade id still has its fills recorded; they simply are not de-duplicated by the database and must instead be matched by quantity and price.

## Equity Holdings

One account's delivery holding in one symbol, plus the AlgoMirror-specific configuration (trade nature, stop loss, target, exit mode) and exit-claim state that the broker itself does not store.

| Column | Type | What it holds |
|---|---|---|
| id | Integer (primary key) | Unique identifier for the holding. |
| user_id | Integer (foreign key to users.id) | Owner of the holding. |
| account_id | Integer (foreign key to trading_accounts.id) | Which trading account holds these shares. |
| symbol | String(50) | The stock's trading symbol. |
| exchange | String(20), default 'NSE' | The exchange the symbol trades on. |
| quantity | Integer | The number of shares currently held. |
| avg_cost | Float | The average purchase cost per share. |
| trade_nature_id | Integer (foreign key to equity_trade_natures.id), optional | The trade nature tag attached to this holding. |
| stop_loss | Float | The stop loss price level the monitor watches for this holding. |
| target | Float | The target price level the monitor watches for this holding. |
| exit_mode | String(10), default 'CONFIRM' | Whether a breached level exits automatically ('AUTO') or waits for admin approval ('CONFIRM'). |
| pledged_quantity | Integer | Shares of this holding pledged to the clearing member against a margin loan, and so not deliverable. |
| last_price | Float | The most recently known market price for this holding. |
| last_price_updated | DateTime | When `last_price` was last refreshed. |
| exit_status | String(20), default 'ACTIVE' | The holding's exit-claim state: see Status vocabularies below. |
| exit_reason | String(20) | Why an exit was started: STOP_LOSS, TARGET, or MANUAL. |
| exit_quantity | Integer | How many shares were claimed for the in-flight exit. |
| exit_claimed_at | DateTime | When the exit claim was taken. |
| exit_submitted_at | DateTime | When the broker accepted the exit order. |
| exit_completed_at | DateTime | When the exit was recorded as filled. |
| exit_broker_order_id | String(100) | The broker's order id for the sell currently in flight, if any. |
| exit_split_id | Integer (foreign key to equity_order_splits.id), optional | The order split carrying the in-flight sell, linking the holding into the order book and trade book. |
| exit_error | Text | The last exit failure or reconciliation note, kept even after a claim is reverted, so the Holdings screen can explain what happened last time. |
| sl_hit_at | DateTime | When the stop loss level was last breached; NULL means it has not fired and is armed. |
| sl_hit_price | Float | The price at which the stop loss was breached. |
| tp_hit_at | DateTime | When the target level was last breached; NULL means it has not fired and is armed. |
| tp_hit_price | Float | The price at which the target was breached. |
| last_monitored_at | DateTime | The last time the background monitor evaluated this row. |
| created_at | DateTime | When the row was created. |
| updated_at | DateTime | When the row was last changed. |

**Rules.** There is one holding row per account, symbol, and exchange (unique constraint). Sellable quantity is `quantity` minus `pledged_quantity` (never below zero): pledged shares are lying with the clearing member against a margin loan and cannot be delivered, so selling them would fail at the broker or, worse, succeed and leave the pledge short. The exit claim exists because two things can decide to sell the same shares — the background stop loss/target monitor and an admin pressing Sell — and without a claim both could read the same holding and both place a sell, leaving the account short shares it never owned. `exit_status` is that claim: nothing in the module may place an equity sell against a holding without going through the `claim_for_exit` transition first, which locks the row, re-checks it is still claimable and still carries no broker order id, sets EXIT_PENDING, and commits — the commit is the claim, because an uncommitted status change is invisible to any other worker. `exit_broker_order_id` is the second half of that claim: EXIT_PENDING with no order id can be safely reverted (`release_exit_claim`), but EXIT_PENDING with an order id must never be reverted, because that order is real. `mark_exit_submitted` writes the broker order id unconditionally, whatever the current status is, because losing an order id is described as the worst failure the module can have — the claim would later look releasable and the same shares could be sold twice. EXIT_INDETERMINATE is terminal for every automated path: the monitor skips it, `claim_for_exit` refuses it, and nothing retries it automatically, because the sell may already be live at the broker; only a human can clear it, through `resolve_exit_indeterminate`, after checking the broker's own order book. `mark_exit_completed` clears the exit-claim fields so the row can be exited again in future — the audit trail is not lost, it lives on the Equity Order, Equity Order Split, and Equity Trade rows instead. The breach fields (`sl_hit_at`/`sl_hit_price`, `tp_hit_at`/`tp_hit_price`) are deliberately **not** cleared when an exit completes: after a partial exit the price is usually still through the level, and clearing them would fire a second exit on the very next monitor tick; the admin must re-arm a level explicitly (via editing stop loss or target, which calls `clear_breach`) before it can fire again. For a CONFIRM-mode holding, a breach moves the holding from ACTIVE to AWAITING_CONFIRM rather than exiting it; for an AUTO-mode holding, the breach proceeds straight to `claim_for_exit`.

## Equity Brokerage Rates

Brokerage and statutory charge rates for one account, versioned by an effective date.

| Column | Type | What it holds |
|---|---|---|
| id | Integer (primary key) | Unique identifier for the rate row. |
| user_id | Integer (foreign key to users.id) | Owner of the account these rates apply to. |
| account_id | Integer (foreign key to trading_accounts.id) | Which trading account this rate version applies to. |
| broker_name | String(100) | The broker these rates were configured for. |
| brokerage_per_order | Float | The flat rupee charge levied per executed order. |
| stt_pct | Float | Securities Transaction Tax, as a percentage (0.1 means 0.1%). |
| exchange_txn_pct | Float | Exchange transaction charge, as a percentage. |
| sebi_pct | Float | SEBI turnover charge, as a percentage. |
| stamp_duty_pct | Float | Stamp duty, as a percentage. |
| gst_pct | Float | GST on brokerage and charges, as a percentage. |
| dp_amc_charge | Float | The flat rupee charge applied per delivery sell (DP charge) or annually (AMC). |
| effective_from | Date | The date from which this rate version applies. |
| is_active | Boolean | Whether this rate version is currently usable. |
| created_at | DateTime | When the row was created. |
| updated_at | DateTime | When the row was last changed. |

**Rules.** Cost changes must apply to future trades only, so a rate change is stored as a brand new row with a later `effective_from` — historical rows must never be edited in place, which is what keeps past cost calculations reproducible. There is one rate row per account per effective date (unique constraint on account and effective_from). `get_effective_rate()` is the one supported way to find the rate that applies on a given date: it picks the active row with the latest `effective_from` that is not after the date in question, and returns nothing (never raises) when no row applies yet — for example before any rate has been configured, or when every configured row starts in the future.

## Equity Settings

Module-wide equity preferences, one row per user.

| Column | Type | What it holds |
|---|---|---|
| id | Integer (primary key) | Unique identifier for the settings row. |
| user_id | Integer (foreign key to users.id, unique) | The user these settings belong to. |
| insufficient_funds_action | String(10), default 'SKIP' | The default funds policy applied to a new order: SKIP or ABORT. |
| default_exit_mode | String(10), default 'CONFIRM' | The exit mode given to a newly created holding: AUTO or CONFIRM. |
| sl_monitor_enabled | Boolean, default True | Whether the background stop loss/target monitor is switched on. |
| sl_monitor_interval_seconds | Integer, default 30 | How often, in seconds, the monitor re-checks holdings. |
| price_alerts_enabled | Boolean, default True | The master switch for watch list price alerts. |
| order_timeout_seconds | Integer, default 30 | How long, in seconds, to wait for the broker to answer an order write before treating it as indeterminate. |
| monitor_last_run_at | DateTime | The last time the background monitor actually ran. |
| monitor_last_error | Text | The last error the background monitor recorded. |
| created_at | DateTime | When the row was created. |
| updated_at | DateTime | When the row was last changed. |

**Rules.** There is exactly one settings row per user (unique constraint on user_id). This table is kept deliberately apart from Equity Brokerage Rates, which is per account and versioned by date, and apart from the shared F&O `trading_settings` table, which must not be disturbed by the equity module — this table only holds switches that change how the equity module behaves, not what a trade costs. Screens and background jobs should call `get_or_create(user_id)` rather than querying the table directly, so that a request or a scheduler job running before the row exists still gets sensible defaults; the method is safe to call concurrently, because if two callers race to create the row, the unique constraint rejects one of them and that caller simply re-reads the row the other one committed. `order_timeout_seconds` is configurable rather than hardcoded because the correct wait time depends entirely on what is on the other end of the connection — a live broker answers in a second or two, while a sandbox environment can take much longer, and every second shaved off the timeout turns a slow success into an order marked INDETERMINATE that a person then has to reconcile by hand. The model docstring records a real incident on 2026-08-30 where a 63-second placement was wrongly reported as failed while both orders had in fact gone through, which is the reasoning behind making this value tunable.

## Status vocabularies

### Parent order status (EquityOrder.status)

| Value | Meaning | Terminal? |
|---|---|---|
| PENDING | Order created; not yet fully worked at every account. | No |
| PARTIAL | Some accounts received their order and some did not — a normal outcome, not an error. | No |
| COMPLETED | The order is fully done. | Yes |
| CANCELLED | The order was cancelled. | Yes |

PENDING and PARTIAL together are the "open" statuses — the only ones in which the order can still be modified or cancelled.

### Split fill status (EquityOrderSplit.fill_status)

| Value | Meaning | Terminal? |
|---|---|---|
| PENDING | Working at the broker for this account; not yet filled. | No |
| PARTIAL | Partially filled at the broker for this account. | No |
| COMPLETED | Fully filled for this account. | Yes |
| CANCELLED | Cancelled for this account. | Yes |
| FAILED | The broker gave a definite placement error; nothing reached the broker, so re-sending cannot create a duplicate. | Yes |
| REJECTED | The order reached the broker and was rejected; a broker order id exists but no position was created. | Yes |
| INDETERMINATE | The request timed out or the connection dropped; the order may still be live at the broker. Never retried automatically — must be reconciled by a human. | Yes |
| SKIPPED | No broker call was made at all, because pre-trade validation rejected this account (for example, insufficient cash under the SKIP funds policy). | Yes |
| UNSUPPORTED | The broker cannot serve this order type at all (for example, a GTT order sent to a broker whose integration has no GTT support). Terminal for that account only; other accounts in the same parent order still proceed. | Yes |

PENDING and PARTIAL are the "open" statuses (still working at the broker). Every other value is terminal. Of the terminal values, only FAILED and REJECTED are considered safe to retry; INDETERMINATE is deliberately excluded from the retryable set, because re-sending it risks buying or selling the same shares twice.

### Holding exit status (EquityHolding.exit_status)

| Value | Meaning | Terminal? |
|---|---|---|
| ACTIVE | Nothing is in flight; the stop loss/target monitor watches this row. | No |
| AWAITING_CONFIRM | A level was breached on a CONFIRM-mode holding, and the admin has not yet approved the sell. | No |
| EXIT_PENDING | The exit has been claimed and committed; the broker call is about to run or is running. No other path may touch this row. | No |
| EXIT_SUBMITTED | The broker accepted the sell and returned an order id; the fill is awaited. | No |
| EXIT_INDETERMINATE | The sell request never got an answer. Terminal until a human reconciles it against the broker's own order book; never retried automatically. | Yes |
| EXITED | The sell filled and the holding is flat. | Yes |

ACTIVE and AWAITING_CONFIRM are the only statuses a claim may be taken from. EXIT_PENDING and EXIT_SUBMITTED both mean a sell is already on its way, so nothing else may start another exit while a holding is in either of them. Only ACTIVE holdings are evaluated by the background monitor.

### Exit mode (EquityHolding.exit_mode, EquitySetting.default_exit_mode)

| Value | Meaning |
|---|---|
| AUTO | When a stop loss or target is breached, the holding is claimed and sold automatically, with no admin approval step. |
| CONFIRM | When a stop loss or target is breached, the holding moves to AWAITING_CONFIRM and waits for the admin to approve or decline the sell. This is the default for new holdings. |

### Other coded values worth knowing

These are not table-specific statuses but shared vocabularies used across the equity tables above.

| Constant set | Values | Used on |
|---|---|---|
| Order source | MANUAL, STOP_LOSS, TARGET | EquityOrder.source — what raised the order: an admin action from Place Order, Watch List, or Holdings (MANUAL), or the stop loss/target monitor (STOP_LOSS, TARGET). |
| Exit reason | STOP_LOSS, TARGET, MANUAL | EquityHolding.exit_reason — why a holding is being exited. |
| Alert direction | ABOVE, BELOW | EquityWatchlistItem.alert_direction and EquityAlertEvent.alert_direction — which way the price must cross the alert price. |
| Insufficient funds action | SKIP, ABORT | EquityOrder.insufficient_funds_action and EquitySetting.insufficient_funds_action — SKIP (default) marks an account with insufficient cash as SKIPPED and lets every other account proceed; ABORT places nothing for anybody. |

The model file also defines the rule for reading a broker's response to an order placement, used to decide whether a split's `error_type` means the order is safe to re-send. A response is treated as a **definite refusal** (safe to re-send) only when OpenAlgo returned an `api_error`, or an `http_error` with a 4xx status code (a 501 "Not Implemented" is also treated as a definite refusal, since it means the broker has no such endpoint at all — for example, no GTT support). Every other outcome — a timeout, a dropped connection, an unparseable response, or any error type the code does not recognize — is treated as **indeterminate** and must never be retried automatically. The model docstring is explicit that enumerating only the safe cases and defaulting everything else to indeterminate is the correct way round, so an unforeseen new error type fails safe instead of being retried.
