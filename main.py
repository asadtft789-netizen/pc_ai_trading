"""Two independent processes: hourly research and a short-cadence paper supervisor."""
import argparse
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from config import Settings
from paper_store import init, db, put, now
from executor import PAPER
from learning_runtime import LearningRecoveryRequired, observe_new_bars
from market_time import equity_sessions
from regime_detector import point_in_time_regimes
from research import REGIMES

ROOT = Path(__file__).resolve().parent


def research_tick(path, settings, ingest, mutator):
    from research import ingest_observations, review
    from rule_review import drift, learn_from_closes
    train, test = ingest.get_train_test_split()
    with db(path, True) as c:
        put(c, 'ingestion_diagnostics', json.dumps(ingest.diagnostics))
    results = {}
    if not train.empty and not test.empty:
        ingest_observations(path, train, test)
    # A sidecar recovery incident must not suppress other research consumers.
    try:
        results['learning'] = observe_new_bars(path, settings)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        reason = str(exc) if isinstance(exc, LearningRecoveryRequired) else type(exc).__name__
        results['learning'] = {'reason': reason, 'state': 'recovery_required', 'at': now(),
                               'next_action': 'Verify the bound operational and learning databases before resuming.'}
        with db(path, True) as c:
            put(c, 'learning_progress', json.dumps(results['learning']))
    if not train.empty and not test.empty:
        frame = pd.concat([train, test]).sort_index()
        labels, summary = point_in_time_regimes(frame)
        equity = ~labels.index.get_level_values('symbol').str.contains('/', regex=False)
        by_time = {t.isoformat(): label for t, label in labels[equity].groupby(level='timestamp').first().items()}
        for scope in REGIMES:
            regime = dict(summary, label=scope, observed_regime=summary['label'], observed_by_time=by_time)
            try:
                results[scope] = review(path, settings, train, test, regime, mutator, observations_committed=True)
            except Exception as exc:
                # Preserve independent scopes and publish the failure without provider payloads.
                results[scope] = {'error': type(exc).__name__}
                with db(path, True) as c:
                    put(c, 'entry_gate_' + scope, 'closed')
    else:
        results['ingestion'] = 'insufficient_history'
    learn_from_closes(path, mutator)
    drift(path)
    with db(path, True) as c:
        put(c, 'research_scope_status', json.dumps(results))
    return results


def seconds_to_next_observation(stamp):
    # Equity intervals close on exchange-local half hours and at the actual close.
    local_day = datetime.fromtimestamp(stamp, ZoneInfo('America/New_York')).date()
    sessions = equity_sessions(local_day, local_day + timedelta(days=2))
    future = [cut + 15 for day in sessions for cut in day.cuts if cut + 15 > stamp]
    # UTC-hourly crypto observations have a separate schedule.
    next_crypto = ((int(stamp) - 15) // 3600 + 1) * 3600 + 15
    return max(1, min([next_crypto, *future]) - stamp)


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
        from llm_mutator import LLMMutator
        ingest = DataIngestor(s, None, observation_path=path)
        mut = LLMMutator(s)

        def tick():
            return research_tick(path, s, ingest, mut)
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
                with db(path,True) as c:
                    for rl in REGIMES:
                        put(c, f'entry_gate_{rl}', 'closed')
            log.error('%s unavailable (%s); no automatic replay of submissions', args.role, type(e).__name__)
            if args.once:
                raise SystemExit(1)
            time.sleep(20)
            continue
        if args.once:
            return
        time.sleep(10 if args.role == 'supervise' else seconds_to_next_observation(time.time()))
if __name__ == '__main__':
    main()
