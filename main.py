import logging
import time
import json
import pandas as pd
from datetime import datetime
from typing import Optional, Dict, Any, List
from config import Settings
from memory import MemoryManager
from llm_mutator import StrategyMutator
from data_ingestor import DataIngestor
from executor import StrategyExecutor
from risk_engine import RiskEngine
from strategy import BaseStrategy
from backtester import Backtester
from builtin_strategy import get_fallback_strategy

# Runnable skills wired into the self-improvement loop.
from skills.ml4t.advanced_ai.agent_state_memory import AgentState, EvidenceStore
from skills.ml4t.risk_management.drift_detection import DriftDetector
from skills.ml4t.risk_management.position_sizing import PositionSizer

# Price action and market structure skills (from YouTube course gap analysis).
from skills.tradermonty.skills.price_action_swing_trading.price_action import (
    PriceActionDetector,
    detect_crowded_long,
    detect_crowded_short,
    detect_impulse_pullback,
    detect_support_resistance_breakout,
)
from skills.tradermonty.skills.price_action_swing_trading.market_structure import MarketStructureDetector
from skills.tradermonty.skills.price_action_swing_trading.support_resistance import (
    SupportResistanceDetector,
    FibonacciRetracement,
)
from skills.tradermonty.skills.price_action_swing_trading.volume_analysis import VolumeAnalyzer
from skills.tradermonty.skills.price_action_swing_trading.dashboard import (
    format_trade_signal,
    format_portfolio_summary,
    format_analysis_report,
)
from skills.tradermonty.skills.price_action_swing_trading.risk_sizing import (
    RiskSizer,
    size_position_for_trade,
)

logger = logging.getLogger(__name__)


def _setup_logging(log_level: str = "INFO") -> None:
    """Configure logging for both local and unattended/remote operation.

    Writes to stdout (captured by systemd/journald when run as a service)
    and to daemon.log for local inspection.
    """
    logging.basicConfig(
        level=getattr(logging, str(log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),  # systemd / journald captures stdout
            logging.FileHandler("daemon.log"),
        ],
    )


def _load_strategy_instance(strategy_row: Dict[str, Any]) -> Optional[BaseStrategy]:
    """Dynamically loads an LLM-generated strategy class from its stored code."""
    code = strategy_row.get("code")
    if not code:
        return None
    try:
        namespace: Dict[str, Any] = {
            "BaseStrategy": BaseStrategy,
            "pd": pd,
            "np": __import__("numpy"),
        }
        exec(compile(code, f"<strategy:{strategy_row['id']}>", "exec"), namespace)
        for _, cls in namespace.items():
            if isinstance(cls, type) and issubclass(cls, BaseStrategy) and cls is not BaseStrategy:
                return cls(name=strategy_row["name"])
        logger.warning("No BaseStrategy subclass found in stored strategy code.")
        return None
    except Exception as e:
        logger.error(f"Failed to load strategy {strategy_row['id']}: {e}")
        return None


def _latest_close_for_symbol(data: pd.DataFrame, symbol: str) -> float:
    """Latest close price for a symbol from the multi-index DataFrame."""
    try:
        if symbol in data.index.get_level_values('symbol'):
            group = data.xs(symbol, level='symbol')
            return float(group['close'].iloc[-1])
    except Exception:
        pass
    return 0.0


def _log_backtest_detail(label: str, metrics: Dict[str, Any]) -> None:
    """Print full per-trade backtest detail so you can see if each guess was right."""
    trades = metrics.get("trades", []) or []
    print(f"\n=== {label} ({metrics.get('total_trades', 0)} trades) ===")
    print(f"  PnL: ${metrics.get('pnl', 0):.4f}  "
          f"Win rate: {metrics.get('win_rate', 0):.2%}  "
          f"Avg PnL: ${metrics.get('avg_pnl', 0):.4f}  "
          f"Max DD: {metrics.get('max_drawdown', 0):.2%}  "
          f"Sharpe: {metrics.get('sharpe', 0):.2f}  "
          f"Total return: {metrics.get('total_return_pct', 0):.2%}")
    if trades:
        for t in trades:
            right = "RIGHT" if t.get("direction_right") else "WRONG"
            print(f"  {t['symbol']} {t['direction']} "
                  f"entry={t['entry_px']} exit={t['exit_px']} "
                  f"pnl={t['pnl_pct']}% (${t['pnl_usd']}) [{right}]")
    print("====================================\n")
    logger.info(f"{label}: {metrics}")


