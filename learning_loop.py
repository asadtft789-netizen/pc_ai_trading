"""Hourly paper research. No network, broker, order routing, exec, or daemon DB writes.

Python 3.10+. Times are integer UTC seconds. Calendar labels are exchange dates.
The caller supplies actual sessions, completed bars, features and point-in-time gates.
"""
from __future__ import annotations
from portfolio_policy import MAX_POSITIONS, protection_prices

import ast
import calendar
import hashlib
import json
import math
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

FEATURES = ('trend','breakout','oversold','hammer','pullback','structure','support','resistance','golden','volume','quiet','momentum')
LOCKED = frozenset({'SPREAD','EXIT'})
Json = dict[str, Any]

def encode(x: Any) -> str:
    return json.dumps(x, sort_keys=True, separators=(',', ':'), allow_nan=False)

def digest(x: Any) -> str:
    return hashlib.sha256(encode(x).encode()).hexdigest()

def require(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)

# Bitwise truth tables, not Python execution. All 4,096 feature assignments are checked.
ALL = (1 << (1 << len(FEATURES))) - 1
BITS = {f: sum(1 << n for n in range(1 << len(FEATURES)) if n & (1 << i)) for i, f in enumerate(FEATURES)}

@lru_cache(maxsize=256)
def truth(expr: str) -> int:
    require(isinstance(expr, str) and 0 < len(expr) <= 1800, 'invalid rule length')
    root = ast.parse(expr, mode='eval').body
    def walk(n: ast.AST) -> int:
        if isinstance(n, ast.Name) and n.id in BITS: return BITS[n.id]
        if isinstance(n, ast.Constant) and type(n.value) in (bool,int) and n.value in (0,1): return ALL if n.value else 0
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not): return ALL ^ walk(n.operand)
        if isinstance(n, ast.BoolOp) and isinstance(n.op, (ast.And,ast.Or)):
            vals = [walk(v) for v in n.values]; value = vals[0]
            for v in vals[1:]: value = value & v if isinstance(n.op, ast.And) else value | v
            return value
        raise ValueError('only the 12 feature names, boolean operators and 0/1 literals are allowed')
    return walk(root)

@dataclass(frozen=True)
class Rule:
    key: str
    text: str
    expression: str | None
    parent: str = ''

    @property
    def version(self) -> str: return digest(asdict(self))

Book = dict[str, Rule]

def fixture() -> Book:
    """Examples only. Do not initialize a real champion from these by default."""
    return {r.key:r for r in (Rule('TREND','Require trend.','trend'), Rule('ENTRY','Require breakout.','breakout'), Rule('SPREAD','Executor-owned spread gate; needs verified quotes.',None), Rule('EXIT','Executor-owned protection and session exit.',None))}

def expression(book: Mapping[str, Rule]) -> str:
    require(LOCKED <= book.keys(), 'missing locked references')
    for k,r in book.items():
        require(k == r.key and re.fullmatch('[A-Z][A-Z0-9_]{0,35}', k) is not None, 'invalid logical rule ID')
        require((k in LOCKED) == (r.expression is None), 'execution guards cannot become signal rules')
        if r.expression is not None: truth(r.expression)
    terms = [f'({book[k].expression})' for k in sorted(book) if book[k].expression is not None]
    require(bool(terms), 'empty signal')
    return ' and '.join(terms)

def source(book: Mapping[str, Rule]) -> str:
    s = 'def signal(' + ', '.join(FEATURES) + '):\n    return 1 if ' + expression(book) + ' else 0\n'
    require(len(s) <= 2400,'strategy exceeds worker limit'); return s

def patch(book: Book, key: str, expr: str, text: str) -> Book:
    require(key not in LOCKED and bool(text.strip()), 'invalid change to a protected or undescribed rule')
    child = dict(book); child[key] = Rule(key,text,expr,book[key].version if key in book else '')
    source(child); require(truth(expression(book)) != truth(expression(child)), 'no change in signal behavior')
    return child

def unpack(b: Json) -> Book: return {k:Rule(**v) for k,v in b.items()}
def pack(b: Book) -> Json: return {k:asdict(v) for k,v in b.items()}

def mask(flags: Mapping[str, bool]) -> int:
    require(set(flags) == set(FEATURES) and all(type(v) is bool for v in flags.values()), 'need all 12 boolean features')
    return sum(int(flags[k]) << i for i,k in enumerate(FEATURES))

