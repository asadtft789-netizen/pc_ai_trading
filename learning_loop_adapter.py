"""Read-only repo inspection and shadow-only hourly integration surfaces.

No network, broker SDK, daemon scheduler, entry permission, or champion writes.
Invoke close_tick AFTER feature calculation on a completed portfolio batch. Do not
wrap a production review that promotes before the new gate can run.
"""
from __future__ import annotations

import argparse
import ast
import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Protocol

from learning_loop import Bar, Book, FEATURES, Json, Ledger, expression, mask, require, source, truth

UNKNOWN_GATES = {k: None for k in ('passes','supported','incumbent_beaten','model_parity','safety_attested')}
EXPECTED = {'strategy_worker.py':('validate_source','compile_strategy'),'hourly_features.py':('enrich','signals'),'backtester.py':('run','passes'),'research.py':('review',),'executor.py':('sync_positions',),'paper_store.py':(),'config.py':(),'main.py':()}


def inspect_repo(path: str|Path, database: str|Path|None=None) -> Json:
    """Inspect signatures, not code execution. No secret values or rows are read."""
    root=Path(path); result:Json={'files':{},'auto_promotion':'DISABLED: schema, transaction ownership and model parity need human verification'}
    for name,wanted in EXPECTED.items():
        p=root/name
        if not p.is_file(): result['files'][name]={'status':'missing'}; continue
        try:
            tree=ast.parse(p.read_text(encoding='utf-8-sig'),filename=name)
            hits={n.name:[a.arg for a in n.args.posonlyargs+n.args.args+n.args.kwonlyargs] for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in wanted}
            result['files'][name]={'status':'present','signatures':hits,'not_found':sorted(set(wanted)-set(hits))}
        except (OSError,ValueError,SyntaxError) as e: result['files'][name]={'status':'cannot inspect','reason':type(e).__name__}
    if database:
        p=Path(database).resolve(); require(p.is_file(),'database not found')
        db=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)
        try:
            db.execute('PRAGMA query_only=ON')
            tables=('strategies','trials','rules','lessons','reflections','decisions','bars','watermarks','regimes','intents','fills','outcomes','lots')
            result['columns']={t:[r[1] for r in db.execute(f'PRAGMA table_info({t})')] for t in tables}
        finally: db.close()
    return result


def fresh_snapshot(status:str, age_seconds:float) -> bool:
    """Diagnostic only, not entry permission. Independent of hourly signal time."""
    return status=='ok' and math.isfinite(age_seconds) and 0<=age_seconds<=30


def verify_mapping(champion_src:str, book:Book) -> None:
    """Check all feature assignments; the actual logical rule IDs must also be verified in the repo."""
    require(len(champion_src)<=2400,'champion too long'); module=ast.parse(champion_src)
    require(len(module.body)==1 and isinstance(module.body[0],ast.FunctionDef),'unmapped strategy source'); fn=module.body[0]
    require(fn.name=='signal' and not fn.decorator_list and not fn.args.defaults and not fn.args.kw_defaults and not fn.args.posonlyargs and not fn.args.kwonlyargs and not fn.args.vararg and not fn.args.kwarg and tuple(a.arg for a in fn.args.args)==FEATURES,'unexpected champion signature')
    require(len(fn.body)==1 and isinstance(fn.body[0],ast.Return) and isinstance(fn.body[0].value,ast.IfExp),'unexpected champion return')
    ret=fn.body[0].value
    require(isinstance(ret.body,ast.Constant) and ret.body.value==1 and isinstance(ret.orelse,ast.Constant) and ret.orelse.value==0,'unexpected signal results')
    require(truth(ast.unparse(ret.test))==truth(expression(book)), 'rule map changes incumbent behavior')


