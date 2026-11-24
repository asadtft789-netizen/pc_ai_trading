"""New hourly frontier triggers one review. Historical catch-up is never a backlog of orders."""
import json, math, sqlite3
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

LEARNING_DB_PATH = Path(__file__).resolve().parent / 'data' / 'learning_loop.sqlite'

def compact(m):
    return {k: v for k, v in m.items() if k != 'trades'}

def passes(m):
    return not m.get('_reason') and m['total_trades'] >= 20 and (m['pnl'] > 0) and (m['max_drawdown'] > -0.1)

def paired_lower(a, b):
    """One-sided 90% moving-block bootstrap on paired UTC-day realized PnL differences.
    A screening statistic, not a population guarantee or a multiple-testing correction.
    """
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

def review(path, s, train, test, regime, mutator):
    if train.empty or test.empty:
        return 'insufficient history'
    data = pd.concat([train, test]).sort_index()
    times = data.index.get_level_values('timestamp')
    cut = times.max().isoformat()
    bt = Backtester(s)
    if data.index.has_duplicates or train.index.get_level_values('timestamp').max() >= test.index.get_level_values('timestamp').min():
        raise ValueError('overlap or duplicate bar')
    with db(path) as c:
        marks = dict(c.execute('select symbol,t from watermarks'))
        active_since = get(c, 'champion_since')
        champ = c.execute("select * from strategies where status='champion'").fetchone()
        child = c.execute("select * from strategies where status='challenger'").fetchone()
        evidence = [dict(r) for r in c.execute('select symbol,gross,at,strategy from outcomes order by at desc limit 30')]
        rules = [dict(r) for r in c.execute('select text,state,feature from rules order by created_at desc limit 30')]
        last_trial = c.execute('select phase,metrics from trials order by cut desc limit 1').fetchone()
        lessons = [r[0] for r in c.execute('select lesson from lessons order by created_at desc limit 12')]
        serial = c.execute('select count(*) from strategies').fetchone()[0]
    for sym, start in data.groupby(level='symbol').apply(lambda g: g.index.get_level_values('timestamp').min()).items():
        if sym in marks and pd.Timestamp(marks[sym]) < start:
            raise ValueError('history gap exceeds retrieval window; backfill required')
    newer = [(sym, t) for sym, t in data.index if t.isoformat() > marks.get(sym, '')]
    if not newer:
        return 'no new completed bar'
    # No retroactive promotion: child must already exist before the test observations arrive.

    def evaluate(source, guard, frame):
        sig = signals(source, data)
        # Do not qualify an equity-order strategy on instruments the adapter cannot trade.
        frame=frame.loc[~frame.index.get_level_values('symbol').str.contains('/', regex=False)]
        return bt.run(None, frame.assign(signal=sig.loc[frame.index] & frame[guard].astype(int) if guard else sig.loc[frame.index]))
    gate = None
    if champ:
        gate = evaluate(champ['source'], json.loads(champ['rule_ids'])[0], test)
    incumbent_ok = bool(champ and passes(gate))
    proposal = None
    trial = None
    action = None
    ablate = None
    control = None
    reason = ''
    rule_state = None
    if child:
        future = data.loc[times > pd.Timestamp(child['born'])]
        days = future.index.get_level_values('timestamp').normalize().unique()
        guard = json.loads(child['rule_ids'])[0]
        if len(days) >= 15:
            trial = evaluate(child['source'], guard, future)
            ablate = evaluate(child['source'], None, future)
            control = evaluate(champ['source'], json.loads(champ['rule_ids'])[0], future) if champ else bt._empty_metrics('cash')
            buckets = np.array_split(days.sort_values(), 3)
            paired = []
            for dayslice in buckets:
                f = future.loc[future.index.get_level_values('timestamp').normalize().isin(dayslice)]
                m = evaluate(child['source'], guard, f)
                a = evaluate(child['source'], None, f)
                inc = evaluate(champ['source'], json.loads(champ['rule_ids'])[0], f) if champ else bt._empty_metrics('cash')
                paired.append((m['total_trades'] >= 3, m['pnl'] - a['pnl'], m['pnl'] - max(0, inc['pnl'])))
            enough = all((x[0] for x in paired)) and trial['total_trades'] >= 20
            lower = paired_lower(trial, ablate)
            rule_state = 'supported' if enough and lower is not None and (lower > 0) and all((x[1] > 0 for x in paired)) else 'harmful' if enough and all((x[1] < 0 for x in paired)) else 'inconclusive'
            halted = False
            if LEARNING_DB_PATH.is_file():
                try:
                    led = Ledger(LEARNING_DB_PATH)
                    halted = bool(led.state().get('halted'))
                    led.close()
                except Exception:
                    halted = True
            action = 'champion' if passes(trial) and rule_state == 'supported' and all((x[2] > 0 for x in paired)) and (not champ or trial['max_drawdown'] >= control['max_drawdown']) and not halted else 'retired'
            reason = json.dumps(dict(blocks=paired, guard_lower_90=lower, rule=rule_state, attribution='paired guard ablation; not a significance claim'))
    else:
        evidence_obj = dict(parent=champ['source'] if champ else SEED, confirmed_paper_exits=evidence, attributed_lessons=lessons, rules=rules, previous_test=dict(last_trial) if last_trial else None)
        repair = bool(champ and any((r['state'] == 'review' and r['feature'] == json.loads(champ['rule_ids'])[0] for r in rules)))
        proposal = (mutator.generate_rule_correction(evidence_obj) if repair else mutator.generate_new_strategy(evidence_obj)) or dict(source=champ['source'] if champ else SEED, name='Hourly paired challenger', guard=FEATURES[(serial + 5) % len(FEATURES)], lesson='Test one causal guard against the identical unguarded strategy.')
        halted = False
        if LEARNING_DB_PATH.is_file():
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
        # Only change the guard, never rewrite the whole parent.
        if repair and proposal is not None:
            proposal['source'] = champ['source']
        trial = evaluate(proposal['source'], proposal['guard'], train) if proposal is not None else None
        hold = evaluate(proposal['source'], proposal['guard'], test) if proposal is not None else None
        action = 'challenger' if proposal and passes(trial) and passes(hold) else 'retired'
    incumbent_sig = signals(champ['source'], data) & data[json.loads(champ['rule_ids'])[0]].astype(int) if champ else None
    with db(path, True) as c:
        if dict(c.execute('select symbol,t from watermarks')) != marks:
            return 'concurrent review already committed'
        max_by_symbol = {s: data.xs(s).index[-1].isoformat() for s in data.index.get_level_values('symbol').unique()}
        if gate is not None:
            c.execute('insert or replace into trials values(?,?,?,?)', (champ['id'], cut, 'hourly_monitor', json.dumps(compact(gate))))
        print(f'DEBUG: champ={champ is not None}, gate={gate}, incumbent_ok={incumbent_ok}')
        put(c, 'entry_gate', champ['id'] if incumbent_ok else 'closed')
        for sym, t in newer:
            r = data.loc[sym, t]
            ts = t.isoformat()
            vals = [float(r[k]) for k in ('open', 'high', 'low', 'close', 'volume')]
            o, h, l, cl, v = vals
            if not all(map(math.isfinite, vals)) or min(o, l, cl) <= 0 or (not l <= min(o, cl) <= max(o, cl) <= h) or (v < 0):
                raise ValueError('invalid OHLCV')
            c.execute('insert into bars values(?,?,?,?,?,?,?)', (sym, ts, *vals))
            target = int(incumbent_sig.loc[sym, t]) if champ else 0
            # Only the newest observed bar can become actionable; catch-up is historical.
            if ts == max_by_symbol[sym] or (champ and active_since and (ts >= active_since)):
                c.execute('insert into decisions values(?,?,?,?,?,?)', (sym, ts, champ['id'] if champ else None, target if champ else None, float(r.atr) if pd.notna(r.atr) else None, regime['label'] if ts == max_by_symbol[sym] else 'historical_replay'))
            c.execute('insert or replace into watermarks values(?,?)', (sym, ts))
        c.execute('insert or replace into regimes values(?,?,?)', (cut, regime['label'], json.dumps(regime['indicators'], allow_nan=False)))
        if proposal:
            rid = key('rule', serial, proposal['guard'])
            sid = key('strategy', serial, proposal['source'], proposal['guard'])
            parent = champ['id'] if champ else None
            c.execute('insert into rules values(?,?,?,?,?,?)', (rid, proposal['lesson'], proposal['guard'], 'testing' if action == 'challenger' else 'unvalidated', now(), json.loads(champ['rule_ids'])[1] if repair else None))
            c.execute('insert into strategies values(?,?,?,?,?,?,?,?)', (sid, proposal['name'], proposal['source'], json.dumps([proposal['guard'], rid]), parent, serial, action, max(cut, now())))
            reason = 'Historical train and held-out gates: ' + action
            for phase, m in [('train', trial), ('held_out', hold)]:
                c.execute('insert into trials values(?,?,?,?)', (sid, cut, phase, json.dumps(compact(m))))
        elif action:
            sid = child['id']
            rid = json.loads(child['rule_ids'])[1]
            c.execute('update rules set state=? where id=?', (rule_state, rid))
            if action == 'champion':
                put(c, 'champion_since', now())
            if action == 'champion' and champ:
                c.execute("update strategies set status='retired' where id=?", (champ['id'],))
            c.execute('update strategies set status=? where id=?', (action, sid))
            c.execute('insert into trials values(?,?,?,?)', (sid, cut, 'forward', json.dumps(dict(candidate=compact(trial), without_guard=compact(ablate), incumbent=compact(control)))))
        if action:
            c.execute('insert into reflections values(?,?,?,?)', (key('review', cut, sid), sid, reason, now()))
        if action:
            c.execute('insert into reflections values(?,?,?,?)', (key('rule-review', cut, rid), rid, reason, now()))
        put(c, 'research_at', now())
        put(c, 'mode', 'PAPER')
    return 'hour recorded'