@dataclass(frozen=True)
class Session:
    label: str
    opened: int
    cuts: tuple[int,...]  # ordered, final cut = official close; short boundary slots are mark/exit only

    def validate(self) -> None:
        date.fromisoformat(self.label); require(bool(self.cuts) and type(self.opened) is int,'empty session')
        for a,b in zip((self.opened,*self.cuts),self.cuts): require(type(b) is int and 0 < b-a <= 3600,'invalid hourly calendar grid')

@dataclass(frozen=True)
class Risk:
    cash: float
    order_cap: float
    fee_bps: float
    daily_loss: float
    max_drawdown_fraction: float
    max_positions: int
    quantity_step: float
    max_stop_risk: float
    no_entry_last_seconds: int
    policy_revision: int = 1

    def validate(self) -> None:
        require(type(self.policy_revision) is int and self.policy_revision in (1, 2), "unknown shadow policy")
        for v in asdict(self).values(): require(type(v) in (float,int) and math.isfinite(v),'non-finite risk setting')
        require(min(self.cash,self.order_cap,self.daily_loss,self.quantity_step,self.max_stop_risk)>0 and 0<self.max_drawdown_fraction<1 and 0<=self.fee_bps<1000 and type(self.max_positions) is int and self.max_positions>0 and self.no_entry_last_seconds>=0,'invalid risk limits')

@dataclass(frozen=True)
class Bar:
    o: float; h: float; l: float; c: float
    bits: int                     # features known only at bar close
    stop_gap: float                # distance known at that close, used on next open
    entry_ok: bool | None          # external gate as of this bar OPEN; None = unverified
    entry_cap: float               # external point-in-time notional cap at open

    def validate(self) -> None:
        require(all(type(v) in (float,int) and math.isfinite(v) for v in (self.o,self.h,self.l,self.c,self.stop_gap,self.entry_cap)), 'bad price or cap')
        require(0<self.l<=min(self.o,self.c)<=max(self.o,self.c)<=self.h and self.stop_gap>0 and self.entry_cap>=0,'invalid bar')
        require(type(self.bits) is int and 0<=self.bits<4096 and (self.entry_ok is None or type(self.entry_ok) is bool),'invalid features or gate')

@dataclass(frozen=True)
class Step:
    start: float; end: float; flat: bool; actions: tuple[tuple[str,str],...]; fills: int; audit: tuple[Json,...]