def _test_passes(test_metrics: Dict[str, Any]) -> bool:
    """Out-of-sample gate: a strategy must pass on held-out test data before trading."""
    if test_metrics.get("total_trades", 0) == 0:
        return False
    if test_metrics.get("win_rate", 0.0) < 0.40:
        return False
    if test_metrics.get("sharpe", 0.0) <= 0.0:
        return False
    if test_metrics.get("total_return_pct", 0.0) <= 0.0:
        return False
    return True


# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("Orchestrator")


def _retire_and_mutate(strategy_row, memory, mutator, fallback_fn, executor, latest_prices, test_df, regime_label):
    """Retire a failing/crashing strategy, close its positions, and mutate a replacement."""
    sid = strategy_row['id']
    # Close all positions for the failing strategy
    open_trades = memory.get_open_trades()
    failing_trades = [t for t in open_trades if t['strategy_id'] == sid]
    for t in failing_trades:
        sym = t['symbol']
        exit_px = latest_prices.get(sym) or _latest_close_for_symbol(test_df, sym) or 0.0
        entry_px = t.get('entry_price') or 0.0
        qty = float(t.get('qty', 0))
        side = t.get('side', '').lower()
        pnl = (exit_px - entry_px) * qty if side == 'buy' else (entry_px - exit_px) * qty
        memory.close_trade_by_order_id(t['id'], exit_px, pnl)
        executor._close_position(sym)
        logger.info(
            f"FAIL CLOSE: {sym} {side} qty={qty} exit={exit_px:.2f} pnl={pnl:.2f} "
            f"strategy={sid[:8]}"
        )
    memory.update_strategy_status(sid, "retired")
    new_id = mutator.generate_new_strategy(parent_id=sid)
    if not new_id:
        logger.warning("LLM unavailable — using built-in fallback strategy.")
        fb = fallback_fn()
        new_id = fb["id"]
        memory.save_strategy(
            new_id, fb["name"], fb["code"],
            fb["parent_id"], fb["generation"],
        )
        logger.info(f"Using built-in strategy {new_id[:8]}")
    current_strategy = memory.get_strategy(new_id)
    memory.update_strategy_status(new_id, "active")
    # Log the failure as a lesson and rule immediately
    lesson = mutator.generate_lesson(
        strategy_id=sid,
        trades=memory.get_recent_trades(limit=20),
        metrics={**memory.get_trade_metrics()},
        regime_label=regime_label,
    )
    if lesson:
        print(f"\n=== LESSON (mutation trigger, strategy {new_id[:8]}) ===")
        print(lesson)
        print("==================================\n")
        logger.info(f"Lesson learned: {lesson}")
        reflection_id = memory.log_reflection(new_id, "Lesson", lesson)
        memory.save_rule(lesson, source_reflection_id=reflection_id)
        logger.info(f"Saved rule: {lesson}")
    return current_strategy


def _signals_from_test(test_signals_df: pd.DataFrame) -> Dict[str, int]:
    """Convert the most recent signal per symbol from the test DataFrame."""
    signals: Dict[str, int] = {}
    if 'signal' in test_signals_df.columns:
        for symbol, group in test_signals_df.groupby(level='symbol'):
            last_signal = int(group['signal'].iloc[-1])
            signals[symbol] = last_signal
    return signals


