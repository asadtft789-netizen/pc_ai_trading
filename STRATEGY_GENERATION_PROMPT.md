# Strategy Generation Prompt — for another AI

Use this prompt verbatim when asking another AI to propose a new trading-strategy rule for the self-evolving paper trader.

---

You are proposing a new entry rule for an hourly paper-trading strategy. The host runs QQQ (and other symbols) hourly bars in New York time, long-or-flat, no overnight positions, flat at session close.

## Strategy contract (non-negotiable)

Your output must be exactly one Python source string defining:

```python
def signal(trend, breakout, oversold, hammer, pullback, structure, support, resistance, golden, volume, quiet, momentum):
    return 1 if <boolean expression> else 0
```

Constraints (enforced by the host's `strategy_worker.validate_source`):
- Exactly one `FunctionDef` named `signal`, no decorators, no return annotation.
- Exactly the 12 feature arguments above, in that order, no defaults, no `*args`/`**kwargs`, no annotations, no keyword-only or positional-only args.
- Body is a single `return` statement: `return 1 if <expr> else 0`.
- Expression may use only: `and`, `or`, `not`, feature names, and constants `0`/`1`/`True`/`False`.
- No other names, no calls, no arithmetic, no comparisons, no attribute access.
- Source length ≤ 2400 characters.
- The 12 features are boolean (0/1) computed on completed hourly bars:
  - `trend` = EMA20 close > EMA50 close
  - `breakout` = close > 30-bar shifted high max
  - `oversold` = RSI14 < 30 OR close < 20-bar mean − 2×20-bar std
  - `hammer` = nonzero body, lower wick ≥ 2× body, upper wick ≤ body, close > open
  - `pullback` = trend and low < 20-bar mean and close > 20-bar mean
  - `structure` = high > 5-bar shifted high max
  - `support` = low < 30-bar shifted low min + ATR14
  - `resistance` = high > 30-bar shifted high max − ATR14
  - `golden` = (close − 30-bar shifted low min) / (30-bar shifted high max − 30-bar shifted low min) in [0.382, 0.5]
  - `volume` = volume > 20-bar shifted volume mean
  - `quiet` = ATR14 / close < 0.01
  - `momentum` = close > close 5 bars ago

## Backtest semantics (host)

- Vectorized next-open long/flat. Signal at close of bar i → position held from open of bar i+1 until signal turns off or session close.
- Per-side fee/spread/slippage: 5 bps each (≈10 bps round trip + commission).
- Capital: $10 max, $1 max per position per symbol, equal sleeves across symbols.
- Pass gates: ≥20 trades, net P&L > 0, max drawdown > −10%, no `_reason`.
- Champion baseline (gen 1): `oversold and not trend` → on current data: 146 trades, P&L +$0.082, DD −1.26%.
- Gen 3 challenger: `oversold and not trend and support and not momentum` → 137 trades, P&L +$0.124, DD −1.39%.
- strategy_v2 candidate: `oversold and not trend and (support or hammer)` → 144 trades, P&L +$0.097, DD −1.36%.

## What good looks like

To be worth adding as a challenger, your rule must:
1. Pass the sandbox validation (run `strategy_worker.validate_source(src)` first).
2. Beat the champion's P&L on the same enriched hourly frame and fee settings.
3. Keep ≥20 trades and drawdown > −10%.
4. Change only ONE logical condition versus the parent (do not rewrite the whole signal).

## What NOT to do

- Do NOT reference RSI, EMA, ATR, close, open, high, low, volume, or any price directly — only the 12 boolean feature names.
- Do NOT add support/hammer/quiet/momentum as *confirmations* blindly; on five years of QQQ data each reduced net when used as a required confirmation (see `qqq_hourly_candidate_v3_rules.pdf` evidence).
- Do NOT propose `oversold and not trend and (support or quiet)` — that revision lost after costs and failed to reach 20 trades on its reserved test.
- Do NOT claim profitability after costs without running the host backtest on the identical frame and fees as the champion.

## Output format

Return only:

```
SOURCE: <the single signal(...) source string>
GUARD: <one feature name used as the ablation guard, e.g. oversold>
LESSON: <one sentence: what causal filter you are testing and why>
```

Then stop. Do not add code to run trades, orders, or database writes. The host handles registration and promotion.

---

Example of what to aim for (do not copy verbatim — generate your own):

```
SOURCE: def signal(trend, breakout, oversold, hammer, pullback, structure, support, resistance, golden, volume, quiet, momentum):
    return 1 if oversold and not trend and support and not momentum else 0
GUARD: oversold
LESSON: Require support confirmation and exclude momentum bars to filter unconfirmed oversold entries into weak bounces.
```

## Reference evidence (from the QQQ v3 study, do not treat as proven)

- `oversold and not trend` (champion) made money on 2021-05-13 to 2026-07-01 history then lost −8.2 bps (−$0.0334) on its single unseen test (2026-07-02 to 2026-11-20).
- REV (oversold-and-not-trend) buys into falling markets with no stop; three such trades (Oct 30, Nov 5, Nov 20 2026) lost $0.0582 — more than the whole test loss.
- A 0.5% stop would have brought that window to break-even but removed about a third of earlier profit — untested as an improvement.
- QQQ was chosen after a two-symbol version failed validation because of AAPL — that symbol choice is itself a selection.
- Edge is small (~9 bps per trade before 2026) and may have faded.

Your rule should be designed to survive a *frozen forward trial* on unseen sessions, not tuned to history. One window scores one frozen rule once; a window that has been looked at is no longer unseen.
