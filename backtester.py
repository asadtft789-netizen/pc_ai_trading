"""Vectorized backtester for the self-evolving trader.

Simulates a strategy's signals on historical data and returns realistic
performance metrics (PnL, win rate, max drawdown, Sharpe) for the
post-mortem / mutation loop.

The equity curve is aggregated at portfolio level: per-symbol returns are
aligned on a common timestamp index and equal-weighted, so Sharpe and
drawdown are computed on a single coherent series rather than a flat
concatenation of per-symbol curves that may live on different time bases.
"""

import logging
import pandas as pd
import numpy as np
from typing import Dict, Any

from strategy import BaseStrategy
from risk_engine import RiskEngine
from config import Settings

logger = logging.getLogger(__name__)


class Backtester:
    """Runs a vectorized backtest of a strategy on historical bars."""

    def __init__(self, settings: Settings, risk_engine: RiskEngine):
        self.settings = settings
        self.risk_engine = risk_engine
        self.position_usd = settings.risk.max_order_size_usd

        # Validate costs up front — a negative round-trip cost would pay the
        # strategy to trade, which corrupts every metric downstream.
        round_trip_bps = (
            settings.risk.spread_bps + settings.risk.slippage_bps
        )
        comm_per_order = settings.risk.commission_per_order_usd
        self.round_trip_cost_usd = (
            self.position_usd * round_trip_bps / 10000.0 + 2 * comm_per_order
        )
        if self.round_trip_cost_usd < 0:
            logger.warning(
                "Round-trip trading cost is negative (%s) — clamping to 0.",
                self.round_trip_cost_usd,
            )
            self.round_trip_cost_usd = 0.0

    def run(self, strategy: BaseStrategy, df: pd.DataFrame) -> Dict[str, Any]:
        """Backtest a strategy on a multi-index (timestamp, symbol) DataFrame.

        Returns metrics: pnl, total_trades, win_rate, avg_pnl,
        max_drawdown, sharpe, total_return_pct, plus per-trade details
        under 'trades'.
        """
        if df.empty or 'signal' not in df.columns:
            return self._empty_metrics(reason="no_data_or_no_signals")

        all_trade_pnls: list = []
        all_trades: list = []

        try:
            # Build per-symbol return series aligned on a common timestamp index.
            symbols = df.index.get_level_values('symbol').unique()
            symbol_rets: Dict[str, pd.Series] = {}

            for symbol in symbols:
                group = df.xs(symbol, level='symbol').sort_index()
                close = group['close'].astype(float)
                signal = group['signal'].astype(float)

                # Position: nonzero signal sets direction; 0 = flat; carry last nonzero forward.
                position = signal.where(signal != 0).ffill().fillna(0.0)

                # Strategy return: position taken at t-1 realizes return at t.
                ret = close.pct_change()
                strat_ret = position.shift(1).fillna(0.0) * ret

                # Deduct realistic round-trip trading cost at each entry (position change).
                entry_bars = (position != 0) & (position.shift(1).fillna(0.0) != position)
                cost_frac = self.round_trip_cost_usd / max(self.position_usd, 1e-9)
                strat_ret = strat_ret - entry_bars.astype(float) * cost_frac

                symbol_rets[symbol] = strat_ret

                # Detect trades: entry when position goes 0 -> nonzero; exit when nonzero -> 0 or flips.
                in_trade = position != 0
                trade_starts = in_trade & (~in_trade.shift(1).fillna(False))
                trade_ends = (~in_trade) & (in_trade.shift(1).fillna(False))

                # Flip entries/exits: when position changes sign while in a trade
                flips = (position != 0) & (position.shift(1) != 0) & (
                    np.sign(position) != np.sign(position.shift(1))
                )
                trade_ends = trade_ends | flips

                start_idx = None
                for i in range(len(group)):
                    if trade_starts.iloc[i] and start_idx is None:
                        start_idx = i
                    elif trade_ends.iloc[i] and start_idx is not None:
                        entry_px = float(close.iloc[start_idx])
                        exit_px = float(close.iloc[i])
                        entry_pos = float(position.iloc[start_idx])
                        if entry_pos > 0:
                            pnl_pct = (exit_px - entry_px) / entry_px
                        else:
                            pnl_pct = (entry_px - exit_px) / entry_px
                        trade_pnl = pnl_pct * self.position_usd - self.round_trip_cost_usd
                        direction = "LONG" if entry_pos > 0 else "SHORT"
                        direction_right = (exit_px > entry_px) if entry_pos > 0 else (exit_px < entry_px)
                        all_trade_pnls.append(trade_pnl)
                        all_trades.append({
                            "symbol": symbol,
                            "entry_time": str(group.index[start_idx][1]) if isinstance(group.index[start_idx], tuple) else str(group.index[start_idx]),
                            "exit_time": str(group.index[i][1]) if isinstance(group.index[i], tuple) else str(group.index[i]),
                            "entry_px": round(entry_px, 4),
                            "exit_px": round(exit_px, 4),
                            "direction": direction,
                            "pnl_pct": round(pnl_pct * 100, 2),
                            "pnl_usd": round(trade_pnl, 4),
                            "direction_right": direction_right,
                        })
                        start_idx = None

                # Close any open trade at the last bar
                if start_idx is not None:
                    entry_px = float(close.iloc[start_idx])
                    exit_px = float(close.iloc[-1])
                    entry_pos = float(position.iloc[start_idx])
                    pnl_pct = (exit_px - entry_px) / entry_px if entry_pos > 0 else (entry_px - exit_px) / entry_px
                    trade_pnl = pnl_pct * self.position_usd - self.round_trip_cost_usd
                    direction = "LONG" if entry_pos > 0 else "SHORT"
                    direction_right = (exit_px > entry_px) if entry_pos > 0 else (exit_px < entry_px)
                    all_trade_pnls.append(trade_pnl)
                    all_trades.append({
                        "symbol": symbol,
                        "entry_time": str(group.index[start_idx][1]) if isinstance(group.index[start_idx], tuple) else str(group.index[start_idx]),
                        "exit_time": str(group.index[-1][1]) if isinstance(group.index[-1], tuple) else str(group.index[-1]),
                        "entry_px": round(entry_px, 4),
                        "exit_px": round(exit_px, 4),
                        "direction": direction,
                        "pnl_pct": round(pnl_pct * 100, 2),
                        "pnl_usd": round(trade_pnl, 4),
                        "direction_right": direction_right,
                    })

            # --- Portfolio-level equity curve (equal-weighted across symbols) ---
            if symbol_rets:
                rets_df = pd.DataFrame(symbol_rets)
                portfolio_ret = rets_df.mean(axis=1).fillna(0.0)
                cumulative = (1.0 + portfolio_ret).cumprod()
                equity_curve = cumulative.tolist()
            else:
                equity_curve = []

        except Exception as e:
            logger.error(f"Backtest error: {e}")
            return self._empty_metrics(reason="error")

        total_pnl = sum(all_trade_pnls)
        total_trades = len(all_trade_pnls)
        wins = [p for p in all_trade_pnls if p > 0]
        win_rate = len(wins) / total_trades if total_trades else 0.0
        avg_pnl = total_pnl / total_trades if total_trades else 0.0

        # Max drawdown from equity curve
        if equity_curve:
            equity = pd.Series(equity_curve)
            running_max = equity.cummax()
            drawdown = (equity - running_max) / running_max
            max_drawdown = float(drawdown.min()) if len(drawdown) else 0.0
            total_return_pct = float(equity.iloc[-1] - 1.0) if len(equity) else 0.0
        else:
            max_drawdown = 0.0
            total_return_pct = 0.0

        # Sharpe (daily, annualized sqrt(252))
        if equity_curve and len(equity_curve) > 1:
            rets = pd.Series(equity_curve).pct_change().dropna()
            sharpe = float(rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0.0
        else:
            sharpe = 0.0

        return {
            "pnl": round(total_pnl, 4),
            "total_trades": total_trades,
            "win_rate": round(win_rate, 4),
            "avg_pnl": round(avg_pnl, 4),
            "max_drawdown": round(max_drawdown, 4),
            "sharpe": round(sharpe, 4),
            "total_return_pct": round(total_return_pct, 4),
            "trades": all_trades,
        }

    def _empty_metrics(self, reason: str) -> Dict[str, Any]:
        """Return a zeroed metrics dict with a reason tag for the zero-trade gate."""
        return {
            "pnl": 0.0,
            "total_trades": 0,
            "win_rate": 0.0,
            "avg_pnl": 0.0,
            "max_drawdown": 0.0,
            "sharpe": 0.0,
            "total_return_pct": 0.0,
            "trades": [],
            "_reason": reason,
        }
