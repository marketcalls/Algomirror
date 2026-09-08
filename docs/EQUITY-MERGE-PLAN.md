# Equity Module Merge Plan

How Saravanan's equity commit gets folded into AlgoMirror: what is adopted, what is
rewritten, what is discarded, and what has to be fixed first.

Status: Phases 0 and 1 done. Phase 2 core done. Phase 3 started (3.1 done). Phases 4 and 5 not started.
Prepared: 8 September 2026.

## 0. Provenance

| Input | Detail |
| --- | --- |
| Our baseline | `master` @ `e9ddee5` (Equity increment 2) |
| Client tree | `D:\algomirror\saravanan\Algomirror`, branch `equity-module` |
| Client commit | `f44a9a0` "Equity module for AlgoMirror", one commit on top of `e9ddee5` |
| Size | 52 files, +30,746 / -3,087 |
| Requirement | `AlgoMirror_Equity_Trading_Module_PRD_v1.0_FINAL_1.docx` (v1.0 FINAL, 20 Aug 2026) |
| API reference | `openalgo/docs/api/`, `docs/prompt/order-constants.md`, `websockets-format.md` |
| SDK | `openalgo==2.0.4` pinned (published, verified to ship `GTTAPI`). Production still runs 1.0.50 |
| Study method | Seven parallel research agents plus direct verification of every load-bearing claim |
| Production | Inspected read-only over SSH, 8 September 2026. See section 0a |

## 0a. Production baseline (verified, not assumed)

| Fact | Value |
| --- | --- |
| Host | `algomirror.service` (systemd), user `www-data`, no Docker |
| Gunicorn | `--worker-class gthread --threads 16 --timeout 120 -w 1` |
| Workers | **1** |
| Database | **PostgreSQL** at `localhost:5432/algomirror` |
| Flask | `FLASK_ENV=production`, `SESSION_TYPE=filesystem`, `LOG_LEVEL=INFO` |
| Python | 3.12.3 |
| Deployed commit | `e9ddee5`, our baseline. None of Saravanan's work is deployed |
| Installed SDK | **openalgo 1.0.50**, which contains no GTT |
| Stale artifact | `/var/python/algomirror/instance/algomirror.db`, SQLite, last written 27 Feb 2026, unused |

Five accounts, each pointed at its own dedicated OpenAlgo instance:

| Account | Broker | OpenAlgo host | Service |
| --- | --- | --- | --- |
| mps | Dhan | `mps.patsen.in` | `openalgo2` |
| sathya | Upstox | `sathya.patsen.in` | `openalgo3` |
| suji | Angel | `suji.patsen.in` | `openalgo4` |
| patsen | Fyers | `patsen.patsen.in` | `openalgo5` |
| Unicorp | Zerodha | `unicorp.patsen.in` | `openalgo1` |

Two consequences that change this plan, both covered below: **every production broker is
GTT-capable**, and **each account has its own rate-limit bucket**.

One ops nit: `openalgo2.service` is described as "mps.patsen.in - zerodha" but AlgoMirror
records that account's broker as Dhan. The unit description was probably not updated after a
broker change. Harmless, but misleading during an incident.

## 1. Verdict

The commit is competent work, not AI slop. That was the main thing to establish and it is
settled: zero bare `except:`, zero raw or f-string SQL, zero `commit()` inside a loop,
every HTTP call carries a timeout, `@login_required` on all 72 routes, no CSRF exemptions,
no API-key logging, 26 native `<dialog>` modals, zero CDN references, zero hardcoded hex in
any equity template. The multi-account fan-out uses a bounded `ThreadPoolExecutor` whose
workers make no database calls, which is a cleaner shape than our own F&O executor.

What it is not is finished. Three things disqualify it from being merged as-is:

1. Nothing in it has ever placed an order against a real broker.
   `docs/equity/12-LIVE-VALIDATION.md` reads as live testing; precisely, it used live broker
   market data with simulated execution in every single test. Never exercised: a real order,
   settlement, any broker other than Dhan, more than two accounts, GTT end to end, and the
   entire intraday short subsystem.
2. It changes F&O behaviour in ways the commit message does not disclose.
3. The test suite goes from 96 passing to 12 failing, with zero new tests for 10,373 new
   lines of `routes.py`.

The plan below keeps the ideas, keeps most of the screens, and rebuilds the parts that touch
money or F&O.

