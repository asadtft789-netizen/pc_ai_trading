"""Phoenix Multi-Regime Shadow-Tournament Research Engine.
Each market regime gets its own champion. Challengers run in Shadow Mode
until they statistically prove superiority on live bars before promotion.
"""
import json, math, sqlite3, concurrent.futures
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd
from paper_store import db, now, key, put, get
from strategy_worker import SEED, FEATURES
from hourly_features import signals
from backtester import Backtester
from learning_loop import Ledger
from learning_loop_adapter import UNKNOWN_GATES
from regime_detector import classify

LEARNING_DB_PATH = Path(__file__).resolve().parent / 'data' / 'learning_loop.sqlite'
REGIMES = ['trend_up', 'trend_down', 'range_quiet', 'vol_expansion']

FAILURE_PATTERNS = {
    'counter_trend': {'logic': 'trend == 0', 'guards': ['trend', 'momentum'], 'desc': 'Loss occurred against prevailing EMA trend.'},
    'low_volume': {'logic': 'volume == 0', 'guards': ['volume'], 'desc': 'Signal lacked relative volume confirmation.'},
    'overextended': {'logic': 'rsi > 70 or rsi < 30', 'guards': ['golden', 'pullback'], 'desc': 'Entry at RSI extremes without mean reversion.'},
    'whipsaw': {'logic': 'quiet == 0', 'guards': ['quiet'], 'desc': 'High ATR volatility caused immediate stop-out.'},
    'resistance_hit': {'logic': 'resistance == 1', 'guards': ['breakout', 'structure'], 'desc': 'Entry into overhead shifted resistance.'},
    'no_momentum': {'logic': 'momentum == 0', 'guards': ['momentum', 'breakout'], 'desc': 'Signal lacked 5-bar momentum confirmation.'}
}


def analyze_failure(outcome, frame):
    """Diagnose trade failure by inspecting feature states at entry."""
    try:
        symbol = outcome['symbol']
        entry_t = pd.Timestamp(outcome['at'])
        # Find the entry bar - the decision bar is usually one bar before the outcome at
        decision_idx = frame.xs(symbol, level='symbol').index.get_indexer([entry_t], method='pad')[0]
        if decision_idx < 0: return None
        bar = frame.xs(symbol, level='symbol').iloc[decision_idx]

        causes = []
        for name, p in FAILURE_PATTERNS.items():
            # Evaluate logic string against bar values
            if eval(p['logic'], {}, bar.to_dict()):
                causes.append({'id': name, 'desc': p['desc'], 'guards': p['guards']})

        return {
            'symbol': symbol,
            'at': entry_t.isoformat(),
            'pnl': float(outcome['gross']),
            'causes': causes,
            'features': {f: bool(bar[f]) for f in FEATURES if f in bar}
        }
    except Exception:
        return None


def compact(m):
    return {k: v for k, v in m.items() if k != 'trades'}


def passes(m):
    return not m.get('_reason') and m['total_trades'] >= 20 and (m['pnl'] > 0) and (m['max_drawdown'] > -0.10)


