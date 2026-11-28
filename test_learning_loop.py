"""Offline regression tests; never imports or calls Alpaca or a real executor."""
from __future__ import annotations

import ast
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from example_hourly_run import Clock, GATES, SYMBOLS, add_discovery, batch, risk, run, synthetic_calendar
from learning_loop import FEATURES, Bar, Ledger, Portfolio, expression, fixture, judge, next_month, paired, patch, source, truth
from learning_loop_adapter import UNKNOWN_GATES, close_tick, fresh_snapshot, inspect_repo, session_open, verify_mapping
from datetime import date

class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.p=Path(self.tmp.name)/'sidecar.db'
        self.days=synthetic_calendar(); self.clock=Clock(self.days[0].opened-60); self.l=Ledger(self.p,self.clock); self.addCleanup(self.l.close)
        self.l.setup('2025-04-01',self.days,SYMBOLS,risk(),fixture()); self.clock.value=self.days[0].cuts[0]

    def plan(self,add=False) -> str:
        add_discovery(self.l,self.days[0]); opt=self.l.suggest(add=add,origin='shadow'); self.assertIsNotNone(opt)
        return self.l.register(self.days[1].label,**opt)

    def fill(self, *, change=None, skip=None) -> None:
        for d in self.days[1:6]:
            for i,cut in enumerate(d.cuts):
                self.clock.value=cut
                if cut==skip: continue
                vals=batch(i)
                if change: vals=change(vals)
                self.l.bar(cut,vals)

    def test_feature_contract_and_atomic_diff(self):
        base=fixture(); cand=patch(base,'ENTRY','breakout and volume','confirm volume'); s=source(cand); fn=ast.parse(s).body[0]
        self.assertEqual(tuple(a.arg for a in fn.args.args),FEATURES); self.assertLessEqual(len(s),2400); self.assertEqual(len(fn.body),1)
        self.assertEqual([k for k in base if base[k]!=cand[k]],['ENTRY']); self.assertEqual(cand['ENTRY'].parent,base['ENTRY'].version)
        self.assertIsNotNone(cand['ENTRY'].expression)

    def test_noop_invalid_expression_and_guards(self):
        for key,ex in [('SPREAD','trend'),('EXIT','trend'),('ENTRY','breakout and trend'),('ENTRY','breakout + 1'),('ENTRY','ask'),('ENTRY','abs(trend)')]:
            with self.subTest(key=key,expr=ex),self.assertRaises(ValueError): patch(fixture(),key,ex,'test')
        self.assertEqual(truth('not (trend or volume)'),truth('(not trend) and (not volume)'))

    def test_observations_and_corrections_are_append_only(self):
        add_discovery(self.l,self.days[0]); add_discovery(self.l,self.days[0]); self.assertEqual(self.l.db.execute('select count(*) from ll_obs').fetchone()[0],4)
        with self.assertRaises(sqlite3.IntegrityError): self.l.db.execute('delete from ll_obs')
        self.l.note('n','lesson',{'text':'valid loss'}); self.l.note('n','lesson',{'text':'valid loss'})
        with self.assertRaises(ValueError): self.l.note('n','lesson',{'text':'different'})
        self.l.note('n2','correction',{'corrects':'n','why':'broker revision'})

    def test_readonly_and_refuse_daemon_database(self):
        root=Path(self.tmp.name); path=root/'daemon.db'; db=sqlite3.connect(path); db.execute('create table fills(id integer)'); db.commit(); db.close()
        with self.assertRaisesRegex(ValueError,'separate sidecar'): Ledger(path)
        inventory=inspect_repo(root,path); self.assertEqual(inventory['columns']['fills'],['id']); self.assertEqual(inventory['files']['research.py']['status'],'missing')
        db=sqlite3.connect(path); self.assertEqual([r[0] for r in db.execute('select name from sqlite_master')],['fills']); db.close()

    def test_partial_missing_and_conflicting_bar(self):
        t=self.days[0].cuts[0]; self.clock.value=t-1
        with self.assertRaisesRegex(ValueError,'unfinished'): self.l.bar(t,batch(0))
        self.clock.value=t; partial=batch(0); del partial['AAA']
        with self.assertRaisesRegex(ValueError,'incomplete'): self.l.bar(t,partial)
        self.assertTrue(self.l.bar(t,batch(0))); self.assertFalse(self.l.bar(t,batch(0)))
        changed=batch(0); changed['AAA']=replace(changed['AAA'],entry_ok=None)
        with self.assertRaisesRegex(ValueError,'conflicting'): self.l.bar(t,changed)

    def test_restart_does_not_duplicate_hour(self):
        t=self.days[0].cuts[0]; self.l.bar(t,batch(0)); other=Ledger(self.p,self.clock)
        try: self.assertFalse(close_tick(other,t,batch(0))['inserted']); self.assertEqual(other.db.execute('select count(*) from ll_bars').fetchone()[0],1)
        finally: other.close()

    def test_register_needs_prospective_unique_window(self):
        ident=self.plan(); self.assertTrue(ident)
        with self.assertRaisesRegex(ValueError,'another candidate'): self.l.register(self.days[1].label,**self.l.suggest(origin='shadow'))
        with self.assertRaisesRegex(ValueError,'window not complete'): self.l.decide(ident,GATES)

    def test_final_holdout_cannot_be_used_for_learning(self):
        add_discovery(self.l,self.days[0]); opt=self.l.suggest(origin='shadow')
        with self.assertRaisesRegex(ValueError,'holdout'): self.l.register(self.days[-10].label,**opt)
        self.assertEqual(next_month(date(2024,1,31)),date(2024,2,29))

    def test_consumed_sidecar_holdout_does_not_change_after_late_receipts(self):
        self.clock.value=self.days[-1].cuts[-1]
        first=self.l.holdout()
        self.assertEqual(first['status'],'inconclusive')
        self.l.bar(self.days[-1].cuts[-1],batch(0))
        self.assertEqual(self.l.holdout(),first)
        self.assertEqual(self.l.db.execute("SELECT count(*) FROM ll_events WHERE kind='holdout_consumed'").fetchone()[0],1)

    def test_overlapping_evidence_disallowed(self):
        v=fixture()['ENTRY'].version; d=self.days[0]
        for n in range(3): self.l.observe(str(n),d.opened,d.opened+10+n,'ENTRY',v,0,-0.01,'shadow')
        with self.assertRaisesRegex(ValueError,'independent'): self.l.register(self.days[1].label,'ENTRY','breakout and volume','different',['0','1','2'],'ENTRY')

    def test_no_promotion_from_single_loss(self):
        d=self.days[0]; self.l.observe('one',d.opened,d.opened+1,'ENTRY',fixture()['ENTRY'].version,0,-0.1,'broker_paper'); self.assertIsNone(self.l.suggest())

    def test_missing_data_inconclusive_and_does_not_replace_champion(self):
        tid=self.plan(); missed=self.days[2].cuts[1]; self.fill(skip=missed); res=self.l.decide(tid,GATES)
        self.assertEqual(res['status'],'inconclusive'); self.assertIn(missed,res['missing_cuts']); self.assertEqual(self.l.state()['active'],self.l.state()['original'])
        self.l.bar(missed,batch(1)); self.assertEqual(self.l.decide(tid,GATES)['status'],'inconclusive')

    def test_late_bar_cannot_count_as_forward_evidence(self):
        tid=self.plan(); missed=self.days[2].cuts[1]; self.fill(skip=missed); self.l.bar(missed,batch(1)); self.assertIn(missed,self.l.decide(tid,GATES)['missing_cuts'])

    def test_unknown_gates_block_a_perfect_backtest(self):
        tid=self.plan(); self.fill(); res=self.l.decide(tid,UNKNOWN_GATES); self.assertEqual(res['status'],'inconclusive')

    def test_unverified_spread_stays_unverified(self):
        tid=self.plan(); self.fill(change=lambda d:{s:replace(b,entry_ok=None) if s=='AAA' else b for s,b in d.items()}); self.assertEqual(self.l.decide(tid,GATES)['status'],'inconclusive')

    def test_zero_disagreements_even_with_many_fills(self):
        tid=self.plan(); self.fill(change=lambda d:{s:replace(b,bits=b.bits | (1<<FEATURES.index('volume'))) for s,b in d.items()}); res=self.l.decide(tid,GATES)
        self.assertGreaterEqual(res['one_bp']['inc_trades'],20); self.assertEqual(res['one_bp']['episodes'],[]); self.assertEqual(res['status'],'inconclusive')

    def test_branch_in_cash_has_zero_pnl(self):
        old=fixture(); new=patch(old,'ENTRY','breakout and 0','never enter'); d=self.days[1]; rows={cut:batch(i) for i,cut in enumerate(d.cuts)}
        z=paired(old,new,[d],rows,risk(),1); self.assertTrue(z['episodes']); self.assertTrue(all(ep['cand']==0 for ep in z['episodes'])); self.assertEqual(z['cand_trades'],0)

    def test_all_gates_and_rollback(self):
        tid=self.plan(); self.fill(); res=self.l.decide(tid,GATES); self.assertEqual(res['status'],'staged'); self.assertEqual(len(res['one_bp']['episodes']),25)
        self.assertGreater(res['one_bp']['inc_trades'],25); self.assertGreater(res['one_bp']['cand_pnl'],res['two_bp']['cand_pnl']); self.assertEqual(self.l.state()['active'],self.l.state()['original'])
        with self.assertRaisesRegex(ValueError,'before next'): self.l.activate(tid)
        self.clock.value=self.days[6].opened; session_open(self.l); state=self.l.state(); self.assertNotEqual(state['active']['ENTRY'],state['original']['ENTRY']); self.assertEqual(state['active']['TREND'],state['original']['TREND'])
        self.l.halt('confirmed executor guard violation',tid); self.assertTrue(self.l.state()['halted']); self.assertEqual(self.l.state()['active'],self.l.state()['original']); self.assertEqual(self.l.db.execute('select state from ll_trials where id=?',(tid,)).fetchone()[0],'rolled_back')

    def test_new_rule_is_removed_on_rollback(self):
        tid=self.plan(add=True); self.fill(); self.l.decide(tid,GATES); self.clock.value=self.days[6].opened; session_open(self.l); self.assertIn('FILTER_VOLUME',self.l.state()['active'])
        self.l.halt('confirmed violation',tid); self.assertNotIn('FILTER_VOLUME',self.l.state()['active']); self.assertEqual(self.l.state()['active']['ENTRY'],self.l.state()['original']['ENTRY'])

    def test_stale_parent_cannot_activate(self):
        tid=self.plan(); self.fill(); self.l.decide(tid,GATES); changed=self.l.state()['active']; changed['TREND']['text']='independent change'; self.l._set('active',changed)
        self.clock.value=self.days[6].opened; self.l.activate(tid); self.assertEqual(self.l.state()['active'],changed); self.assertEqual(self.l.db.execute('select state from ll_trials where id=?',(tid,)).fetchone()[0],'expired')

    def test_missed_activation_not_replayed(self):
        tid=self.plan(); self.fill(); self.l.decide(tid,GATES); self.clock.value=self.days[6].opened+61; session_open(self.l); self.assertEqual(self.l.state()['active'],self.l.state()['original'])

    def test_violation_has_priority_over_insufficient_evidence(self):
        tid=self.plan(); self.clock.value=self.days[5].cuts[-1]; g={**GATES,'safety_attested':False}; self.assertEqual(self.l.decide(tid,g)['status'],'rejected'); self.assertTrue(self.l.state()['halted'])

    def test_cost_drawdown_splits_and_legacy_gates(self):
        dummy={'episodes':[{'session':i%5,'inc':0,'cand':1} for i in range(20)],'unknown_gate':False,'cand_dd':0.01,'inc_dd':0.02,'cand_trades':20,'cand_pnl':1,'inc_pnl':0}
        self.assertEqual(judge(dummy,{**dummy,'cand_pnl':0},GATES)[0],'rejected'); self.assertEqual(judge({**dummy,'cand_dd':0.03},dummy,GATES)[0],'rejected'); self.assertEqual(judge(dummy,dummy,{**GATES,'supported':False})[0],'rejected')
        eps=[{'session':i%5,'inc':0,'cand':(1 if i%5<2 else -0.5)} for i in range(20)]; self.assertEqual(judge({**dummy,'episodes':eps},dummy,GATES)[0],'rejected')

    def test_no_fill_at_signal_close_or_short_stub(self):
        p=Portfolio(fixture(),risk(),1); b=batch(0); a=p.step(b,True,True,False,True); self.assertEqual(a.fills,0); stub=p.step(b,False,False,True,True); self.assertEqual(stub.fills,0); self.assertTrue(stub.flat)

    def test_snapshot_31_seconds_old_is_not_current(self):
        self.assertTrue(fresh_snapshot('ok',30)); self.assertFalse(fresh_snapshot('ok',31)); self.assertFalse(fresh_snapshot('ok',-1)); self.assertFalse(fresh_snapshot('stale',3))

    def test_real_source_mapping_must_preserve_all_feature_vectors(self):
        base=fixture(); src=source(base); verify_mapping(src,base)
        with self.assertRaisesRegex(ValueError,'incumbent behavior'): verify_mapping(src,patch(base,'ENTRY','breakout and volume','test'))

    def test_runtime_cannot_redefine_frozen_cost_model(self):
        tid=self.plan(); self.fill(); cfg=self.l._get('config'); cfg['model_policy']['engine_sha256']='different'; self.l._set('config',cfg)
        self.assertEqual(self.l.decide(tid,GATES)['status'],'inconclusive')

    def test_end_to_end_month_and_restart_refusal(self):
        p=Path(self.tmp.name)/'month.db'; res=run(p); self.assertEqual(res['broker_orders'],0); self.assertEqual(res['final_holdout'],'shadow_only'); self.assertEqual(res['symbols'],6); self.assertIn('volume',res['active_entry'])
        with self.assertRaisesRegex(ValueError,'overwrite'): run(p)

if __name__=='__main__': unittest.main()