## 2. Decisions

D1 and D3 are answered by the production inspection in section 0a and are recorded here for
the reasoning. **D2, D4 and D5 still need your call.**

### D1. Production database backend (RESOLVED)

We call `with_for_update(nowait=False)` in 21 places, including all nine equity holding
exit-claim transitions (`app/models.py:1690-2074`) and the F&O exit paths
(`app/utils/risk_manager.py:1113`, `app/utils/strategy_executor.py:2739`,
`app/utils/supertrend_exit_service.py:616`). SQLite emits no `FOR UPDATE`, so on SQLite every
one of those locks is a no-op. `risk_manager.py:1112` carries a comment asserting the opposite.

The default URI is SQLite (`config.py:12`) and `ProductionConfig` sets only `DEBUG = False`
(`config.py:142-143`). Nothing forces PostgreSQL. The SQLite branch even sets a 30 second lock
timeout commented "for concurrent background services" (`config.py:36-40`), so SQLite under
concurrency is an anticipated configuration.

Saravanan hit this exact defect, reproduced a double sell by racing it 40 times, and fixed his
path with a conditional UPDATE (`docs/equity/13-AUDIT-2026-09-01.md`).

**RESOLVED by inspection, severity downgraded.** Production runs PostgreSQL, so every one of
those 21 locks does hold today. This is a latent trap rather than a live defect: the codebase
still defaults to SQLite and `ProductionConfig` still does not require otherwise, so a fresh
deploy, a container, or a `DATABASE_URL` typo silently degrades every exit claim to an
unlocked read.

Remaining action, low urgency: make `ProductionConfig` fail fast when the URI is SQLite, so
the guarantee is enforced rather than merely true. The conditional-UPDATE rewrite is no longer
needed for correctness and is dropped from scope.

### D2. Scope of shared-infrastructure fixes

The rule is that Equity changes and F&O does not. Phase 0 items are shared infrastructure, not
F&O features. Leaving them unfixed means the equity work is built on a foundation that
double-executes. Recommendation: correctness fixes to shared infrastructure are in scope, F&O
feature and UI changes are out of scope. Confirm.

### D3. Gunicorn worker count (RESOLVED)

**RESOLVED by inspection.** Production runs `-w 1 --threads 16` under systemd, not Docker and
not `start.sh`. The singleton hazard is therefore not live today.

The codebase should be made to say what production does:

| File | Now | Should be |
| --- | --- | --- |
| `gunicorn_config.py:15-16` | `workers = 1`, `threads = 4` | `workers = 1`, `threads = 16` |
| `start.sh:34` | `--workers 2` | `--workers 1` |
| `README.md:1020` | `-w 4` | `-w 1`, with a note on why |

`start.sh` and the Dockerfile are unused in production but remain a live footgun: anyone who
containerises this inherits two workers and duplicate monitors. Fixing the three files is
cheap and closes D2's main risk without touching F&O.

A singleton guard is still worth adding as defence in depth, but it drops from blocker to
hygiene.

### D4. Intraday shorts

PRD section 2.2 scopes intraday out explicitly. Saravanan built a full intraday short subsystem
anyway. Recommendation: discard entirely. See section 8.

### D5. Order Qty Ratio denominator

PRD 9.1 specifies this account's allocation divided by total allocation across all active
accounts. Saravanan computes it across participating (ticked) accounts only.

**DECIDED: both, because they are two different numbers.** Read literally, the PRD is broken
whenever the admin does not tick every account. With 50L across five accounts and only two
ticked (20L and 10L), PRD 9.1 gives 40% and 20%, so a 100-share order places 60 shares and
silently drops 40. That cannot be what M4's "Total Quantity" means.

So:

* **M2 (Accounts) shows the PRD 9.1 ratio**, over all active accounts. It is a stable property
  of the account and answers "what share of the family corpus is this member".
* **M4 (Place Order) splits over the participating accounts**, normalised so the split sums to
  the total quantity entered. This is Saravanan's behaviour and it is the correct one.
* **Both are recorded point-in-time on the order split**, per PRD 10.4, so a historical order
  can be reconciled against the ratio actually used rather than a later recomputation.

Show both on the split table so the difference is visible rather than surprising: the M2 ratio
as the member's standing share, and the applied percentage as what this order actually used.

## 3. Phase 0: make the foundation safe

