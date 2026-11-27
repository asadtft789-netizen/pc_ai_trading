"""Development-only regime reviews with immutable inputs and independent progress."""
import hashlib
import json
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path

import pandas as pd

from backtester import Backtester
from hourly_features import ALL_READY, FEATURE_VERSION, feature_column, signals, strategy_flags, validate_raw
from market_observations import record_observation
from paper_store import db, key, now, put
from phoenix_tournament import FOCUSES, run_development
from research_admission import reserve_lanes
from review_progress import advance_review, review_marks
from strategy_worker import SEED

REGIMES = ['trend_up', 'trend_down', 'range_quiet', 'vol_expansion']


def analyze_failure(outcome):
    """Report observable entry associations, preserving unidentifiable cases."""
    flags = json.loads(outcome['flags']) if outcome.get('flags') else None
    causes = []
    if flags is not None:
        for flag, expected, label in (
                ('trend', False, 'counter_trend'), ('volume', False, 'low_volume'),
                ('quiet', False, 'high_volatility'), ('resistance', True, 'near_resistance'),
                ('momentum', False, 'no_momentum')):
            if flags[flag] == expected:
                causes.append(label)
    return {'lot': outcome['lot'], 'strategy': outcome['strategy'], 'symbol': outcome['symbol'],
            'gross': outcome['gross'], 'entry_time': outcome.get('entry_time'),
            'associations': causes, 'diagnosis': 'hypotheses_only' if flags else 'entry_snapshot_unavailable'}


def compact(metrics):
    return {k: v for k, v in metrics.items() if k != 'trades'}


def passes(metrics):
    return (not metrics.get('_reason') and metrics['total_trades'] >= 20
            and metrics['pnl'] > 0 and metrics['max_drawdown'] > -0.10)


def evaluate(source, guard, frame, settings, *, scope=None, observed_by_time=None):
    symbols = frame.index.get_level_values('symbol')
    eligible = symbols.isin(settings.universe.equities) & ~symbols.isin(['SPY', 'QQQ'])
    frame = frame.loc[eligible]
    signal = signals(source, frame)
    if guard:
        signal &= frame[feature_column(guard)].astype(int)
    if scope is not None:
        routing = observed_by_time or {}
        timestamps = frame.index.get_level_values('timestamp')
        eligible_regime = [routing.get(t.isoformat(), 'unknown') == scope and scope in REGIMES
                           for t in timestamps]
        signal = signal.where(eligible_regime, 0)
    if 'interval_start' in frame:
        full_hour = frame.interval_end - frame.interval_start == pd.Timedelta(hours=1)
        signal = signal.where(full_hour, 0)
    return Backtester(settings).run(None, frame.assign(signal=signal))


def get_champion_for_regime(c, regime_label):
    return c.execute("""SELECT * FROM strategies WHERE status='champion' AND (regime=? OR regime IS NULL)
                        ORDER BY regime IS NULL, id LIMIT 1""", (regime_label,)).fetchone()


def ingest_observations(path, train, test):
    """Commit raw observations separately from optional research work in bounded batches."""
    if not train.empty and not test.empty:
        if train.index.get_level_values('timestamp').max() >= test.index.get_level_values('timestamp').min():
            raise ValueError('overlapping development windows')
    for frame in (train, test):
        if frame.empty:
            continue
        validate_raw(frame)
        times = frame.index.get_level_values('timestamp')
        if times.tz is None or times.hasnans:
            raise ValueError('expected offset-aware timestamps')
        for offset in range(0, len(frame), 250):
            with db(path, True) as c:
                slice_ = frame.iloc[offset:offset + 250]
                for (symbol, t), row in slice_.iterrows():
                    ts = t.tz_convert('UTC').isoformat()
                    record_observation(c, symbol, ts, row)
                for symbol, group in slice_.groupby(level='symbol'):
                    latest = group.index.get_level_values('timestamp').max().tz_convert('UTC').isoformat()
                    c.execute("""INSERT INTO watermarks(symbol,t) VALUES(?,?)
                        ON CONFLICT(symbol) DO UPDATE SET t=excluded.t WHERE excluded.t > watermarks.t""",
                              (symbol, latest))


@lru_cache(maxsize=1)
def _implementation_digest():
    source = hashlib.sha256()
    for name in ('research.py', 'hourly_features.py', 'backtester.py', 'strategy_worker.py'):
        source.update(Path(__file__).with_name(name).read_bytes())
    return source.hexdigest()


def _input_digest(frame):
    h = hashlib.sha256()
    h.update(json.dumps(list(frame.columns)).encode())
    # Receipt times can vary on an identical download. The original receipts remain in bar_versions.
    columns = [c for c in frame if c != 'received_at']
    h.update(pd.util.hash_pandas_object(frame[columns], index=True).to_numpy().tobytes())
    return h.hexdigest()


