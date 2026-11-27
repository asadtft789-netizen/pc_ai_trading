"""Bounded research tournament. No broker, database, or promotion authority."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from itertools import product
from llm_mutator import validate
from strategy_worker import FEATURES, compile_strategy

FOCUSES = (
    'trend and momentum continuation',
    'breakout and relative volume confirmation',
    'pullback and mean reversion within a regime',
    'volatility, support, and adverse conditions',
    'challenge the parent hypothesis with a genuinely different rule',
)

def signature(source, guard):
    """Exact behavioural fingerprint over all 4096 allowed feature vectors."""
    fn = compile_strategy(source)
    i = FEATURES.index(guard) if guard else None
    return bytes(int(bool(fn(*row)) and (i is None or row[i]))
                 for row in product((False, True), repeat=len(FEATURES)))

def generate(mutator, evidence):
    """Parallel network requests; isolated evidence; deterministic result order."""
    proposals = []
    errors = []
    with ThreadPoolExecutor(max_workers=len(FOCUSES)) as pool:
        jobs = {}
        for i, focus in enumerate(FOCUSES):
            context = deepcopy(evidence)
            context['research_focus'] = focus
            jobs[pool.submit(mutator.propose, context)] = i
        for job in as_completed(jobs):
            i = jobs[job]
            try:
                p = job.result()
                if p is None:
                    errors.append((i, 'no_proposal'))
                    continue
                validate(p)
                proposals.append((i, dict(p)))
            except Exception as exc:
                errors.append((i, type(exc).__name__))
    return sorted(proposals), sorted(errors)

def screen(proposals, evaluate, passes, *, incumbent=None, preserve_source=None):
    """Bound RAM: backtest serially on the development set, never the holdout.

    A failed experiment must not abort the others. Fingerprints are exact for
    the current restricted Boolean feature grammar, not for future feature sets.
    """
    seen = set()
    if incumbent is not None:
        seen.add(signature(*incumbent))
    best = None
    records = []
    for i, raw in proposals:
        p = dict(raw)
        try:
            if preserve_source is not None:
                p['source'] = preserve_source
            validate(p)
            fp = signature(p['source'], p['guard'])
            if fp in seen:
                records.append({'candidate': i, 'status': 'duplicate'})
                continue
            seen.add(fp)
            m = evaluate(p['source'], p['guard'])
            ok = bool(passes(m))
            records.append({'candidate': i, 'status': 'passed' if ok else 'rejected',
                            'pnl': float(m['pnl']), 'trades': int(m['total_trades']),
                            'drawdown': float(m['max_drawdown']),
                            'model': p.get('provider_model', 'unknown')})
            if ok:
                rank = (float(m['pnl']), float(m['max_drawdown']), -i)
                if best is None or rank > best[0]:
                    best = (rank, p)
        except Exception as exc:
            records.append({'candidate': i, 'status': 'error',
                            'error': type(exc).__name__})
    return (best[1] if best else None), records