Nothing else should land before these. None of them are equity features.

| # | Item | Status | Why |
| --- | --- | --- | --- |
| 0.1 | Fail fast on SQLite in `ProductionConfig` | DONE | `config.py:142`. Production is PostgreSQL, so the 21 row locks hold. This enforces it rather than relying on it |
| 0.2 | Singleton guard for background services | DONE | New `app/utils/service_lock.py`, wired at `app/__init__.py:314`. PostgreSQL advisory lock, PID file elsewhere. Never raises: a broken lock costs the monitors, not the app |
| 0.3 | Make the three deploy files match production | DONE | `gunicorn_config.py`, `start.sh`, `README.md` all now say `-w 1 --threads 16`, with a comment saying why |
| 0.4 | Websocket auth acknowledgement | DONE | `websocket_manager.py` now trusts `connect()`'s return value instead of setting `authenticated = True` unconditionally |
| 0.5 | Subscription de-duplication | DONE | New `_track_subscriptions()`. The list no longer grows without bound or replays duplicates on reconnect |
| 0.6 | Act on the subscribe result | DONE | A `False` from `subscribe_ltp/quote/depth` is now surfaced instead of reported as success |
| 0.7 | Delete the dead reconnect code | DONE | `ExponentialBackoff` removed. SDK 2.0.4 owns reconnect, backoff and subscription replay |
| 0.8 | Upgrade the production SDK 1.0.50 to 2.0.4 | TODO (deploy) | Repo is pinned to 2.0.4 and audited. The server venv still needs the upgrade |

### 0a-1. SDK 1.0.50 to 2.0.4 compatibility audit

Audited statically against the published 2.0.4 wheel, not the local dev copy.

| Check | Result |
| --- | --- |
| Methods AlgoMirror calls | 18 of 18 present, no renames, no new required arguments |
| `api.__init__` kwargs | All six our wrapper passes are accepted. `verbose` and `auto_reconnect` are new and defaulted |
| Transport | All 18 route through `_make_request`, so our subclass override still applies. Only `gtt.py` and `strategy_api.py` use the new `_post` |
| Base internals our override uses | `base_url`, `headers`, `timeout`, `api_key` all still set in `BaseAPI.__init__` |
| `_handle_response` | Present, and identical across `orders`, `account`, `data`, `options`, `telegram`. It attaches `code` on a non-200, which is exactly what `_is_gtt_unsupported` relies on for its 501 check |

**One real regression found and fixed.** 2.0.4 introduced a pooled `httpx.Client` on the
instance specifically because the old module-level `httpx.post` left "thousands of sockets in
TIME_WAIT over a trading session and eventually exhausting ephemeral ports". Our
`_make_request` override called `httpx.post` directly, so upgrading would have kept the old
behaviour for all 18 methods while only GTT got the fix. `app/utils/openalgo_client.py` now
reuses `self.client` when present and falls back to `httpx.post` on older SDKs, so the code is
safe to deploy either before or after the venv upgrade.

**One behaviour change to watch.** 2.0.4's feed defaults to `auto_reconnect=True`, reconnects
with a 1/2/5/10/30/60 second backoff, replays every active subscription, and runs
`ping_interval=20, ping_timeout=10` to detect zombie sockets. That is strictly better than what
we have and it retires item 0.7 as originally written. The thing to verify is duplication: our
subscription list has no de-duplication (item 0.5) and a 30s watchdog also re-subscribes, so
with the SDK replaying as well, confirm subscriptions do not compound. Server-side refcounting
by unique symbol limits the blast radius to wasted capacity rather than wrong data.

Items 0.4 to 0.7 are prerequisites specifically because Phase 3 adds equity consumers to this
shared connection. Adding load to a connection that cannot prove it is authenticated and cannot
reconnect makes the equity feed less reliable, not more.

Two adjacent defects found but out of scope, logged for later: `strategy_executor.py:2171-2172`
registers a depth handler then subscribes in LTP mode so it never fires, and
`/api/websocket-status` always reports zero because `main/routes.py:721` iterates the
subscriptions dict as JSON strings.

## 4. Phase 1: GTT lifecycle

The client reports GTT does not work. The diagnosis is not what it looks like.

### What already works