def _entry_evidence(c):
    return [dict(r) for r in c.execute("""SELECT o.lot,o.strategy,o.symbol,sum(o.gross) gross,max(o.at) exit_at,
              snap.flags,snap.t AS entry_time,snap.regime FROM outcomes o
              JOIN lots l ON l.id=o.lot AND cast(l.qty AS REAL)=0
              LEFT JOIN decision_snapshots snap ON snap.lot=o.lot AND snap.strategy=o.strategy
                AND snap.symbol=o.symbol AND EXISTS (
                    SELECT 1 FROM intents i WHERE i.lot=o.lot AND i.symbol=o.symbol
                    AND i.strategy=o.strategy AND i.side='buy')
              GROUP BY o.lot,o.strategy,o.symbol ORDER BY exit_at DESC LIMIT 30""")]


def _persist_decisions(c, scope, data, newer, regime, champion, settings, sig):
    observations = regime.get('observed_by_time', {})
    latest = data.index.get_level_values('timestamp').max().isoformat()
    eligible = set(settings.universe.equities) - {'SPY', 'QQQ'}
    indices = [(sym, t) for sym, t in newer if sym in eligible]
    if not indices:
        return
    if champion:
        guard = feature_column(json.loads(champion['rule_ids'])[0])
    for sym, t in indices:
        ts = t.isoformat()
        observed = observations.get(ts, regime.get('observed_regime', 'unknown') if ts == latest else 'unknown')
        if observed != scope or observed not in REGIMES:
            continue
        row = data.loc[sym, t]
        if champion and ts <= champion['born']:
            continue
        if c.execute("SELECT 1 FROM decisions WHERE symbol=? AND t=?", (sym, ts)).fetchone():
            continue
        flags = strategy_flags(row)
        full = row.get('interval_kind') == 'XNYS_1m' and row.interval_end - row.interval_start == pd.Timedelta(hours=1)
        fact = c.execute("SELECT received_at,content_hash FROM usable_bars WHERE symbol=? AND t=?",
                         (sym, ts)).fetchone()
        received = pd.Timestamp(fact['received_at']) if fact and fact['received_at'] else None
        timely = (received is not None and 0 <= (received - t).total_seconds() <= 120
                  and 0 <= (pd.Timestamp(now()) - received).total_seconds() <= 30)
        target = int(sig.loc[sym, t] and row[guard]) if champion and flags and full and timely else None
        sid = champion['id'] if champion else None
        c.execute("""INSERT INTO decisions(symbol,t,strategy,target,atr,regime) VALUES(?,?,?,?,?,?)""",
                  (sym, ts, sid, target, float(row.atr) if pd.notna(row.atr) else None, observed))
        if champion:
            c.execute("""INSERT INTO decision_snapshots(symbol,t,strategy,lot,regime,feature_version,
                          readiness_mask,history_ready,flags,rsi,received_at,content_hash)
                          SELECT ?,?,?,?,?,?,?,?,?,?,received_at,content_hash FROM usable_bars
                          WHERE symbol=? AND t=? AND content_hash IS NOT NULL""",
                      (sym, ts, sid, key('lot', sym, ts), observed, FEATURE_VERSION, int(row.readiness_mask),
                       int(row.history_ready), json.dumps(flags) if flags is not None else None,
                       float(row.rsi) if pd.notna(row.rsi) else None, sym, ts))


