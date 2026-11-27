"""Paper supervision and simulated order lifecycle."""
import os
from datetime import datetime, timedelta
from decimal import Decimal as D, InvalidOperation, ROUND_DOWN

from config import PAPER_ORIGIN as PAPER, PAPER_EQUITIES
from paper_gateway import AlpacaPaper
from paper_order_policy import EntryEvidence, WriterBusy, age, fresh, instant, writer_lock
from portfolio_policy import STOP_ATR, portfolio_entry_budget, protection_prices
from paper_store import db, now, stamp, amt, key, get, put, rebuild
from phoenix_integrity import learning_halted, entry_allowed
from strategy_worker import FEATURES

TERMINAL_SQL = "('filled','canceled','expired','rejected','rejected_locally')"


def sym(s):
    return str(s).replace('/', '').upper()


def status(x):
    return str(getattr(x, 'value', x)).lower()


class StrategyExecutor:

    def __init__(self, path, broker, settings):
        self.path = path
        self.b = broker
        self.s = settings
        self.risk = settings.risk

    def reconcile_trades(self, open_trades=None, latest_prices=None):
        try:
            with writer_lock(self.path):
                return self._reconcile_locked()
        except WriterBusy:
            return [], False

    def _reconcile_locked(self, reserved_id=""):
        try:
            account, equity, bp, positions, orders, blocked = self.b.snapshot()
            with db(self.path) as c:
                account_was = get(c, 'account')
                since = get(c, 'since')
                full_scan = get(c, 'full_scan_day') != now()[:10]
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
                    if age(row[-1]) < -2:
                        raise ValueError('future execution timestamp')
                    old = c.execute('select id,intent,qty,price,at from fills where id=?', (row[0],)).fetchone()
                    if old and tuple(old) != row:
                        raise ValueError('changed execution')
                    if not old:
                        changed=True
                        c.execute('insert into fills(id,intent,qty,price,at) values(?,?,?,?,?)', row)
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
                    if age(r['opened']) < -2:
                        raise ValueError('future position timestamp')
                    local[sym(r['symbol'])] = local.get(sym(r['symbol']), D(0)) + D(r['qty'])
                known = {r[0] for r in c.execute(f"select id from intents where state not in {TERMINAL_SQL} and broker is not null")}
                if positions != local or set(orders) - known:
                    raise ValueError('external holdings or orders')
                put(c, 'snapshot', 'ok')
                put(c, 'snapshot_reason', 'reconciled')
                put(c, 'snapshot_attempt_at', now())
                put(c, 'sync_at', now())
                if full_scan:put(c,'full_scan_day',now()[:10])
                put(c, 'equity', equity)
                put(c, 'buying_power', bp)
                put(c, 'account_blocked', int(blocked))
                put(c, 'peak', max(equity, D(get(c, 'peak', str(equity)))))
            return (rows, True)
        except Exception as exc:
            with db(self.path, True) as c:
                put(c, 'snapshot', 'unknown')
                # Categories only. Remote exception messages can contain account data.
                put(c, 'snapshot_reason', type(exc).__name__)
                put(c, 'snapshot_attempt_at', now())
                rows = [dict(r, reconciliation='unknown') for r in c.execute('select * from lots') if D(r['qty'])]
            return (rows, False)

    def _entry_evidence(self, symbol, decision):
        try:
            quote = self.b.quote(symbol)
            evidence = self.b.entry_evidence(symbol, decision)
            if not fresh(quote) or not isinstance(evidence, EntryEvidence):
                return None
            return quote, evidence
        except Exception:
            return None

    def _allocated_capital(self, c):
        allocation_limit = min(D(10), amt(self.risk.max_capital_usd, True))
        allocated = peak = allocation_limit
        daily_gross = daily_net = D(0)
        today = now()[:10]
        for outcome in c.execute("select gross,qty,entry,exit,at from outcomes order by at,id"):
            gross = D(str(outcome['gross']))
            # Existing cost scenario, until a versioned fee ledger is available.
            turnover = (D(str(outcome['entry'])) + D(str(outcome['exit']))) * D(outcome['qty'])
            net = gross - D('.001') * turnover
            allocated += net
            peak = max(peak, allocated)
            if allocated <= peak * D('.90'):
                put(c, 'allocated_drawdown_halt', 'YES')
            if outcome['at'][:10] == today:
                daily_gross += gross
                daily_net += net
                if min(daily_gross, daily_net) <= -min(D('.50'), amt(self.risk.max_daily_loss_usd, True)):
                    put(c, 'daily_halt_utc', today)
        if (get(c, 'allocated_drawdown_halt') == 'YES' or get(c, 'daily_halt_utc') == today
                or D(get(c, 'equity')) <= D(get(c, 'peak')) * D('.90')):
            return D(0)
        return max(D(0), min(allocated, allocation_limit))

    def _authorized(self, c, cid, symbol, side, qty, lot, strategy, gap, decision, evidence):
        if (getattr(self.b, 'orders_enabled', False) is not True or not self.s.alpaca.is_paper
                or get(c, 'snapshot') != 'ok' or not 0 <= age(get(c, 'sync_at')) <= 30
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
        if not fresh(quote) or not capabilities.permits(get(c, 'account'), symbol, decision, quote[2]):
            return False
        fact = c.execute("""select b.interval_start,b.interval_end,b.received_at,d.atr
            from usable_bars b join decision_snapshots s on s.symbol=b.symbol and s.t=b.t
            join decisions d on d.symbol=s.symbol and d.t=s.t and d.strategy=s.strategy
            where b.symbol=? and b.t=? and s.strategy=? and s.lot=? and s.regime=d.regime
            and s.content_hash=b.content_hash and s.received_at=b.received_at
            and s.history_ready=1 and s.readiness_mask=? and s.flags is not null and b.interval_kind='XNYS_1m'""",
            (symbol, decision, strategy, lot, (1 << len(FEATURES)) - 1)).fetchone()
        if (not fact or instant(fact['interval_start']) != instant(capabilities.interval_start)
                or instant(fact['interval_end']) != instant(decision)
                or instant(fact['received_at']) != instant(capabilities.published_at)):
            return False
        atr = fact['atr']
        if atr is None or gap != amt(atr, True) * STOP_ATR or gap >= quote[0]:
            return False
        cash = self._allocated_capital(c)
        notional = qty * quote[1]
        minimum = max(D(1), amt(self.risk.min_order_size_usd, True), capabilities.minimum_notional)
        maximum = portfolio_entry_budget(cash, min(amt(get(c, 'buying_power')), amt(get(c, 'equity'))),
                      amt(self.risk.max_order_size_usd, True), amt(self.risk.max_position_usd, True))
        return minimum <= notional <= maximum and gap * qty <= cash * D('.01')

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
                              (cid, lot, symbol, strategy, side, str(qty), str(gap), now()))
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
            put(c, 'cancel_requested_' + entry['id'], now())
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

    def sync_positions(self):
        lots, ok = self.reconcile_trades()
        if not ok:
            return 'unknown: no order'
        now_iso = now()[:16]
        with db(self.path) as c:
            decisions = {r['symbol']: dict(r) for r in c.execute('select d.* from decisions d join watermarks w on w.symbol=d.symbol and w.t=d.t')}
        for lot in lots:
            quote = self.b.quote(lot['symbol'])  # exits always use fresh quotes
            if not fresh(quote, entry=False):
                continue
            bid, ask, _ = quote
            basis = amt(lot['basis'], True) / amt(lot['qty'], True)
            gap = amt(lot['gap'], True)
            d = decisions.get(lot['symbol'])
            flat = (d and d['strategy'] == lot['strategy'] and d['target'] == 0
                    and instant(d['t']) >= instant(lot['opened']) and 0 <= age(d['t']) <= 300)
            close_clock = getattr(self.b, 'session_close', None)
            close = close_clock(lot['symbol']) if close_clock else None
            session_exit = close and age(close) >= -300
            stop, target = protection_prices(basis, gap)
            if bid <= stop or bid >= target or session_exit or flat:
                return 'exit submitted' if self._close_position(lot, now_iso) else 'exit pending or blocked'
        if lots or getattr(self.b, 'orders_enabled', False) is not True or os.getenv('PAPER_ENTRIES_ENABLED') != 'YES':
            return 'entries paused or exposure held'
        for d in decisions.values():
            if d['target'] != 1 or d['symbol'] not in PAPER_EQUITIES or not 0 <= age(d['t']) <= 300:
                continue
            evidence = self._entry_evidence(d['symbol'], d['t'])
            if not evidence or d['atr'] is None or not D(str(d['atr'])).is_finite() or D(str(d['atr'])) <= 0:
                continue
            quote, _ = evidence
            bid, ask, _ = quote
            gap = amt(d['atr'], True) * 2
            with db(self.path, True) as c:
                cash = self._allocated_capital(c)
                size = min(cash * D('.01') / gap * ask, cash, amt(get(c, 'buying_power')),
                           amt(get(c, 'equity')), D(1), amt(self.risk.max_order_size_usd, True),
                           amt(self.risk.max_position_usd, True))
            qty = (size / ask).quantize(D('.000000001'), rounding=ROUND_DOWN)
            if qty > 0 and qty * ask >= amt(self.s.risk.min_order_size_usd, True) and (gap < bid):
                return 'entry submitted' if self._open_position(d['symbol'], qty, d['strategy'], gap, d['t']) else 'entry pending or blocked'
        return 'no new order'