def _try_rule_fix(mutator: StrategyMutator,
                  memory: MemoryManager,
                  current_strategy: Dict[str, Any],
                  test_metrics: Dict[str, Any],
                  test_signals_df: pd.DataFrame,
                  regime_label: str,
                  latest_prices: Dict[str, float],
                  test_df: pd.DataFrame,
                  executor: StrategyExecutor,
                  drift_detector: DriftDetector) -> bool:
    """Attempt to fix exactly ONE rule that caused the out-of-sample failure.

    Returns True if a single rule was corrected and saved; False if no rule fix
    was applied (caller should fall back to whole-strategy mutation).

    The rule to fix is chosen by drift detection on real paper-trading outcomes:
    only a rule whose recent trades underperform its own baseline is a candidate.
    This means rules are decided/corrected based on paper trading results, and
    only the single responsible rule is touched — never the whole strategy.
    """
    try:
        rules = memory.get_all_rules()
        if not rules:
            return False

        # Prefer the rule that drift detection flags as actually going wrong.
        failing_rule = None
        drifting = drift_detector.evaluate_all_rules(window_days=14, drift_threshold=0.20)
        if drifting:
            worst = drifting[0]
            failing_rule = {'id': worst.rule_id, 'rule_text': worst.rule_text}
            logger.info(
                f"Drift flagged rule id={worst.rule_id} score={worst.drift_score:.2f}: "
                f"{worst.recommendation}"
            )
        else:
            # No drift data yet / nothing drifting: fall back to most recent rule.
            failing_rule = rules[0]
            logger.info("No drift signal — falling back to most recent rule for correction.")

        failing_trades = [t for t in memory.get_recent_trades(limit=20)
                          if t['strategy_id'] == current_strategy['id']]
        corrected = mutator.generate_rule_correction(
            failing_rule=failing_rule.get('rule_text', failing_rule.get('text', '')),
            failing_trades=failing_trades,
            metrics=test_metrics,
            regime_label=regime_label,
        )
        if not corrected:
            return False

        # Targeted update: replace ONLY this rule's text in-place
        old_text = failing_rule.get('rule_text') or failing_rule.get('text')
        if old_text:
            updated = memory.update_rule_by_text(old_text, corrected)
            logger.info(
                f"Rule fix applied: replaced [{old_text[:60]}...] -> "
                f"[{corrected[:60]}...] (updated={updated})"
            )
            # Save as a new rule too so the lesson is captured even if text match failed
            memory.save_rule(f"Rule: {corrected}", source_reflection_id=None)
            return True
        return False
    except Exception as e:
        logger.error(f"Rule fix attempt errored: {e}")
        return False


