"""Query confirmed gross results separately from cost scenarios. No broker imports."""
import os, sqlite3, json
from contextlib import closing
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]


from operational import freshness, read_status


def read_view(route, arg=None, path=None):
    file = Path(path or os.getenv('PAPER_DB', str(ROOT / 'data' / 'hourly-paper.sqlite'))).resolve()
    with closing(sqlite3.connect(file.as_uri() + '?mode=ro', uri=True, timeout=2)) as c:
        c.row_factory = sqlite3.Row
        c.execute('pragma query_only=on')
        c.execute('begin')
        rows = lambda sql, params=(): [dict(r) for r in c.execute(sql, params)]
        meta = dict(c.execute('select k,v from meta'))
        if meta.get('schema') != 'hourly-paper-1':
            raise ValueError('unsupported database')
        if route == 'health':
            telemetry = read_status(os.getenv('PHOENIX_STATUS_FILE', file.parent / 'health.json'))
            broker = json.loads(meta.get('broker_observation', '{}'))
            broker_freshness = freshness(broker.get('observed_at'), 45)
            data = rows("""SELECT symbol,max(t) AS latest_bar,max(received_at) AS latest_received
                FROM usable_bars GROUP BY symbol ORDER BY symbol""")
            batches = rows("""SELECT consumer,cut,results FROM development_batches
                ORDER BY cut DESC,consumer LIMIT 12""")
            for batch in batches:
                batch["results"] = [{key: result.get(key) for key in ("lane", "status", "reason")}
                                    for result in json.loads(batch["results"])]
            return dict(status='readable', schema_version=c.execute('PRAGMA user_version').fetchone()[0],
                        source_revision=meta.get('supervise_revision'), snapshot=meta.get('snapshot','unknown'),
                        snapshot_reason=meta.get('snapshot_reason'), broker_sync=meta.get('sync_at'),
                        supervisor_liveness=freshness(meta.get('supervise_heartbeat'),45),
                        research_liveness=freshness(meta.get('research_heartbeat'),3900),
                        reconciliation_freshness=freshness(meta.get('sync_at'),45), research=meta.get('research_at'),
                        learning_sequence=int(meta.get('learning_consumed_seq',0)),
                        learning_progress=json.loads(meta.get('learning_progress','{}')),
                        entries='unreleased', evidence='no_validated_activation',
                        marked_allocated_equity=None, confirmed_fees=None,
                        allocation_limit_usd=meta.get('allocation_limit_usd'),
                        broker_observation=broker, broker_observation_freshness=broker_freshness,
                        local_open_orders=rows("""SELECT id,symbol,side,qty,state,created FROM intents
                            WHERE state NOT IN ('filled','canceled','expired','rejected','rejected_locally')
                            ORDER BY created DESC LIMIT 50"""),
                        data_watermarks=data,
                        risk={'entry_halt':meta.get('integrity_entries_paused', 'YES'),
                              'daily_halt_utc':meta.get('daily_halt_utc'),
                              'allocated_drawdown_halt':meta.get('allocated_drawdown_halt'),
                              'position_protection':'unverified'},
                        telemetry=telemetry, telemetry_freshness=freshness(telemetry.get('at'), 90),
                        research_scopes=json.loads(meta.get('research_scope_status','{}')),
                        development_batches=batches,
                        failures=rows("""SELECT symbol,regime,strategy,attempts,status,created_at
                            FROM unsolved_failures ORDER BY created_at DESC LIMIT 12"""),
                        uncertain_orders=c.execute("""SELECT count(*) FROM intents WHERE state IN
                            ('reserved','dispatching','submission_unknown','reconciliation_required','unknown')""").fetchone()[0])
        if route == 'strategies':
            return rows("""SELECT id,name,regime,status,generation FROM strategies
                WHERE status IN ('champion','challenger') ORDER BY regime,status,id""")
        if route == 'mode':
            return {'mode': meta.get('mode', 'UNKNOWN'), 'live_enabled': False}
        if route == 'symbols':
            return [r[0] for r in c.execute('select symbol from watermarks order by symbol')]
        if route == 'price':
            return list(reversed(rows('select b.t,o,h,l,c,d.target as signal from bars b left join decisions d on d.symbol=b.symbol and d.t=b.t where b.symbol=? order by b.t desc limit 300', (arg,))))
        if route == 'regime':
            a = rows('select * from regimes order by t desc limit 1')
            return dict(label=a[0]['label'], indicators=json.loads(a[0]['indicators']), timestamp=a[0]['t']) if a else {}
        if route == 'strategy':
            a = rows("select id,name,parent_id,generation,status from strategies where status='champion'")
            return a[0] if a else {}
        if route == 'rules':
            return rows("select text||' ['||state||']' as rule,created_at from rules order by created_at desc")
        if route == 'lessons':
            return rows('select lesson,created_at from lessons order by created_at desc limit ?', (arg,))
        if route == 'equity-curve':
            return rows('select at as t,sum(gross) over(order by at,id) as equity from outcomes order by at,id')
        # One observation per fully closed lot. Realized dollars accrue on each exit fill.
        closed = 'select o.lot,sum(o.gross) pnl,max(o.at) at,sum((o.exit-o.entry)*cast(o.qty as real)) directional from outcomes o join lots l on l.id=o.lot where cast(l.qty as real)=0 group by o.lot'
        if route == 'trades':
            return rows("select l.id,l.symbol,'buy' as side,cast(l.qty as real) remaining, (select sum(cast(f.qty as real)) from fills f join intents i on i.id=f.intent where i.lot=l.id and i.side='buy') qty,(select sum(cast(f.qty as real)*cast(f.price as real))/sum(cast(f.qty as real)) from fills f join intents i on i.id=f.intent where i.lot=l.id and i.side='buy') entry_price,sum(o.exit*cast(o.qty as real))/sum(cast(o.qty as real)) exit_price,sum(o.gross) pnl,l.opened created_at,case when cast(l.qty as real)=0 then 'closed' when ?!='ok' then 'unknown' else 'open' end status,case when cast(l.qty as real)=0 then sum(o.gross)>0 end direction_right,case when cast(l.qty as real)>0 then 'pending' when sum(o.gross)>0 then 'correct' when sum(o.gross)<0 then 'wrong' else 'flat' end observation from lots l left join outcomes o on o.lot=l.id group by l.id order by max(coalesce(o.at,l.opened)) desc limit ?", (meta.get('snapshot', 'unknown'), arg))
        if route in ('metrics', 'hourly'):
            hourly = 'with closed as (' + closed + '), hours as (select substr(at,1,13) h from outcomes union select substr(at,1,13) from closed union select substr(created_at,1,13) from lessons union select substr(t,1,13) from decisions), base as (select h,(select count(*) from closed where substr(at,1,13)=h) total_trades,(select count(*) from closed where substr(at,1,13)=h and pnl>0) wins,(select count(*) from closed where substr(at,1,13)=h and pnl<0) losses,(select count(*) from lessons where substr(created_at,1,13)=h) lessons,coalesce((select sum(gross) from outcomes where substr(at,1,13)=h),0) gross,(select sum(cast(qty as real)*(entry+exit)) from outcomes where substr(at,1,13)=h) turnover from hours order by h desc limit 48) select *,1.0*wins/nullif(total_trades,0) win_rate,1.0*wins/nullif(wins+losses,0) observation_accuracy,gross-coalesce(turnover,0)*0.001 real_profit_sim from base order by h desc'
            if route == 'hourly':
                return rows(hourly)
            m = rows('select count(*) total_trades,count(case when pnl>0 then 1 end) win_trades,count(case when pnl<0 then 1 end) loss_trades,avg(pnl) avg_pnl from (' + closed + ')')[0]
            m.update(mode=meta.get('mode', 'UNKNOWN'), correct_observations=m['win_trades'], wrong_observations=m['loss_trades'], win_rate=m['win_trades'] / m['total_trades'] if m['total_trades'] else None, total_pnl=c.execute('select sum(gross) from outcomes').fetchone()[0], daily_pnl=c.execute("select sum(gross) from outcomes where substr(at,1,10)=date('now')").fetchone()[0], snapshot=meta.get('snapshot', 'unknown'), broker_sync=meta.get('sync_at'), research=meta.get('research_at'))
            return m
        raise ValueError('unknown view')