def paired_lower(a, b):
    """One-sided 90% moving-block bootstrap on paired UTC-day realized PnL differences."""
    diffs = {}
    for sign, metrics in ((1, a), (-1, b)):
        for trade in metrics['trades']:
            day = pd.Timestamp(trade['exit_time']).normalize()
            diffs[day] = diffs.get(day, 0.0) + sign * trade['pnl_usd']
    if len(diffs) < 10:
        return None
    days = pd.date_range(min(diffs), max(diffs), freq='D')
    v = np.array([diffs.get(d, 0.0) for d in days])
    n = len(v)
    rng = np.random.default_rng(42)
    starts = rng.integers(0, n, (500, (n + 2) // 3))
    idx = (starts[:, :, None] + np.arange(3)) % n
    return float(np.quantile(v[idx.reshape(500, -1)[:, :n]].mean(axis=1), 0.1))


def evaluate(source, guard, frame, settings):
    """Run backtester on a source/guard combination over a frame."""
    sig = signals(source, frame)
    frame = frame.loc[~frame.index.get_level_values('symbol').str.contains('/', regex=False)]
    if guard:
        sig = sig.loc[frame.index] & frame[guard].astype(int)
    else:
        sig = sig.loc[frame.index]
    return Backtester(settings).run(None, frame.assign(signal=sig))


def get_champion_for_regime(c, regime_label):
    """Fetch current champion for a regime, falling back to global champion."""
    row = c.execute(
        "select * from strategies where status='champion' and regime=?",
        (regime_label,)
    ).fetchone()
    if not row:
        row = c.execute(
            "select * from strategies where status='champion' and regime IS NULL"
        ).fetchone()
    return row


def review(path, s, train, test, regime, mutator):
    """Phoenix review: per-regime champion evaluation + shadow tournament."""
    if train.empty or test.empty:
        return 'insufficient history'
    data = pd.concat([train, test]).sort_index()
    times = data.index.get_level_values('timestamp')
    cut = times.max().isoformat()
    bt = Backtester(s)
    if data.index.has_duplicates or train.index.get_level_values('timestamp').max() >= test.index.get_level_values('timestamp').min():
        raise ValueError('overlap or duplicate bar')

    regime_label = regime['label']

    with db(path) as c:
        marks = dict(c.execute('select symbol,t from watermarks'))
        champ = get_champion_for_regime(c, regime_label)
        child = c.execute(
            "select * from strategies where status='challenger' and regime=?",
            (regime_label,)
        ).fetchone()
        if not child:
            child = c.execute(
                "select * from strategies where status='challenger' and regime IS NULL"
            ).fetchone()
        evidence = [dict(r) for r in c.execute(
            'select symbol,gross,at,strategy from outcomes order by at desc limit 30')]
        rules = [dict(r) for r in c.execute(
            'select text,state,feature from rules order by created_at desc limit 30')]
        last_trial = c.execute(
            'select phase,metrics from trials order by cut desc limit 1'
        ).fetchone()
        lessons = [r[0] for r in c.execute(
            'select lesson from lessons order by created_at desc limit 12')]
        serial = c.execute('select count(*) from strategies').fetchone()[0]

    # --- Data integrity checks ---
    for sym, start in data.groupby(level='symbol').apply(
        lambda g: g.index.get_level_values('timestamp').min()).items():
        if sym in marks and pd.Timestamp(marks[sym]) < start:
            raise ValueError('history gap exceeds retrieval window; backfill required')
    newer = [(sym, t) for sym, t in data.index if t.isoformat() > marks.get(sym, '')]
    if not newer:
        return 'no new completed bar'

    # --- Incumbent gate ---
    gate = None
    incumbent_ok = False
    if champ:
        gate = evaluate(champ['source'], json.loads(champ['rule_ids'])[0], test, s)
        incumbent_ok = bool(passes(gate))

    action = None
    trial = None
    ablate = None
    control = None
    proposal = None
    reason = ''
    rule_state = None
    hold = None

    if child:
        # --- Shadow Tournament: Challenger vs Incumbent ---
        future = data.loc[times > pd.Timestamp(child['born'])]
        days = future.index.get_level_values('timestamp').normalize().unique()
        guard = json.loads(child['rule_ids'])[0]
        if len(days) >= 15:
            trial = evaluate(child['source'], guard, future, s)
            ablate = evaluate(child['source'], None, future, s)
            control = evaluate(champ['source'], json.loads(champ['rule_ids'])[0], future, s) if champ else bt._empty_metrics('cash')
            buckets = np.array_split(days.sort_values(), 3)
            paired = []
            for dayslice in buckets:
                f = future.loc[future.index.get_level_values('timestamp').normalize().isin(dayslice)]
                m = evaluate(child['source'], guard, f, s)
                a = evaluate(child['source'], None, f, s)
                inc = evaluate(champ['source'], json.loads(champ['rule_ids'])[0], f, s) if champ else bt._empty_metrics('cash')
                paired.append((m['total_trades'] >= 3, m['pnl'] - a['pnl'], m['pnl'] - max(0, inc['pnl'])))
            enough = all((x[0] for x in paired)) and trial['total_trades'] >= 20
            lower = paired_lower(trial, ablate)
            rule_state = ('supported' if enough and lower is not None and (lower > 0)
                          and all((x[1] > 0 for x in paired))
                          else 'harmful' if enough and all((x[1] < 0 for x in paired))
                          else 'inconclusive')

            halted = False
            try:
                led = Ledger(LEARNING_DB_PATH)
                halted = bool(led.state().get('halted'))
                led.close()
            except Exception:
                halted = True

            # Phoenix promotion requires statistical superiority over incumbent;
            # with no incumbent yet, the incumbent-comparison is vacuously true.
            beats_incumbent = (not champ) or (
                all((x[2] > 0 for x in paired)) and trial['max_drawdown'] >= control['max_drawdown'])
            action = ('champion' if passes(trial) and rule_state == 'supported'
                      and beats_incumbent and not halted else 'retired')
            reason = json.dumps(dict(blocks=paired, guard_lower_90=lower, rule=rule_state,
                                     attribution='paired guard ablation; not a significance claim'))
    else:
        # --- Candidate Generation: build new challenger ---
        evidence_obj = dict(
            parent=champ['source'] if champ else SEED,
            confirmed_paper_exits=evidence,
            attributed_lessons=lessons,
            rules=rules,
            previous_test=dict(last_trial) if last_trial else None,
            regime=regime_label,
            failure_analysis=[analyze_failure(e, data) for e in evidence if e['gross'] < 0]
        )

        # Parallel candidate generation
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(mutator.propose, evidence_obj) for _ in range(5)]
            proposals = [f.result() for f in concurrent.futures.as_completed(futures) if isinstance(f.result(), dict)]

        repair = bool(champ and any(
            r['state'] == 'review' and r['feature'] == json.loads(champ['rule_ids'])[0]
            for r in rules))

        # Select best proposal based on brief backtest
        proposal = None
        if proposals:
            best_trial = None
            for p in proposals:
                trial = evaluate(p['source'], p['guard'], train, s)
                if passes(trial) and (not best_trial or trial['pnl'] > best_trial['pnl']):
                    best_trial = trial
                    proposal = p

        if not proposal: # Fallback
            proposal = dict(
                source=champ['source'] if champ else SEED,
                name=f'Hourly {regime_label} challenger',
                guard=FEATURES[(serial + 5) % len(FEATURES)],
                lesson=f'Test one causal guard for {regime_label} regime.'
            )

        halted = False
        try:
            led = Ledger(LEARNING_DB_PATH)
            halted = bool(led.state().get('halted'))
            led.close()
        except Exception:
            halted = True

        if not halted and not repair and champ:
            proposal['source'] = champ['source']
        elif halted:
            proposal = None

        # Final logic after candidate generation
        if not proposal and evidence_obj['failure_analysis']:
            # Record first failure for human review if AI cannot repair
            f = evidence_obj['failure_analysis'][0]
            if f:
                with db(path, True) as c:
                    from paper_store import record_unsolved_failure
                    record_unsolved_failure(c, 'unsolved', f['symbol'], regime_label, champ['id'] if champ else 'SEED', f['pnl'], f['causes'][0]['desc'] if f['causes'] else 'unknown', 'AI_mutation_failed')

        if proposal:
            trial = evaluate(proposal['source'], proposal['guard'], train, s) if proposal is not None else None
            hold = evaluate(proposal['source'], proposal['guard'], test, s) if proposal is not None else None
            action = 'challenger' if proposal and passes(trial) and passes(hold) else 'retired'

    incumbent_sig = (
        signals(champ['source'], data) & data[json.loads(champ['rule_ids'])[0]].astype(int)
        if champ else None
    )

    with db(path, True) as c:
        if dict(c.execute('select symbol,t from watermarks')) != marks:
            return 'concurrent review already committed'
        max_by_symbol = {
            s: data.xs(s).index[-1].isoformat()
            for s in data.index.get_level_values('symbol').unique()
        }
        if gate is not None:
            c.execute(
                'insert or replace into trials values(?,?,?,?)',
                (champ['id'], cut, 'hourly_monitor', json.dumps(compact(gate)))
            )
        # Entry gate per regime: open only if incumbent passes
        put(c, f'entry_gate_{regime_label}', champ['id'] if incumbent_ok else 'closed')
        for sym, t in newer:
            r = data.loc[sym, t]
            ts = t.isoformat()
            vals = [float(r[k]) for k in ('open', 'high', 'low', 'close', 'volume')]
            o, h, l, cl, v = vals
            if not all(map(math.isfinite, vals)) or min(o, l, cl) <= 0 or \
               (not l <= min(o, cl) <= max(o, cl) <= h) or (v < 0):
                raise ValueError('invalid OHLCV')
            c.execute(
                'insert into bars values(?,?,?,?,?,?,?)', (sym, ts, *vals))
            target = int(incumbent_sig.loc[sym, t]) if champ else 0
            if ts == max_by_symbol[sym] or (champ and get(c, 'champion_since') and ts >= get(c, 'champion_since')):
                c.execute(
                    'insert into decisions values(?,?,?,?,?,?)',
                    (sym, ts, champ['id'] if champ else None,
                     target if champ else None,
                     float(r.atr) if pd.notna(r.atr) else None, regime_label))
            c.execute('insert or replace into watermarks values(?,?)', (sym, ts))

        c.execute(
            'insert or replace into regimes values(?,?,?)',
            (cut, regime_label, json.dumps(regime['indicators'], allow_nan=False)))

        if proposal:
            rid = key('rule', serial, proposal['guard'])
            sid = key('strategy', serial, proposal['source'], proposal['guard'])
            parent = champ['id'] if champ else None
            c.execute(
                'insert into rules values(?,?,?,?,?,?)',
                (rid, proposal['lesson'], proposal['guard'],
                 'testing' if action == 'challenger' else 'unvalidated', now(),
                 json.loads(champ['rule_ids'])[1] if repair else None))
            c.execute(
                'insert into strategies values(?,?,?,?,?,?,?,?,?)',
                (sid, proposal['name'], proposal['source'],
                 json.dumps([proposal['guard'], rid]), parent, serial,
                 action, regime_label, max(cut, now())))
            reason = f'Historical train and held-out gates: {action}'
            for phase, m in [('train', trial), ('held_out', hold)]:
                c.execute(
                    'insert into trials values(?,?,?,?)',
                    (sid, cut, phase, json.dumps(compact(m))))
        elif action:
            sid = child['id']
            rid = json.loads(child['rule_ids'])[1]
            c.execute('update rules set state=? where id=?', (rule_state, rid))
            if action == 'champion':
                put(c, 'champion_since', now())
            if action == 'champion' and champ:
                c.execute("update strategies set status='retired' where id=?", (champ['id'],))
            c.execute('update strategies set status=? where id=?', (action, sid))
            c.execute(
                'insert into trials values(?,?,?,?)',
                (sid, cut, 'forward',
                 json.dumps(dict(candidate=compact(trial),
                                 without_guard=compact(ablate),
                                 incumbent=compact(control)))))
        if action:
            c.execute(
                'insert into reflections values(?,?,?,?)',
                (key('review', cut, sid), sid, reason, now()))
            c.execute(
                'insert into reflections values(?,?,?,?)',
                (key('rule-review', cut, rid), rid, reason, now()))
        put(c, 'research_at', now())
        put(c, 'mode', 'PAPER')
    return f'hour recorded [{regime_label}]'