Our baseline already implements GTT place, modify and cancel over raw HTTP
(`app/utils/equity_order_engine.py:185-187`, `_call_endpoint` at `:410-426`), because SDK 2.0.3
has zero GTT support. It also already degrades correctly per broker: `_is_gtt_unsupported`
(`:352-366`) maps HTTP 501 to `EQUITY_SPLIT_STATUS_UNSUPPORTED`, terminal for that account while
the others proceed.

### SDK 2.0.4 changes the transport, not the problem

2.0.4 adds `GTTAPI` to the `api` MRO with four keyword-only methods:

```
placegttorder(strategy, symbol, action, exchange, trigger_type, product,
              quantity, price_type, price, triggerprice_sl, triggerprice_tg,
              stoploss, target)
modifygttorder(trigger_id, ...same as above...)
cancelgttorder(trigger_id, strategy)
gttorderbook()
```

Note `price_type` in the SDK maps to `pricetype` in the REST body, consistent with `placeorder`.

Once 2.0.4 is pinned, delete our hand-rolled GTT calls from `equity_order_engine.py` and use the
native methods. Two things to verify at that point:

- `gtt.py` routes through `BaseAPI._post`, **not** `_make_request`. Our
  `ExtendedOpenAlgoAPI._make_request` override therefore does not apply to GTT calls. `_post` is
  functionally better (pooled `httpx.Client`, same `timeout_error` / `connection_error` vocabulary
  our indeterminate logic keys on, and it preserves the non-200 JSON body with `code` attached,
  which is exactly what `_is_gtt_unsupported` needs for its 501 check). So this is safe, but it
  means two transports coexist in our app.
- Consider retiring our `_make_request` override entirely in favour of `_post` semantics, since
  the override loses 2.0.4's connection pooling on the hotter order path.

### The three real causes of "GTT is not working"

1. **Broker coverage. RULED OUT for this deployment.** GTT ships for exactly five brokers:
   angel, dhan, fyers, upstox, zerodha (each has `broker/<name>/api/gtt_api.py`). The other 19
   return HTTP 501. All five production accounts (Dhan, Upstox, Angel, Fyers, Zerodha) are on
   that list, so every one of them can hold a GTT. Keep the 501 handling for portability, but
   this is not why the client's GTTs are failing.
2. **No lifecycle back half.** We place triggers and never ask the broker which are still alive.
   There is no `gttorderbook` call anywhere in either tree, and nothing bridges
   `EquityOrderSplit.broker_gtt_id` to `broker_order_id` (`app/models.py:1395-1398`). A GTT places
   successfully and then never resolves. This is the main work item.
3. **The status string.** OpenAlgo emits the two-word `"trigger pending"`, not `"pending"`. The
   published table in `orderstatus.md` and `orderbook.md` lists `pending`, which is wrong.
   Confirmed from the broker mappers (zerodha maps `"TRIGGER PENDING"`, fyers maps code `4`). Any
   matcher looking for bare `"pending"` silently never fires, and a fired GTT lands in exactly
   this state.

### The structural difficulty, mostly dissolved

`gttorderbook` returns active triggers only **by default**, but that is not the ceiling. It
accepts a `status` field (`restx_api/gtt_orderbook.py:28`), and `status="all"` sets
`include_history` all the way down to the broker mappers, which normalise the terminal states to
`triggered`, `cancelled`, `expired` and `rejected` (plus `transit` on Fyers). SDK 2.0.4's
`gttorderbook(**kwargs)` passes the field straight through. This is undocumented:
`gttorderbook.md` says status is "Always active" and never mentions the parameter.

So the hard half, distinguishing "fired" from "cancelled" when a trigger stops resting, needs no
heuristic at all. What remains is the last hop: a fired trigger still yields no order id, because
no broker links a trigger to the order it released. That stays a bounded search over the account's
order book, and it refuses to answer when two rows fit rather than attaching the wrong fill.

Caveat: Upstox's mapper reports every row as `active`, so terminal states may never appear there.
A trigger that is absent from the book entirely is therefore recorded as `unknown` and kept under
watch, never assumed dead.

### Work items