def main():
    # 1. Initialize components
    _setup_logging()
    settings = Settings.load(env_path=".env")
    memory = MemoryManager(settings)
    risk_engine = RiskEngine(settings)
    data_ingestor = DataIngestor(settings, memory)
    executor = StrategyExecutor(settings, risk_engine)
    mutator = StrategyMutator(settings, memory)
    backtester = Backtester(settings, risk_engine)

    # Runnable skills (self-reflection + drift detection + position sizing).
    agent_state = AgentState(task="self-evolving swing-trading daemon")
    evidence_store = EvidenceStore(db_path="data/agent_state.sqlite")
    drift_detector = DriftDetector(memory=memory, db_path="data/drift.sqlite")
    position_sizer = PositionSizer(
        max_risk_per_trade=0.02,
        kelly_cap=0.25,
        min_notional=float(settings.risk.max_order_size_usd or 1.0),
    )

    # Checkpoint agent state at startup so learning resumes across restarts.
    evidence_store.save_state(agent_state)

    logger.info("Orchestrator daemon started.")

    # 2. Main loop
    while True:
        try:
            logger.info("Starting new iteration...")

            # A. Fetch REAL market data and split into train / held-out test
            train_df, test_df = data_ingestor.get_train_test_split(train_days=60, test_days=14)
            if train_df.empty or test_df.empty:
                logger.warning("No market data received; skipping iteration.")
                time.sleep(60)
                continue

            # Current prices for live trading (most recent close per symbol)
            latest_prices: Dict[str, float] = {}
            for symbol in settings.universe.symbols:
                latest_prices[symbol] = _latest_close_for_symbol(test_df, symbol)

            regime = data_ingestor.detect_regime(test_df)
            regime_id = memory.save_regime(regime["label"], regime["indicators"])
            logger.info(f"Regime: {regime['label']} indicators={regime['indicators']}")

            # --- Price action and market structure analysis ---
            price_action_detector = PriceActionDetector()
            market_structure_detector = MarketStructureDetector()
            support_resistance_detector = SupportResistanceDetector()
            fibonacci_retracement = FibonacciRetracement()
            volume_analyzer = VolumeAnalyzer()

            price_patterns = price_action_detector.detect_candlestick_patterns(test_df)
            market_structure = market_structure_detector.detect_bos_choch(test_df)
            swing_points = market_structure_detector.detect_swing_points(test_df)
            s_r_zones = support_resistance_detector.detect_zones(test_df)
            fibonacci_levels = fibonacci_retracement.detect_retracement_levels(test_df)
            volume_profile = volume_analyzer.analyze_volume_profile(test_df)

            # --- Crowd/level detection (from course: crowded long/short, impulse+pullback) ---
            crowded_long = detect_crowded_long(test_df)
            crowded_short = detect_crowded_short(test_df)
            impulse_pullback = detect_impulse_pullback(test_df)

            # --- Proper risk-based position sizing ---
            risk_sizer = RiskSizer(default_risk_pct=0.01)  # 1% risk per trade

            # Daily loss kill switch
            daily_pnl = memory.get_daily_pnl()
            if daily_pnl <= -settings.risk.max_daily_loss_usd:
                logger.warning(
                    f"DAILY LOSS LIMIT REACHED: {daily_pnl:.2f} <= "
                    f"-{settings.risk.max_daily_loss_usd:.2f}. No new trades this cycle."
                )

            # B. Load / select / mutate strategy
            current_strategy = memory.get_active_strategy()
            strategy_changed = False

            if not current_strategy:
                logger.info("No active strategy found, generating new one.")
                strategy_id = mutator.generate_new_strategy()
                if not strategy_id:
                    logger.warning("LLM unavailable — using built-in fallback strategy.")
                    fb = get_fallback_strategy()
                    strategy_id = fb["id"]
                    memory.save_strategy(
                        strategy_id, fb["name"], fb["code"],
                        fb["parent_id"], fb["generation"],
                    )
                    current_strategy = memory.get_strategy(strategy_id)
                    logger.info(f"Using built-in strategy {current_strategy['id'][:8]}")
                else:
                    current_strategy = memory.get_strategy(strategy_id)
                memory.update_strategy_status(strategy_id, "active")
                strategy_changed = True
            elif memory.should_mutate(current_strategy['id']):
                logger.info("Alpha decay detected, mutating strategy.")
                # Close all positions for the retiring strategy before replacing it
                open_trades = memory.get_open_trades()
                retiring_trades = [t for t in open_trades if t['strategy_id'] == current_strategy['id']]
                for t in retiring_trades:
                    sym = t['symbol']
                    exit_px = latest_prices.get(sym) or _latest_close_for_symbol(test_df, sym) or 0.0
                    entry_px = t.get('entry_price') or 0.0
                    qty = float(t.get('qty', 0))
                    side = t.get('side', '').lower()
                    pnl = (exit_px - entry_px) * qty if side == 'buy' else (entry_px - exit_px) * qty
                    memory.close_trade_by_order_id(t['id'], exit_px, pnl)
                    executor._close_position(sym)
                    logger.info(
                        f"RETIRE CLOSE: {sym} {side} qty={qty} exit={exit_px:.2f} pnl={pnl:.2f} "
                        f"strategy={current_strategy['id'][:8]}"
                    )
                memory.update_strategy_status(current_strategy['id'], "retired")
                new_id = mutator.generate_new_strategy(parent_id=current_strategy['id'])
                if not new_id:
                    logger.warning("LLM unavailable — using built-in fallback strategy.")
                    fb = get_fallback_strategy()
                    new_id = fb["id"]
                    memory.save_strategy(
                        new_id, fb["name"], fb["code"],
                        fb["parent_id"], fb["generation"],
                    )
                    logger.info(f"Using built-in strategy {new_id[:8]}")
                current_strategy = memory.get_strategy(new_id)
                memory.update_strategy_status(new_id, "active")
                strategy_changed = True

            # C. Out-of-sample validation (NO CHEATING):
            #    Backtest on TRAIN data only, validate on held-out TEST data never seen before.
            instance = _load_strategy_instance(current_strategy)
            if instance is None:
                logger.error("Could not load strategy instance — treating as test failure, mutating.")
                _retire_and_mutate(current_strategy, memory, mutator, get_fallback_strategy, executor, latest_prices, test_df, regime["label"])
                logger.info("Iteration complete. Sleeping 1 hour.")
                time.sleep(3600)
                continue

            try:
                train_signals_df = instance.generate_signals(train_df)
                bt_metrics = backtester.run(instance, train_signals_df)
                _log_backtest_detail("Train backtest", bt_metrics)

                test_signals_df = instance.generate_signals(test_df)
                test_metrics = backtester.run(instance, test_signals_df)
                _log_backtest_detail("TEST (held-out) backtest", test_metrics)
            except Exception as e:
                logger.error(f"Strategy {current_strategy['id'][:8]} crashed backtester: {e} — treating as test failure, mutating.")
                _retire_and_mutate(current_strategy, memory, mutator, get_fallback_strategy, executor, latest_prices, test_df, regime["label"])
                logger.info("Iteration complete. Sleeping 1 hour.")
                time.sleep(3600)
                continue

            # D. Only trade if the strategy passes out-of-sample test.
            #    If it fails, self-improve immediately: retire it and mutate a new one.
            if not _test_passes(test_metrics):
                # --- Circuit breaker: pause after consecutive failures ---
                memory.increment_strategy_failure(current_strategy['id'])
                fail_count = memory.get_strategy_failure_count(current_strategy['id'])
                if fail_count >= 3:
                    logger.warning(
                        f"CIRCUIT BREAKER: Strategy {current_strategy['id'][:8]} has "
                        f"{fail_count} consecutive failures. Pausing new strategy "
                        f"generation for 24 hours."
                    )
                    logger.info("Iteration complete. Sleeping 24 hours.")
                    time.sleep(24 * 3600)
                    continue

                logger.warning(
                    f"Strategy {current_strategy['id'][:8]} FAILED out-of-sample test: "
                    f"win_rate={test_metrics.get('win_rate', 0):.2%} "
                    f"sharpe={test_metrics.get('sharpe', 0):.2f} "
                    f"return={test_metrics.get('total_return_pct', 0):.2%}. "
                )
                memory.log_reflection(
                    current_strategy['id'], "Out-of-sample Fail",
                    json.dumps(test_metrics)
                )

                # --- Targeted rule fix FIRST: try to fix just the responsible rule ---
                rule_fixed = _try_rule_fix(
                    mutator, memory, current_strategy, test_metrics,
                    test_signals_df, regime["label"], latest_prices, test_df, executor,
                    drift_detector,
                )
                if rule_fixed:
                    logger.info("Rule-level fix applied — re-testing the same strategy with corrected rules.")
                    # Re-run tests with the updated rules (strategy code unchanged)
                    try:
                        train_signals_df = instance.generate_signals(train_df)
                        bt_metrics = backtester.run(instance, train_signals_df)
                        _log_backtest_detail("Train backtest (after rule fix)", bt_metrics)

                        test_signals_df = instance.generate_signals(test_df)
                        test_metrics = backtester.run(instance, test_signals_df)
                        _log_backtest_detail("TEST (held-out) backtest (after rule fix)", test_metrics)
                    except Exception as e:
                        logger.error(f"Re-test after rule fix crashed: {e}")
                        current_strategy = _retire_and_mutate(
                            current_strategy, memory, mutator, get_fallback_strategy,
                            executor, latest_prices, test_df, regime["label"]
                        )
                        logger.info("Iteration complete. Sleeping 1 hour.")
                        time.sleep(3600)
                        continue

                    if _test_passes(test_metrics):
                        logger.info(
                            f"Strategy PASSED out-of-sample test after rule fix — "
                            f"win_rate={test_metrics.get('win_rate', 0):.2%} "
                            f"sharpe={test_metrics.get('sharpe', 0):.2f}"
                        )
                        signals = _signals_from_test(test_signals_df)
                        memory.reset_strategy_failure(current_strategy['id'])
                    else:
                        logger.warning(
                            f"Strategy STILL FAILED after rule fix — falling back to whole-strategy mutation."
                        )
                        current_strategy = _retire_and_mutate(
                            current_strategy, memory, mutator, get_fallback_strategy,
                            executor, latest_prices, test_df, regime["label"]
                        )
                        logger.info("Iteration complete. Sleeping 1 hour.")
                        time.sleep(3600)
                        continue
                else:
                    # No rule found to fix; fall back to whole-strategy mutation
                    current_strategy = _retire_and_mutate(
                        current_strategy, memory, mutator, get_fallback_strategy,
                        executor, latest_prices, test_df, regime["label"]
                    )
                    logger.info("Iteration complete. Sleeping 1 hour.")
                    time.sleep(3600)
                    continue
            else:
                logger.info(
                    f"Strategy {current_strategy['id'][:8]} PASSED out-of-sample test — "
                    f"win_rate={test_metrics.get('win_rate', 0):.2%} "
                    f"sharpe={test_metrics.get('sharpe', 0):.2f}"
                )
                memory.reset_strategy_failure(current_strategy['id'])
                signals = _signals_from_test(test_signals_df)

            # --- Build trading signals with full trade details ---
            trade_signals = []
            for symbol, signal in signals.items():
                if signal == 0:
                    continue
                signal_type = 'buy' if signal == 1 else 'sell'
                entry_price = latest_prices.get(symbol) or _latest_close_for_symbol(test_df, symbol) or 0.0
                stop_loss_price = entry_price * 0.95 if signal == 1 else entry_price * 1.05
                target_price = entry_price * 1.10 if signal == 1 else entry_price * 0.90

                position_size = size_position_for_trade(
                    account_equity_usd=float(settings.risk.max_capital_usd),
                    entry_price=entry_price,
                    stop_loss_price=stop_loss_price,
                    risk_pct=0.01,
                    side=signal_type
                )

                trade_signals.append({
                    'signal_type': signal_type,
                    'entry_price': entry_price,
                    'stop_loss_price': stop_loss_price,
                    'target_price': target_price,
                    'qty': position_size.qty,
                    'risk_amount_usd': position_size.risk_amount_usd,
                    'risk_pct': position_size.risk_pct,
                    'reward_to_risk_ratio': position_size.reward_to_risk_ratio,
                    'confidence': 'medium',
                    'pattern': '',
                    'regime': regime['label'],
                })

            # --- Print owner-friendly dashboard ---
            print("\n" + "=" * 60)
            print("TRADING AI DASHBOARD")
            print("=" * 60)
            print(f"Time:         {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"Strategy:     {current_strategy['name']} ({current_strategy['id'][:8]})")
            print(f"Market Regime: {regime['label'].title()}")
            print(f"Indicators:   {regime['indicators']}")
            print(f"Daily PnL:    ${daily_pnl:+,.2f}")
            print()

            # Show detected patterns and structure
            print("PRICE ACTION & MARKET STRUCTURE:")
            print("-" * 60)
            for symbol, patterns in price_patterns.items():
                if patterns:
                    print(f"  {symbol}: {', '.join(patterns[:5])}")
            if crowded_long:
                print("  CROWDED LONG detected - potential reversal signal")
            if crowded_short:
                print("  CROWDED SHORT detected - potential reversal signal")
            if impulse_pullback:
                print("  IMPULSE + PULLBACK rhythm detected")
            print()

            print("BREAK OF STRUCTURE / CHANGE OF CHARACTER:")
            print("-" * 60)
            for b in market_structure.get('bos', [])[:5]:
                print(f"  {b['type']} at ${b['level']:,.2f} ({b['direction']})")
            for c in market_structure.get('choch', [])[:5]:
                print(f"  {c['type']} at ${c['level']:,.2f}")
            print()

            print("SUPPORT / RESISTANCE ZONES:")
            print("-" * 60)
            for zone in s_r_zones.get('support', [])[:3]:
                print(f"  Support: ${zone['price_low']:,.2f} - ${zone['price_high']:,.2f} ({zone['confidence']})")
            for zone in s_r_zones.get('resistance', [])[:3]:
                print(f"  Resistance: ${zone['price_low']:,.2f} - ${zone['price_high']:,.2f} ({zone['confidence']})")
            print()

            print("FIBONACCI GOLDEN ZONE:")
            print("-" * 60)
            for fib in fibonacci_levels.get('golden_zone', []):
                at_zone = "AT ZONE" if fib.get('current_at_zone') else ""
                print(f"  {fib['symbol']}: ${fib['low']:,.2f} - ${fib['high']:,.2f} {at_zone}")
            print()

            print("TRADE SIGNALS:")
            print("-" * 60)
            if trade_signals:
                for ts in trade_signals:
                    print(format_trade_signal(
                        symbol=ts['entry_price'] and "N/A" or "N/A",  # placeholder
                        signal_type=ts['signal_type'],
                        entry_price=ts['entry_price'],
                        stop_loss_price=ts['stop_loss_price'],
                        target_price=ts['target_price'],
                        qty=ts['qty'],
                        risk_amount_usd=ts['risk_amount_usd'],
                        risk_pct=ts['risk_pct'],
                        reward_to_risk_ratio=ts['reward_to_risk_ratio'],
                        confidence=ts['confidence'],
                        pattern=ts['pattern'],
                        regime=ts['regime'],
                    ))
            else:
                print("  No active trade signals")
            print()

            # Full analysis report for each symbol in universe
            for symbol in settings.universe.symbols:
                price_data = {'current_price': latest_prices.get(symbol, 0)}
                print(format_analysis_report(
                    symbol=symbol,
                    price_data=price_data,
                    support_resistance=s_r_zones,
                    fibonacci=fibonacci_levels,
                    volume_profile=volume_profile,
                    market_structure=market_structure,
                    trade_signals=[ts for ts in trade_signals if ts.get('entry_price') > 0],
                ))

            logger.info(
                f"Strategy {current_strategy['id'][:8]} ready — "
                f"win_rate={test_metrics.get('win_rate', 0):.2%} "
                f"sharpe={test_metrics.get('sharpe', 0):.2f}"
            )

            logger.info(
                f"Strategy {current_strategy['id'][:8]} PASSED out-of-sample test — "
                f"win_rate={test_metrics.get('win_rate', 0):.2%} "
                f"sharpe={test_metrics.get('sharpe', 0):.2f}"
            )

            # F. Exit checks (stop-loss / take-profit / max-hold) on open positions
            open_trades = memory.get_open_trades()
            trade_opened_at: Dict[str, float] = {}
            for t in open_trades:
                try:
                    opened_at = datetime.fromisoformat(t['created_at']).timestamp()
                except Exception:
                    opened_at = time.time()
                trade_opened_at[t['symbol']] = opened_at

            exit_events = executor.check_exits(
                open_trades,
                latest_prices,
                trade_opened_at,
                max_hold_seconds=7 * 86400,      # 7 days max hold (swing)
                stop_loss_pct=0.05,               # 5% stop loss
                take_profit_pct=0.10,             # 10% take profit
            )

            for ev in exit_events:
                sym = ev['symbol']
                memory.close_trade_for_symbol(sym, ev['price'], ev['pnl'])
                executor._close_position(sym)
                logger.info(
                    f"EXIT {ev['reason']}: {sym} {ev['side']} qty={ev['qty']} "
                    f"price={ev['price']:.2f} pnl={ev['pnl']:.2f} strategy={current_strategy['id'][:8]}"
                )
                # Record outcome against each rule for drift detection (paper-trading based).
                try:
                    for rule in memory.get_all_rules():
                        drift_detector.record_outcome(
                            rule_id=rule['id'],
                            rule_text=rule['rule_text'],
                            trade_id=str(ev['symbol'] + ev['price']),
                            pnl=float(ev['pnl']),
                            trade_date=datetime.now(),
                        )
                except Exception as e:
                    logger.debug(f"Drift outcome record skipped: {e}")

            # G. Reconcile DB open trades with Alpaca (mark filled if position gone)
            reconciled = executor.reconcile_trades(open_trades, latest_prices)
            for t in reconciled:
                if t['status'] == 'filled':
                    memory.close_trade_by_order_id(t['id'], t['price'], t['pnl'])
                    logger.info(
                        f"RECONCILED CLOSE: {t['symbol']} exit={t['price']:.2f} pnl={t['pnl']:.2f}"
                    )

            # H. New position opens (paper trading, only if daily loss limit not hit)
            if daily_pnl > -settings.risk.max_daily_loss_usd:
                open_events = executor.sync_positions(signals)
                for event in open_events:
                    if event["type"] == "open":
                        price = event["price"]
                        if price <= 0:
                            price = latest_prices.get(event["symbol"]) or _latest_close_for_symbol(test_df, event["symbol"]) or 0.0

                        # --- Proper risk-based position sizing ---
                        # Get entry and stop loss from signals or calculate from price action
                        entry_price = price
                        stop_loss_price = event["price"] * 0.95 if event["side"] == "buy" else event["price"] * 1.05

                        # Calculate position size using risk-based sizing
                        position_size = size_position_for_trade(
                            account_equity_usd=float(settings.risk.max_capital_usd),
                            entry_price=entry_price,
                            stop_loss_price=stop_loss_price,
                            risk_pct=0.01,  # 1% risk per trade
                            side=event["side"]
                        )

                        qty = position_size.qty
                        logger.info(
                            f"Position sizing: Risk-based sizing - qty={qty:.4f}, "
                            f"risk=${position_size.risk_amount_usd:.2f} "
                            f"R:R={position_size.reward_to_risk_ratio}:1"
                        )

                        memory.log_trade_open(
                            order_id=str(event["order_id"]),
                            strategy_id=current_strategy['id'],
                            symbol=event["symbol"],
                            qty=qty,
                            side=event["side"],
                            regime_id=regime_id,
                        )
                        logger.info(
                            f"TRADE OPEN: {event['symbol']} {event['side']} qty={qty:.4f} "
                            f"price={price:.2f} strategy={current_strategy['id'][:8]}"
                        )
            else:
                logger.info("Skipping new position opens due to daily loss limit.")

            # I. Learn Loop — generate lesson from real trades, print it, save as rule
            recent_trades = memory.get_recent_trades(limit=20)
            all_metrics = {**memory.get_trade_metrics(), **test_metrics}
            lesson = mutator.generate_lesson(
                strategy_id=current_strategy['id'],
                trades=recent_trades,
                metrics=all_metrics,
                regime_label=regime["label"],
            )
            if lesson:
                print(f"\n=== LESSON (strategy {current_strategy['id'][:8]}, regime {regime['label']}) ===")
                print(lesson)
                print("==================================\n")
                logger.info(f"Lesson learned: {lesson}")
                reflection_id = memory.log_reflection(
                    current_strategy['id'], "Lesson", lesson
                )
                memory.save_rule(lesson, source_reflection_id=reflection_id)
                logger.info(f"Saved rule #{reflection_id}: {lesson}")
            else:
                logger.warning("Could not generate a lesson this iteration.")

            # J. Reflection / metrics logging
            metrics = memory.get_trade_metrics()
            logger.info(f"Performance Metrics: {metrics}")
            memory.log_reflection(
                current_strategy['id'], "Performance Metrics", json.dumps(metrics)
            )

            logger.info("Iteration complete. Sleeping 1 hour.")
            time.sleep(3600)  # Run hourly

        except Exception as e:
            logger.error(f"Error in orchestrator loop: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()
