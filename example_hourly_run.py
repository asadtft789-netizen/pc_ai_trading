"""One reproducible, synthetic hourly month. No Alpaca connection or daemon required.

Usage: python example_hourly_run.py [--output NEW_SIDECAR.db]
The fixture is engineered to exercise both outcomes, NOT to measure a trading edge.
"""
from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from learning_loop import Bar, FEATURES, Ledger, Risk, Session, fixture, mask
from learning_loop_adapter import close_tick, session_open

SYMBOLS=('AAA','BBB','CCC','DDD','EEE','FFF')
GATES={k:True for k in ('passes','supported','incumbent_beaten','model_parity','safety_attested')}

class Clock:
    def __init__(self,value:int) -> None: self.value=value
    def __call__(self) -> float: return self.value

def synthetic_calendar() -> list[Session]:
    # Weekdays are deliberate test fixtures, not a real market/holiday calendar.
    days=[]; d=date(2025,4,1)
    while d.month==4:
        if d.weekday()<5:
            op=int(datetime.combine(d,time(13,30),tzinfo=timezone.utc).timestamp()); days.append(Session(d.isoformat(),op,tuple([op+3600*i for i in range(1,7)]+[op+23400])))
        d+=timedelta(days=1)
    return days

def risk() -> Risk: return Risk(10.0,1.0,0.3,0.5,0.10,6,0.0001,0.10,1800)

def batch(j:int) -> dict[str,Bar]:
    data={}
    for i,s in enumerate(SYMBOLS):
        feat={k:True for k in FEATURES}; feat['trend']=feat['breakout']=i<2; feat['volume']=i==0
        o=105. if i==0 and j==6 else 100.; c=105. if i==0 and j in (5,6) else o
        low=(o if i==0 and j>=5 else (o-0.25 if i==0 else o-2.)); data[s]=Bar(o,max(o,c)+0.01,low,c,mask(feat),0.1 if i==0 else 1.,True,1.)
    return data

def add_discovery(l:Ledger,day:Session) -> None:
    """Three non-overlapping discovery losses and one different winning observation."""
    version=fixture()['ENTRY'].version
    for n in range(4):
        f={k: True for k in FEATURES}; f['volume']=n==3
        l.observe(f'discovery-{n}',day.opened+300*n,day.opened+300*n+120,'ENTRY',version,mask(f),0.05 if n==3 else -0.02,'shadow')

def run(path:Path) -> dict[str,Any]:
    require_new=not path.exists()
    if not require_new: raise ValueError('example refuses to overwrite an existing file')
    days=synthetic_calendar(); clock=Clock(days[0].opened-60); l=Ledger(path,clock); candidate=None; decision=None
    try:
        l.setup('2025-04-01',days,SYMBOLS,risk(),fixture())
        for i,day in enumerate(days):
            clock.value=day.opened; session_open(l)
            for j,cut in enumerate(day.cuts):
                clock.value=cut
                values=batch(j)
                if i==0:
                    # No planted trade outcomes: the model computes discovery losses and wins from these first-day inputs.
                    for sym in SYMBOLS:
                        eligible=(sym=='BBB' and j in (0,1,2)) or (sym=='AAA' and j==4)
                        if not eligible: values[sym]=replace(values[sym],bits=values[sym].bits & ~(1<<FEATURES.index('breakout')))
                r=close_tick(l,cut,values,gates=lambda _:GATES,origin='shadow')
                candidate=r.get('candidate',candidate); decision=r.get('decision',decision)
        state=l.state(); hold=l.holdout()
        return {'source':'Synthetic test fixture only; all legacy gates are test doubles.','sessions':len(days),'symbols':len(SYMBOLS),'hourly_or_close_batches':l.db.execute('SELECT count(*) FROM ll_bars').fetchone()[0], 'candidate':candidate, 'trial':decision['status'] if decision else None,'reason':decision['why'] if decision else None,'different_episodes':len(decision['one_bp']['episodes']) if decision and 'one_bp' in decision else None,'active_entry':state['active']['ENTRY']['expression'],'final_holdout':hold['status'],'broker_orders':0}
    finally: l.close()

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--output',type=Path); args=p.parse_args()
    if args.output: print(json.dumps(run(args.output),indent=2))
    else:
        with tempfile.TemporaryDirectory() as d: print(json.dumps(run(Path(d)/'research.sqlite'),indent=2))