class Portfolio:
    """Reference OHLC shadow model, NOT the daemon's verified fill engine. No broker calls."""
    def __init__(self, book: Book, risk: Risk, bps: int) -> None:
        self.table=truth(expression(book)); self.r=risk; self.slip=bps/10000; self.fee=risk.fee_bps/10000
        self.cash=self.peak=self.day_start=risk.cash; self.dd=0.; self.pos:dict[str,tuple[float,float]]={}; self.prev:dict[str,tuple[bool,float]]={}
        self.targets={}; self.position_limit = min(MAX_POSITIONS,risk.max_positions) if risk.policy_revision == 2 else risk.max_positions; self.day_block=False; self.run_block=False; self.trades=0; self.fills=0

    def step(self, bars: Mapping[str,Bar], fresh: bool, full: bool, final: bool, can_enter: bool) -> Step:
        if fresh:
            require(not self.pos,'overnight exposure'); self.prev={}; self.day_start=self.cash; self.day_block=False
        def equity(prices: Mapping[str,float]) -> float: return self.cash+sum(q*prices[s] for s,(q,_) in self.pos.items())
        def mark(eq: float) -> None:
            self.peak=max(self.peak,eq); self.dd=max(self.dd,(self.peak-eq)/self.peak)
            self.day_block |= self.day_start-eq >= self.r.daily_loss
            self.run_block |= eq <= self.peak*(1-self.r.max_drawdown_fraction)
        actions=[]; audit:list[Json]=[]; before=self.fills; op={s:b.o for s,b in bars.items()}; cl={s:b.c for s,b in bars.items()}
        start=equity(op); mark(start); held=set(self.pos)
        def sell(s: str,p: float,why: str) -> None:
            q,_=self.pos.pop(s); self.targets.pop(s); px=p*(1-self.slip); audit.append({'kind':'shadow_fill','side':'sell','symbol':s,'why':why,'qty':q,'price':px,'fee':q*px*self.fee}); self.cash+=q*px*(1-self.fee); self.trades+=1; self.fills+=1; actions.append((s,why))
        for s in sorted(held):
            b=bars[s]; _,stop=self.pos[s]
            if b.o<=stop: sell(s,b.o,'gap_stop')
            elif self.r.policy_revision == 2 and b.o >= self.targets[s]: sell(s,b.o,'gap_target')
            elif self.run_block or self.day_block or not self.prev.get(s,(False,0))[0]: sell(s,b.o,'exit')
        # Full batch, one cash pool, alphabetical priority. Never spend the same cash twice.
        for s in sorted(bars):
            b=bars[s]; take,gap=self.prev.get(s,(False,0.))
            reason=next((msg for fail,msg in ((not take,'no_prior_signal'),(not full,'short_slot'),(not can_enter,'session_cutoff'),(b.entry_ok is None,'unverified_gate'),(b.entry_ok is False,'executor_veto'),(s in held,'already_held'),(len(self.pos)>=self.position_limit,'position_cap'),(self.day_block or self.run_block,'loss_limit')) if fail),None)
            if reason:
                audit.append({'kind':'skip','symbol':s,'why':reason}); continue
            px=b.o*(1+self.slip); unit=px*(1+self.fee)
            if gap>=px:
                audit.append({'kind':'skip','symbol':s,'why':'invalid_stop_distance'}); continue
            cap=min(self.r.order_cap,b.entry_cap,self.cash)
            q=math.floor(min(cap/unit,self.r.max_stop_risk/gap)/self.r.quantity_step)*self.r.quantity_step
            if q<=0:
                audit.append({'kind':'skip','symbol':s,'why':'below_quantity_step'}); continue
            stop, self.targets[s] = protection_prices(px,gap)
            audit.append({'kind':'shadow_fill','side':'buy','symbol':s,'qty':q,'price':px,'fee':q*px*self.fee,'initial_stop':px-gap}); self.cash-=q*unit; self.pos[s]=(q,stop); actions.append((s,'buy')); self.fills+=1
        mark(equity(op))
        # If both levels trade in a bar, use the adverse ordering for exploratory replay.
        for s,(_,stop) in sorted(list(self.pos.items())):
            if bars[s].l<=stop: sell(s,stop,'stop')
            elif self.r.policy_revision == 2 and bars[s].h>=self.targets[s]: sell(s,self.targets[s],'target')
        end=equity(cl); mark(end)
        if final or self.run_block or self.day_block:
            for s in sorted(list(self.pos)): sell(s,bars[s].c,'close' if final else 'risk_exit')
            end=equity(cl); mark(end)
        self.prev=({s:(bool((self.table>>b.bits)&1),b.stop_gap) for s,b in bars.items()} if full and not final else {})
        require(self.cash>=-1e-8,'cash limit violated')
        return Step(start,end,not self.pos,tuple(actions),self.fills-before,tuple(audit))


def paired(book: Book, child: Book, sessions: Sequence[Session], rows: Mapping[int,Mapping[str,Bar]], risk: Risk, slip_bps: int) -> Json:
    """Count differing portfolio episodes, not symbols, repeated signals or fills."""
    p,q=Portfolio(book,risk,slip_bps),Portfolio(child,risk,slip_bps); episodes=[]; active=None; unknown=False; curve=[]; audit=[]
    for i,day in enumerate(sessions):
        beg=day.opened
        for j,cut in enumerate(day.cuts):
            bs=rows[cut]; unknown |= any(b.entry_ok is None for b in bs.values())
            args=(bs,j==0,cut-beg==3600,cut==day.cuts[-1],beg<day.cuts[-1]-risk.no_entry_last_seconds)
            a=p.step(*args); b=q.step(*args); audit.append({'open':beg,'cut':cut,'inc':a.audit,'cand':b.audit})
            if active is None and a.actions!=b.actions: active=(i,beg,a.start,b.start)
            if active is not None and a.flat and b.flat:
                d,t,x,y=active; episodes.append({'session':d,'start':t,'end':cut,'inc':a.end-x,'cand':b.end-y}); active=None
            curve.append([cut,a.end,b.end]); beg=cut
    require(active is None,'unresolved portfolio episode')
    return {'episodes':episodes,'curve':curve,'audit':audit,'inc_pnl':p.cash-risk.cash,'cand_pnl':q.cash-risk.cash,'inc_dd':p.dd,'cand_dd':q.dd,'inc_trades':p.trades,'cand_trades':q.trades,'unknown_gate':unknown,'slip_bps':slip_bps}


