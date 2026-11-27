"""Dedicated paper account adapter. No live endpoint, no short selling, no symbol liquidation."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal as D, ROUND_DOWN
from urllib.parse import urlencode
from urllib.request import Request, build_opener
import json, os
from llm_mutator import NoRedirect
from paper_store import db, now, stamp, amt, key, get, put, rebuild
from phoenix_integrity import learning_halted, entry_allowed
PAPER = 'https://paper-api.alpaca.markets'
TERMINAL = {'filled', 'canceled', 'expired', 'rejected'}

def sym(s):
    return str(s).replace('/', '').upper()

def status(x):
    return str(getattr(x, 'value', x)).lower()

class AlpacaPaper:

    def __init__(self, s):
        if s.alpaca.base_url.rstrip('/') != PAPER:
            raise ValueError('paper release: real or unknown endpoint locked')
        from alpaca.trading.client import TradingClient
        from alpaca.data.historical import StockHistoricalDataClient
        self.s = s
        self.client = TradingClient(s.alpaca.api_key, s.alpaca.api_secret, paper=True, url_override=PAPER)
        self.quotes = StockHistoricalDataClient(s.alpaca.api_key, s.alpaca.api_secret)

    def snapshot(self):
        a = self.client.get_account()
        positions = self.client.get_all_positions()
        orders = self.get_active_orders()
        return (str(a.id), amt(a.equity, True), amt(a.buying_power), {sym(p.symbol): D(str(p.qty)) for p in positions}, [str(o.client_order_id) for o in orders], bool(a.trading_blocked or a.account_blocked))

    def get_active_orders(self):
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        orders = self.client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
        if len(orders) >= 500:
            raise ValueError('open order snapshot truncated')
        return orders

    def fills(self, since):
        options = {'after': since, 'direction': 'asc', 'page_size': 100}
        seen = set()
        rows = []
        for _ in range(1000):
            req = Request(PAPER + '/v2/account/activities/FILL?' + urlencode(options), headers={'APCA-API-KEY-ID': self.s.alpaca.api_key, 'APCA-API-SECRET-KEY': self.s.alpaca.api_secret})
            op = build_opener(NoRedirect())
            with op.open(req, timeout=10) as response:
                page = json.load(response)
            if not isinstance(page, list):
                raise ValueError('bad activity response')
            rows.extend(page)
            if len(page) < 100:
                return rows
            token = page[-1]['id']
            if token in seen:
                raise ValueError('repeated activity page')
            seen.add(token)
            options['page_token'] = token
        raise ValueError('activity limit reached')

    def lookup(self, cid):
        return self.client.get_order_by_client_id(cid)

    def quote(self, symbol):
        # Crypto fee-in-kind accounting is not released.
        if '/' in symbol:
            return None
        from alpaca.data.requests import StockLatestQuoteRequest
        from alpaca.data.enums import DataFeed
        if not self.client.get_clock().is_open:
            return None
        q = self.quotes.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX))[symbol]
        return (amt(q.bid_price, True), amt(q.ask_price, True), stamp(q.timestamp.isoformat()))

    def submit(self, cid, symbol, side, qty):
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        return self.client.submit_order(order_data=MarketOrderRequest(symbol=symbol, client_order_id=cid, qty=float(qty), side=OrderSide(side), time_in_force=TimeInForce.DAY))

class StrategyExecutor:

    def __init__(self, path, broker, settings):
        self.path = path
        self.b = broker
        self.s = settings
        self._entry_quote_cache = {}  # symbol -> (bid, ask, ts, fetch_time)

    def _entry_quote(self, symbol):
        """Cache quotes for 30 seconds for entry decisions only (latency optimization)."""
        now_ts = datetime.now(timezone.utc)
        cached = self._entry_quote_cache.get(symbol)
        if cached:
            bid, ask, qts, fetch_ts = cached
            if (now_ts - fetch_ts).total_seconds() <= 30:
                return bid, ask, qts
        quote = self.b.quote(symbol)
        if quote:
            bid, ask, qts = quote
            self._entry_quote_cache[symbol] = (bid, ask, qts, now_ts)
        return quote

    def reconcile_trades(self, open_trades=None, latest_prices=None):
        try:
            account, equity, bp, positions, orders, blocked = self.b.snapshot()
            with db(self.path) as c:
                account_was = get(c, 'account')
                since = get(c, 'since')
                full_scan = get(c, 'full_scan_day') != now()[:10]
                previous = get(c, 'sync_at')
                if previous and not full_scan:
                    since = max(since,(datetime.fromisoformat(previous)-timedelta(days=2)).isoformat())
                pending = [dict(r) for r in c.execute("select * from intents where state not in ('filled','canceled','expired','rejected')")]
            if account_was and account_was != account:
                raise ValueError('account changed')
            observed = {}
            for i in pending:
                o = self.b.lookup(i['id'])
                if str(o.client_order_id) != i['id'] or sym(o.symbol) != sym(i['symbol']) or status(o.side) != i['side']:
                    raise ValueError('order identity mismatch')
                observed[i['id']] = (str(o.id), status(o.status), amt(o.filled_qty or 0))
            activities = self.b.fills(since)
            with db(self.path, True) as c:
                if get(c, 'account', account) != account:
                    raise ValueError('account mismatch')
                put(c, 'account', account)
                for cid, (oid, st, q) in observed.items():
                    c.execute('update intents set broker=? where id=? and (broker is null or broker=?)', (oid, cid, oid))
                changed=False
                for f in activities:
                    i = c.execute('select * from intents where broker=?', (str(f['order_id']),)).fetchone()
                    if not i or not f.get('id') or sym(f['symbol']) != sym(i['symbol']) or (f['side'] != i['side']):
                        raise ValueError('unmatched activity')
                    row = (str(f['id']), i['id'], str(amt(f['qty'], True).normalize()), str(amt(f['price'], True).normalize()), stamp(f['transaction_time']))
                    old = c.execute('select * from fills where id=?', (row[0],)).fetchone()
                    if old and tuple(old) != row:
                        raise ValueError('changed execution')
                    if not old:
                        changed=True
                        c.execute('insert into fills values(?,?,?,?,?)', row)
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
                    local[sym(r['symbol'])] = local.get(sym(r['symbol']), D(0)) + D(r['qty'])
                known = {r[0] for r in c.execute('select id from intents')}
                if positions != local or set(orders) - known:
                    raise ValueError('external holdings or orders')
                put(c, 'snapshot', 'ok')
                put(c, 'sync_at', now())
                if full_scan:put(c,'full_scan_day',now()[:10])
                put(c, 'equity', equity)
                put(c, 'buying_power', bp)
                put(c, 'account_blocked', int(blocked))
                put(c, 'peak', max(equity, D(get(c, 'peak', str(equity)))))
            return (rows, True)
        except Exception:
            with db(self.path, True) as c:
                put(c, 'snapshot', 'unknown')
                rows = [dict(r, reconciliation='unknown') for r in c.execute('select * from lots') if D(r['qty'])]
            return (rows, False)

    def _order(self, symbol, side, qty, lot, strategy, gap, decision):
        cid = key('order', side, symbol, decision, lot)
        qty = amt(qty, True)
        # A sidecar halt or unreadable initialized sidecar blocks buys, not exits.
        if side == 'buy' and learning_halted(self.path):
            return None
        with db(self.path, True) as c:
            if get(c, 'snapshot') != 'ok' or not 0 <= age(get(c, 'sync_at')) <= 30 or get(c, 'account_blocked') != '0':
                return None
            if c.execute("select 1 from intents where state not in ('filled','canceled','expired','rejected')").fetchone():
                return None
            if c.execute('select 1 from intents where id=?', (cid,)).fetchone():
                return None
            active = [r for r in c.execute('select * from lots') if D(r['qty'])]
            # max one exposed symbol (conservative correlation limit)
            if side == 'buy' and (active or not entry_allowed(c, symbol, decision, strategy)):
                return None
            if side == 'sell' and (not any((r['id'] == lot and D(r['qty']) >= qty for r in active))):
                return None
            c.execute('insert into intents values(?,?,?,?,?,?,?,?,?,?)', (cid, lot, symbol, strategy, side, str(qty), str(gap), 'reserved', None, now()))
        try:
            return self.b.submit(cid, symbol, side, qty)
        except Exception:
            # outcome ambiguous; reconcile by original client ID only
            return None

    def _open_position(self, symbol, qty, strategy, gap, bar):
        return self._order(symbol, 'buy', qty, key('lot', symbol, bar), strategy, gap, bar)

    def _close_position(self, lot, reason):
        return self._order(lot['symbol'], 'sell', D(lot['qty']), lot['id'], lot['strategy'], lot['gap'], reason) is not None

    def get_active_orders(self):
        return self.b.get_active_orders()

    def sync_positions(self):
        lots, ok = self.reconcile_trades()
        if not ok:
            return 'unknown: no order'
        now_iso = now()[:16]
        with db(self.path) as c:
            decisions = {r['symbol']: dict(r) for r in c.execute('select d.* from decisions d join watermarks w on w.symbol=d.symbol and w.t=d.t')}
            champ = c.execute("select id from strategies where status='champion'").fetchone()
            equity = D(get(c, 'equity'))
            peak = D(get(c, 'peak'))
            bp = D(get(c, 'buying_power'))
            daily = D(str(c.execute('select coalesce(sum(gross),0) from outcomes where substr(at,1,10)=?', (now_iso[:10],)).fetchone()[0]))
            recent = [r[0] for r in c.execute('select sum(gross) from outcomes join lots l on l.id=outcomes.lot where cast(l.qty as real)=0 group by lot order by max(at) desc limit 100')]
            gate = get(c, 'entry_gate')
            regimes = {symbol: d['regime'] for symbol, d in decisions.items()}
            history = [D(str(r[0])) for r in c.execute('select gross-0.001*(entry+exit)*cast(qty as real) from outcomes order by at,id')]
            day_net = D(str(c.execute('select coalesce(sum(gross-0.001*(entry+exit)*cast(qty as real)),0) from outcomes where substr(at,1,10)=?', (now_iso[:10],)).fetchone()[0]))
        for lot in lots:
            quote = self.b.quote(lot['symbol'])  # exits always use fresh quotes
            if not fresh(quote):
                continue
            bid, ask, _ = quote
            basis = amt(lot['basis'], True) / amt(lot['qty'], True)
            gap = amt(lot['gap'], True)
            d = decisions.get(lot['symbol'])
            flat = d and d['strategy'] and (age(d['t']) < 4200) and (d['target'] == 0)
            if bid <= basis - gap or bid >= basis + 2 * gap or age(lot['opened']) >= 604800 or flat:
                return 'exit submitted' if self._close_position(lot, now_iso) else 'exit pending or blocked'
        peak_alloc = allocated = amt(self.s.risk.max_capital_usd, True)
        for value in history:
            allocated += value
            peak_alloc = max(peak_alloc, allocated)
        if lots or not champ or learning_halted(self.path) or (allocated <= peak_alloc * D('.90')) or (os.getenv('PAPER_ENTRIES_ENABLED') != 'YES') or (equity <= peak * D('.90')) or (min(daily, day_net) <= -amt(self.s.risk.max_daily_loss_usd, True)):
            return 'entries paused or exposure held'
        for d in decisions.values():
            if d['target'] != 1 or (not 3600 <= age(d['t']) <= 4200) or ('/' in d['symbol']):
                continue
            regime = regimes.get(d['symbol'])
            regime_gate = None
            if regime:
                with db(self.path) as c2:
                    regime_gate = get(c2, f'entry_gate_{regime}')
            if regime_gate is None:
                regime_gate = gate if not regime else None
            if regime_gate != d['strategy']:
                continue
            quote = self._entry_quote(d['symbol'])
            if not fresh(quote) or d['atr'] is None or not D(str(d['atr'])).is_finite() or D(str(d['atr'])) <= 0:
                continue
            bid, ask, _ = quote
            gap = amt(d['atr'], True) * 2
            cap = min(equity, amt(self.s.risk.max_capital_usd, True))
            fraction = D('.01')
            if len(recent) >= 20:
                wins = [x for x in recent if x > 0]
                loss = [-x for x in recent if x < 0]
                p = D(len(wins)) / len(recent)
                if not wins:
                    continue
                if loss:
                    odds = D(str(sum(wins) / len(wins) / (sum(loss) / len(loss))))
                    fraction = min(fraction, max(D(0), min(D('.25'), p - (1 - p) / odds)))
            size = min(cap * fraction / gap * ask, cap, bp, amt(self.s.risk.max_order_size_usd, True), amt(self.s.risk.max_position_usd, True))
            qty = (size / ask).quantize(D('.000000001'), rounding=ROUND_DOWN)
            if qty > 0 and qty * ask >= amt(self.s.risk.min_order_size_usd, True) and (gap < bid):
                return 'entry submitted' if self._open_position(d['symbol'], qty, d['strategy'], gap, d['t']) else 'entry pending or blocked'
        return 'no new order'

def age(ts):
    return (datetime.now(timezone.utc) - datetime.fromisoformat(stamp(ts))).total_seconds()

def fresh(q):
    return q and 0 <= age(q[2]) <= 60 and (0 < q[0] <= q[1]) and (q[1] / q[0] < D('1.01'))
