# Agent 1 portfolio and accounting handoff

Branch `phoenix/portfolio-engine` starts at `e955019656e5bf101982bb4f0e5623bed09dd894`. Draft PR #3 targets `phoenix/professional-autonomous-v1`. The early contract commit was `d66d094`. GitHub accepted both the branch and the initial implementation after repository access became available.

## Implementation

- `portfolio_events.py` defines the immutable tape, common timing and quote gates, Decimal sizing and frozen protection, alphabetical allocation, and deterministic simulated fills.
- `portfolio_account.py`, `operational_account.py` and `paper_store.py` implement average-cost accounting, exact gross outcomes, durable buy reservations, proportional partial fills and fees, immutable fee confirmation, bid marks and loss latches. Schema v3 backs up the previous ledger before migrating and keeps old raw fills and lot owners.
- `backtester.py` handles observed quotes and labels the exploratory OHLC price and availability assumptions. `research.py` persists the completion timestamp of new decisions and binds the development manifest to the replay code.
- `shadow_campaign.py` and `learning_runtime.py` consume read-only observed quotes for new revision 3 shadow policies. Legacy `learning_loop.py` and its frozen v1/v2 model are byte-identical to the base.
- `executor.py` and `paper_order_policy.py` apply the common rules to reconciled lots, independent authorization, and the existing fake order lifecycle. An actual broker may reject, execute asynchronously or partially, fill at a different price, deliver fees later, or race a cancellation. The ledger books reported facts in those cases.
- `marked_session.py` prepares Agent 4's full roster of marked sessions, including zero-trade days and scenario receipts. `dashboard/paper_view.py` and `dashboard/static/app.js` expose Agent 5's accounting fields.

The callable signatures, input rules, JSON fields and integration points are in `coordination/PORTFOLIO_CONTRACT.md`.

## Verification and remaining evidence

The isolated Linux baseline passed 104 tests in 9.058 seconds using CPython 3.12.14 and `requirements-verified.txt`. It was run before editing. The first implementation passed 130 tests in 8.805 seconds. The parity tests compare successive tape prefixes through historical replay, prospective shadow replay and the real supervisor code backed by a fake broker and a disposable SQLite ledger. They cover alphabetical contention, quote eligibility, owner flat signals, early close, target and stop gaps, drawdown across restart and a divergent partial-fill/cancellation/late-fee path.

F18 and F22 are verified for the exercised common-event equity and fake-broker paths. Real-account observation completeness, fee source, venue fill behavior, protection and broker capability remain release evidence. The production gateway is observation-only and does not attest fractional entry eligibility. Historical OHLC values lack observed executable quotes. A campaign collector must supply validated prospective quote and strategy decision streams, plus benchmark and regime coverage. The adapter prepares the panel and the existing registry owns receipt and promotion. The F16 hold remains in effect.

## Integration manager

- With PR #1, reconcile `research.py` to keep both experiment registration and the persisted decision completion time. Apply `experiment_store.install` along with the schema v3 accounting transaction. PR #1 changes `learning_loop.py`, whose file digest is part of existing frozen campaign identity. Verify backward compatibility of that combined code before treating existing trials as comparable.
- With PR #2, retain infrastructure health and wire `account_metrics(c, meta)` into its dashboard health and metrics handlers. Replace its placeholder accounting fields and join the complementary JS fields. Review merged supervisor and store changes for duplicate clock, mark and reconciliation behavior.
- The complete combined daemon, permanent quote collection and verified broker fee adapter remain integration and release checks. Preserve the paper-only gates and frozen campaign evidence.

Final standalone verification ran 133 tests in 9.063 seconds. 132 passed and the one direct Agent 4 registry test was skipped because that module lives on PR #1. In a detached disposable checkout with `campaign_registry.py` and `experiment_store.py` from PR #1 head `5abd2b7`, all three marked-session tests passed in 0.511 seconds, including the actual registry write and retry. The Git merge preview reported a `research.py` conflict against PR #1 and dashboard API and JavaScript conflicts against PR #2 head `27afc3b`. The combined application suite remains to be run after the integration manager resolves the shared files.