def review(path, s, train, test, regime, mutator, *, observations_committed=False):
    if not observations_committed:
        ingest_observations(path, train, test)
    if train.empty or test.empty:
        return 'insufficient history'
    scope = regime['label']
    data = pd.concat([train, test]).sort_index()
    times = data.index.get_level_values('timestamp')
    cut = times.max().isoformat()
    with db(path) as c:
        marks = review_marks(c, scope)
        quarantined = {r[0] for r in c.execute(
            'SELECT DISTINCT symbol FROM bar_quarantine WHERE t BETWEEN ? AND ?',
            (times.min().isoformat(), cut))}
        champion = get_champion_for_regime(c, scope)
        exits = _entry_evidence(c)
        lessons = [r[0] for r in c.execute('SELECT lesson FROM lessons ORDER BY created_at DESC LIMIT 12')]
    if data.index.has_duplicates or train.index.get_level_values('timestamp').max() >= test.index.get_level_values('timestamp').min():
        raise ValueError('overlap or duplicate bar')
    data = data.loc[~data.index.get_level_values('symbol').isin(quarantined)]
    if data.empty:
        with db(path, True) as c:
            put(c, 'research_diagnostic_' + scope, 'all_symbols_quarantined')
        return 'all symbols quarantined'
    train = train.loc[~train.index.get_level_values('symbol').isin(quarantined)]
    test = test.loc[~test.index.get_level_values('symbol').isin(quarantined)]
    newer = [(sym, t) for sym, t in data.index if sym not in marks or t > pd.Timestamp(marks[sym])]
    if not newer:
        return 'no new completed bar'
    evidence = dict(regime=scope, parent=champion['source'] if champion else SEED,
                    confirmed_paper_exits=exits, attributed_lessons=lessons,
                    failure_analysis=[analyze_failure(e) for e in exits if e['gross'] < 0])
    protocol = dict(scope=scope, parent=champion['id'] if champion else None, features=FEATURE_VERSION,
                    implementation=_implementation_digest(), cost_and_risk=asdict(s.risk),
                    symbols=sorted(set(s.universe.equities) - {'SPY', 'QQQ'}),
                    train=_input_digest(train), validation=_input_digest(test), end=cut,
                    evaluation='historical-development-only-v1')
    trial_ids = {}

    def evaluate_train(source, guard):
        manifest = json.dumps(dict(protocol, source=source, guard=guard), sort_keys=True, allow_nan=False)
        ident = hashlib.sha256(manifest.encode()).hexdigest()
        with db(path, True) as c:
            c.execute("""INSERT INTO development_manifests(id,manifest,registered_at) VALUES(?,?,?)
                         ON CONFLICT(id) DO NOTHING""", (ident, manifest, now()))
        trial_ids[source, guard] = ident
        return evaluate(source, guard, train, s, scope=scope, observed_by_time=regime.get('observed_by_time'))

    incumbent = (champion['source'], json.loads(champion['rule_ids'])[0]) if champion else None
    if reserve_lanes(path, len(FOCUSES)):
        winner, records = run_development(mutator, evidence, evaluate_train, passes, incumbent=incumbent)
    else:
        winner, records = None, [{"status": "waiting", "reason": "daily_variant_budget"}]
    validation = evaluate(winner['source'], winner['guard'], test, s, scope=scope,
                          observed_by_time=regime.get('observed_by_time')) if winner else None
    # Evaluate strategy code outside operational write transactions.
    decision_signals = None
    if champion:
        decision_signals = signals(champion['source'], data)
    with db(path, True) as c:
        if review_marks(c, scope) != marks:
            return 'concurrent review already committed'
        current = get_champion_for_regime(c, scope)
        if (current['id'] if current else None) != protocol['parent']:
            return 'routing changed during review'
        c.execute("INSERT INTO development_batches(consumer,cut,results) VALUES(?,?,?)",
                  (scope, cut, json.dumps(records, allow_nan=False)))
        if winner:
            sid = trial_ids[winner['source'], winner['guard']]
            rid = key('development_rule', sid)
            result = ('inconclusive' if validation['total_trades'] < 20 else
                      'development' if passes(validation) else 'rejected')
            c.execute("""INSERT INTO rules(id,text,feature,state,created_at,parent) VALUES(?,?,?,?,?,NULL)""",
                      (rid, winner['lesson'], winner['guard'], 'hypothesis', now()))
            c.execute("""INSERT INTO strategies(id,name,source,rule_ids,parent_id,generation,status,regime,born)
                         VALUES(?,?,?,?,?,?,?,?,?)""", (sid, winner['name'], winner['source'],
                      json.dumps([winner['guard'], rid]), protocol['parent'],
                      (champion['generation'] + 1) if champion else 1, result, scope, now()))
            c.execute("INSERT INTO trials(strategy,cut,phase,metrics) VALUES(?,?,?,?)",
                      (sid, cut, 'development_validation', json.dumps(compact(validation), allow_nan=False)))
        if not winner:
            from paper_store import record_unsolved_failure
            for loss in evidence['failure_analysis']:
                # One incident per lot/evidence set, even when multiple regime consumers observe it.
                if c.execute('SELECT 1 FROM unsolved_failures WHERE lot=? AND strategy=?',
                             (loss['lot'], loss['strategy'])).fetchone() is None:
                    record_unsolved_failure(c, loss['lot'], loss['symbol'], 'unknown', loss['strategy'],
                                            loss['gross'], loss['diagnosis'], json.dumps(loss))
        _persist_decisions(c, scope, data, newer, regime, champion, s, decision_signals)
        for symbol, group in data.groupby(level='symbol'):
            latest = group.index.get_level_values('timestamp').max()
            if symbol not in marks or latest > pd.Timestamp(marks[symbol]):
                advance_review(c, scope, symbol, latest.isoformat())
        actual = regime.get('observed_regime', 'unknown')
        c.execute("""INSERT INTO regimes(t,label,indicators) VALUES(?,?,?) ON CONFLICT(t) DO NOTHING""",
                  (cut, actual if actual in REGIMES else 'unknown', json.dumps(regime['indicators'], allow_nan=False)))
        put(c, 'research_at', now())
        put(c, 'research_diagnostic_' + scope, json.dumps({'quarantined': sorted(quarantined),
            'unready': int((~data.history_ready | data.readiness_mask.ne(ALL_READY)).sum()), 'rows': len(data)}))
        put(c, 'mode', 'PAPER')
    return f'hour recorded [{scope}]'
