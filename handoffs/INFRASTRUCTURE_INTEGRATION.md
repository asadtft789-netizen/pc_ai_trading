# Infrastructure integration milestone

This work extends PR #2 from `27afc3b`. The gateway stays observation-only. Runtime systemd enforcement, rollback rehearsal and the 48-hour final-release soak are unverified.

## Agent 2 authorization boundary

The current observation deployment shares a writable operational SQLite directory. Keep it in observation mode until the supervisor owns a separate authorization store and the installed permission probe passes. A SQLite trigger, process lock or read-only application connection provides no protection against a Unix identity that can replace the database, write its WAL, or change its directory.

- Have the supervisor own `/var/lib/phoenix/authorization` and the canonical authorization store. Place account ownership, exposure limits and latches, broker intents and fills, pending activations, the versioned routing manifest, and certificate-consumption/replay fences under that boundary. Only the supervisor can write the directory, database, WAL, SHM and rollback journal. Keep credentials in the root-owned supervisor environment or systemd credentials. The research process may receive independently scoped market-data credentials.
- Give research its own writable state directory and an input interface for proposals and evidence references. Research may produce study records, candidate manifests, and historical results. The supervisor independently checks provenance, immutable digests, the current routing generation, F16, and all runtime gates before committing any authorization state. An Agent 4 registry digest is an evidence reference and supplies no write authority over the supervisor store.
- Set supervisor ownership for the routing publication directory, with group read/execute only for dashboard readers. Publish `/var/lib/phoenix/telemetry/routing/status.json` using a sibling temporary file and atomic rename. Neither research nor the dashboard may write the file or replace its parent directory. The dashboard reads it strictly for display.
- Before enabling a writer, decide with Agents 2 and 4 how separate authorization and experiment state is fenced across migration and backup. Add a versioned backup manifest for all stores and verify store identity, cursor coverage and certificate replay fences after restore. Keep the existing paired backup while the observation layout is shared.

The supervisor display projection is one snapshot with `format: 1`, a full release `revision`, a timezone-aware UTC `as_of`, a nonnegative `generation`, and a `routes` mapping from explicit scope to an active 64-character strategy identity or `null` for cash. Omitted scopes are unknown. In `validated`, provide `candidate`, `certificate` and `protocol` as lowercase SHA-256 hex digests only after independent Agent 4 verification. The dashboard only lists validations whose protocol is in Agent 4's release allowlist. The view rejects stale observations, revision mismatches and group-writable projections. A missing producer yields unknown routing. Legacy champion and challenger rows always display as exploratory. A current observed route can coexist with a statistical hold and does not imply current permission to add exposure.

## Agent 1 marked accounting

At PR #3 head `6f55c87`, the integration point is `operational_account.account_metrics(c, meta)` in the same read-only snapshot for dashboard health and metrics. Set the connection row factory to `sqlite3.Row` before building `meta`. Replace the two accounting placeholders, preserve broker account equity and the allocation ceiling as separate readings, and combine both sets of JavaScript fields. Avoid a second DB read or a new P&L computation in the dashboard.

Use exact Decimal strings and preserve nulls. Display `valuation_at` with reconciliation freshness and a stale indicator. Treat absent or stale values as unknown for current marked equity. Keep historical realized gross, confirmed fees and latches visible with their provenance when current marks are unavailable. Preserve all of the following meanings.

| Fields | Display contract |
|---|---|
| `allocated_cash`, `reserved_cash`, `available_cash` | USD. Reserved cash reduces spendable cash and does not reduce equity a second time. |
| `realized_gross`, `unrealized_gross` | USD before fees. Use the supervisor's bid-marked valuation for unrealized gross. |
| `confirmed_fees`, `estimated_fees`, `unconfirmed_fee_count`, `missing_fee_estimate_count` | Separate confirmed fees from outstanding estimates and disclose missing estimates. |
| `marked_allocated_equity`, `marked_net_pnl`, `daily_marked_pnl`, `confirmed_net_pnl` | USD within the allocated portfolio. Confirmed net requires flat exposure and all fees reconciled. |
| `peak_equity`, `drawdown`, `max_drawdown`, `daily_halt`, `drawdown_halt` | Peak in USD, drawdowns as fractions, and durable halt states. Preserve false values independently of nulls. |

Do not substitute broker account cash or equity, historical gross, OHLC estimates, or zero for missing marks. Before accepting the combined projection, exercise zero exposure and zero equity, a stale bid, an unresolved partial fill, a late confirmed fee replacing its estimate, reservations, and a restart with both risk latches. Retain Agent 1's version 3 accounting migration and Agent 4's experiment setup in the same explicit migration workflow.

## PR #1 and PR #2 merge review

`git merge-tree --write-tree 27afc3b 5abd2b7` reports conflicts in `CHANGELOG.md`, `PROGRESS.md`, and `TEST_RESULTS.md`. Preserve both branches' historical entries and combine the current work-package statuses. In the auto-merged files:

- `main.py` retains both the operational startup and WAL keeper and Agent 4's `expire_campaigns`. Keep that expiry call and its persisted result, the fixture's learning clock, both role fences, and observation-only broker behavior.
- `paper_store.py` retains both the WAL keeper and `experiment_store.install` within initialization. Ensure the installation remains under the transaction and executes on existing version 2 databases as well as fresh ones. Validate the combined migration to Agent 1's version 3 with the registry and learning binding intact.
- `BUG_REGISTER.md` retains F16's statistical release hold and WP7's open F23/F24 runtime and data items. Do not replace either with a closed status.

The targeted experiment restore test ran in a disposable archive of the merge preview with the milestone code overlaid. It checks the real PR #1 registry, a failed holdout inspection that charges 30 dates, an exhausted 12-slot annual budget, excluded uncommitted rows, immutable evidence triggers and digests, and blocked certificate consumption. No integration merge was published.

## Commands and acceptance status

```sh
python offline_suite.py test_strategy_status test_operational test_experiment_restore
sudo bash deploy/phoenix/verify-state-isolation.sh --fixture
```

The standalone targeted run passed 11 tests and skipped the registry case. The disposable PR #1 and PR #2 preview passed all five targeted registry and strategy display tests. The registry test requires PR #1's actual modules and fixtures. Run it again on the resolved integration tree and require that it executes rather than skips. The permission fixture verifies standard Unix permissions under two different UIDs without starting systemd. The installed probe must use the actual service identities and paths. See [Disposable Ubuntu runtime acceptance](../deploy/phoenix/UBUNTU_ACCEPTANCE.md).