def host_backtest(settings:Any, raw_frame:Any, book:Book) -> Json:
    """Optional per-symbol diagnostic using the reported public contracts.

    Not a portfolio simulator and not a substitute for guard ablation or review().
    Validate implementation against the real repo before using results as evidence.
    """
    from strategy_worker import FEATURES as host_features, validate_source
    from hourly_features import enrich, signals
    from backtester import Backtester
    require(tuple(host_features)==FEATURES,'feature order differs')
    src=source(book); res=validate_source(src)
    if res is False or (isinstance(res,tuple) and res and res[0] is False): raise ValueError('host rejected strategy source')
    frame=enrich(raw_frame.copy()); require(frame is not None,'enrich() return contract differs'); frame=frame.copy()
    out=signals(src,frame)
    require(len(out)==len(frame),'signal length mismatch')
    if hasattr(out,'index') and not callable(out.index): require(out.index.equals(frame.index),'signal index mismatch')
    values=list(out); require(all(v in (0,1) for v in values),'invalid signal vector'); frame['signal']=[int(v) for v in values]
    result=Backtester(settings).run(None,frame); require(isinstance(result,dict),'unexpected backtest result'); return result


def from_host_bar(o:float,h:float,l:float,c:float,features:Mapping[str,bool],stop_gap:float,entry_ok:bool|None,entry_cap:float) -> Bar:
    """Features/stop gap are close-time. Execution gate/cap MUST be as of this open."""
    b=Bar(o,h,l,c,mask(features),stop_gap,entry_ok,entry_cap); b.validate(); return b


class GateReader(Protocol):
    def __call__(self,trial_id:str) -> Mapping[str,bool|None]: ...


def close_tick(ledger:Ledger, cut:int, batch:Mapping[str,Bar], *, gates:GateReader|None=None, auto_propose:bool=True, origin:str='shadow', add_rule:bool=False, target:str='ENTRY') -> Json:
    """Idempotent hourly observer. Never calls review(), sync_positions() or a broker.

    A missing gate intentionally makes a completed trial inconclusive. The review
    call remains exactly where it is until its pre-promotion transaction is verified.
    """
    inserted=ledger.bar(cut,batch); result:Json={'inserted':inserted,'cut':cut}; now=int(ledger.clock())
    ended=next((d for d in ledger._days() if d.cuts[-1]==cut),None)
    if ended is not None and auto_propose and origin=='shadow': result['discovery']=ledger.discover_session(ended.label,target)
    testing=ledger.db.execute("SELECT id,frozen FROM ll_trials WHERE state='testing'").fetchone()
    if testing and now>=json.loads(testing[1])['close']:
        result['decision']=ledger.decide(testing[0],gates(testing[0]) if gates else UNKNOWN_GATES)
    if ledger._get('halted') or not auto_propose or ledger.db.execute("SELECT 1 FROM ll_trials WHERE state IN ('testing','staged')").fetchone(): return result
    proposal=ledger.suggest(target=target,add=add_rule,origin=origin)
    if not proposal: return result
    days=ledger._days(); possible=[d for d in days[:-10] if d.opened>now]
    if not possible: return result
    try: result['candidate']=ledger.register(first=possible[0].label,**proposal)
    except ValueError as e:
        # Repeated observation / competing hourly call is not a new experiment.
        if str(e) in ('candidate already tested on this discovery evidence','cannot reuse a trial window','another candidate is active'): result['no_candidate']=str(e)
        else: raise
    return result


def session_open(ledger:Ledger) -> None:
    """Optional sidecar-only pointer change, called on the existing session-open clock."""
    r=ledger.db.execute("SELECT id,frozen FROM ll_trials WHERE state='staged'").fetchone()
    if r and ledger.clock()>=json.loads(r[1])['next']: ledger.activate(r[0])


def hard_boundary_violation(ledger:Ledger, reason:str, affected_trial:str|None=None) -> None:
    """Call AFTER the real executor has independently halted entries. No auto-resume."""
    ledger.halt(reason,affected_trial)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Read-only hourly daemon compatibility inventory. Never starts trading.')
    parser.add_argument('--inspect',metavar='REPO',required=True); parser.add_argument('--db',metavar='EXISTING_DB')
    args=parser.parse_args(); print(json.dumps(inspect_repo(args.inspect,args.db),indent=2))