| # | Item | Status | Note |
| --- | --- | --- | --- |
| 1.1 | Native GTT methods / SDK 2.0.4 | DONE (repo) | Pinned and audited. Placement deliberately stays on the raw post so GTT keeps AlgoMirror's own error envelope, which every retry and indeterminacy rule reads. Server venv upgrade still pending (0.8) |
| 1.2 | Persist `trigger_id` as the lifecycle key | DONE | `broker_gtt_id` is now authoritative, joined by `gtt_status`, `gtt_synced_at`, `gtt_triggered_at` (migration 016) |
| 1.3 | Poll `gttorderbook` per account | DONE | New `app/utils/equity_gtt_reconciler.py`, swept every 60s from the shared scheduler. Reads with `status="all"` |
| 1.4 | Resolve the terminal state | DONE | Read directly from the book rather than inferred. Child-order match is the only heuristic and refuses to guess between two candidates |
| 1.5 | Per-account GTT capability detection | DONE (existing) | `_is_gtt_unsupported` already maps 501 to UNSUPPORTED per account. The reconciler treats an unreadable book as "settles nothing" |
| 1.6 | Reject MIS for GTT | DONE (by construction) | `_gtt_payload` hardcodes CNC. Documented at the constant rather than adding a guard for an unreachable case |
| 1.7 | Guard Upstox OCO | DONE (by construction) | Every GTT placed is SINGLE. The Upstox OCO trap is documented where the trigger type is defined |
| 1.8 | Surface resting GTTs as a first-class view | TODO | Deferred to the Phase 5 template work |

If a future OpenAlgo release echoes the originating `trigger_id` on the child order in
`orderbook` and `orderstatus`, item 1.4 collapses from a heuristic to an exact lookup.

## 5. Phase 2: fill reconciliation and the books

This is the largest genuine gap, and it is a gap in **our** baseline, not something Saravanan
invented.

Today nothing writes an `EquityTrade`. Trade Book is structurally always empty and the template
says so rather than looking broken (`app/templates/equity/trade_book.html:231-232`). There is no
equity order status poller, though `recompute_parent_status` is already written for one that does
not exist. Holdings can sit in `EXIT_SUBMITTED` indefinitely, and `accounts_label` reports
placed-over-selected rather than the PRD's filled-over-selected (`app/equity/routes.py:5611-5616`).

Saravanan filled this with an 1,843-line polling `equity_fill_reconciler.py`. The gap is real;
the substrate is the question.

**Recommendation: build on the order-update websocket, keep polling as the backstop.** The SDK
exposes `subscribe_orders()`, an account-level stream carrying fills, partial fills, rejections
and cancellations, registered for 17 brokers (`openalgo/services/order_update_service.py:36`)
against GTT's five. Constraint: order updates are genuinely per-account, so this needs one
connection per broker account, unlike market data which is shared.

| # | Item | Status | Note |
| --- | --- | --- | --- |
| 2.1 | Equity fill poller, modelled on the F&O poller | DONE | New `app/utils/equity_fill_poller.py`, 10s from the shared scheduler. Scheduler-callable rather than its own thread, matching the equity house style |
| 2.2 | `subscribe_orders` consumer per account | TODO | Needs one websocket connection per broker account. The poller covers the same ground first |
| 2.3 | Write `EquityTrade` rows on fill | DONE | Trade Book is no longer structurally empty. De-duplicated by the unique index, with a quantity/price fallback for brokers that return no trade id |
| 2.4 | Drive `recompute_parent_status` from real fills | DONE | Parent status now rolls up from booked fills |
| 2.5 | Fix `accounts_label` to filled-over-selected | DONE | `app/equity/routes.py:3018`, per PRD 7.1 and 7.6. `accounts_placed` still carries "reached the broker" |
| 2.6 | Adopt the external broker activity model | TODO | Highest-value idea in the client commit |
| 2.7 | Adopt holding notices and Check With Broker | TODO | |
| 2.8 | Resolve the stuck-state set | PARTIAL DONE | PARTIAL-for-ever fixed: `is_open` now requires a live split, so a settled mixed order stops offering Modify and Cancel. INDETERMINATE-with-no-candidate and EXIT_PENDING-on-crash still open |

Note for Holdings: OpenAlgo's `holdings` returns quantity, pnl and pnlpercent but **no average
price and no LTP**. PRD M7 requires Avg Cost, so it must come from our own trade history. Another
reason 2.3 is load-bearing.

## 6. Phase 3: realtime data

Equity is further along than expected. `app/utils/equity_price_feed.py` already uses the shared
websocket in LTP mode, and is in three ways stricter than F&O: it keeps its own tick timestamps
and rejects anything older than 90s rather than trusting the manager's indefinitely-cached LTP;
`equity_is_indeterminate_response()` (`app/models.py:1040`) defaults to indeterminate so an
unenumerated error is never retried; and the exit monitor paces off a DB heartbeat so it survives
restarts. So this phase is mostly surfacing and widening, not plumbing.

