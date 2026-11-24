"""Two independent processes: hourly research and a short-cadence paper supervisor."""
import argparse, ast, logging, os, sqlite3, time
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path
import pandas as pd
from config import Settings
from paper_store import init, db, put, now
from executor import PAPER
from learning_loop import Bar as LLBar, Ledger, Risk, Session, mask, FEATURES as LL_FEATURES, Rule
from learning_loop_adapter import close_tick, session_open, UNKNOWN_GATES
from hourly_features import enrich
ROOT = Path(__file__).resolve().parent

LEARNING_DB = str(ROOT / 'data' / 'learning_loop.sqlite')
_ledger = None


def _build_forward_sessions(start_date, num_days=60):
    from calendar import monthrange
    sessions = []
    d = start_date
    added = 0
    year = d.year + (d.month == 12)
    month = d.month % 12 + 1
    stop = date(year, month, min(d.day, monthrange(year, month)[1]))
    while added < num_days and d < stop:
        if d.weekday() < 5:
            opened = int(datetime.combine(d, dtime(13, 30), tzinfo=timezone.utc).timestamp())
            cuts = tuple(opened + 3600 * i for i in range(1, 7)) + (opened + 23400,)
            sessions.append(Session(label=d.isoformat(), opened=opened, cuts=cuts))
            added += 1
        d += timedelta(days=1)
    return sessions


def _champion_to_book(con):
    row = con.execute("select source from strategies where status='champion'").fetchone()
    if not row:
        return None
    tree = ast.parse(row[0])
    fn = tree.body[0]
    expr = ast.unparse(fn.body[0].value.test)
    return {
        'ENTRY': Rule('ENTRY', 'Daemon champion', expr),
        'SPREAD': Rule('SPREAD', 'Executor-owned spread gate; needs verified quotes.', None),
        'EXIT': Rule('EXIT', 'Executor-owned protection and session exit.', None),
    }


def _init_learning_ledger(db_path, s):
    global _ledger
    if _ledger is not None:
        return _ledger

    # Read champion from the daemon DB; sidecar ledger is separate.
    con = sqlite3.connect(db_path)
    book = _champion_to_book(con)
    con.close()
    if not book:
        return None

    sessions = _build_forward_sessions(date.today() + timedelta(days=1))
    if not sessions:
        return None

    clock_state = {'t': float(sessions[0].opened - 1)}

    def clock():
        return clock_state['t']

    ledger = Ledger(LEARNING_DB, clock=clock)
    risk = Risk(
        cash=float(s.risk.max_capital_usd),
        order_cap=float(s.risk.max_order_size_usd),
        fee_bps=float(s.risk.spread_bps + s.risk.slippage_bps),
        daily_loss=float(s.risk.max_daily_loss_usd),
        max_drawdown_fraction=0.10,
        max_positions=1,
        quantity_step=0.000000001,
        max_stop_risk=1.0,
        no_entry_last_seconds=1800,
    )

    ledger.setup(sessions[0].label, sessions, s.universe.symbols, risk, book)
    clock_state['t'] = time.time()
    _ledger = ledger
    return _ledger


def _observe_new_bars(db_path, s):
    """Feed new completed bars to the learning loop sidecar."""
    ledger = _init_learning_ledger(db_path, s)
    if ledger is None:
        return

    with db(db_path) as c:
        marks = dict(c.execute('select symbol, t from watermarks'))

    con = sqlite3.connect(db_path)
    rows = con.execute('select symbol, t, o as open, h as high, l as low, c as close, v as volume from bars').fetchall()
    con.close()
    if not rows:
        return

    df = pd.DataFrame(rows, columns=['symbol', 't', 'open', 'high', 'low', 'close', 'volume'])
    df['t'] = pd.to_datetime(df['t'])
    df = df.set_index(['symbol', 't']).sort_index()

    try:
        enriched = enrich(df)
    except Exception:
        return

    symbols = set(s.universe.symbols)
    for cut, group in enriched.groupby(level='t'):
        if set(group.index.get_level_values('symbol')) != symbols:
            continue
        if not all(marks.get(sym, '') < cut.isoformat() for sym in symbols):
            continue

        batch = {}
        for sym in symbols:
            row = group.xs(sym, level='symbol')
            features = {f: bool(row[f].iloc[0]) for f in LL_FEATURES}
            batch[sym] = LLBar(
                o=float(row['open'].iloc[0]),
                h=float(row['high'].iloc[0]),
                l=float(row['low'].iloc[0]),
                c=float(row['close'].iloc[0]),
                bits=mask(features),
                stop_gap=float(row['atr'].iloc[0]) if pd.notna(row['atr'].iloc[0]) else 0.01,
                entry_ok=True,
                entry_cap=float(s.risk.max_order_size_usd),
            )

        cut_ts = int(cut.timestamp())
        try:
            close_tick(ledger, cut_ts, batch, gates=lambda _: UNKNOWN_GATES)
        except Exception:
            pass

