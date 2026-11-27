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

def drift(path):
    today = datetime.now(timezone.utc)
    window = (today - timedelta(days=14)).isoformat()
    prior = (today - timedelta(days=28)).isoformat()
    with db(path, True) as c:
        for s in c.execute("select id,rule_ids from strategies where status in ('champion','retired')").fetchall():
            rid = json.loads(s['rule_ids'])[1]
            rows = c.execute('select sum(gross),max(at) from outcomes where strategy=? and at>=? group by lot having lot in (select id from lots where cast(qty as real)=0)', (s['id'], prior)).fetchall()
            a = [p for p, t in rows if t < window]
            b = [p for p, t in rows if t >= window]
            if min(len(a), len(b)) < 20:
                continue
            old = sum(a) / len(a)
            new = sum(b) / len(b)
            if old <= 0 or new > old * 0.8:
                continue
            msg = 'Confirmed paper gross expectancy declined at least 20% in two distinct 14-day windows; review, not proof the guard caused the change.'
            c.execute('insert or ignore into reflections(id,subject,message,at) values(?,?,?,?)', (key('drift', rid, today.date().isoformat()), rid, msg, now()))
            c.execute("update rules set state='review' where id=?", (rid,))

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
