"""Prospective quote capture and replay for revision 3 shadow campaigns."""
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import learning_loop as legacy
import portfolio_account
import portfolio_events
import portfolio_policy
from portfolio_events import Decision, ExecutionPolicy, MarketFrame, Quote, QuotePortfolio

D = Decimal
UTC = timezone.utc


def utc(seconds):
    return datetime.fromtimestamp(seconds, UTC)


def replay_shadow(frames, policy=None):
    portfolio = QuotePortfolio(policy)
    for frame in frames:
        portfolio.step(frame)
    return portfolio


def engine_digest():
    # Source changes invalidate a frozen v3 experiment, including its accounting dependency.
    files = (__file__, legacy.__file__, portfolio_events.__file__, portfolio_account.__file__,
             portfolio_policy.__file__)
    return legacy.digest([hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in files])


@dataclass(frozen=True)
class QuoteRisk(legacy.Risk):
    minimum: float = 1.0
    fee_per_order: float = 0.0

    def policy(self):
        return ExecutionPolicy(capital=self.cash, order_cap=self.order_cap, position_cap=self.order_cap,
                               minimum=self.minimum, daily_loss=self.daily_loss,
                               quantity_step=self.quantity_step, fee_per_order=self.fee_per_order)

    def validate(self):
        legacy.require(self.policy_revision == 3 and self.max_drawdown_fraction == .10
                       and self.max_positions == 1 and self.no_entry_last_seconds == 1800
                       and self.max_stop_risk == self.cash * .01 and self.fee_bps == 0,
                       'unsupported quote campaign risk')
        self.policy()


@dataclass(frozen=True)
class TimedBar(legacy.Bar):
    published: str | None = None
    decided: str | None = None


def save_quotes(c, at, quotes, unknown=()):
    """Append one read-only market poll to the bound sidecar. Repeats are idempotent."""
    if at.utcoffset() is None:
        raise ValueError('quote poll requires an aware timestamp')
    key = at.astimezone(UTC).isoformat(timespec='microseconds')
    records = {}
    for quote in quotes:
        if (quote.symbol in records or quote.at.utcoffset() is None
                or type(quote.entry_ok) is not bool):
            raise ValueError('invalid shadow quote identity or gate')
        records[quote.symbol] = [quote.at.astimezone(UTC).isoformat(), str(quote.bid),
                                 str(quote.ask), quote.entry_ok]
    missing = sorted(set(unknown))
    if any(records.get(s, [None, None, None, False])[3] for s in missing):
        raise ValueError('unknown shadow gate permits entry')
    body = legacy.encode({'quotes': records, 'unknown': missing})
    row = c.execute('SELECT body FROM ll_quote_polls WHERE at=?', (key,)).fetchone()
    if row:
        legacy.require(row[0] == body, 'conflicting quote poll')
        return False
    c.execute('INSERT INTO ll_quote_polls(at,body) VALUES(?,?)', (key, body))
    return True


