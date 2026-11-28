"""Daemon integration for the independent, observational equity learning journal."""
import json
import time
import sqlite3
import uuid
from contextlib import closing
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from hourly_features import enrich
from learning_loop import Bar, Risk, Rule, digest, mask, next_month, unpack
from shadow_campaign import CampaignLedger, QuoteRisk, TimedBar, save_quotes, utc
from learning_loop_adapter import UNKNOWN_GATES, close_tick
from market_time import equity_sessions
from paper_store import db, get, put

CALENDAR_ID = "XNYS/exchange-calendars-4.13.3"


class LearningRecoveryRequired(ValueError):
    """Reason codes for restoring and reconciling local research records."""


def shadow_book():
    return {
        "ENTRY": Rule("ENTRY", "Unproven shadow trend-and-momentum control.", "trend and momentum"),
        "SPREAD": Rule("SPREAD", "Execution-owned quote gate.", None),
        "EXIT": Rule("EXIT", "Execution-owned position protection.", None),
    }


def learning_risk(settings, revision=3):
    r = settings.risk
    common = (r.max_capital_usd, min(r.max_position_usd, r.max_order_size_usd))
    limits = (r.max_daily_loss_usd, 0.10, 1, 0.000000001, r.max_capital_usd * 0.01, 1800)
    if revision in (1, 2):
        return Risk(*common, r.spread_bps + r.slippage_bps, *limits, policy_revision=revision)
    return QuoteRisk(*common, 0, *limits, policy_revision=3,
                     minimum=r.min_order_size_usd, fee_per_order=r.commission_per_order_usd)


def record_shadow_quotes(path, at, quotes, *, unknown=()):
    """Record quotes already observed by the supervisor. Never open a broker connection."""
    with db(path) as c:
        if get(c, 'shadow_quote_capture') != 'v3':
            return False
        expected_store, owner = get(c, 'learning_store'), get(c, 'learning_owner')
    sidecar = Path(path).resolve().with_name('learning_loop.sqlite')
    # mode=rw preserves missing-store recovery semantics and cannot create an unbound journal.
    with closing(sqlite3.connect(sidecar.as_uri()+'?mode=rw', uri=True, timeout=2)) as c, c:
        c.execute('BEGIN IMMEDIATE')
        meta = {k:json.loads(v) for k,v in c.execute(
            "SELECT k,v FROM ll_meta WHERE k IN ('owner','store_id','config')")}
        if not owner or not expected_store or (meta.get('owner'), meta.get('store_id')) != (owner, expected_store):
            raise LearningRecoveryRequired('quote_journal_identity_mismatch')
        cfg = meta['config']
        if cfg['risk'].get('policy_revision') != 3 or not any(
                d['opened'] <= at.timestamp() <= d['cuts'][-1] for d in cfg['sessions']):
            return False
        return save_quotes(c, at, quotes, unknown)


def bind_store(path, ledger):
    """Two local commits are replayable. A missing or rolled-back journal is visible."""
    with db(path, True) as c:
        op_id = get(c, "learning_owner")
        if op_id is None:
            op_id = uuid.uuid4().hex
            put(c, "learning_owner", op_id)
    with db(path, True) as c:
        expected = get(c, "learning_store")
        expected_seq = int(get(c, "learning_consumed_seq", 0))
        with ledger.tx():
            actual = ledger._get("store_id")
            if expected is not None and actual != expected:
                raise LearningRecoveryRequired("journal_identity_mismatch")
            owner = ledger._get("owner")
            if owner is not None and owner != op_id:
                raise LearningRecoveryRequired("operational_owner_changed")
            if actual is None:
                actual = uuid.uuid4().hex
                ledger._set("store_id", actual)
            ledger._set("owner", op_id)
            ledger.db.execute("""CREATE TABLE IF NOT EXISTS ll_receipts(
                seq INTEGER PRIMARY KEY, cut INTEGER NOT NULL UNIQUE, completed_at INTEGER NOT NULL)""")
            seq = ledger.db.execute("SELECT coalesce(max(seq),0) FROM ll_receipts").fetchone()[0]
            if seq < expected_seq:
                raise LearningRecoveryRequired("journal_checkpoint_regressed")
        put(c, "learning_store", actual)


def prepare_campaign(ledger, settings):
    equity_date = datetime.fromtimestamp(ledger.clock(), ZoneInfo("America/New_York")).date()
    cfg = ledger._get("config")
    symbols = sorted(set(settings.universe.equities) - {"SPY", "QQQ"})
    risk = learning_risk(settings)
    if cfg is not None and equity_date < date.fromisoformat(cfg["end_exclusive"]):
        if cfg.get("calendar_id") != CALENDAR_ID:
            return "legacy_calendar_quarantined"
        revision = cfg["risk"].get("policy_revision", 1)
        frozen = (QuoteRisk if revision == 3 else Risk)(**cfg["risk"])
        frozen.validate()
        expected = asdict(learning_risk(settings, revision))
        if cfg["symbols"] != symbols or asdict(frozen) != expected:
            raise LearningRecoveryRequired("frozen_settings_changed")
        return "resumed"
    today = equity_sessions(equity_date, equity_date + timedelta(days=1))
    start = equity_date if today and ledger.clock() < today[0].opened else equity_date + timedelta(days=1)
    sessions = equity_sessions(start, next_month(start))
    ledger.setup(start.isoformat(), sessions, symbols, risk,
                 unpack(ledger._get("active")) if cfg else shadow_book(),
                 previous_config=digest(cfg) if cfg else None, calendar_id=CALENDAR_ID)
    return "rolled_over" if cfg else "initialized"