| # | Item | Note |
| --- | --- | --- |
| 3.1 | Move the price feed from LTP to Quote mode | DONE | Quote carries the previous close that LTP does not, removing a per-symbol per-day REST call. Stored separately from the traded price and not age gated, since it belongs to a finished session |
| 3.2 | Websocket depth for Place Order | PRD M4 wants 5-level depth. Websocket depth supplies strictly more than REST (adds a per-level `orders` count). Currently REST at 15s (`app/equity/routes.py:5183`). Subscribe one symbol, only while the panel is open, keep REST as the cold-start fallback since the first frame has not arrived on open |
| 3.3 | SSE for the live equity screens | Follow `app/trading/routes.py:1519`: capture `current_app._get_current_object()` before the generator, `with app.app_context()` plus `db.session.expire_all()` each iteration, `X-Accel-Buffering: no`. Client pattern from `strategy/builder.html:2503-2541` |
| 3.4 | Feed freshness badge on equity screens | Endpoints already return `price_feed` and nothing renders it. Reuse the F&O badge vocabulary |
| 3.5 | Analyze-mode badge | Analyzer mode is application-wide per OpenAlgo instance, not per API key. Detect via `POST /api/v1/analyzer`, read `data.analyze_mode`. The existing per-account F&O badge is really reporting a host-level fact |
| 3.6 | Pause polling on `visibilitychange` | Only `equity_alerts.js` does this today; `positions.html` never clears its timer at all |

Hard limit to design to: **there is no position or margin stream.** The OpenAlgo proxy explicitly
skips private position and margin topics. Prices stream and order updates stream; positions,
funds, holdings and margins are REST-only. SL and target monitoring can be tick-driven; position
and funds state cannot.

Capacity is not a concern: 1,000 symbols per upstream connection, 3 connections, and
subscriptions are refcounted by unique symbol rather than by consumer.

Cleanup: `socket.io.min.js` is loaded in `base.html:36` and there is no SocketIO server anywhere.
`websocket_service.py` at the repo root runs as a systemd daemon writing
`instance/websocket_data.json` that nothing under `app/` reads. Both are dead weight to retire or
wire up deliberately.

## 7. Phase 4: order engine

PRD 10.1 requires concurrent fan-out, and our equity engine already does this: `_run_jobs`
(`app/utils/equity_order_engine.py:664-695`) runs a single job inline and otherwise bounds a
`ThreadPoolExecutor` at `MAX_ORDER_WORKERS = 10`, with workers making no database calls and the
calling thread writing results in one transaction. The gap is pacing, not parallelism.

**Rate limiting, corrected against production.** OpenAlgo's limiter is per IP and in-memory,
which reads as a single shared 10-per-second bucket for order writes. In this deployment it is
not shared: each account has its own OpenAlgo instance (`openalgo1` through `openalgo5`), each a
separate process with its own limiter. A five-account fan-out therefore sends one write to each
of five independent buckets and is nowhere near the limit.

This relaxes the constraint but does not remove it. It becomes a deployment invariant to hold
onto, not a property of the code: the moment two accounts share an OpenAlgo instance, their
order writes share one 10/sec bucket, and there is no `Retry-After` to tell us.

| # | Item |
| --- | --- |
| 4.1 | DONE. `_run_jobs` groups by `host_url` and serialises within a host while keeping hosts parallel. Production is one instance per account, so behaviour is unchanged today; two accounts on one instance now degrade to sequential instead of colliding on one 10/sec bucket |
| 4.2 | DONE (already correct, now pinned by a fan-out crash test). Preserve the indeterminate rule. A timeout or connection error means the order may be live at the broker, so it is never retried. Our F&O executor gets this right (`app/utils/strategy_executor.py:911-932`, commit `32c5d4d`) and so does our equity engine (`EQUITY_SPLIT_STATUSES_SAFE_TO_RETRY` at `app/models.py:1016` deliberately excludes `INDETERMINATE`). Any new path inherits it |
| 4.3 | Check `response.ok` on every write. Module-wide it is checked zero times, so a 429 or 403 currently reads as "may have reached the broker" when nothing was sent. Our baseline shares this gap; his commit multiplies the endpoints it applies to |
| 4.4 | DONE. Both ratios exist and both are surfaced: `qty_ratio` is what this order applied over the participating accounts, `standing_qty_ratio` is the PRD 9.1 figure over all active accounts that M2 shows. See D5 |
| 4.5 | Add Product to Place Order. His M4 removed the control entirely; the PRD lists it as an order field |
| 4.6 | Add the brokerage and statutory cost estimate to Place Order. His commit message claims "estimated costs" but the screen shows only Est. Value (quantity times price) |

