# AlgoMirror Equity Module — Documentation

This folder documents the equity (cash / delivery) module of AlgoMirror: what it
is, how it is built, the rules it follows, and the decisions behind those rules.

The equity module is entirely separate from the futures and options module. It
has its own routes, its own background workers, its own database tables and its
own screens. Nothing in this folder describes F&O, and nothing built for equity
modifies F&O behaviour.

## How to read this

| Document | What it covers | Who it is for |
|---|---|---|
| `01-ARCHITECTURE.md` | The three layers, who owns which truth, how data moves | Everyone. Read this first. |
| `02-DATA-MODEL.md` | Every equity table, column by column, and its rules | Developers |
| `03-ORDER-LIFECYCLE.md` | Placing an order, splitting it, and every state it can reach | Everyone |
| `04-BOOKS.md` | How Order Book and Trade Book are assembled | Everyone |
| `05-HOLDINGS-AND-EXITS.md` | Holdings, trade nature, stop loss and target, the exit claim | Everyone |
| `06-EXTERNAL-BROKER-ACTIVITY.md` | Trades placed outside AlgoMirror, including emergency exits | Everyone |
| `07-ALERTS.md` | Watch list price alerts and the alert log | Everyone |
| `08-OPERATIONS.md` | Running it, settings, sandbox versus live, the numbered scripts | The operator |
| `09-DESIGN-DECISIONS.md` | Why it is built this way, including options rejected | Developers, reviewers |
| `10-KNOWN-GAPS.md` | What is not built, what is broken, and what is risky | Everyone |
| `11-WHERE-WE-ARE.md` | Current state, what is owed, what is next. Read at the start of a session | Everyone |
| `12-LIVE-VALIDATION.md` | Eight tests that exercise every path only ever proven against a fixture | The operator |
| `13-AUDIT-2026-09-01.md` | Full adversarial audit: what was found, what was fixed, what is owed | Everyone |
| `14-SEGMENT-ARCHITECTURE.md` | PLANNED. How equity and F&O share a database and separate their transactions | Everyone. Agree before building. |

## Status labels used throughout

Every rule in these documents carries one of three labels, so the documentation
never claims something works when it does not:

- **BUILT** — implemented and exercised.
- **PLANNED** — designed and agreed, not yet written.
- **GAP** — a known shortcoming, with its consequence stated.

A document that describes intended behaviour without saying which of these it is
would be worse than no document at all.

## Provenance

The concept, requirements and specification of AlgoMirror originate with the
product owner, a practising Chartered Accountant who trades Indian equities and
derivatives. The application was first built to that specification by Rajendran
of Marketcalls and later opened up under AGPL-3.0. The equity module documented
here was specified by the same product owner and is intended to be contributed
back upstream.