def pending_frame(path, symbols, first, last, as_of):
    """Bound reads to this campaign and the preceding indicator warm-up per symbol."""
    start = pd.Timestamp(first, unit="s", tz="UTC").isoformat()
    stop = pd.Timestamp(last, unit="s", tz="UTC").isoformat()
    known_by = pd.Timestamp(as_of, unit="s", tz="UTC").isoformat()
    select = """SELECT symbol,t,o,h,l,c,v,interval_kind,interval_end,received_at FROM usable_bars
                WHERE symbol=? AND interval_kind='XNYS_1m' AND interval_end=t
                AND julianday(received_at)<=julianday(?)"""
    rows = []
    with db(path) as c:
        for symbol in symbols:
            rows.extend(c.execute(select + " AND t<? ORDER BY t DESC LIMIT 600", (symbol, known_by, start)))
            rows.extend(c.execute(select + " AND t>=? AND t<=? ORDER BY t", (symbol, known_by, start, stop)))
    frame = pd.DataFrame.from_records(rows, columns=["symbol", "timestamp", "open", "high", "low",
                                                      "close", "volume", "interval_kind", "interval_end", "received_at"])
    if frame.empty:
        return frame
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, format="ISO8601")
    frame["interval_end"] = pd.to_datetime(frame["interval_end"], utc=True, format="ISO8601")
    return enrich(frame.set_index(["symbol", "timestamp"]).sort_index())


def observe_new_bars(path, settings, clock=None):
    from hourly_features import strategy_flags
    clock = time.time if clock is None else clock
    sidecar = Path(path).resolve().with_name("learning_loop.sqlite")
    with db(path) as c:
        expected_store = get(c, "learning_store")
    if expected_store and not sidecar.is_file():
        raise LearningRecoveryRequired("journal_missing")
    with closing(CampaignLedger(sidecar, clock=clock)) as ledger:
        bind_store(path, ledger)
        lifecycle = prepare_campaign(ledger, settings)
        cfg = ledger._get("config")
        quote_campaign = cfg["risk"].get("policy_revision") == 3
        with db(path, True) as c:
            put(c, "shadow_quote_capture", "v3" if quote_campaign and lifecycle != "legacy_calendar_quarantined" else "off")
        receipts = {r[0] for r in ledger.db.execute(
            "SELECT cut FROM ll_receipts WHERE cut>=?", (cfg["sessions"][0]["opened"],))}
        pending = [cut for day in cfg["sessions"] for cut in day["cuts"]
                   if cut <= clock() and cut not in receipts and lifecycle != "legacy_calendar_quarantined"]
        progress = {"campaign": cfg["start"], "lifecycle": lifecycle, "consumed": 0,
                    "missing_batch": 0, "features_unavailable": 0}
        if pending:
            symbols = cfg["symbols"]
            frame = pending_frame(path, symbols, cfg["sessions"][0]["cuts"][0], pending[-1], clock())
            batches = {} if frame.empty else frame.groupby(level="timestamp").groups
            for cut in pending:
                stored = ledger.db.execute("SELECT body FROM ll_bars WHERE cut=?", (cut,)).fetchone()
                timestamp = pd.Timestamp(cut, unit="s", tz="UTC")
                if stored is not None:
                    bar_class = TimedBar if quote_campaign else Bar
                    batch = {s: bar_class(**item) for s, item in json.loads(stored[0]).items()}
                elif timestamp not in batches:
                    progress["missing_batch"] += 1
                    continue
                else:
                    rows = frame.xs(timestamp, level="timestamp")
                    if set(rows.index) != set(symbols):
                        progress["missing_batch"] += 1
                        continue
                    batch = {}
                    for symbol, row in rows.iterrows():
                        flags = strategy_flags(row)
                        if flags is None or pd.isna(row.atr) or row.atr <= 0:
                            break
                        # No independently attested gate at this interval's open.
                        values = (float(row.open), float(row.high), float(row.low), float(row.close),
                                  mask(flags), 2 * float(row.atr), None, 0.0)
                        batch[symbol] = (TimedBar(*values, published=row.received_at, decided=utc(clock()).isoformat())
                                         if quote_campaign else Bar(*values))
                    if len(batch) != len(symbols):
                        progress["features_unavailable"] += 1
                        continue
                close_tick(ledger, cut, batch, gates=lambda _: UNKNOWN_GATES)
                with ledger.tx():
                    ledger.db.execute("""INSERT INTO ll_receipts(cut,completed_at) VALUES(?,?)
                        ON CONFLICT(cut) DO NOTHING""", (cut, int(clock())))
                progress["consumed"] += 1
        seq = ledger.db.execute("SELECT coalesce(max(seq),0) FROM ll_receipts").fetchone()[0]
        with db(path, True) as c:
            put(c, "learning_consumed_seq", max(seq, int(get(c, "learning_consumed_seq", 0))))
            put(c, "learning_progress", json.dumps(progress))
    return progress