def judge(base: Json, stress: Json, gates: Mapping[str,bool|None]) -> tuple[str,str]:
    """Legacy gates must be supplied by the existing research code, not approximated."""
    required={'passes','supported','incumbent_beaten','model_parity','safety_attested'}
    require(set(gates)==required and all(v is None or type(v) is bool for v in gates.values()),'missing or invalid host gates')
    if gates['safety_attested'] is False: return 'rejected','hard-boundary violation'
    if base['unknown_gate'] or stress['unknown_gate'] or gates['model_parity'] is not True or any(v is None for v in gates.values()): return 'inconclusive','missing verified gate or execution parity'
    eps=base['episodes']
    if len(eps)<20: return 'inconclusive',f'{len(eps)}/20 differing portfolio episodes'
    halves=[[e['cand']-e['inc'] for e in eps if (e['session']<2)==first] for first in (True,False)]
    if not all(halves): return 'inconclusive','one comparison block has no opportunities'
    if not all(gates.values()): return 'rejected','existing research gate failed'
    if base['cand_trades']<20 or base['cand_pnl']<=0 or base['cand_dd']>=0.1: return 'rejected','candidate shadow trade count, profit or drawdown gate failed'
    if not all(sum(x)/len(x)>1e-10 for x in halves): return 'rejected','no net advantage in both time blocks'
    if base['cand_dd']>base['inc_dd']+1e-12: return 'rejected','worse portfolio drawdown'
    if stress['cand_pnl']-stress['inc_pnl']<=1e-10: return 'rejected','advantage did not survive two basis points'
    return 'staged','all gates passed; not an entry authorization'


def next_month(d: date) -> date:
    year=d.year+(d.month==12); month=d.month%12+1
    return date(year,month,min(d.day,calendar.monthrange(year,month)[1]))

