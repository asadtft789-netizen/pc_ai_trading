"""Equal initial capital sleeves, vectorized next-open long/flat returns; flat boundary at end."""
import numpy as np
import pandas as pd

class Backtester:

    def __init__(self, settings, risk_engine=None):
        self.r = settings.risk

    def _empty_metrics(self, reason):
        return dict(_reason=reason, total_trades=0, trades=[], pnl=0.0, max_drawdown=0.0, win_rate=None, avg_pnl=None, sharpe=None, total_return_pct=0.0)

    def run(self, strategy, df):
        if df.empty or 'signal' not in df:
            return self._empty_metrics('no_data_or_no_signals')
        symbols = df.index.get_level_values('symbol').unique()
        capital = float(self.r.max_capital_usd)
        unit = min(capital / len(symbols), self.r.max_order_size_usd, self.r.max_position_usd)
        fee = max(0.0, unit * (self.r.spread_bps + self.r.slippage_bps) / 20000 + self.r.commission_per_order_usd)
        trades = []
        legs = []
        if not unit > 0 or not np.isfinite([unit, fee, capital]).all():
            return self._empty_metrics('invalid_cost_or_capital')
        for sym in symbols:
            g = df.xs(sym, level='symbol').sort_index()
            o = g.open.astype(float)
            p = g.close.astype(float)
            sig = g.signal
            if g.index.has_duplicates or not sig.isin([0, 1]).all() or (not (np.isfinite(o) & np.isfinite(p) & (o > 0) & (p > 0)).all()):
                return self._empty_metrics('invalid_input')
            target = sig.shift(fill_value=0)
            change = target.diff().fillna(target)
            entries = change.eq(1)
            exits = change.eq(-1)
            qty = (unit / o).where(entries).ffill().fillna(0) * target
            increments = qty.shift(fill_value=0) * (o - p.shift().fillna(o)) + qty * (p - o) - fee * (entries | exits)
            starts = list(np.flatnonzero(entries))
            ends = [(i, o.iloc[i]) for i in np.flatnonzero(exits)]
            if target.iloc[-1]:
                ends.append((len(g) - 1, p.iloc[-1]))
                increments.iloc[-1] -= fee
            for i, (j, price) in zip(starts, ends):
                net = unit * (price / o.iloc[i] - 1) - 2 * fee
                trades.append(dict(symbol=sym, entry_time=g.index[i].isoformat(), exit_time=g.index[j].isoformat(), entry_price=float(o.iloc[i]), exit_price=float(price), direction='LONG', pnl_pct=net / unit, pnl_usd=float(net), direction_right=int(price > o.iloc[i])))
            legs.append(increments)
        equity = capital + pd.concat(legs, axis=1).fillna(0).sum(axis=1).cumsum()
        n = len(trades)
        if not n:
            return self._empty_metrics('zero_trades')
        changes = equity.diff().fillna(equity.iloc[0] - capital)
        dd = equity / equity.cummax().clip(lower=capital) - 1
        net = float(equity.iloc[-1] - capital)
        return dict(total_trades=n, trades=trades, pnl=net, max_drawdown=float(dd.min()), win_rate=sum((t['pnl_usd'] > 0 for t in trades)) / n, avg_pnl=net / n, sharpe=float(changes.mean() / changes.std()) if changes.std() > 0 else None, total_return_pct=net / capital)
