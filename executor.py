"""Paper supervision and simulated order lifecycle."""
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D, InvalidOperation

from config import PAPER_ORIGIN as PAPER, PAPER_EQUITIES
from paper_gateway import AlpacaPaper
from paper_order_policy import EntryEvidence, WriterBusy, age, fresh, instant, writer_lock
from operational_account import allocated_funds, reconcile_account, record_fill_fee, record_reservation
from portfolio_events import ExecutionPolicy, Quote, entry_quantity, exit_reason
from portfolio_policy import STOP_ATR
from paper_store import db, stamp, amt, key, get, put, rebuild
from phoenix_integrity import learning_halted, entry_allowed
from strategy_worker import FEATURES

TERMINAL_SQL = "('filled','canceled','expired','rejected','rejected_locally')"


def sym(s):
    return str(s).replace('/', '').upper()


def status(x):
    return str(getattr(x, 'value', x)).lower()


class StrategyExecutor:

    def __init__(self, path, broker, settings, *, clock=None):
        self.path = path
        self.b = broker
        self.s = settings
        self.risk = settings.risk
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.policy = ExecutionPolicy(capital=amt(self.risk.max_capital_usd, True),
                                      order_cap=amt(self.risk.max_order_size_usd, True),
                                      position_cap=amt(self.risk.max_position_usd, True),
                                      minimum=max(D(1), amt(self.risk.min_order_size_usd, True)),
                                      daily_loss=amt(self.risk.max_daily_loss_usd, True),
                                      fee_per_order=amt(self.risk.commission_per_order_usd))

    def _now(self):
        return stamp(self.clock())

    def _recent_quote(self, symbol, *, entry):
        try:
            quote = self.b.quote(symbol)
        except Exception:
            return None
        try:
            at = self.clock()
            if not fresh(quote, at=at, entry=False):
                return None
            bid, ask, ts = quote
            ts = stamp(ts)
            # Market data can arrive late or be re-delivered after a restart.
            # Track valid source times even when the spread prevents a new entry.
            with db(self.path, True) as c:
                name = 'last_quote_' + symbol
                stored = get(c, name)
                if stored:
                    last, last_bid, last_ask = json.loads(stored)
                    if ts < last or (ts == last and (bid != D(last_bid) or ask != D(last_ask))):
                        return None
                if not stored or ts > last:
                    put(c, name, json.dumps([ts, str(bid), str(ask)]))
            return quote if not entry or fresh(quote, at=at, entry=True) else None
        except (ValueError, TypeError, AttributeError, InvalidOperation, OSError, KeyError):
            return None

    def reconcile_trades(self, open_trades=None, latest_prices=None):
        try:
            with writer_lock(self.path):
                return self._reconcile_locked()
        except WriterBusy:
            return [], False

    def _reconcile_locked(self, reserved_id="", market_quotes=None):
        try:
            account, equity, bp, positions, orders, blocked = self.b.snapshot()
            with db(self.path) as c:
                account_was = get(c, 'account')
                since = get(c, 'since')
                full_scan = get(c, 'full_scan_day') != self._now()[:10]
                previous = get(c, 'sync_at')
                if previous and not full_scan:
                    since = max(since,(datetime.fromisoformat(previous)-timedelta(days=2)).isoformat())
                pending = [dict(r) for r in c.execute(
                    f"select * from intents where state not in {TERMINAL_SQL} and state!='reserved_local'")]
            if not isinstance(account, str) or not account or not isinstance(blocked, bool):
                raise ValueError("invalid account snapshot")
            if account_was and account_was != account:
                raise ValueError('account changed')
            equity, bp = amt(equity, True), amt(bp)
            observed = {}
            for i in pending:
                o = self.b.lookup(i['id'])
                if (not o.id or str(o.client_order_id) != i['id'] or sym(o.symbol) != sym(i['symbol'])
                        or status(o.side) != i['side'] or amt(o.qty, True) != amt(i['qty'], True)):
                    raise ValueError('order identity mismatch')
                if status(o.status) not in ('accepted', 'new', 'pending_new', 'partially_filled',
                                             'pending_cancel', 'canceled', 'expired', 'rejected', 'filled'):
                    raise ValueError('unresolved order state')
                observed[i['id']] = (str(o.id), status(o.status), amt(o.filled_qty or 0))
            activities = self.b.fills(since)
            # Snapshot holdings are still untrusted. Only fetch marks for known Phoenix symbols.
            quotes = {symbol: self._recent_quote(symbol, entry=False)
                      for symbol in positions if symbol in PAPER_EQUITIES}
            sessions = {symbol: self._broker_session(symbol) for symbol in quotes}
            with db(self.path, True) as c:
                if get(c, 'account', account) != account:
                    raise ValueError('account mismatch')
                put(c, 'account', account)
                # Only the new protocol proves these abandoned reservations never crossed dispatch.
                c.execute("update intents set state='rejected_locally' where state='reserved_local' and id!=?", (reserved_id,))
                for cid, (oid, st, q) in observed.items():
                    c.execute('update intents set broker=? where id=? and (broker is null or broker=?)', (oid, cid, oid))
                changed=False
                for f in activities:
                    i = c.execute('select * from intents where broker=?', (str(f['order_id']),)).fetchone()
                    if not i or not f.get('id') or sym(f['symbol']) != sym(i['symbol']) or (f['side'] != i['side']):
                        raise ValueError('unmatched activity')
                    row = (str(f['id']), i['id'], str(amt(f['qty'], True).normalize()), str(amt(f['price'], True).normalize()), stamp(f['transaction_time']))
                    if age(row[-1], self.clock()) < -2:
                        raise ValueError('future execution timestamp')
                    old = c.execute('select id,intent,qty,price,at from fills where id=?', (row[0],)).fetchone()
                    if old and tuple(old) != row:
                        raise ValueError('changed execution')
                    if not old:
                        changed=True
                        c.execute('insert into fills(id,intent,qty,price,at) values(?,?,?,?,?)', row)
                    record_fill_fee(c, row[0], f.get('fee'))
                for cid, (oid, st, q) in observed.items():
                    i = c.execute('select qty,broker from intents where id=?', (cid,)).fetchone()
                    filled = sum((D(r[0]) for r in c.execute('select qty from fills where intent=?', (cid,))), D(0))
                    if i['broker'] != oid or q != filled or filled > D(i['qty']) or (st == 'filled' and filled != D(i['qty'])):
                        raise ValueError('order/activity discrepancy')
                    c.execute('update intents set state=? where id=?', (st, cid))
                for order in c.execute('select id,qty from intents'):
                    if sum((D(x[0]) for x in c.execute('select qty from fills where intent=?', (order[0],))), D(0)) > D(order[1]):
                        raise ValueError('overfilled order')
                if changed:rebuild(c)
                rows = [dict(r) for r in c.execute('select * from lots') if D(r['qty'])]
                local = {}
                for r in rows:
                    if age(r['opened'], self.clock()) < -2:
                        raise ValueError('future position timestamp')
                    local[sym(r['symbol'])] = local.get(sym(r['symbol']), D(0)) + D(r['qty'])
                known = {r[0] for r in c.execute(f"select id from intents where state not in {TERMINAL_SQL} and broker is not null")}
                if positions != local or set(orders) - known:
                    raise ValueError('external holdings or orders')
                at = self.clock()
                bids = {}
                for lot in rows:
                    symbol = lot['symbol']
                    quote = quotes.get(symbol)
                    opened, close = self._session_window(c, lot, sessions.get(symbol))
                    if self._eligible_mark(lot, quote, at, opened, close):
                        bids[symbol] = quote[0]
                reconcile_account(c, self.risk, at, bids)
                put(c, 'snapshot', 'ok')
                put(c, 'snapshot_reason', 'reconciled')
                put(c, 'snapshot_attempt_at', self._now())
                put(c, 'sync_at', self._now())
                if full_scan:put(c,'full_scan_day',self._now()[:10])
                put(c, 'equity', equity)
                put(c, 'buying_power', bp)
                put(c, 'account_blocked', int(blocked))
                put(c, 'peak', max(equity, D(get(c, 'peak', str(equity)))))
            if market_quotes is not None:
                market_quotes.update(quotes)
            return (rows, True)
        except Exception as exc:
            with db(self.path, True) as c:
                put(c, 'snapshot', 'unknown')
                # Categories only. Remote exception messages can contain account data.
                put(c, 'snapshot_reason', type(exc).__name__)
                put(c, 'snapshot_attempt_at', self._now())
                rows = [dict(r, reconciliation='unknown') for r in c.execute('select * from lots') if D(r['qty'])]
            return (rows, False)

    def _entry_evidence(self, symbol, decision):
        quote = self._recent_quote(symbol, entry=True)
        if quote is None:
            return None
        try:
            evidence = self.b.entry_evidence(symbol, decision)
            if isinstance(evidence, EntryEvidence):
                return quote, evidence
        except Exception:
            pass
        return None

    def _sized_quantity(self, c, quote, gap, exclude_intent=None):
        if amt(get(c, 'equity')) <= amt(get(c, 'peak')) * D('.90'):
            return D(0)
        equity, available = allocated_funds(c, self.risk, self.clock(), exclude_intent)
        available = min(available, amt(get(c, 'buying_power')), amt(get(c, 'equity')))
        return entry_quantity(self.policy, equity, available, quote[0], quote[1], gap)

    def _decision_fact(self, c, symbol, decision, strategy):
        return c.execute("""select b.interval_start,b.interval_end,b.received_at,s.decided_at,d.*
            from usable_bars b join decision_snapshots s on s.symbol=b.symbol and s.t=b.t
            join decisions d on d.symbol=s.symbol and d.t=s.t and d.strategy=s.strategy
            where b.symbol=? and b.t=? and s.strategy=? and s.lot=? and s.regime=d.regime
            and s.content_hash=b.content_hash and s.received_at=b.received_at
            and s.history_ready=1 and s.readiness_mask=? and s.flags is not null and b.interval_kind='XNYS_1m'""",
            (symbol, decision, strategy, key('lot', symbol, decision),
             (1 << len(FEATURES)) - 1)).fetchone()

    def _decision_permits(self, fact, evidence, account, quote, *, entry):
        try:
            return (fact is not None and isinstance(evidence, EntryEvidence)
                    and instant(fact['interval_start']) == instant(evidence.interval_start)
                    and instant(fact['interval_end']) == instant(fact['t'])
                    and instant(fact['received_at']) == instant(evidence.published_at)
                    and (fact['decided_at'] is None or instant(fact['decided_at']) == instant(evidence.decided_at))
                    and evidence.permits(account, fact['symbol'], fact, quote, self.clock(), entry=entry))
        except (ValueError, TypeError, AttributeError, OverflowError):
            return False

    def _authorized(self, c, cid, symbol, side, qty, lot, strategy, gap, decision, evidence):
        if (getattr(self.b, 'orders_enabled', False) is not True or not self.s.alpaca.is_paper
                or get(c, 'snapshot') != 'ok' or not 0 <= age(get(c, 'sync_at'), self.clock()) <= 30
                or not get(c, 'account') or get(c, 'account_blocked') != '0'
                or qty != qty.quantize(D('.000000001'))):
            return False
        if c.execute(f"select 1 from intents where state not in {TERMINAL_SQL} and id!=?", (cid,)).fetchone():
            return False
        held = [r for r in c.execute('select * from lots') if amt(r['qty'])]
        if side == 'sell':
            return any(r['id'] == lot and r['symbol'] == symbol and r['strategy'] == strategy
                       and amt(r['gap'], True) == gap and amt(r['qty']) >= qty for r in held)
        if (side != 'buy' or held or os.getenv('PAPER_ENTRIES_ENABLED') != 'YES'
                or symbol not in PAPER_EQUITIES or lot != key('lot', symbol, decision)
                or symbol not in self.s.universe.equities or not evidence
                or not entry_allowed(c, symbol, decision, strategy)):
            return False
        quote, capabilities = evidence
        if not fresh(quote, at=self.clock()):
            return False
        fact = self._decision_fact(c, symbol, decision, strategy)
        if not self._decision_permits(fact, capabilities, get(c, 'account'), quote, entry=True):
            return False
        atr = fact['atr']
        if atr is None or gap != amt(atr, True) * STOP_ATR:
            return False
        return (qty == self._sized_quantity(c, quote, gap, cid)
                and qty * quote[1] >= capabilities.minimum_notional)

    def _order(self, symbol, side, qty, lot, strategy, gap, decision):
        if getattr(self.b, 'orders_enabled', False) is not True or not self.s.alpaca.is_paper:
            return None
        try:
            qty, gap = amt(qty, True), amt(gap, True)
            with writer_lock(self.path):
                return self._order_locked(symbol, side, qty, lot, strategy, gap, decision)
        except (WriterBusy, ValueError, InvalidOperation, OverflowError):
            return None

    def _order_locked(self, symbol, side, qty, lot, strategy, gap, decision):
        cid = key('order', side, symbol, decision, lot)
        for phase in ('reserve', 'dispatch'):
            if not self._reconcile_locked(reserved_id=cid if phase == 'dispatch' else '')[1]:
                if phase == 'dispatch':
                    with db(self.path, True) as c:
                        c.execute("update intents set state='rejected_locally' where id=? and state='reserved_local'", (cid,))
                return None
            # Read the broker and sidecar outside the operational write transaction.
            evidence = self._entry_evidence(symbol, decision) if side == 'buy' else None
            halted = side == 'buy' and learning_halted(self.path)
            with db(self.path, True) as c:
                allowed = not halted and self._authorized(
                    c, cid, symbol, side, qty, lot, strategy, gap, decision, evidence)
                if phase == 'reserve':
                    if not allowed or c.execute('select 1 from intents where id=?', (cid,)).fetchone():
                        return None
                    c.execute("insert into intents(id,lot,symbol,strategy,side,qty,stop_gap,state,created) "
                              "values(?,?,?,?,?,?,?,'reserved_local',?)",
                              (cid, lot, symbol, strategy, side, str(qty), str(gap), self._now()))
                    record_reservation(c, cid, evidence[0][1] if evidence else None,
                                       self.policy.fee_per_order)
                    if side == 'buy':
                        # The original session boundary remains available after a restart.
                        put(c, 'lot_session_open_' + lot, stamp(evidence[1].session_open))
                        put(c, 'lot_session_close_' + lot, stamp(evidence[1].session_close))
                else:
                    state = 'dispatching' if allowed else 'rejected_locally'
                    updated = c.execute("update intents set state=? where id=? and state='reserved_local'", (state, cid))
                    if updated.rowcount != 1 or not allowed:
                        return None
        try:
            order = self.b.submit(cid, symbol, side, qty)
            if (order is None or not order.id or str(order.client_order_id) != cid
                    or sym(order.symbol) != sym(symbol) or status(order.side) != side):
                raise ValueError("acknowledgement identity mismatch")
        except Exception:
            with db(self.path, True) as c:
                c.execute("update intents set state='submission_unknown' where id=? and state='dispatching'", (cid,))
            return None
        with db(self.path, True) as c:
            c.execute("update intents set broker=?,state='acknowledged' where id=? and state='dispatching'",
                      (str(order.id), cid))
        return order

    def _open_position(self, symbol, qty, strategy, gap, bar):
        return self._order(symbol, 'buy', qty, key('lot', symbol, bar), strategy, gap, bar)

    def _close_position(self, lot, reason):
        if getattr(self.b, 'orders_enabled', False) is not True or not self.s.alpaca.is_paper:
            return False
        try:
            with writer_lock(self.path):
                lots, ok = self._reconcile_locked()
                held = next((row for row in lots if row['id'] == lot['id']), None)
                if (not ok or not held or any(held[name] != lot[name] for name in ('symbol','strategy','gap'))
                        or amt(held['qty']) < amt(lot['qty'], True)):
                    return False
                if not self._cancel_remainder(held):
                    return False
                return self._order_locked(held['symbol'], 'sell', amt(lot['qty'], True), held['id'],
                                          held['strategy'], amt(held['gap'], True), reason) is not None
        except (WriterBusy, ValueError, InvalidOperation, OverflowError):
            return False

    def _cancel_remainder(self, lot):
        """A confirmed partial fill belongs to the lot even if cancel races another fill."""
        with db(self.path, True) as c:
            entry = c.execute(f"""select id,broker,state from intents
                        where lot=? and side='buy' and state not in {TERMINAL_SQL}""", (lot['id'],)).fetchone()
            if entry is None:
                return True
            cancel = getattr(self.b, 'cancel', None)
            if (not callable(cancel) or not entry['broker'] or get(c, 'account_blocked') != '0'
                    or entry['state'] != 'partially_filled' or get(c, 'cancel_requested_' + entry['id'])):
                return False
            put(c, 'cancel_requested_' + entry['id'], self._now())
            c.execute("update intents set state='cancel_requested' where id=?", (entry['id'],))
        try:
            cancel(entry['broker'])
        except Exception:
            return False
        # A cancel acknowledgement only confirms receipt. Actual fills and terminal status decide.
        if not self._reconcile_locked()[1]:
            return False
        with db(self.path) as c:
            return c.execute(f"select 1 from intents where id=? and state in {TERMINAL_SQL}",
                             (entry['id'],)).fetchone() is not None

    def get_active_orders(self):
        return self.b.get_active_orders()

    def _exit_signal(self, lot, decision, quote, account):
        if (not decision or decision['strategy'] != lot['strategy'] or decision['target'] != 0
                or instant(decision['t']) < instant(lot['opened'])):
            return False
        try:
            capabilities = self.b.entry_evidence(lot['symbol'], decision['t'])
            with db(self.path) as c:
                fact = self._decision_fact(c, lot['symbol'], decision['t'], lot['strategy'])
                return self._decision_permits(fact, capabilities, account, quote, entry=False)
        except (AttributeError, ValueError, TypeError, OSError, InvalidOperation):
            return False

    def _broker_session(self, symbol):
        try:
            start = getattr(self.b, 'session_open', None)
            close = getattr(self.b, 'session_close', None)
            return (start(symbol) if start else None, close(symbol) if close else None)
        except Exception:
            return None, None

    @staticmethod
    def _session_window(c, lot, current):
        start, close = current or (None, None)
        start = start or get(c, 'lot_session_open_' + lot['id'])
        close = close or get(c, 'lot_session_close_' + lot['id'])
        return instant(start) if start else None, instant(close) if close else None

    @staticmethod
    def _eligible_mark(lot, quote, at, opened, close):
        if not fresh(quote, at=at, entry=False):
            return False
        quoted_at = instant(quote[2])
        return (quoted_at >= instant(lot['opened'])
                and (opened is None or opened <= min(at, quoted_at))
                and (close is None or max(at, quoted_at) <= close))

    def sync_positions(self):
        quotes = {}
        try:
            with writer_lock(self.path):
                lots, ok = self._reconcile_locked(market_quotes=quotes)
        except WriterBusy:
            return 'unknown: no order'
        if not ok:
            return 'unknown: no order'
        with db(self.path) as c:
            decisions = {r['symbol']: dict(r) for r in c.execute("""
                select d.* from decisions d join watermarks w on w.symbol=d.symbol and w.t=d.t
                order by d.symbol""")}
            account = get(c, 'account')
            halted = (get(c, 'allocated_drawdown_halt') == 'YES'
                      or get(c, 'daily_halt_utc') == self._now()[:10])
            capture = get(c, 'shadow_quote_capture') == 'v3'
        capabilities = {}
        try:
            return self._manage(lots, quotes, decisions, account, halted, capabilities)
        finally:
            if capture:
                self._capture_shadow(quotes, decisions, account, capabilities)

    def _capture_shadow(self, quotes, decisions, account, capabilities):
        """Record this poll after owned reductions have been handled."""
        try:
            from learning_runtime import record_shadow_quotes

            tape_quotes = []
            unknown = []
            for symbol in sorted(PAPER_EQUITIES.intersection(self.s.universe.equities)):
                if symbol not in quotes:
                    quotes[symbol] = self._recent_quote(symbol, entry=False)
                raw = quotes.get(symbol)
                if raw is None:
                    unknown.append(symbol)
                    continue
                evidence = capabilities.get(symbol)
                if evidence is None and symbol in decisions:
                    try:
                        evidence = self.b.entry_evidence(symbol, decisions[symbol]['t'])
                    except Exception:
                        evidence = None
                if not isinstance(evidence, EntryEvidence) or evidence.account != account or evidence.symbol != symbol:
                    unknown.append(symbol)
                    allowed = False
                else:
                    allowed = (evidence.regular_session is True and evidence.fractionable is True
                               and evidence.protection_verified is True and evidence.feature_ready is True
                               and abs(evidence.clock_offset_seconds) <= 2
                               and D(0) < evidence.minimum_notional <= self.policy.order_cap)
                tape_quotes.append(Quote(symbol, instant(raw[2]), raw[0], raw[1], entry_ok=allowed))
            record_shadow_quotes(self.path, self.clock(), tape_quotes, unknown=unknown)
        except Exception as exc:
            # A failed research journal cannot change an owned protective transition.
            import logging
            logging.getLogger('paper_daemon').warning('Shadow quote capture unavailable (%s)', type(exc).__name__)

    def _manage(self, lots, quotes, decisions, account, halted, capabilities):
        for lot in lots:
            symbol = lot['symbol']
            quote = quotes.get(symbol)
            at = self.clock()
            if not fresh(quote, at=at, entry=False):
                continue
            current_session = self._broker_session(symbol)
            with db(self.path) as c:
                opened, close = self._session_window(c, lot, current_session)
            if not self._eligible_mark(lot, quote, at, opened, close):
                continue
            flat = self._exit_signal(lot, decisions.get(symbol), quote, account)
            basis = amt(lot['basis'], True) / amt(lot['qty'], True)
            session_exit = ((close is not None and at >= close - timedelta(minutes=5))
                            or (opened is not None and instant(lot['opened']) < opened))
            why = exit_reason(quote[0], basis, amt(lot['gap'], True), session_exit, flat, halted)
            if why:
                return 'exit submitted' if self._close_position(lot, self._now()[:16]) else 'exit pending or blocked'

        if lots or getattr(self.b, 'orders_enabled', False) is not True or os.getenv('PAPER_ENTRIES_ENABLED') != 'YES':
            return 'entries paused or exposure held'
        for d in decisions.values():
            symbol, decision, strategy = d['symbol'], d['t'], d['strategy']
            if (d['target'] != 1 or symbol not in PAPER_EQUITIES or not 0 <= age(decision, self.clock()) <= 300
                    or d['atr'] is None or not D(str(d['atr'])).is_finite() or D(str(d['atr'])) <= 0):
                continue
            quote = self._recent_quote(symbol, entry=False)
            quotes[symbol] = quote
            if not fresh(quote, at=self.clock()):
                continue
            try:
                capabilities[symbol] = self.b.entry_evidence(symbol, decision)
            except Exception:
                continue
            evidence = (quote, capabilities[symbol])
            gap = amt(d['atr'], True) * STOP_ATR
            lot = key('lot', symbol, decision)
            cid = key('order', 'buy', symbol, decision, lot)
            with db(self.path) as c:
                qty = self._sized_quantity(c, evidence[0], gap)
                if (not qty or c.execute('select 1 from intents where id=?', (cid,)).fetchone()
                        or not self._authorized(c, cid, symbol, 'buy', qty, lot, strategy, gap,
                                                decision, evidence)):
                    continue
            return 'entry submitted' if self._open_position(symbol, qty, strategy, gap, decision) else 'entry pending or blocked'
        return 'no new order'
