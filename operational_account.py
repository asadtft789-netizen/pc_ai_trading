"""Allocated paper account projections over immutable fills, fee receipts and observed bids."""
import json
import sqlite3
from datetime import datetime
from decimal import Decimal as D

from paper_store import amt, get, put, stamp
from portfolio_account import Account

ZERO = D(0)
_TERMINAL = "('filled','canceled','expired','rejected','rejected_locally')"


def install_accounting(c):
    """Migrate the disposable outcome projection to text and keep original input facts."""
    c.execute("ALTER TABLE decision_snapshots ADD COLUMN decided_at TEXT")
    c.execute("ALTER TABLE outcomes RENAME TO outcomes_v2")
    c.execute("""CREATE TABLE outcomes(id TEXT PRIMARY KEY,lot TEXT,strategy TEXT,symbol TEXT,
                 qty TEXT,entry TEXT,exit TEXT,gross TEXT,at TEXT)""")
    cols = "id,lot,strategy,symbol,qty,entry,exit,gross,at"
    c.execute(f"INSERT INTO outcomes({cols}) SELECT {cols} FROM outcomes_v2")
    c.execute("DROP TABLE outcomes_v2")
    c.execute("""CREATE TABLE portfolio_order_terms(intent TEXT PRIMARY KEY REFERENCES intents(id),
                 ask TEXT, fee_per_order TEXT NOT NULL)""")
    c.execute("""CREATE TABLE portfolio_fill_receipts(fill TEXT PRIMARY KEY REFERENCES fills(id),
                 batch INTEGER NOT NULL)""")
    c.execute("""CREATE TABLE portfolio_fees(fill TEXT PRIMARY KEY REFERENCES fills(id),
                 fee TEXT NOT NULL, observed_at TEXT, fills_seen INTEGER)""")
    c.execute("""CREATE TABLE portfolio_bids(at TEXT NOT NULL, lot TEXT NOT NULL, bid TEXT,
                 fills_seen INTEGER NOT NULL, fees_seen INTEGER NOT NULL,
                 PRIMARY KEY(at,lot,fills_seen,fees_seen))""")
    c.execute("CREATE INDEX fills_by_intent ON fills(intent)")
    # Replace the old rounded derived rows from exact fill facts where possible.
    if c.execute("SELECT 1 FROM fills LIMIT 1").fetchone():
        if c.execute("SELECT 1 FROM outcomes WHERE id NOT IN (SELECT id FROM fills) LIMIT 1").fetchone():
            raise ValueError("unattributed historical outcome requires migration review")
        from paper_store import rebuild
        factory = c.row_factory
        try:
            c.row_factory = sqlite3.Row
            rebuild(c)
        finally:
            c.row_factory = factory


def record_reservation(c, intent_id, ask, fee_per_order):
    row = c.execute("SELECT side FROM intents WHERE id=?", (intent_id,)).fetchone()
    if row is None or row[0] not in ('buy', 'sell') or (ask is None) != (row[0] == 'sell'):
        raise ValueError("reservation requires a matching intent and entry ask")
    price = None if ask is None else str(amt(ask, True))
    fee = str(amt(fee_per_order))
    c.execute("INSERT INTO portfolio_order_terms(intent,ask,fee_per_order) VALUES(?,?,?)",
              (intent_id, price, fee))


def record_fill_fee(c, fill_id, confirmed_fee=None):
    """The broker may first omit the fee and confirm it in a later reconciliation."""
    if confirmed_fee is None:
        return
    fee = amt(confirmed_fee)
    prior = c.execute("SELECT fee FROM portfolio_fees WHERE fill=?", (fill_id,)).fetchone()
    if prior is not None:
        if fee != D(prior[0]):
            raise ValueError("conflicting broker fee receipt")
        return
    c.execute("INSERT INTO portfolio_fees(fill,fee) VALUES(?,?)", (fill_id, str(fee)))


def _reservations(c, exclude_intent=None):
    records = c.execute(f"""SELECT i.id,i.qty,t.ask,t.fee_per_order FROM intents i
        LEFT JOIN portfolio_order_terms t ON t.intent=i.id
        WHERE i.side='buy' AND i.state NOT IN {_TERMINAL}""")
    reserved = ZERO
    for cid, qty, ask, fee in records:
        if cid == exclude_intent:
            continue
        if ask is None:
            return None  # A pre-versioned unresolved order has no verified cash bound.
        ordered = D(qty)
        filled = sum((D(r[0]) for r in c.execute("SELECT qty FROM fills WHERE intent=?", (cid,))), ZERO)
        if filled > ordered:
            raise ValueError("reservation overfilled")
        remaining = ordered - filled
        reserved += remaining * D(ask) + (D(fee) - D(fee) * filled / ordered)
    return reserved