class CampaignLedger(legacy.Ledger):
    """Leave the source, engine digest and semantics of frozen v1/v2 campaigns intact."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.db.execute('CREATE TABLE IF NOT EXISTS ll_quote_polls(at TEXT PRIMARY KEY, body TEXT NOT NULL)')
        for op in ('UPDATE', 'DELETE'):
            self.db.execute(f"""CREATE TRIGGER IF NOT EXISTS ll_quote_polls_{op} BEFORE {op}
                ON ll_quote_polls BEGIN SELECT RAISE(ABORT,'immutable quote poll'); END""")

    def _set(self, key, value):
        # setup() freezes this dictionary and records it in the registration event in one transaction.
        if key == 'config' and value['risk'].get('policy_revision') == 3:
            value['model_policy'].update(quote_engine_sha256=engine_digest(),
                execution=portfolio_events.VERSION, drawdown_marks='fresh bid on observed poll',
                cost_scenario='observed bid/ask plus per-side turnover allowance')
        super()._set(key, value)

    def _quote_policy(self):
        cfg = self._get('config')
        if cfg and cfg['risk'].get('policy_revision') == 3:
            return QuoteRisk(**cfg['risk']).policy()
        return None

    def _matches(self):
        return self._get('config')['model_policy']['quote_engine_sha256'] == engine_digest()

    def _day_inputs(self, day, symbols):
        """Merge frozen timely features with captured polls. Never substitute OHLC for quotes."""
        symbols = set(symbols)
        start, end = utc(day.opened), utc(day.cuts[-1])
        decisions = {}
        bits = {}
        unknown = False
        previous = day.opened
        for cut in day.cuts:
            row = self.db.execute('SELECT at,body FROM ll_bars WHERE cut=?', (cut,)).fetchone()
            if not row or row['at'] > cut + 120:
                unknown = True
            else:
                data = json.loads(row['body'])
                for symbol in sorted(symbols):
                    bar = TimedBar(**data[symbol])
                    if not bar.published or not bar.decided:
                        unknown = True
                        continue
                    published, decided = map(datetime.fromisoformat, (bar.published, bar.decided))
                    if (published.utcoffset() is None or decided.utcoffset() is None
                            or not 0 <= (published - utc(cut)).total_seconds() <= 120
                            or not 0 <= (decided - published).total_seconds() <= 30):
                        unknown = True
                        continue
                    if decided <= end:
                        decisions.setdefault(decided, []).append((symbol, utc(previous), utc(cut),
                                                                  published, decided, bar))
                        bits[(symbol, utc(cut))] = bar.bits
            previous = cut
        # Merge at receipt time. Reconstructing an earlier decision on a later poll would
        # place information before Phoenix first saw the completed batch.
        times = iter(sorted(decisions))
        next_decision = next(times, None)
        observed_slots = set()
        for timestamp, body in self.db.execute(
                'SELECT at,body FROM ll_quote_polls WHERE at BETWEEN ? AND ? ORDER BY at',
                (start.isoformat(timespec='microseconds'), end.isoformat(timespec='microseconds'))):
            at = datetime.fromisoformat(timestamp)
            while next_decision is not None and next_decision < at:
                yield MarketFrame(next_decision, start, end), decisions[next_decision], bits, False
                next_decision = next(times, None)
            facts = json.loads(body)
            quotes = tuple(Quote(s, datetime.fromisoformat(t), D(bid), D(ask), gate)
                           for s, (t, bid, ask, gate) in facts['quotes'].items() if s in symbols)
            unknown |= bool(symbols.intersection(facts['unknown'])) or len(quotes) != len(symbols)
            if quotes:
                observed_slots.add(next(c for c in day.cuts if at <= utc(c)))
            batch = decisions.get(at, ())
            if at == next_decision:
                next_decision = next(times, None)
            yield MarketFrame(at, start, end, quotes=quotes), batch, bits, False
        while next_decision is not None:
            yield MarketFrame(next_decision, start, end), decisions[next_decision], bits, False
            next_decision = next(times, None)
        yield None, (), bits, unknown or observed_slots != set(day.cuts)

    def _compare(self, books, days, policy, bps, *, discover_from=None):
        models = [QuotePortfolio(replace(policy, scenario_bps=bps)) for _ in books]
        tables = [legacy.truth(legacy.expression(book)) for book in books]
        ids = [legacy.digest(legacy.pack(book)) for book in books]
        symbols = self._get('config')['symbols']
        episodes, curve, audit, discovery = [], [], [], []
        active = None
        own_entry = None
        unknown = False
        for index, day in enumerate(days):
            for frame, batch, bits, missing in self._day_inputs(day, symbols):
                if frame is None:
                    unknown |= missing or any(m.position is not None for m in models)
                    continue
                actions = []
                balances = [m.account.cash for m in models]
                for i, model in enumerate(models):
                    signals = tuple(Decision(s, ids[i], start, end, published, decided,
                                    int((tables[i] >> b.bits) & 1), D(str(b.stop_gap)) / 2)
                                    for s, start, end, published, decided, b in batch)
                    fills = model.step(replace(frame, decisions=signals))
                    actions.append([(f.side, f.symbol, f.reason) for f in fills])
                    for f in fills:
                        # The legacy comparison requests one/two extra bps per side. This is a
                        # hypothetical fee on actual modeled turnover, never a confirmed charge.
                        extra = f.qty * f.price * bps / D(10000)
                        audit.append(dict(at=f.at.isoformat(), portfolio=i, symbol=f.symbol,
                                          side=f.side, qty=str(f.qty), price=str(f.price),
                                          reason=f.reason, estimated_fee=str(f.fee - extra), scenario_fee=str(extra)))
                        if i == 0:
                            if f.side == 'buy':
                                own_entry = (f.at, balances[0], bits[(f.symbol, f.decision_end)])
                            elif own_entry is not None:
                                at, cash, features = own_entry
                                if day.label == discover_from:
                                    discovery.append((at.timestamp(), f.at.timestamp(), features,
                                                      float(model.account.cash - cash)))
                                own_entry = None
                if active is None and actions[0] != actions[1]:
                    active = (index, frame.at.timestamp(), *balances)
                if active is not None and all(m.position is None for m in models):
                    j, t, x, y = active
                    episodes.append(dict(session=j, start=t, end=frame.at.timestamp(),
                                         inc=float(models[0].account.cash-x), cand=float(models[1].account.cash-y)))
                    active = None
            equity = [m.account.equity for m in models]
            curve.append([day.cuts[-1], *(float(v) if v is not None else None for v in equity)])
        result = dict(episodes=episodes, curve=curve, audit=audit, unknown_gate=unknown or active is not None,
                      slip_bps=bps)
        for label, model in zip(('inc', 'cand'), models):
            value = model.account.equity
            result[label+'_pnl'] = float(value - policy.capital) if value is not None else None
            result[label+'_dd'] = float(model.account.max_drawdown)
            result[label+'_trades'] = sum(f.side == 'sell' for f in model.fills)
        return result, discovery

    def decide(self, ident, gates):
        policy = self._quote_policy()
        if policy is None:
            return super().decide(ident, gates)
        # The verdict and staging rules remain the existing campaign protocol.
        with self.tx():
            trial = self.db.execute('SELECT state,frozen,result FROM ll_trials WHERE id=?', (ident,)).fetchone()
            legacy.require(trial is not None, 'unknown trial')
            if trial['result']:
                return json.loads(trial['result'])
            f = json.loads(trial['frozen'])
            legacy.require(trial['state'] == 'testing' and self.clock() >= f['close'], 'window not complete')
            days = [d for d in self._days() if f['open'] <= d.opened <= f['close']]
            legacy.require(len(days) == 5, 'invalid frozen calendar')
            details = {}
            if gates.get('safety_attested') is False:
                self._set('halted', True)
                self._event('halt', {'reason':'verified hard-boundary violation', 'trial':ident})
                status, why = 'rejected', 'hard-boundary violation'
            elif not self._matches() or f['run_config'] != legacy.digest(self._get('config')):
                status, why = 'inconclusive', 'frozen quote policy changed during experiment'
            else:
                books = [legacy.unpack(f[key]) for key in ('old', 'new')]
                base, _ = self._compare(books, days, policy, 1)
                stress, _ = self._compare(books, days, policy, 2)
                status, why = legacy.judge(base, stress, gates)
                details = {'one_bp':base, 'two_bp':stress}
            result = dict(trial=ident, status=status, why=why, host_gates=dict(gates), **details)
            self.db.execute('UPDATE ll_trials SET state=?,result=? WHERE id=?',
                            (status, legacy.encode(result), ident))
            child = legacy.unpack(f['new'])[f['key']]
            harmful = status == 'rejected' and why in ('worse portfolio drawdown','no net advantage in both time blocks')
            self._set('rule_state:'+child.version, 'supported' if status == 'staged' else
                      'harmful' if harmful else 'unvalidated')
            self._event('decision', result)
            return result

    def activate(self, ident):
        if self._quote_policy() is not None and not self._matches():
            with self.tx():
                row = self.db.execute("UPDATE ll_trials SET state='expired' WHERE id=? AND state='staged'", (ident,))
                legacy.require(row.rowcount == 1, 'not staged')
                self._event('activation_skipped', {'trial':ident, 'at':int(self.clock())})
            return
        return super().activate(ident)

    def holdout(self):
        policy = self._quote_policy()
        if policy is None:
            return super().holdout()
        days = self._days()[-5:]
        legacy.require(self.clock() >= days[-1].cuts[-1], 'holdout still open')
        if self._get('halted') or not self._matches():
            return {'status':'inconclusive', 'halted':self._get('halted'), 'broker_pnl':None}
        books = [legacy.unpack(self._get(k)) for k in ('original', 'active')]
        a, _ = self._compare(books, days, policy, 1)
        b, _ = self._compare(books, days, policy, 2)
        return dict(status='inconclusive' if a['unknown_gate'] or b['unknown_gate'] else 'shadow_only',
                    one_bp=a, two_bp=b, broker_pnl=None)

    def discover_session(self, label, target='ENTRY'):
        policy = self._quote_policy()
        if policy is None:
            return super().discover_session(label, target)
        days = self._days()
        day = next((d for d in days if d.label == label), None)
        legacy.require(day is not None and self.clock() >= day.cuts[-1], 'discovery session still open')
        if self._get('halted') or label >= days[-5].label:
            return {'status':'frozen', 'observations':0}
        saved = self._get('session_book:'+label)
        if not saved or not self._matches():
            return {'status':'inconclusive', 'observations':0}
        book = legacy.unpack(saved)
        legacy.require(target in book and target not in legacy.LOCKED, 'unmapped discovery rule')
        version = book[target].version
        # Discovery has always used standalone sessions. Registered trial and holdout
        # comparisons carry capital, positions and risk latches across their full windows.
        result, observations = self._compare([book,book], [day], policy, 1, discover_from=label)
        if result['unknown_gate']:
            return {'status':'inconclusive', 'why':'incomplete quote, feature, gate or closing evidence',
                    'observations':0}
        for start, end, bits, net in observations:
            identity = legacy.digest(['quote-session-v3', label, target, version, start, end])
            self.observe(identity, start, end, target, version, bits, net, 'shadow')
        self.note('shadow-discovery:'+label+':'+target, 'lesson',
                  dict(session=label, rule=target, version=version, episodes=len(observations),
                       origin='shadow', assumption='standalone quote replay from frozen initial capital'))
        return {'status':'shadow_only', 'observations':len(observations)}