Type traps to handle explicitly, since they cause silent money bugs: `funds` returns all five
values as strings; `positionbook` returns every field as a string; `orderbook.orders[].quantity`
is a string while `tradebook[].quantity` and `holdings[].quantity` are numbers.

Money columns are `db.Float` throughout (85 in his tree, 80 in our baseline, zero `Decimal` in
either). Inherited house style, not his regression. Moving to `Decimal` is a separate decision.

## 8. Adopt / rewrite / discard register

### Discard

| Item | Reason |
| --- | --- |
| Intraday short family: shorts, resting SL-M, cover protocol, square-off monitor, Cover Now | PRD 2.2 scopes intraday out. Never tested. Square-off deadline of 15:12 reverse-engineered from OpenAlgo's *sandbox* constant, not a market rule. `COVER_INDETERMINATE` has no resolution route anywhere, so an open same-day obligation nothing can clear. Contains a verified money-losing bug at `equity_intraday_monitor.py:579-583`: an indeterminate buy-back is treated as a definite refusal, the claim is released, and the next 15s tick places a second market BUY, so a short can end the day long |
| `warm_account_cache()` (`routes.py:14080`) | Undisclosed F&O contamination. Writes shared `TradingAccount.last_funds_data` and `last_data_update` every 20s for every active account, from a job registered on the F&O scheduler (`__init__.py:461`). The F&O funds screen and API serve those columns under a 30s gate, so F&O would never make its own broker call again |
| `alerts.html` (674 lines) | Dead. Nothing renders it, confirmed by his own `routes.py:9002` |
| Watch List CSV import | Blank-clears plus stale-file-reverts plus unversioned levels, on a screen that carries stop-loss levels |
| Two reorder endpoints | Undocumented, no PRD basis |
| `api_fix_watchlist_symbol` cascade | Keep symbol correction, drop the cascade: it rewrites every other row carrying the same symbol and moves the investment note, an unpreviewed multi-row write from a single-row action |
| Migration `022` | Dead code |

### Rewrite before adopting

| Item | Reason |
| --- | --- |
| `equity_fill_reconciler.py` (1,843 lines) | Right problem, wrong substrate. Rebuild on `subscribe_orders` with polling as backstop. See Phase 2 |
| `layout.html` changes | Four leaks: `.module-equity` padding at `:85` also matches the `<aside>` at `:229`; the tint reaches an F&O sidebar via the JS at `:511`; `#module-sidebar{transition}` at `:18` is unscoped; `fnoPrefixes` gained `/dashboard` and `/accounts` at `:520`, a behaviour change to shared F&O nav JS. The 205-line `<style>` also ships on every page including login. The F&O nav block itself is byte-identical, so the fix is scoping, not reversion |
| Migrations `016`-`026` | `021:160` uses `DATETIME` and `026:73` uses `BOOLEAN NOT NULL DEFAULT 0`, both unguarded, both abort on PostgreSQL. `016` can silently delete rows. Renumber and re-style against `migrate/upgrade/013_add_equity_module.py` |
| `settings.html:775-803` | Silently commits zeros for every untouched charge field as a new effective-dated rate version |
| `trade_book.html` | Declares `equityAccountCell` twice (`:551` and `:805`), so the merged-fill feature is dead code and every merged row renders "Account null" |
| `positions.html` | Uncleared timer, stale-row bug, four dead confirm buttons |
| `dashboard.html:516,521` | Reads `kpi.portfolio_value` and `card.id`; the server sends `total_portfolio_value` and `account_id` |
| M5 / M6 filters | His Order Book and Trade Book dropped all 8 filters and the date range our baseline had, so historical lookback is unreachable. PRD 7.6 requires them |

### Adopt