def _replay(c, capital, limit):
    """Value old fills at their execution times, fees at receipt, and bids at observation."""
    account = Account(capital, limit)
    lot = None
    # SQLite merges the three small ordered streams. A partial's fee estimate is its
    # share of the order charge; differences of cumulative shares telescope exactly.
    events = c.execute("""SELECT f.at,CASE i.side WHEN 'buy' THEN 0 ELSE 1 END AS kind,
             f.id,i.lot,f.qty,f.price,i.id AS intent,i.qty AS ordered,
             t.fee_per_order,p.fill IS NOT NULL AS has_receipt,
             r.batch AS fill_batch,0 AS fee_seq
        FROM fills f JOIN intents i ON i.id=f.intent JOIN portfolio_fill_receipts r ON r.fill=f.id
        LEFT JOIN portfolio_order_terms t ON t.intent=i.id
        LEFT JOIN portfolio_fees p ON p.fill=f.id
        UNION ALL SELECT max(p.observed_at,f.at),2,p.fill,NULL,NULL,p.fee,NULL,NULL,NULL,1,p.fills_seen,p.rowid
            FROM portfolio_fees p JOIN fills f ON f.id=p.fill
        UNION ALL SELECT at,3,lot,lot,NULL,bid,NULL,NULL,NULL,0,fills_seen,fees_seen FROM portfolio_bids
        ORDER BY 1,11,12,2,3""")
    quantities = {}
    for at, kind, identity, owner, qty, price, intent, ordered, fee, has_receipt, _, _ in events:
        when = datetime.fromisoformat(stamp(at))
        if kind <= 1:
            if account.qty and lot != owner:
                raise ValueError("overlapping allocated lots")
            before = quantities.get(intent, ZERO)
            after = before + D(qty)
            quantities[intent] = after
            order_fee = D(fee or 0)
            estimate = order_fee * after / D(ordered) - order_fee * before / D(ordered)
            # Only fills with a settlement event need an in-memory lookup identity.
            account.apply_fill(('buy', 'sell')[kind], qty, price, estimated_fee=estimate,
                               fill_id=identity if has_receipt else None)
            lot = owner if account.qty else None
            account.mark(when)
        elif kind == 2:
            account.settle_fee(identity, price)
            account.mark(when)
        elif owner == lot:
            account.mark(when, price)
    return account


def reconcile_account(c, risk, at, bids):
    """Run inside the successful broker snapshot transaction. Bids have passed freshness."""
    at = stamp(at)
    c.execute("""UPDATE portfolio_fees SET observed_at=?,fills_seen=(SELECT max(rowid) FROM fills)
                 WHERE observed_at IS NULL""", (at,))
    capital = min(D(10), amt(risk.max_capital_usd, True))
    limit = min(D('.50'), amt(risk.max_daily_loss_usd, True))
    sequence = list(c.execute("""SELECT (SELECT max(rowid) FROM fills),
                                 (SELECT max(rowid) FROM portfolio_fees)""").fetchone())
    signature = json.dumps([str(capital), str(limit), *sequence])
    if sequence[0] is not None and get(c, 'account_projection_inputs') != signature:
        c.execute("""INSERT INTO portfolio_fill_receipts(fill,batch)
                     SELECT id,? FROM fills WHERE id NOT IN (SELECT fill FROM portfolio_fill_receipts)""",
                  (sequence[0],))
    if get(c, 'account_projection_inputs') == signature:
        account = Account.restore(json.loads(get(c, 'account_projection')))
    else:
        account = _replay(c, capital, limit)
    held = [r for r in c.execute("SELECT id,symbol,qty,basis FROM lots") if D(r['qty'])]
    if len(held) > 1 or (sum((D(row['qty']) for row in held), ZERO) != account.qty
            or sum((D(row['basis']) for row in held), ZERO) != account.basis):
        raise ValueError("allocated projection and reconciled lots disagree")
    bid = bids.get(held[0]['symbol']) if held else None
    when = datetime.fromisoformat(at)
    # Tolerated broker clock skew can place the confirmed fill up to 2s ahead.
    if account.last_at and when < account.last_at:
        when = account.last_at
        at = stamp(when)
    account.mark(when, bid)
    if held:
        lot = held[0]['id']
        bid_text = None if bid is None else str(amt(bid, True))
        last = c.execute("SELECT at,bid FROM portfolio_bids WHERE lot=? ORDER BY at DESC,fills_seen DESC,fees_seen DESC LIMIT 1",
                         (lot,)).fetchone()
        # Persist one timestamp per price change or UTC day, including an unavailable bid.
        # A fill between two equal marks changes quantity and makes the mark economic again.
        if (last is None or last[1] != bid_text or last[0][:10] != at[:10]
                or get(c, 'account_projection_inputs') != signature):
            key = (at, lot, sequence[0] or 0, sequence[1] or 0)
            c.execute("""INSERT INTO portfolio_bids(at,lot,fills_seen,fees_seen,bid)
                         VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING""", (*key, bid_text))
            actual = c.execute("""SELECT bid FROM portfolio_bids WHERE at=? AND lot=?
                                  AND fills_seen=? AND fees_seen=?""", key).fetchone()
            if actual[0] != bid_text:
                raise ValueError("conflicting mark for same lot and instant")
    if account.drawdown_halt:
        put(c, 'allocated_drawdown_halt', 'YES')
    account.drawdown_halt |= get(c, 'allocated_drawdown_halt') == 'YES'
    if account.daily_halt:
        put(c, 'daily_halt_utc', at[:10])
    account.daily_halt |= get(c, 'daily_halt_utc') == at[:10]
    state = account.snapshot()
    state['fills'] = {}  # Receipt facts are normalized in SQLite; replay on the next change.
    # Legacy activities without frozen cost terms cannot establish net equity.
    if get(c, 'account_projection_inputs') != signature:
        missing = c.execute("""SELECT count(*) FROM fills f
                    LEFT JOIN portfolio_order_terms t ON t.intent=f.intent
                    LEFT JOIN portfolio_fees p ON p.fill=f.id
                    WHERE t.intent IS NULL AND p.fill IS NULL""").fetchone()[0]
        put(c, 'missing_fee_estimates', missing)
    put(c, 'account_projection', json.dumps(state))
    put(c, 'account_projection_inputs', signature)
    return account