class SecretRedactingFilter(logging.Filter):

    def __init__(self):
        super().__init__()
        self.values = [v for k, v in os.environ.items() if v and any((x in k.upper() for x in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD')))]

    def filter(self, r):
        text = r.getMessage()
        for v in self.values:
            text = text.replace(v, '[redacted]')
        r.msg = text
        r.args = ()
        r.exc_info = None
        r.exc_text = None
        r.stack_info = None
        return True

def main():
    p = argparse.ArgumentParser()
    p.add_argument('role', choices=['research', 'supervise', 'check', 'preflight', 'import-rules'], nargs='?', default='research')
    p.add_argument('--once', action='store_true')
    p.add_argument('--legacy-db')
    args = p.parse_args()
    s = Settings.load(str(ROOT / '.env'))
    if s.alpaca.base_url.rstrip('/') != PAPER:
        raise SystemExit('Not a paper endpoint. No trading client was created.')
    path = init(os.getenv('PAPER_DB', str(ROOT / 'data' / 'hourly-paper.sqlite')))
    if args.role == 'check':
        print('Paper configuration and isolated schema ready; no broker contacted.')
        return
    if args.role == 'preflight':
        from executor import AlpacaPaper, StrategyExecutor
        _, ok = StrategyExecutor(path, AlpacaPaper(s), s).reconcile_trades()
        print('Paper snapshot reconciled. No order submitted.' if ok else 'Paper snapshot unknown. Stop and inspect the dedicated paper account.')
        raise SystemExit(0 if ok else 1)
    if args.role == 'import-rules':
        from rule_review import import_rules
        if not args.legacy_db:
            p.error('--legacy-db required')
        print('Reviewed legacy rules:', import_rules(path, args.legacy_db))
        return
    # Private client diagnostics must not propagate HTTP headers, bodies or URLs.
    log = logging.getLogger('paper_daemon')
    log.propagate = False
    log.setLevel(s.runtime.log_level)
    handler = logging.FileHandler(path.parent / 'daemon.log')
    handler.addFilter(SecretRedactingFilter())
    log.addHandler(handler)
    logging.getLogger().addHandler(logging.NullHandler())
    if args.role == 'research':
        from data_ingestor import DataIngestor
        from research import review
        from llm_mutator import LLMMutator
        from rule_review import drift, learn_from_closes
        ingest = DataIngestor(s, None)
        mut = LLMMutator(s)

        def tick():
            learn_from_closes(path, mut)
            train, test = ingest.get_train_test_split()
            res = review(path, s, train, test, ingest.detect_regime(test), mut)
            drift(path)
            _observe_new_bars(path, s)
            return res
    else:
        from executor import StrategyExecutor, AlpacaPaper
        adapter = StrategyExecutor(path, AlpacaPaper(s), s)
        tick = adapter.sync_positions
    while True:
        try:
            result = tick()
            log.info('%s: %s', args.role, result)
            with db(path, True) as c:
                put(c, args.role + '_heartbeat', now())
        except Exception as e:
            if args.role=='research':
                with db(path,True) as c:put(c,'entry_gate','closed')
            log.error('%s unavailable (%s); no automatic replay of submissions', args.role, type(e).__name__)
            if args.once:
                raise SystemExit(1)
            time.sleep(20)
            continue
        if args.once:
            return
        time.sleep(10 if args.role == 'supervise' else max(1, 3600 - (time.time() - 30) % 3600))
if __name__ == '__main__':
    main()