| Item | Note |
| --- | --- |
| External broker activity model | Highest-value idea in the commit |
| Holding notices, Check With Broker, `resolve-exit`, the exit queue | Operational necessities |
| `equity_notes.js` (investment notes) and `equity_updated.js` | The best-written code in the commit. Take as-is |
| `equity_alerts.js` | One-line fix |
| Price alerts, account cache, multiple watch lists, Holdings alert chips | Adopt simplified |
| Positions screen | Not a PRD screen but genuinely useful. Adopt after the rewrite items above |
| CSV exports, preferences page, operator script suite | Adopt |
| The three F&O hunks that are real bug fixes | F&O account edit cannot save without retyping the API key; saving silently rewrites `broker_name` to "5paisa". Re-land as a **separate F&O commit**, not inside the equity merge |

## 9. Phase 5: frontend consolidation

75 percent of his 16,833 template lines are inline JavaScript. `EquityFormat` and `equitySetText`
are copy-pasted into all 10 templates, `equityCell`/`equityMessageRow` into 7, the sort engine
into 5. The copies have already drifted: `rupees()` returns different output in holdings versus
positions, and `equityShowBanner` takes three, four and five arguments in three different files.
`holdings.html:656-659` documents a drift that already broke a render at runtime.

**Extracting `equity_common.js` (roughly 2,000 lines) is a merge condition, not a follow-up.**
Adopting ten templates that each carry a drifting private copy of the money formatter is how the
next money bug gets written.

Security posture is good and should be preserved: XSS surface is effectively zero across all ten
templates plus three JS files (one `innerHTML`, and it is a clear at `settings.html:1457`),
everything built with `createElement` plus `textContent`, no `|safe`, no `eval`. CSRF is clean
across 30-plus writes. One latent sink to close: `order_book.html:1265,1330` route server strings
into `showToast`, which interpolates into `innerHTML` at `base.html:125-136`.

## 10. F&O containment rules

1. No equity job may be registered on the F&O scheduler.
2. No equity code may write `TradingAccount.last_funds_data` or `last_data_update`.
3. `layout.html` changes must be scoped so no selector, transition or JS branch reaches an F&O
   page in any state, including after the module toggle is clicked.
4. Equity accounts must not appear on F&O data screens. His own
   `docs/equity/10-KNOWN-GAPS.md:139-162` records that all five F&O data screens currently list
   them and that he declined to fix it because the fix touches F&O. It still needs fixing, on the
   equity side of the boundary.
5. Separate equity Order Book and Trade Book are correct and required by PRD 7.6. They are not
   F&O duplication.
6. The PRD's F&O sidebar trim (section 6.2) is mostly already true in our tree: Funds and Holdings
   are absent from F&O nav, Margin and Trading Settings are already under Admin. Only "remove Risk
   Monitor from Trading" is outstanding, and it stays out of scope.

## 11. Definition of done

| Gate | Criterion |
| --- | --- |
| Tests | 96 baseline tests passing, plus new tests for every adopted subsystem. Current state of his branch is 12 failing and zero new tests |
| GTT | A trigger placed, listed in `gttorderbook`, fired, and resolved to its child order, against a real broker from the supported five |
| Fills | `EquityTrade` rows written from a real fill; Trade Book non-empty; Order Status shows Partial versus Completed correctly |
| Fan-out | A 5-account order stays inside the 10/sec write bucket and no account blocks another |
| Indeterminate | A forced timeout produces no retry and no duplicate |
| F&O | F&O screens byte-identical in behaviour, verified on all five data screens |
| Migrations | `016`-`026` equivalents apply cleanly on PostgreSQL from a baseline database |
| Realtime | Feed badge and Analyze badge visible on equity screens; depth panel driven by websocket |

## 12. Suggested sequence

Phase 0 gates everything. After that, Phase 2 (fills) unblocks the most PRD surface, because
Trade Book, Order Status accuracy, Holdings Avg Cost and the filled-over-selected counts all
depend on `EquityTrade` existing. Phase 1 (GTT) is independent and can run in parallel. Phase 3
and 4 follow. Phase 5 runs alongside whichever template work is active, but `equity_common.js`
lands before the first adopted template.

```
Phase 0  foundation
           |
           +----> Phase 1  GTT lifecycle -----+
           |                                   |
           +----> Phase 2  fills and books ----+---> Phase 3  realtime ---> Phase 4  order engine
                                               |
                             Phase 5  frontend consolidation (continuous)
```
