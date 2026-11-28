"""Historical portfolio replay on an ordered quote tape."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from itertools import groupby
from operator import attrgetter

import numpy as np
import pandas as pd
from config import PAPER_EQUITIES
from hourly_features import ALL_READY
from portfolio_events import Decision, ExecutionPolicy, MarketFrame, Quote, QuotePortfolio
from portfolio_policy import allocated_targets, portfolio_entry_budget


class Backtester:

    def __init__(self, settings, risk_engine=None):
        self.r = settings.risk
        self.symbols = PAPER_EQUITIES.intersection(settings.universe.equities)

    def _empty_metrics(self, reason):
        return dict(_reason=reason, total_trades=0, trades=[], pnl=0.0, max_drawdown=0.0, win_rate=None, avg_pnl=None, sharpe=None, total_return_pct=0.0)

    def _policy(self):
        return ExecutionPolicy(
            capital=Decimal(str(self.r.max_capital_usd)),
            order_cap=Decimal(str(self.r.max_order_size_usd)),
            position_cap=Decimal(str(self.r.max_position_usd)),
            minimum=Decimal(str(self.r.min_order_size_usd)),
            daily_loss=Decimal(str(self.r.max_daily_loss_usd)),
            fee_per_order=Decimal(str(self.r.commission_per_order_usd)),
        )

    def run_tape(self, frames, policy=None):
        """Replay immutable, already joined decisions and executable quotes."""
        portfolio = QuotePortfolio(self._policy() if policy is None else policy)
        for frame in frames:
            portfolio.step(frame)
        return portfolio

    def _decision_frames(self, strategy, df, quotes):
        """Join decisions as of their recorded availability to successive quote batches."""
        required = {'interval_kind', 'interval_start', 'interval_end', 'received_at',
                    'decided_at', 'atr', 'history_ready', 'readiness_mask'}
        if required.difference(df.columns) or (not strategy and 'strategy' not in df):
            raise ValueError('missing_decision_evidence')
        if df.index.names != ['symbol', 'timestamp'] or df.index.has_duplicates:
            raise ValueError('invalid_decision_identity')

        def instant(value):
            stamp = pd.Timestamp(value)
            if pd.isna(stamp) or stamp.tz is None:
                raise ValueError('invalid_decision_timestamp')
            return stamp.to_pydatetime()

        decisions = []
        for row in df.reset_index().itertuples(index=False):
            if row.symbol not in self.symbols or row.interval_kind != 'XNYS_1m':
                continue
            end = instant(row.interval_end)
            if instant(row.timestamp) != end or row.signal not in (0, 1):
                raise ValueError('invalid_decision_identity')
            owner = getattr(row, 'strategy', strategy)
            if not isinstance(owner, str) or not owner:
                raise ValueError('missing_strategy_identity')
            if strategy and owner != strategy:
                raise ValueError('conflicting_strategy_identity')
            ready = (row.history_ready is True and pd.notna(row.readiness_mask)
                     and row.readiness_mask == ALL_READY)
            if pd.isna(row.received_at) or pd.isna(row.decided_at):
                raise ValueError('missing_decision_evidence')
            decision = Decision(
                row.symbol, owner, instant(row.interval_start), end,
                instant(row.received_at), instant(row.decided_at), int(row.signal),
                Decimal('NaN') if pd.isna(row.atr) else Decimal(str(row.atr)), ready,
            )
            decisions.append(decision)
        decisions.sort(key=lambda d: (d.decided, d.end, d.symbol))

        index = 0
        for frame in quotes:
            if frame.decisions:
                raise ValueError('quote_tape_contains_external_decisions')
            batch = {}
            while index < len(decisions) and decisions[index].decided <= frame.at:
                decision = decisions[index]
                index += 1
                if decision.start < frame.session_open or decision.end > frame.session_close:
                    continue
                previous = batch.get(decision.symbol)
                if previous is None or decision.end > previous.end:
                    batch[decision.symbol] = decision
            yield replace(frame, decisions=tuple(batch.values()))

    def _proxy_frames(self, strategy, df, half_spread):
        """Two explicitly hypothetical quote observations per verified XNYS interval."""
        from market_time import equity_sessions

        times = df.index.get_level_values('timestamp')
        if not isinstance(times, pd.DatetimeIndex) or times.tz is None or times.hasnans:
            raise ValueError('invalid_session_timestamp')
        dates = times.tz_convert('America/New_York')
        sessions = equity_sessions(dates.min().date(), dates.max().date() + timedelta(days=1))
        schedule = {}
        for session in sessions:
            opening = pd.Timestamp(session.opened, unit='s', tz='UTC')
            closing = pd.Timestamp(session.cuts[-1], unit='s', tz='UTC')
            start = opening
            for seconds in session.cuts:
                end = pd.Timestamp(seconds, unit='s', tz='UTC')
                schedule[end] = opening, closing, start
                start = end

        if not times.isin(schedule).all():
            raise ValueError('unscheduled_equity_interval')
        ordered = df.reset_index().sort_values(['timestamp', 'symbol']).itertuples(index=False)
        batches = groupby(ordered, key=attrgetter('timestamp'))
        observed, rows = next(batches)
        first_cut, last_cut = times.min(), times.max()
        for cut, (opening, closing, start) in schedule.items():
            if cut < first_cut or cut > last_cut:
                continue
            if cut != observed:
                # Keep missing intervals visible when an open lot depends on them.
                yield MarketFrame(cut, opening, closing)
                continue
            after_open = start + timedelta(seconds=1)
            decisions, first, last = [], [], []
            for row in rows:
                if pd.Timestamp(row.interval_start) != start or pd.Timestamp(row.interval_end) != cut:
                    raise ValueError('invalid_interval_boundaries')
                for price, at, batch in ((row.open, after_open, first), (row.close, cut, last)):
                    mid = Decimal(str(price))
                    if not mid.is_finite() or mid <= 0:
                        raise ValueError('invalid_ohlc_price')
                    batch.append(Quote(row.symbol, at, mid * (1 - half_spread), mid * (1 + half_spread)))
                # Readiness is explicitly unknown during warmup. It never creates a flat signal.
                if row.history_ready is not True or pd.isna(row.readiness_mask) or row.readiness_mask != ALL_READY:
                    continue
                if pd.isna(row.atr):
                    raise ValueError('ready_decision_missing_atr')
                atr = Decimal(str(row.atr))
                if not atr.is_finite() or atr <= 0:
                    raise ValueError('invalid_ready_atr')
                owner = getattr(row, 'strategy', strategy or 'historical-target')
                if not isinstance(owner, str) or not owner:
                    raise ValueError('missing_strategy_identity')
                decisions.append(Decision(row.symbol, owner, start, cut, cut, cut, int(row.signal), atr))
            yield MarketFrame(after_open, opening, closing, quotes=first)
            yield MarketFrame(cut, opening, closing, decisions=decisions, quotes=last)
            observed, rows = next(batches, (None, ()))

    def _session_proxy(self, strategy, df):
        required = {'open', 'close', 'interval_kind', 'interval_start', 'interval_end',
                    'atr', 'history_ready', 'readiness_mask'}
        model = 'exploratory_session_ohlc_quote_proxy_v3'
        if required.difference(df.columns):
            return dict(self._empty_metrics('missing_quote_tape'), execution_model=model)
        if df.index.names != ['symbol', 'timestamp'] or df.index.has_duplicates:
            return dict(self._empty_metrics('invalid_input'), execution_model=model)
        equity = df.loc[df.index.get_level_values('symbol').isin(self.symbols)]
        if equity.empty or not equity.interval_kind.eq('XNYS_1m').all():
            return dict(self._empty_metrics('missing_xnys_intervals'), execution_model=model)
        if not equity.signal.isin([0, 1]).all():
            return dict(self._empty_metrics('invalid_input'), execution_model=model)
        effective_bps = Decimal(str(self.r.spread_bps)) + Decimal(str(self.r.slippage_bps))
        if not effective_bps.is_finite() or not 0 <= effective_bps < 20000:
            return dict(self._empty_metrics('invalid_cost_or_capital'), execution_model=model)
        frames = self._proxy_frames(strategy, equity, effective_bps / 20000)
        portfolio = QuotePortfolio(self._policy())
        try:
            for frame in frames:
                if portfolio.position and all(q.symbol != portfolio.position.symbol for q in frame.quotes):
                    return dict(self._empty_metrics('missing_held_observation'), execution_model=model)
                portfolio.step(frame)
            result = self._quote_metrics(portfolio)
        except (ValueError, InvalidOperation) as exc:
            return dict(self._empty_metrics(str(exc)), execution_model=model)
        result.update(
            execution_model=model,
            execution_assumptions={
                'availability': 'Hypothetical publication and decision at the completed XNYS cut.',
                'quote_time': 'Open price one second after interval start and close price at interval end.',
                'spread': 'OHLC treated as midpoint. Spread and slippage allowances form the effective spread.',
                'effective_spread_bps': str(effective_bps),
                'intrabar': 'No intrabar quotes. Stops and closing reductions wait for the next modeled bid.',
                'fees': 'Configured commission per order is an estimate.',
            },
        )
        return result

    def _quote_metrics(self, portfolio):
        account = portfolio.account
        trades = []
        entry = None
        closed_net = Decimal(0)
        wins = 0
        for fill in portfolio.fills:
            if fill.side == 'buy':
                entry = fill
                continue
            net = fill.qty * (fill.price - entry.price) - entry.fee - fill.fee
            closed_net += net
            wins += int(net > 0)
            trades.append(dict(
                symbol=fill.symbol, strategy=fill.strategy, entry_time=entry.at.isoformat(),
                exit_time=fill.at.isoformat(), entry_price=float(entry.price),
                exit_price=float(fill.price), qty=str(fill.qty), direction='LONG',
                direction_right=int(fill.price > entry.price), exit_reason=fill.reason,
                pnl_pct=float(net / (entry.qty * entry.price)), pnl_usd=float(net),
            ))
            entry = None
        count = len(trades)
        equity = account.equity
        pnl = None if equity is None else equity - portfolio.policy.capital
        result = dict(
            execution_model='observed_quote_simulation_v3',
            total_trades=count, trades=trades, pnl=None if pnl is None else float(pnl),
            total_return_pct=None if pnl is None else float(pnl / portfolio.policy.capital),
            max_drawdown=-float(account.max_drawdown), sharpe=None,
            avg_pnl=float(closed_net / count) if count else None,
            win_rate=wins / count if count else None,
            marked_equity=None if equity is None else str(equity),
            gross_realized=str(account.realized),
            unrealized=None if account.unrealized is None else str(account.unrealized),
            confirmed_fees=str(account.confirmed_fees), estimated_fees=str(account.estimated_fees),
        )
        if account.qty:
            result['_reason'] = 'unresolved_exposure'
        elif not count:
            result['_reason'] = 'zero_trades'
        return result

    def run(self, strategy, df, *, quote_tape=None):
        """Evaluate target rows. A quote-backed run requires point-in-time decision facts.

        Legacy frames without interval provenance retain the historical next-open
        diagnostic. Session-tagged data cannot silently enter that diagnostic.
        """
        if df.empty or 'signal' not in df:
            return self._empty_metrics('no_data_or_no_signals')
        if quote_tape is not None:
            frames = self._decision_frames(strategy, df, quote_tape)
            return self._quote_metrics(self.run_tape(frames))
        if {'interval_kind', 'interval_start', 'interval_end'}.intersection(df.columns):
            return self._session_proxy(strategy, df)
        metrics = self._legacy_ohlc(df)
        metrics['execution_model'] = 'exploratory_legacy_next_open_proxy'
        return metrics

    def _legacy_ohlc(self, df):
        symbols = df.index.get_level_values('symbol').unique()
        capital = float(self.r.max_capital_usd)
        if df.index.has_duplicates or not df.signal.isin([0, 1]).all():
            return self._empty_metrics('invalid_input')
        delayed = df.sort_index().signal.groupby(level='symbol').shift(fill_value=0)
        try:
            targets = allocated_targets(delayed.unstack('symbol'))
        except ValueError:
            return self._empty_metrics('missing_held_observation')
        unit = float(portfolio_entry_budget(*(Decimal(str(x)) for x in
                    (capital, capital, self.r.max_order_size_usd, self.r.max_position_usd))))
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
            target = targets[sym].reindex(g.index).astype(int)
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
