# Equity quote portfolio, revision 3

The integration point for a completed decision is a target, immutable strategy identity and ATR14. Signal generation and promotion remain upstream. One decision is consumed at most once for a buy per symbol, strategy and interval. A filled lot retains its original owner and protection distance.

## Input tape

In portfolio_events.py the immutable records below use timezone-aware datetimes, Decimal prices and quantities, and explicit Boolean gates. MarketFrame is one polling instant and one exchange session. Supply frames in increasing time order. Bundle all instruments observed at the same instant. Symbol ordering inside a frame has no significance.

- Decision(symbol, strategy, start, end, published, decided, target, atr, ready=True). Target is integer 0 or 1. A new entry requires a completed 3600-second interval within the session, publication within 120 seconds and decision within 30 seconds of publication. Decisions expire 300 seconds after the interval end.
- Quote(symbol, at, bid, ask, entry_ok=True). Prices must be positive, finite and orderly. Age relative to frame time is in [-2, 10] seconds. For a buy, spread is at most 20 basis points of midpoint, quote time is strictly after the interval end and at or after the decision.
- MarketFrame(at, session_open, session_close, decisions=(), quotes=()). Decisions become available when the frame arrives. A missing eligible quote leaves the target pending until expiry, replacement or session end. A missing executable closing bid leaves exposure unresolved.
- ExecutionPolicy(capital=10, order_cap=1, position_cap=1, minimum=1, daily_loss=0.50, quantity_step=0.000000001, fee_per_order=0). All values are decimal. Risk overrides may tighten the authorized limits.

## Allocation and transitions

At a poll, mark the held instrument at its fresh bid, evaluate its reduction, then consider buys in alphabetical order if the portfolio began flat. One outstanding order or one open lot consumes the exclusive position slot. An owner flat signal, stop, target, or the last five minutes of a regular session can trigger a reduction. Exits permit wide orderly quotes. Prohibit buys during the last 30 minutes. Out-of-session quotes cannot simulate an execution. A stop is two entry-time ATR from fill basis and the target is two stop distances above basis. A gap executes at the observed bid.

A buy uses the ask. Floor quantity to the supported step after bounding by cash available after fees, both dollar caps, broker capacity and 1% of allocated marked equity divided by stop distance. Never round up to meet the minimum. Reserve buy notional and estimated fee until fill, rejection or confirmed cancellation. A sell reduces confirmed owned quantity only. Transport acknowledgment alone causes no fill. Deterministic quote simulation fills the complete order at the eligible side. Real broker timing, partials, fees, rejects, cancel races and slippage can change that result.

## Accounting and persistence

Use Decimal through valuation and ledger calculations. A purchase transfers cash to cost basis. A sale releases proportional basis and realizes proceeds less released basis. Report gross realized, gross unrealized, confirmed fees and remaining estimates separately. Confirming a fee replaces its outstanding estimate. Marked allocated equity is cash after confirmed and pending costs plus bid value. Reservations reduce spendable cash once and leave equity unchanged. Persist the peak and latch the 10% drawdown stop across days and restarts. The UTC daily loss latch uses the beginning balance for that day and clears no earlier than the next UTC day. Existing safety gates remain prerequisites for paper entries.

QuotePortfolio(policy).step(frame) returns the new simulated fills. Its public fill history and account allow shadow and historical consumers to report the same transitions. The supervisor consumes the same quote, timing, sizing and reduction rules against reconciled facts. Shared functions in portfolio_events.py are quote_ok(quote, at, entry), decision_ok(decision, frame, quote, entry), entry_quantity(policy, equity, available, bid, ask, gap), and exit_reason(bid, basis_price, gap, session_exit, flat, risk_halt). Entry policy revision 3 is distinct from legacy frozen OHLC campaign revisions 1 and 2. Replaying OHLC without observed quotes is a labeled exploratory price and timing assumption.

## Integration ownership

Agent 1 owns the policy, valuation and adapter edits on phoenix/portfolio-engine. The integration manager reviews the changed hunks in shared supervisor, store, shadow, research and dashboard paths before merging. The broker transport and activation protocol retain their existing interfaces. The tests run offline with disposable databases and explicitly supplied fake broker facts.