class Ledger:
    """Private SQLite research journal. Never open the daemon DB as this ledger."""
    def __init__(self, path: str|Path, clock: Callable[[],float]=time.time) -> None:
        self.clock=clock; self.db=sqlite3.connect(path,timeout=10,isolation_level=None); self.db.row_factory=sqlite3.Row
        other=self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'll_*' AND name NOT GLOB 'sqlite_*'").fetchall()
        if other: self.db.close(); raise ValueError('refusing non-learning database; use a separate sidecar file')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS ll_meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ll_rules(id TEXT PRIMARY KEY, logical_id TEXT NOT NULL, created INTEGER NOT NULL, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ll_events(n INTEGER PRIMARY KEY, at INTEGER NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ll_bars(cut INTEGER PRIMARY KEY, at INTEGER NOT NULL, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ll_obs(id TEXT PRIMARY KEY, start INTEGER NOT NULL, end INTEGER NOT NULL, rule TEXT NOT NULL, ver TEXT NOT NULL, bits INTEGER NOT NULL, net REAL NOT NULL, origin TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ll_trials(id TEXT PRIMARY KEY, first TEXT UNIQUE NOT NULL, at INTEGER NOT NULL, state TEXT NOT NULL, frozen TEXT NOT NULL, result TEXT, effective INTEGER);
        CREATE TABLE IF NOT EXISTS ll_solutions(problem_id TEXT PRIMARY KEY, fix_expression TEXT NOT NULL, confirmed_at INTEGER NOT NULL, description TEXT);
        ''')
        for table in ('ll_events','ll_rules','ll_bars','ll_obs','ll_solutions'):
            for op in ('UPDATE','DELETE'):
                self.db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{op} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'immutable research history'); END")

    def close(self) -> None: self.db.close()
    @contextmanager
    def tx(self) -> Iterator[None]:
        self.db.execute('BEGIN IMMEDIATE')
        try: yield
        except BaseException: self.db.rollback(); raise
        else: self.db.commit()
    def _get(self,k:str) -> Any:
        row=self.db.execute('SELECT v FROM ll_meta WHERE k=?',(k,)).fetchone(); return json.loads(row[0]) if row else None
    def _set(self,k:str,v:Any) -> None: self.db.execute('INSERT INTO ll_meta VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',(k,encode(v)))
    def _event(self,k:str,v:Any) -> None: self.db.execute('INSERT INTO ll_events(at,kind,body) VALUES(?,?,?)',(int(self.clock()),k,encode(v)))
    def _reg(self,r:Rule) -> None: self.db.execute('INSERT OR IGNORE INTO ll_rules VALUES(?,?,?,?)',(r.version,r.key,int(self.clock()),encode(asdict(r))))
    def state(self) -> Json: return {r[0]:json.loads(r[1]) for r in self.db.execute('SELECT k,v FROM ll_meta')}
    def _days(self) -> list[Session]: return [Session(x['label'],x['opened'],tuple(x['cuts'])) for x in self._get('config')['sessions']]
    def setup(self, start: str, sessions: Sequence[Session], symbols: Sequence[str], risk: Risk, champion: Book,
              *, previous_config: str | None = None, calendar_id: str | None = None) -> None:
        """Calendar supplied by caller: all regular sessions in [start date, next-month date)."""
        begin=date.fromisoformat(start); stop=next_month(begin); risk.validate(); source(champion)
        require(len(sessions)>=11 and len(set(symbols))==len(symbols)>0 and all(isinstance(s,str) and s.strip()==s and s for s in symbols),'invalid universe/calendar')
        last=0; label=''
        for day in sessions:
            day.validate(); require(begin<=date.fromisoformat(day.label)<stop and day.opened>last and day.label>label,'invalid session sequence'); last=day.cuts[-1]; label=day.label
        cfg={'model_policy':{'engine_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'extra_slippage_bps_per_side':[1,2],'late_batch_seconds':120,'allocation':'alphabetical, shared cash','drawdown_marks':'opens and closes, including stop fills','min_differing_episodes':20,'split_sessions':[2,3]},'start':start,'end_exclusive':stop.isoformat(),'sessions':[asdict(s) for s in sessions],'symbols':sorted(symbols),'risk':asdict(risk)}
        if calendar_id is not None:
            cfg['calendar_id'] = calendar_id
        with self.tx():
            old = self._get('config')
            require(self.clock()<sessions[0].opened,'register before first session')
            if previous_config is None:
                require(old is None,'run already initialized')
                self._set('halted',False)
            else:
                require(old is not None and digest(old) == previous_config, 'campaign changed during rollover')
                old_last = old['sessions'][-1]['cuts'][-1]
                require(self.clock() >= old_last and sessions[0].opened > old_last, 'previous campaign unfinished')
                self._event('campaign_closed', {'config': old, 'original':self._get('original'),
                            'active':self._get('active'), 'halted':self._get('halted')})
                self.db.execute("""UPDATE ll_trials SET state='inconclusive', result=?
                                   WHERE state='testing'""",
                                (encode({'status':'inconclusive','why':'campaign ended without a verdict'}),))
                self.db.execute("UPDATE ll_trials SET state='expired' WHERE state='staged'")
            self._set('config',cfg); self._set('active',pack(champion)); self._set('original',pack(champion))
            for r in champion.values():
                self._reg(r)
                if self._get('rule_state:'+r.version) is None:
                    self._set('rule_state:'+r.version,'unvalidated')
            self._event('run_registered',{'config':cfg,'book':pack(champion)})

    def bar(self, cut: int, values: Mapping[str,Bar]) -> bool:
        """One finalized batch per cutoff. No partial or subsequently edited batch is accepted."""
        now=int(self.clock()); require(now>=cut,'unfinished bar'); cfg=self._get('config'); require(cfg is not None,'not initialized')
        session=next((d for d in cfg['sessions'] if cut in d['cuts']),None); require(session is not None,'cutoff not on declared calendar')
        require(set(values)==set(cfg['symbols']),'incomplete symbol batch')
        for b in values.values(): b.validate()
        body=encode({s:asdict(b) for s,b in values.items()})
        with self.tx():
            row=self.db.execute('SELECT body FROM ll_bars WHERE cut=?',(cut,)).fetchone()
            if row: require(row[0]==body,'conflicting replay; history not overwritten'); return False
            day_key='session_book:'+session['label']
            if self._get(day_key) is None: self._set(day_key,self._get('active'))
            self.db.execute('INSERT INTO ll_bars VALUES(?,?,?)',(cut,now,body)); return True

    def observe(self, event_id:str, start:int, end:int, key:str, version:str, bits:int, net:float, origin:str) -> None:
        """Completed strategy-following episodes only; provenance is explicitly broker or shadow."""
        require(bool(event_id) and start<end<=self.clock() and type(bits) is int and 0<=bits<4096 and math.isfinite(net) and origin in ('broker_paper','shadow'),'invalid observation')
        require(any(d.opened<=start<end<=d.cuts[-1] for d in self._days()),'observation outside this run or spans sessions')
        require(self.db.execute('SELECT 1 FROM ll_rules WHERE id=? AND logical_id=?',(version,key)).fetchone() is not None,'unknown rule version')
        with self.tx():
            row=self.db.execute('SELECT * FROM ll_obs WHERE id=?',(event_id,)).fetchone(); vals=(event_id,start,end,key,version,bits,net,origin)
            if row: require(tuple(row)==vals,'conflicting observation'); return
            self.db.execute('INSERT INTO ll_obs VALUES(?,?,?,?,?,?,?,?)',vals)

    def suggest(self, target:str='ENTRY', add:bool=False, origin:str='broker_paper') -> Json|None:
        """Propose one missing-feature filter from >=3 independent losses, never a promotion."""
        book=unpack(self._get('active')); require(target in book and target not in LOCKED,'unmapped signal rule'); r=book[target]
        rows=self.db.execute('SELECT * FROM ll_obs WHERE rule=? AND ver=? AND origin=? ORDER BY end,id',(target,r.version,origin)).fetchall()
        candidates=[]
        for idx,f in enumerate(FEATURES):
            group=[]; until=-1
            for row in rows:
                if row['start']>=until and not row['bits']&(1<<idx): group.append(row); until=row['end']
            losses=[v['id'] for v in group if v['net']<0]; score=-sum(v['net'] for v in group)
            if len(losses)<3 or score<=0 or not any(r['bits']&(1<<idx) and r['net']>0 for r in rows): continue
            key=('FILTER_'+f.upper()) if add else target
            if add and key in book: continue
            expr=f if add else f'({r.expression}) and {f}'
            try: patch(book,key,expr,'Require '+f)
            except ValueError: continue
            candidates.append((score,f,key,expr,losses))
        if not candidates: return None
        _,f,key,expr,ids=sorted(candidates,key=lambda x:(-x[0],x[1]))[0]
        return {'key':key,'expr':expr,'text':f'Test requiring {f}.','evidence':ids,'anchor':target}

    def register(self, first:str, key:str, expr:str, text:str, evidence:Sequence[str], anchor:str) -> str:
        now=int(self.clock()); days=self._days(); i=next((i for i,d in enumerate(days) if d.label==first),-1)
        require(0<=i and i+5<len(days)-5 and now<days[i].opened,'window must be prospective, with activation before holdout'); last=days[i+4].cuts[-1]
        with self.tx():
            require(not self._get('halted'),'halted'); book=unpack(self._get('active')); require(anchor in book and anchor not in LOCKED and (key not in book or key==anchor),'evidence must concern affected signal rule')
            refs=[]
            for eid in set(evidence):
                row=self.db.execute('SELECT * FROM ll_obs WHERE id=?',(eid,)).fetchone(); require(row is not None and row['rule']==anchor and row['ver']==book[anchor].version and row['end']<=now and row['net']<0,'evidence missing or not for this rule version'); refs.append(row)
            refs.sort(key=lambda r:r['start']); require(len(refs)>=3 and len({r['origin'] for r in refs})==1 and all(a['end']<=b['start'] for a,b in zip(refs,refs[1:])),'three independent loss episodes of one provenance required')
            require(not self.db.execute("SELECT 1 FROM ll_trials WHERE state IN ('testing','staged')").fetchone(),'another candidate is active')
            for x in self.db.execute('SELECT frozen FROM ll_trials'):
                old=json.loads(x[0]); require(not (old['key']==key and old['new'][key]['expression']==expr and set(old['evidence'])==set(evidence) and old['old']==pack(book)), 'candidate already tested on this discovery evidence'); require(last<old['open'] or days[i].opened>old['close'],'cannot reuse a trial window')
            new=patch(book,key,expr,text); self._reg(new[key]); self._set('rule_state:'+new[key].version,'testing')
            frozen={'old':pack(book),'new':pack(new),'key':key,'evidence':sorted(evidence),'first':first,'open':days[i].opened,'close':last,'next':days[i+5].opened,'policy':'hourly-pairs-v1','run_config':digest(self._get('config'))}
            ident=digest(frozen); self.db.execute('INSERT INTO ll_trials VALUES(?,?,?,?,?,NULL,NULL)',(ident,first,now,'testing',encode(frozen))); self._event('hypothesis',{'trial':ident,**frozen}); return ident

    def _data(self,days:Sequence[Session]) -> tuple[dict[int,dict[str,Bar]],list[int]]:
        rows={}; gaps=[]
        for d in days:
            for cut in d.cuts:
                row=self.db.execute('SELECT at,body FROM ll_bars WHERE cut=?',(cut,)).fetchone()
                if not row or row[0]>cut+120: gaps.append(cut); continue
                rows[cut]={s:Bar(**b) for s,b in json.loads(row[1]).items()}
        return rows,gaps

    def decide(self,ident:str,gates:Mapping[str,bool|None]) -> Json:
        with self.tx():
            row=self.db.execute('SELECT * FROM ll_trials WHERE id=?',(ident,)).fetchone(); require(row is not None,'unknown trial')
            if row['result']: return json.loads(row['result'])
            f=json.loads(row['frozen']); require(row['state']=='testing' and self.clock()>=f['close'],'window not complete')
            days=[d for d in self._days() if f['open']<=d.opened<=f['close']]; require(len(days)==5,'invalid frozen calendar'); rows,gaps=self._data(days)
            if gates.get('safety_attested') is False:
                self._set('halted',True); self._event('halt',{'reason':'verified hard-boundary violation','trial':ident}); status,why='rejected','hard-boundary violation'; details={'missing_cuts':gaps}
            elif self._get('config')['model_policy']['engine_sha256'] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest(): status,why='inconclusive','reference engine changed during experiment'; details={}
            elif gaps: status,why='inconclusive','missing/late hourly batch'; details={'missing_cuts':gaps}
            else:
                args=(unpack(f['old']),unpack(f['new']),days,rows,Risk(**self._get('config')['risk'])); a=paired(*args,1); b=paired(*args,2); status,why=judge(a,b,gates); details={'one_bp':a,'two_bp':b}
            r={'trial':ident,'status':status,'why':why,'host_gates':dict(gates),**details}; child=unpack(f['new'])[f['key']]
            self.db.execute('UPDATE ll_trials SET state=?,result=? WHERE id=?',(status,encode(r),ident)); self._set('rule_state:'+child.version,'supported' if status=='staged' else ('harmful' if status=='rejected' and why in ('worse portfolio drawdown','no net advantage in both time blocks') else 'unvalidated')); self._event('decision',r); return r

    def activate(self,ident:str) -> None:
        """Sidecar pointer only; cannot publish a daemon champion or set an entry gate."""
        with self.tx():
            row=self.db.execute('SELECT * FROM ll_trials WHERE id=?',(ident,)).fetchone(); require(row is not None and row['state']=='staged','not staged'); f=json.loads(row['frozen']); now=int(self.clock())
            if self._get('halted') or self._get('active')!=f['old'] or now>=f['next']+60 or self._get('config')['model_policy']['engine_sha256'] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
                self.db.execute("UPDATE ll_trials SET state='expired' WHERE id=?",(ident,)); self._event('activation_skipped',{'trial':ident,'at':now}); return
            require(now>=f['next'],'before next session'); self._set('active',f['new']); self.db.execute("UPDATE ll_trials SET state='active',effective=? WHERE id=?",(now,ident)); self._set('summary:'+f['key'],{'trial':ident,'text':f['new'][f['key']]['text']}); self._event('activated',{'trial':ident,'key':f['key'],'old':f['old'].get(f['key']),'new':f['new'][f['key']],'effective':now})

    def halt(self,reason:str,affected_trial:str|None=None) -> None:
        require(bool(reason.strip()),'reason required')
        with self.tx():
            self._set('halted',True); self._event('halt',{'reason':reason,'trial':affected_trial}); self.db.execute("UPDATE ll_trials SET state='halted' WHERE state IN ('staged','testing')")
            if not affected_trial:
                self._event('rollback_needs_attribution',{'reason':reason}); return
            row=self.db.execute('SELECT state,frozen FROM ll_trials WHERE id=?',(affected_trial,)).fetchone()
            if not row or row[0]!='active': self._event('rollback_not_applicable',{'trial':affected_trial}); return
            f=json.loads(row[1]); current=self._get('active'); k=f['key']
            if current.get(k)!=f['new'][k]: self._event('rollback_conflict',{'trial':affected_trial}); return
            if k in f['old']: current[k]=f['old'][k]
            else: current.pop(k,None)
            self._set('active',current); self._set('summary:'+k,{'rollback':affected_trial,'current':current.get(k),'reason':reason}); self._set('rule_state:'+unpack(f['new'])[k].version,'harmful'); self.db.execute("UPDATE ll_trials SET state='rolled_back' WHERE id=?",(affected_trial,)); self._event('rollback',{'trial':affected_trial,'key':k,'restored':current.get(k)})

    def holdout(self) -> Json:
        """Final five sessions, original vs frozen active signals; shadow P&L only."""
        days=self._days()[-5:]; require(self.clock()>=days[-1].cuts[-1],'holdout still open'); rows,gaps=self._data(days)
        if gaps or self._get('halted'): return {'status':'inconclusive','missing_cuts':gaps,'halted':self._get('halted')}
        args=(unpack(self._get('original')),unpack(self._get('active')),days,rows,Risk(**self._get('config')['risk'])); a=paired(*args,1); b=paired(*args,2)
        return {'status':'inconclusive' if a['unknown_gate'] else 'shadow_only','one_bp':a,'two_bp':b,'broker_pnl':None}

    def note(self, identity:str, kind:str, details:Mapping[str,Any]) -> None:
        """Append a deduplicated decision, skip, operational issue, lesson or correction."""
        require(bool(identity) and kind in ('decision','skip','process_fault','lesson','correction'),'invalid journal record')
        body={'id':identity,'details':dict(details)}
        with self.tx():
            prev=self._get('note:'+identity)
            if prev is not None: require(prev==digest([kind,body]),'conflicting journal record'); return
            self._event(kind,body); self._set('note:'+identity,digest([kind,body]))

    def daily_report(self,label:str) -> Json:
        day=next((d for d in self._days() if d.label==label),None); require(day is not None,'unknown session'); assert day is not None
        obs=[dict(r) for r in self.db.execute('SELECT * FROM ll_obs WHERE start>=? AND end<=? ORDER BY end',(day.opened,day.cuts[-1]))]
        # Observation samples are not a broker account statement.
        records=[{'at':r[0],'kind':r[1],'body':json.loads(r[2])} for r in self.db.execute('SELECT at,kind,body FROM ll_events WHERE at BETWEEN ? AND ? ORDER BY n',(day.opened,day.cuts[-1]))]
        return {'session':label,'outcome_observations':obs,'journal':records,'current_rule_versions':{k:v.version for k,v in unpack(self._get('active')).items()},'broker_account_pnl':None,'halted':self._get('halted')}

    def discover_session(self,label:str,target:str='ENTRY') -> Json:
        """Turn observed hourly bars into attributable, non-overlapping SHADOW episodes.

        Simultaneous entries are one portfolio episode. A feature counts as absent only
        if absent from every entry in the episode. Proposals, not broker outcomes.
        """
        days=self._days(); day=next((d for d in days if d.label==label),None); require(day is not None,'unknown session'); assert day is not None
        require(self.clock()>=day.cuts[-1],'discovery session still open')
        if self._get('halted') or day.label>=days[-5].label: return {'status':'frozen','observations':0}
        rows,gaps=self._data([day]); saved=self._get('session_book:'+label)
        if gaps or saved is None or any(b.entry_ok is None for bs in rows.values() for b in bs.values()): return {'status':'inconclusive','missing_cuts':gaps}
        book=unpack(saved); require(target in book and target not in LOCKED,'unmapped discovery rule'); v=book[target].version
        r=Risk(**self._get('config')['risk']); p=Portfolio(book,r,1); begin=day.opened; prev:Mapping[str,Bar]={}; ep=None; union=0; count=0
        for i,cut in enumerate(day.cuts):
            bs=rows[cut]; st=p.step(bs,i==0,cut-begin==3600,cut==day.cuts[-1],begin<day.cuts[-1]-r.no_entry_last_seconds)
            buys=[f['symbol'] for f in st.audit if f.get('side')=='buy']
            if buys and ep is None: ep=(begin,st.start); union=0
            for sym in buys: union |= prev[sym].bits
            if ep is not None and st.flat:
                start,e0=ep; identity=digest(['shadow-session-v1',label,target,v,start,cut]); self.observe(identity,start,cut,target,v,union,st.end-e0,'shadow'); count+=1; ep=None
            begin=cut; prev=bs
        self.note('shadow-discovery:'+label+':'+target,'lesson',{'session':label,'rule':target,'version':v,'episodes':count,'origin':'shadow','assumption':'standalone session started flat at configured reference cash'})
        return {'status':'shadow_only','observations':count}