def allocated_funds(c, risk, at, exclude_intent=None):
    """Return marked equity and spendable cash. Reconciliation owns the current projection."""
    state = get(c, 'account_projection')
    if state is None or get(c, 'snapshot') != 'ok':
        return ZERO, ZERO
    account = Account.restore(json.loads(state))
    reserved = _reservations(c, exclude_intent)
    if (get(c, 'missing_fee_estimates', '0') != '0' or account.equity is None or reserved is None or account.drawdown_halt or account.daily_halt
            or account.capital != amt(risk.max_capital_usd, True)
            or account.daily_loss != amt(risk.max_daily_loss_usd, True)
            or get(c, 'allocated_drawdown_halt') == 'YES' or get(c, 'daily_halt_utc') == stamp(at)[:10]):
        return ZERO, ZERO
    return account.equity, max(ZERO, account.cash - reserved)


def account_metrics(c, meta):
    state = meta.get('account_projection')
    account = Account.restore(json.loads(state)) if state else None
    current = account is not None and meta.get('snapshot') == 'ok'
    costs_known = meta.get('missing_fee_estimates', '0') == '0'
    valued = current and costs_known and account.equity is not None
    reserved = _reservations(c) if account is not None else None
    # All decimal JSON fields preserve the exact amount as a string.
    def value(number):
        return str(number) if number is not None else None

    return dict(
        valuation_at=account.last_at.isoformat() if account else None,
        allocated_cash=value(account.cash) if current and costs_known else None,
        reserved_cash=value(reserved) if current else None,
        available_cash=value(account.cash - reserved) if current and costs_known and reserved is not None else None,
        realized_gross=value(account.realized) if account else None,
        unrealized_gross=value(account.unrealized) if current else None,
        confirmed_fees=value(account.confirmed_fees) if account else None,
        estimated_fees=value(account.estimated_fees) if account and costs_known else None,
        unconfirmed_fee_count=account.pending_fee_count if account else None,
        missing_fee_estimate_count=int(meta.get('missing_fee_estimates', 0)) if account else None,
        marked_allocated_equity=value(account.equity) if valued else None,
        marked_net_pnl=value(account.equity - account.capital) if valued else None,
        daily_marked_pnl=value(account.equity - account.day_start) if valued else None,
        confirmed_net_pnl=value(account.cash - account.capital)
            if current and not account.qty and not account.pending_fee_count else None,
        peak_equity=value(account.peak) if account else None,
        drawdown=value(account.drawdown) if valued else None,
        max_drawdown=value(account.max_drawdown) if account else None,
        daily_halt=account.daily_halt if account else None,
        drawdown_halt=account.drawdown_halt if account else None,
    )
