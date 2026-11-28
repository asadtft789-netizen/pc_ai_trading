"""Append-only decisions; no mutating legacy rules on the strength of text alone."""
from datetime import datetime, timezone, timedelta
from paper_store import db, key, now
import json

def import_rules(path, legacy):
    with db(legacy) as c:
        rows = c.execute('select id,rule_text from rules order by id').fetchall()
    with db(path, True) as c:
        for r in rows:
            rid = key('legacy', r[0], r[1])
            c.execute('insert or ignore into rules(id,text,feature,state,created_at,parent) values(?,?,?,?,?,?)', (rid, r[1], None, 'unattributed', now(), None))
            c.execute('insert or ignore into reflections(id,subject,message,at) values(?,?,?,?)', (key('unscored', rid), rid, 'Legacy rule retained; no reliable decision-to-fill attribution. Not eligible for activation.', now()))
    return len(rows)

def drift(path, *, as_of=None):
    """Descriptive gross episode monitor on fixed, disjoint UTC 28-day blocks."""
    at = datetime.now(timezone.utc) if as_of is None else as_of
    if at.tzinfo is None:
        raise ValueError('UTC offset required')
    epoch = datetime(1970,1,1,tzinfo=timezone.utc)
    end = epoch + timedelta(days=((at-epoch).days//28)*28)
    start, middle = end-timedelta(days=28), end-timedelta(days=14)
    from experiment_store import append, read
    with db(path, True) as c:
        for s in c.execute("SELECT id FROM strategies WHERE status IN ('champion','retired')"):
            sid = s['id']
            ident = key('gross-drift-v1',sid,end.isoformat())
            if read(c,'drift_review',ident) is not None:
                continue
            rows = c.execute("""SELECT sum(o.gross),max(o.at) FROM outcomes o JOIN lots l ON l.id=o.lot
                WHERE o.strategy=? AND cast(l.qty AS REAL)=0 GROUP BY o.lot
                HAVING julianday(max(o.at))>=julianday(?) AND julianday(max(o.at))<julianday(?)""",
                (sid,start.isoformat(),end.isoformat())).fetchall()
            earlier, recent = [], []
            for pnl, closed in rows:
                (earlier if datetime.fromisoformat(closed) < middle else recent).append(pnl)
            complete = min(len(earlier),len(recent)) >= 20
            old = sum(earlier)/len(earlier) if earlier else None
            new = sum(recent)/len(recent) if recent else None
            state = 'inconclusive'
            if complete:
                state = 'investigate' if old > 0 and new <= .8*old else 'observed'
            append(c,'drift_review',ident,dict(origin='broker_paper_gross',strategy=sid,
                   start=start.isoformat(),end=end.isoformat(),counts=[len(earlier),len(recent)],
                   gross_means=[old,new],status=state))
            if state == 'investigate':
                c.execute("INSERT OR IGNORE INTO reflections(id,subject,message,at) VALUES(?,?,?,?)",
                          (ident,sid,'Gross P&L per closed paper lot declined at least 20% across two fixed '
                           '14-day windows. Investigate costs, attribution and market conditions.',now()))


def learn_from_closes(path, mutator):
    """One read-only lot summary per lesson. Never assign a result to unrelated rules."""
    with db(path) as c:
        row = c.execute('select o.lot,o.strategy,o.symbol,sum(o.gross) gross,max(o.at) exit_time,s.rule_ids from outcomes o join lots l on l.id=o.lot join strategies s on s.id=o.strategy where cast(l.qty as real)=0 and not exists(select 1 from lessons n where n.lot=o.lot) group by o.lot order by max(o.at) limit 1').fetchone()
        if not row:
            return
        snapshot = c.execute("""SELECT t,regime,feature_version,readiness_mask,history_ready,
                                flags,rsi,received_at,content_hash FROM decision_snapshots
                                WHERE lot=? AND strategy=? AND symbol=?""",
                             (row['lot'],row['strategy'],row['symbol'])).fetchone()
        fills = [dict(f) for f in c.execute("""SELECT f.id AS fill_id,f.intent,f.at,f.qty,f.price,i.side
                            FROM fills f JOIN intents i ON i.id=f.intent
                            WHERE i.lot=? AND i.symbol=? ORDER BY f.at,f.id""", (row['lot'],row['symbol']))]
    evidence = dict(row)
    evidence['entry_snapshot'] = dict(snapshot) if snapshot else None
    evidence['fills'] = fills
    evidence['gross_pnl_only'] = True
    rid = json.loads(evidence.pop('rule_ids'))[1]
    if snapshot is None:
        text = 'Entry snapshot unavailable for this confirmed lot. Entry-feature attribution is unknown.'
    else:
        text = mutator.generate_lesson(evidence) or 'Retest the entry guard across independent episodes with costs.'
    with db(path, True) as c:
        c.execute('insert or ignore into lessons(id,lot,rule_id,lesson,created_at) values(?,?,?,?,?)', (key('lesson', row['lot']), row['lot'], rid, text, now()))
        c.execute('insert or ignore into reflections(id,subject,message,at) values(?,?,?,?)', (key('lesson-reflection', row['lot']), rid, 'Hypothesis from confirmed paper lot ' + row['lot'] + ': ' + text, now()))
