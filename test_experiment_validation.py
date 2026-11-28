import copy
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from importlib.util import find_spec
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import statistical_release

import numpy as np
import pandas as pd

from campaign_calibration import design, register_calibration, upper_bound
from campaign_evidence import (close_campaign, differing_count, record_verification,
                               supported_claim, verify_eligibility)
from campaign_registry import neighbors, frozen_campaign, register, record_session, replay_inputs
from campaign_statistics import CAMPAIGN_ALPHA
from experiment_store import append, needed
from historical_validation import (development_windows, inspect_holdout, register_dataset,
                                   walk_forward)
from learning_loop import Ledger, digest
from learning_runtime import bind_store
from market_time import equity_sessions
from paper_store import db, init
from sqlite_snapshot import copy_verified
from strategy_worker import SEED


def spec(expr):
    return dict(source=SEED.split(":")[0] + ":\n    return 1 if " + expr + " else 0",
                guard=None, parameters={}, weights={})


def panel(days):
    ts = pd.to_datetime([d.opened+3600 for d in days], unit="s", utc=True)
    frame = pd.DataFrame({"momentum": np.arange(len(days),dtype=float),
                          "label": np.sin(np.arange(len(days))), "available_at":ts,
                          "label_end": ts + pd.Timedelta(hours=1), "position_end":ts,
                          "label_available_at": ts + pd.Timedelta(hours=1)},
                         index=pd.MultiIndex.from_arrays([["AAPL"]*len(days),ts],names=["symbol","timestamp"]))
    return frame


class History(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = init(Path(tmp.name)/"research.sqlite")
        self.frame = panel(equity_sessions(date(2023,1,1),date(2025,1,1)))
        self.provenance = dict(feed="IEX fixture",vintage="synthetic",feature_version="v1",universe=["AAPL"],
                               quality_evidence=digest("fixture"))
        self.candidate = dict(specification="trend", code=digest("fit"), feature_schema="v1",
                              fit_protocol="training mean", score_protocol="error")
        self.dataset = self.enroll()

    def enroll(self):
        return register_dataset(self.path,self.frame,scope="equity",features=["momentum"],
                                provenance=self.provenance,horizon_sessions=2)

    def test_folds_and_no_future_fit_or_evaluation(self):
        frame = self.frame.copy()
        frame["label_end"] = frame.label_end.shift(-2).fillna(frame.label_end)
        frame["label_available_at"] = frame.label_end
        ident = register_dataset(self.path,frame,scope="equity",features=["momentum"],provenance=self.provenance,
                                 horizon_sessions=2)
        observed = []
        def fit(x,y):
            observed.append((len(y), x.index[-1]))
            self.assertEqual(list(x.columns), ["momentum"])
            def predict(v):
                self.assertEqual(v.index.get_level_values("timestamp").nunique(),1)
                self.assertLess(x.index[-1][1], v.index[0][1])
                self.assertGreaterEqual(v.momentum.iloc[0]-x.momentum.iloc[-1], 5)
                return np.full(len(v), y.mean())
            return predict
        result = walk_forward(self.path,ident,frame,fit,lambda y, p: float(abs(y-p).mean()),candidate=self.candidate)
        self.assertEqual(result["status"],"historical_simulated")
        folds = result["folds"]
        self.assertEqual(len(folds),6)
        self.assertEqual(len({len(f["validation"]) for f in folds}),1)
        self.assertTrue(all(a[0]<b[0] for a,b in zip(observed,observed[1:])))
        self.assertLess(folds[-1]["validation"][-1],frame.index[-30][1].date().isoformat())
        self.assertEqual(walk_forward(self.path,ident,frame,None,None,candidate=self.candidate),result)

    def test_missing_history_or_fit_failure_has_no_claim(self):
        ident = register_dataset(self.path,self.frame.iloc[-80:],scope="equity",features=["momentum"],
                                 provenance=self.provenance,horizon_sessions=1)
        self.assertEqual(walk_forward(self.path,ident,self.frame.iloc[-80:],None,None,
                         candidate=self.candidate)["status"],"inconclusive")
        result = walk_forward(self.path,self.dataset,self.frame,None,None,candidate=self.candidate)
        self.assertEqual(result["status"],"invalid")

    def test_future_feature_horizon_and_revision(self):
        self.frame["available_at"] += pd.Timedelta(seconds=1)
        with self.assertRaisesRegex(ValueError,"look-ahead"):
            self.enroll()
        self.frame["available_at"] -= pd.Timedelta(seconds=1)
        self.frame["position_end"] = self.frame.position_end.shift(-4).fillna(self.frame.position_end)
        with self.assertRaisesRegex(ValueError,"exceeds"):
            self.enroll()
        family = {digest(self.candidate):self.candidate}
        with self.assertRaisesRegex(ValueError,"revision"):
            inspect_holdout(self.path,self.dataset,self.frame,family,None,None)

    def test_reserved_dates_and_family_consumed_even_on_error(self):
        dev, held = development_windows(self.path,[self.frame.iloc[:-30],self.frame.iloc[-30:]])
        self.assertEqual(len(dev),len(self.frame)-30)
        self.assertTrue(held.empty)
        family = {digest(self.candidate):self.candidate}
        def fail(f,training,labels):
            def predict(rows):
                self.assertLessEqual(len(rows),30)
                raise RuntimeError("simulated failure after reveal")
            return predict
        report = inspect_holdout(self.path,self.dataset,self.frame,family,fail,None)
        self.assertEqual(report["status"],"inconclusive")
        self.assertEqual(inspect_holdout(self.path,self.dataset,self.frame,family,None,None),report)
        other = {**self.candidate,"specification":"revised"}
        with self.assertRaisesRegex(ValueError,"consumed"):
            inspect_holdout(self.path,self.dataset,self.frame,{digest(other):other},None,None)
        self.provenance["vintage"] = "revised"
        with self.assertRaisesRegex(ValueError,"consumed"):
            self.enroll()

    def test_holdout_success_is_a_historical_result_only(self):
        family = {digest(self.candidate): self.candidate}
        seen = []
        def fit(specification, training, labels):
            self.assertEqual(specification,self.candidate)
            self.assertLess(training.index[-1][1],self.frame.index[-31][1])
            def predict(rows):
                seen.append(rows.index[0])
                return np.array([labels.mean()])
            return predict
        result = inspect_holdout(self.path,self.dataset,self.frame,family,fit,lambda y,p: float(abs(y-p).mean()))
        self.assertEqual(result["status"],"historical_simulated")
        self.assertEqual(seen,list(self.frame.index[-30:]))
        claim = dict(family=result["family"],candidate=digest(self.candidate),
                     value=result["results"][digest(self.candidate)]["metric"])
        self.assertTrue(supported_claim(self.path,"historical_holdout_metric",**claim))
        self.assertFalse(supported_claim(self.path,"historical_holdout_metric",**{**claim,"value":999.}))
        self.assertFalse(supported_claim(self.path,"registered_prospective_superiority",family=result["family"]))

    def test_previously_seen_dates_cannot_become_pristine(self):
        with self.assertRaisesRegex(ValueError,"conflicting partition"):
            register_dataset(self.path,self.frame.iloc[:-30],scope="equity",features=["momentum"],
                             provenance=self.provenance,horizon_sessions=1)


class Tournament(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = init(Path(tmp.name)/"ledger.sqlite")
        self.frame = panel(equity_sessions(date(2024,8,1),date(2025,3,1)))
        ident = register_dataset(self.path,self.frame,scope="equity",features=["momentum"],
                    provenance=dict(feed="test",vintage="original",feature_version="1",universe=["AAPL"],
                                    quality_evidence=digest("fixture")),horizon_sessions=1)
        self.model = design(0.,.8,.001,2,42)
        common = {k:digest(k) for k in ("code","feature_schema","risk_policy","execution_policy","cost_policy",
                                       "producer","verifier")}
        common.update(base_fill_bps=1.5,universe=["AAPL"],benchmark="SPY",delay_quotes=1,gap_atr=2.,
                      minimum_effect=.001,power_target=.8,null_model=digest(self.model))
        incumbent = spec("trend")
        self.proposal = dict(spec=spec("momentum"),parent=digest(incumbent),feature=None,hypothesis="whole_policy")
        self.kw = dict(first=date(2025,4,1),dataset=ident,common=common,incumbent=incumbent,baseline=spec("breakout"),
                       challengers=[self.proposal],routing={"generation":7,"global":digest(incumbent),"target":"global",
                                "scopes":dict.fromkeys(["trend_up","trend_down","range_quiet","vol_expansion"])},seed=771)
        self.now = equity_sessions(date(2025,3,31),date(2025,4,1))[0].opened
        self.kw["clock"] = lambda:self.now

    def start(self):
        cid = register(self.path,**self.kw)
        m = frozen_campaign(self.path,cid)
        return cid,m,next(iter(m["candidates"]))

    def fill(self,cid,m,cand,*,missing=None, dip=False):
        totals = {}
        panels = {}
        for i,(day,op,cl) in enumerate(m["calendar"]):
            def portfolio(arm,increment,active):
                prev = totals.get(arm,10.)
                end = prev+increment
                totals[arm] = end
                marks = [[tick,prev] for tick in range(op,cl,3600)] + [[cl,end]]
                if dip and arm == cand and i == 4:
                    marks[1][1] = prev*.89
                return dict(marks=marks, episodes=[[op+1,cl-1,"AAPL",digest([arm, i])] ] if active else [],
                            by_regime={"trend_up":increment},by_symbol={"AAPL":increment},turnover=2. if active else 0.)
            portfolios = {arm:portfolio(arm, (.015 + .002*np.sin(i)).item() if arm == cand else 0., arm == cand)
                          for arm in m["arms"]}
            scenarios = {cand:{s:portfolio(s,.005,True) for s in m["scenarios"][cand]}}
            payload = dict(portfolios=portfolios,scenarios=scenarios,origin="prospective_shadow",
                           benchmark_return=.001,event_digest=digest(day),regime_seconds={"trend_up":cl-op})
            payload["replay_inputs"] = replay_inputs(m, payload["event_digest"])
            if day != missing:
                record_session(self.path,cid,day,payload,clock=lambda:cl+1)
            panels[day] = payload
        return panels

    def finish(self,cid,m):
        return close_campaign(self.path,cid,clock=lambda:m["calendar"][-1][2]+121)

    def checks(self,cid,m,panels,calibration):
        att = {"passed":True,"artifact":digest("verified test artifact")}
        record_verification(self.path,cid,replay_panels={d:digest(p) for d,p in panels.items()},
                            producer=m["common"]["producer"],verifier=m["common"]["verifier"],
                            safety=att,data_quality=att,assumptions=att,calibration=calibration,clock=lambda:m["calendar"][-1][2]+60)

    def test_calendar_budget_retries_cross_scope_identity_and_endpoint(self):
        cid,m,cand = self.start()
        self.assertEqual(len(m["calendar"]),30)
        self.assertNotIn("2025-04-18",[d for d,_,_ in m["calendar"]])
        self.assertEqual(register(self.path,**self.kw),cid)
        self.now = m["calendar"][0][1]
        with self.assertRaisesRegex(ValueError,"after start"):
            register(self.path,**{**self.kw,"seed":123})
        self.now -= 1
        for seed in range(11):
            register(self.path,**{**self.kw,"seed":seed})
        with self.assertRaisesRegex(ValueError,"budget exhausted"):
            register(self.path,**{**self.kw,"seed":123})
        with self.assertRaisesRegex(ValueError,"still open"):
            close_campaign(self.path,cid,clock=lambda:m["calendar"][-1][2])
        result = self.finish(cid,m)
        self.assertEqual(len(result["missing_sessions"]),30)
        self.assertFalse(verify_eligibility(self.path,result["certificates"][cand],candidate=cand,
                                          routing=m["routing"],parent=self.proposal["parent"]))

    def test_identity_dedup_ablation_and_cap(self):
        cid,_,cand = self.start()
        self.proposal["spec"]["source"] = spec("momentum or (momentum and volume)")["source"]
        with self.assertRaisesRegex(ValueError,"equivalent"):
            register(self.path,**{**self.kw,"challengers":[self.proposal,{**self.proposal,"spec":spec("momentum")}]})
        self.proposal["spec"]["guard"] = "volume"
        self.proposal["hypothesis"] = "guard"
        _,m,new = self.start()
        self.assertNotEqual(new,cand)
        self.assertEqual(m["arms"][new+"/ablation"]["guard"],None)
        self.assertEqual(len(m["family"]),4)
        self.assertEqual(m["scenarios"][new]["cost"]["fill_bps"],3.5)
        self.proposal.update(hypothesis="feature", feature="momentum")
        _,m,f = self.start()
        self.assertEqual(neighbors({"fast_ema":10}),{"neighbor/fast_ema/20":{"fast_ema":20}})
        self.proposal["spec"]["parameters"] = {"fast_ema":10}
        with self.assertRaisesRegex(ValueError,"unregistered policy_probes"):
            self.start()
        self.proposal["spec"]["parameters"] = {}
        with self.assertRaisesRegex(ValueError,"four"):
            register(self.path,**{**self.kw,"challengers":[self.proposal]*5})
        many = [{**self.proposal,"spec":dict(spec(x),guard="volume"),"feature":x} for x in
                ("hammer","quiet","resistance","breakout")]
        with self.assertRaisesRegex(ValueError,"twelve"):
            register(self.path,**{**self.kw,"challengers":many})

    def test_incomplete_and_late_results_are_terminal(self):
        cid,m,cand = self.start()
        day = m["calendar"][-1][0]
        panels = self.fill(cid,m,cand,missing=day)
        r = self.finish(cid,m)
        self.assertEqual(r["missing_sessions"],[day])
        with self.assertRaisesRegex(ValueError,"timely"):
            record_session(self.path,cid,day,panels[day],clock=lambda:m["calendar"][-1][2]+121)
        self.assertEqual(self.finish(cid,m),r)

    def test_comparisons_without_calibration_cannot_validate(self):
        cid,m,cand = self.start()
        panels = self.fill(cid,m,cand,dip=True)
        r = self.finish(cid,m)
        self.assertGreater(r["portfolios"][cand]["max_drawdown"],.1)
        claim = dict(campaign=cid,candidate=cand,value=r["portfolios"][cand]["net_pnl"])
        self.assertTrue(supported_claim(self.path,"prospective_shadow_net_pnl",**claim))
        self.assertFalse(supported_claim(self.path,"broker_confirmed_gross_lot_pnl",**claim))
        self.assertEqual(len(r["comparisons"]),3)
        self.assertTrue(all(v["holm_p"] < CAMPAIGN_ALPHA for v in r["comparisons"].values()))
        self.assertTrue(all(len(v["daily_increment"]) == 30 for v in r["comparisons"].values()))
        with db(self.path) as c:
            cert = needed(c,"certificate",r["certificates"][cand])
        self.assertIn("net_performance_or_drawdown",cert["reasons"])
        self.assertIn("missing_independent_verification",cert["reasons"])
        self.assertEqual(cert["status"],"inconclusive")
        self.assertFalse(supported_claim(self.path,"broker_confirmed_profit",certificate=r["certificates"][cand]))
        copy_panel = copy.deepcopy(panels[m["calendar"][0][0]])
        copy_panel["origin"] = "historical_simulated"
        with self.assertRaisesRegex(ValueError,"provenance"):
            record_session(self.path,cid,m["calendar"][0][0],copy_panel)
        with db(self.path,True) as c, self.assertRaises(sqlite3.IntegrityError):
            c.execute("UPDATE experiment_records SET body='{}' WHERE kind='certificate'")

    def test_real_calibration_insufficient_despite_large_effect(self):
        cid,m,cand = self.start()
        panels = self.fill(cid,m,cand)
        study = register_calibration(self.path,self.model,3,.001,771)
        self.checks(cid,m,panels,study)
        report = self.finish(cid,m)
        with db(self.path) as c:
            cert = needed(c,"certificate",report["certificates"][cand])
        self.assertEqual(cert["reasons"],["insufficient_calibration_or_power", statistical_release.BLOCKER])
        self.assertGreater(upper_bound(0,32),CAMPAIGN_ALPHA)

    def verified_fixture(self):
        cid,m,cand = self.start()
        panels = self.fill(cid,m,cand)
        study = dict(design=digest(self.model),family_size=3,effect=.001,bootstrap_seed=771,
                     implementation=m["protocol"]["implementation"],null_upper=.001,power_lower=.9)
        with db(self.path,True) as c:
            append(c,"calibration","test-double",study)
        self.checks(cid,m,panels,"test-double")
        return cid,m,cand

    def test_supervisor_contract_with_synthetic_verifier_fixture(self):
        cid,m,cand = self.verified_fixture()
        report = self.finish(cid,m)
        with db(self.path) as c:
            cert = needed(c,"certificate",report["certificates"][cand])
        self.assertEqual(cert["status"],"inconclusive")
        self.assertIn(statistical_release.BLOCKER,cert["reasons"])
        self.assertFalse(verify_eligibility(self.path,report["certificates"][cand],candidate=cand,
                         routing=m["routing"],parent=self.proposal["parent"]))

    def test_old_eligible_certificate_is_blocked_at_consumption(self):
        cid,m,cand = self.verified_fixture()
        # Simulate a decision issued while a protocol was released, then revoke it.
        with patch.object(statistical_release,"RELEASED_PROTOCOLS",frozenset({digest(m["protocol"])})):
            report = self.finish(cid,m)
        args = dict(certificate=report["certificates"][cand],candidate=cand,
                    routing=m["routing"],parent=self.proposal["parent"])
        with db(self.path) as c:
            self.assertEqual(needed(c,"certificate",args["certificate"])["status"],"eligible")
            before = c.execute("SELECT * FROM strategies ORDER BY id").fetchall()
        self.assertFalse(verify_eligibility(self.path,**args))
        self.assertFalse(supported_claim(self.path,"registered_prospective_superiority",**args))
        with db(self.path) as c:
            self.assertEqual(c.execute("SELECT * FROM strategies ORDER BY id").fetchall(),before)
            self.assertEqual(c.execute("SELECT count(*) FROM intents").fetchone()[0],0)
        with patch.object(statistical_release,"RELEASED_PROTOCOLS",frozenset({digest(m["protocol"])})):
            self.assertTrue(supported_claim(self.path,"registered_prospective_superiority",**args))
            for change in ({"routing":{}},{"parent":"stale"},{"candidate":"swapped"},{"certificate":"missing"}):
                self.assertFalse(supported_claim(self.path,"registered_prospective_superiority",**{**args,**change}))
        self.assertFalse(supported_claim(self.path,"future_profit",**args))
        with db(self.path) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM strategies WHERE status='champion'").fetchone()[0],0)

    def _assert_restored_statistical_ledger(self, backup_and_restore):
        cid,m,cand = self.verified_fixture()
        report = self.finish(cid,m)
        family_spec = {"specification":"disposable fixture"}
        family = {digest(family_spec):family_spec}
        dataset = self.kw["dataset"]

        def fit(specification, training, labels):
            raise RuntimeError("fixture failure after the holdout charge")

        historical = inspect_holdout(self.path,dataset,self.frame,family,fit,None)
        self.assertEqual(historical["status"],"inconclusive")
        for seed in range(11):
            register(self.path,**{**self.kw,"seed":seed})

        # Capture committed data while the WAL also contains an uncommitted write.
        with closing(sqlite3.connect(self.path, isolation_level=None)) as writer:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            append(writer,"restore_probe","committed",{"value":1})
            expected = writer.execute("SELECT * FROM experiment_records ORDER BY kind,id").fetchall()
            self.assertTrue(Path(f"{self.path}-wal").stat().st_size > 32)
            writer.execute("BEGIN IMMEDIATE")
            append(writer,"restore_probe","uncommitted",{"value":2})
            restored = backup_and_restore()
            writer.rollback()
        with db(restored) as c:
            self.assertEqual([tuple(row) for row in c.execute(
                "SELECT * FROM experiment_records ORDER BY kind,id")],expected)
            for kind,identity,_,_ in expected:
                needed(c,kind,identity)  # Verify the content digest of every record.
            self.assertEqual(c.execute("SELECT count(*) FROM experiment_records WHERE kind='budget'").fetchone()[0],12)
            self.assertEqual(c.execute("SELECT count(*) FROM experiment_records WHERE kind='consumption'").fetchone()[0],30)
        self.assertEqual(frozen_campaign(restored,cid),m)
        self.assertEqual(close_campaign(restored,cid),report)
        self.assertFalse(verify_eligibility(restored,report["certificates"][cand],candidate=cand,
                         routing=m["routing"],parent=self.proposal["parent"]))
        self.assertEqual(register(restored,**self.kw),cid)
        with self.assertRaisesRegex(ValueError,"budget exhausted"):
            register(restored,**{**self.kw,"seed":122})
        self.assertEqual(inspect_holdout(restored,dataset,self.frame,family,None,None),historical)
        revised = {"specification":"second attempt"}
        with self.assertRaisesRegex(ValueError,"consumed holdout"):
            inspect_holdout(restored,dataset,self.frame,{digest(revised):revised},None,None)
        for sql in ("UPDATE experiment_records SET body='{}' WHERE kind='campaign'",
                    "DELETE FROM experiment_records WHERE kind='budget'",
                    "INSERT OR REPLACE INTO experiment_records SELECT * FROM experiment_records WHERE kind='consumption'"):
            with self.subTest(sql=sql), db(restored,True) as c, self.assertRaisesRegex(
                    sqlite3.IntegrityError,"immutable experiment record"):
                c.execute(sql)

    def test_sqlite_backup_restore_preserves_statistical_ledger(self):
        def backup_and_restore():
            snapshot = copy_verified(self.path,self.path.with_name("snapshot.sqlite"))
            return copy_verified(snapshot,self.path.with_name("restore.sqlite"))

        self._assert_restored_statistical_ledger(backup_and_restore)

    @unittest.skipUnless(find_spec("backup_sets"),"requires Agent 5 backup sets")
    def test_agent5_backup_set_restore_preserves_statistical_ledger(self):
        from backup_sets import backup, restore, pair_state

        with closing(Ledger(self.path.with_name("learning_loop.sqlite"))) as journal:
            bind_store(self.path,journal)

        def backup_and_restore():
            root = self.path.parent / "sets"
            root.mkdir()
            snapshot = backup(self.path,root,"a"*40)
            destination = self.path.parent / "recovered"
            restore(snapshot,destination)
            restored = destination / "operational.sqlite"
            self.assertEqual(pair_state(restored,destination / "learning_loop.sqlite"),
                             pair_state(self.path,self.path.with_name("learning_loop.sqlite")))
            return restored

        self._assert_restored_statistical_ledger(backup_and_restore)

    @unittest.skipUnless(find_spec("marked_session"),"requires Agent 1 canonical adapter")
    def test_agent1_replay_through_registered_campaign_and_sealed_decision(self):
        from marked_session import SessionTape, replay_marked_session
        from portfolio_events import Decision, MarketFrame, Quote

        cid,m,cand = self.start()
        candidate_spec = digest(m["arms"][cand])
        tapes = {}
        for i,(day,opened,close) in enumerate(m["calendar"]):
            op = datetime.fromtimestamp(opened,timezone.utc)
            cl = datetime.fromtimestamp(close,timezone.utc)
            cut = op + timedelta(hours=1)
            times = {t:100 for t in range(opened,close,3600)}
            times[close] = 100
            signals = {digest(spec): {} for spec in m["arms"].values()}
            if i == 0:
                signals[candidate_spec][cut] = (Decision("AAPL",candidate_spec,op,cut,cut,cut,1,Decimal(1)),)
                times[opened+3602] = 100
                times[opened+3603] = 100
                times[opened+3604] = 98
            frames = tuple(
                MarketFrame(at,op,cl,quotes=(Quote("AAPL",at,Decimal(price),Decimal(price)),))
                for stamp,price in sorted(times.items())
                for at in (datetime.fromtimestamp(stamp,timezone.utc),)
            )
            tapes[day] = SessionTape(frames,signals,((opened,"trend_up"),),Decimal(100),Decimal(101))
            payload = replay_marked_session(m,day,tapes)
            record_session(self.path,cid,day,payload,clock=lambda:close+1)

        # 0.01 shares at 100 then 98, with 1.5 bp modeled fees on both notionals.
        net = -.02 - 1.98*.00015
        report = self.finish(cid,m)
        self.assertEqual(report["missing_sessions"],[])
        portfolio = report["portfolios"][cand]
        self.assertEqual(portfolio["episodes"],1)
        self.assertAlmostEqual(portfolio["net_pnl"],net)
        self.assertAlmostEqual(portfolio["turnover"],1.98)
        self.assertAlmostEqual(portfolio["by_symbol"]["AAPL"],net)
        self.assertAlmostEqual(portfolio["max_drawdown"],-net/10)
        self.assertLess(report["sensitivity"][cand]["cost"]["net_pnl"],net)
        result = report["comparisons"][cand+":cash"]
        self.assertEqual(result["differing_episodes"],1)
        self.assertEqual(result["status"],"inconclusive")
        self.assertAlmostEqual(result["daily_increment"][0],(net-.0002*1.98)/10)
        self.assertEqual(result["daily_increment"][1:],[0.]*29)
        with db(self.path) as c:
            cert = needed(c,"certificate",report["certificates"][cand])
        self.assertIn(statistical_release.BLOCKER,cert["reasons"])
        self.assertFalse(verify_eligibility(self.path,report["certificates"][cand],candidate=cand,
                         routing=m["routing"],parent=self.proposal["parent"]))

    def test_shared_market_correlation_is_not_an_increment(self):
        from campaign_statistics import paired_result
        idx = [date(2025,1,1)+timedelta(days=i) for i in range(30)]
        factor = dict(zip(idx,np.linspace(.01,.06,30)))
        r = paired_result(idx,factor,factor,dict.fromkeys(idx,0.),10.,30,30,17)
        self.assertEqual(r["reason"],"degenerate_series")
        same = [[1,5,"A",digest(1)]]
        self.assertEqual(differing_count(same,same),0)
        self.assertEqual(differing_count(same,[[1,3,"A",digest(2)],[3,8,"A",digest(3)]]),1)


    def test_global_budget_race_between_different_families(self):
        from concurrent.futures import ThreadPoolExecutor
        for i in range(11):
            register(self.path,**{**self.kw,"seed":i})
        def reserve(i):
            try:
                return register(self.path,**{**self.kw,"seed":i})
            except ValueError as error:
                self.assertIn("budget exhausted",str(error))
                return None
        with ThreadPoolExecutor(max_workers=2) as worker:
            accepted = list(worker.map(reserve,[32,33]))
        self.assertEqual(sum(x is not None for x in accepted),1)
        with db(self.path) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM experiment_records WHERE kind='budget'").fetchone()[0],12)

    def test_crypto_calendar_uses_same_utc_budget_as_equity(self):
        from learning_loop import Session
        ts = pd.date_range("2024-09-01",periods=100,tz="UTC")
        days = [Session(t.date().isoformat(),int(t.timestamp()),(int(t.timestamp())+86400,)) for t in ts]
        frame = panel(days).rename(index={"AAPL":"ETH/USD"},level="symbol")
        dataset = register_dataset(self.path,frame,scope="crypto",features=["momentum"],horizon_sessions=2,
                    provenance=dict(feed="crypto",vintage="test",feature_version="1",universe=["ETH/USD"],
                                    quality_evidence=digest("quality")))
        kw = {**self.kw,"dataset":dataset,"scope":"crypto",
              "common":{**self.kw["common"],"universe":["ETH/USD"],"benchmark":"BTC/USD"}}
        with self.assertRaisesRegex(ValueError,"first confirmatory"):
            register(self.path,**kw)
        self.start()
        for seed in range(11):
            cid = register(self.path,**{**kw,"seed":seed})
        m = frozen_campaign(self.path,cid)
        self.assertEqual(m["calendar"][-1][2]-m["calendar"][0][1],30*86400)
        with self.assertRaisesRegex(ValueError,"budget exhausted"):
            register(self.path,**{**self.kw,"seed":56})

    def test_frozen_inputs_wrong_mark_grid_and_automatic_expiry(self):
        from campaign_evidence import expire_campaigns
        cid,m,cand = self.start()
        day,_,cl = m["calendar"][0]
        panels = self.fill(cid,m,cand,missing=day)
        p = copy.deepcopy(panels[day])
        p["replay_inputs"]["portfolios"][cand] = digest("parent source substituted")
        with self.assertRaisesRegex(ValueError,"identity"):
            record_session(self.path,cid,day,p,clock=lambda:cl)
        p = copy.deepcopy(panels[day])
        p["scenarios"][cand]["gap"]["marks"].pop(2)
        with self.assertRaisesRegex(ValueError,"grid"):
            record_session(self.path,cid,day,p,clock=lambda:cl)
        self.assertEqual(expire_campaigns(self.path,clock=lambda:m["calendar"][-1][2]),[])
        self.assertEqual(expire_campaigns(self.path,clock=lambda:m["calendar"][-1][2]+90000),[cid])
        self.assertEqual(expire_campaigns(self.path,clock=lambda:m["calendar"][-1][2]+90001),[])

    def test_replay_mismatch_and_receipt_retry(self):
        cid,m,cand = self.start()
        panels = self.fill(cid,m,cand)
        day,_,cl = m["calendar"][0]
        record_session(self.path,cid,day,panels[day],clock=lambda:cl+1000)
        panels[day]["event_digest"] = digest("different feed revision")
        study = register_calibration(self.path,self.model,3,.001,771)
        self.checks(cid,m,panels,study)
        report = self.finish(cid,m)
        with db(self.path) as c:
            cert = needed(c,"certificate",report["certificates"][cand])
        self.assertIn("independent_replay_disagrees",cert["reasons"])
        self.assertEqual(cert["status"],"inconclusive")

    def test_parameter_duplicates_use_frozen_behavioral_probes(self):
        from historical_validation import register_probes
        frame = panel(equity_sessions(date(2024,8,1),date(2025,3,1)))
        self.proposal["spec"]["parameters"] = {"fast_ema":10}
        alternate = copy.deepcopy(self.proposal)
        alternate["spec"]["parameters"]["fast_ema"] = 20
        self.kw["challengers"].append(alternate)
        universe = [dict(spec("trend"),source=SEED.split(":")[0]+":\n    return 0"),self.kw["incumbent"],
                    self.kw["baseline"],self.proposal["spec"],alternate["spec"]]
        def evaluate(s, data):
            self.assertEqual(list(data.columns),["momentum"])
            self.assertLess(data.index[-1][1], frame.index[-30][1])
            if s["source"].endswith("return 0"):
                return np.zeros(len(data))
            mod = 2 if s == self.kw["incumbent"] else 3 if s == self.kw["baseline"] else 4
            return data.momentum.to_numpy() % mod == 0
        for s in universe:
            register_probes(self.path,self.kw["dataset"],frame,s,self.kw["common"]["code"],
                            self.kw["common"]["feature_schema"],evaluate)
        with self.assertRaisesRegex(ValueError,"equivalent"):
            self.start()
        self.kw["challengers"].pop()
        _,m,cand = self.start()
        self.assertEqual(m["scenarios"][cand]["neighbor/fast_ema/20"],{"fast_ema":20})


class IndependentChecks(unittest.TestCase):
    def test_reference_bootstrap_draws_and_holm(self):
        from campaign_statistics import paired_result, holm, BLOCK_LENGTHS, RESAMPLES
        days = [date(2025,1,1)+timedelta(days=i) for i in range(30)]
        x = np.random.default_rng(83157).normal(.0002,.001,30)
        cash = dict.fromkeys(days,0.)
        result = paired_result(days,dict(zip(days,x)),cash,cash,1.,20,10,17)
        rng = np.random.default_rng(17)
        for length in BLOCK_LENGTHS:
            total = 0
            for offset in range(0,RESAMPLES,500):
                starts = rng.integers(0,30,size=(min(500,RESAMPLES-offset),30//length))
                indices = (starts[:,:,None]+np.arange(length))%30
                draws = x[indices].mean(axis=(1,2))-x.mean()
                total += (draws>=x.mean()).sum()
            self.assertEqual(result["block_p"][length],(1+total)/(RESAMPLES+1))
        adjusted = holm(("x","y","z"),{"y":{"status":"measured","p":.1},"z":{"status":"measured","p":.101}})
        self.assertEqual(adjusted["x"],1.)
        self.assertAlmostEqual(adjusted["y"],.3)
        self.assertAlmostEqual(adjusted["z"],.3)

    def test_drift_fixed_windows_and_complete_lot_attribution(self):
        from datetime import datetime,timezone
        from rule_review import drift
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = init(Path(tmp.name)/"ledger.sqlite")
        epoch = datetime(1970,1,1,tzinfo=timezone.utc)
        end = epoch + timedelta(days=28*710)
        with db(path,True) as c:
            c.execute("INSERT INTO strategies(id,status) VALUES('s','champion')")
            for i in range(40):
                t = end - timedelta(days=20 if i < 20 else 2)
                c.execute("INSERT INTO lots(id,strategy,qty) VALUES(?,?,?)",(str(i),"s","0"))
                c.execute("INSERT INTO outcomes(id,lot,strategy,gross,at) VALUES(?,?,?,?,?)",
                          (str(i),str(i),"s",1. if i < 20 else .4,t.isoformat()))
            c.execute("INSERT INTO outcomes(id,lot,strategy,gross,at) VALUES('partial','0','s',.5,?)",
                      ((end-timedelta(days=40)).isoformat(),))
        drift(path,as_of=end)
        drift(path,as_of=end+timedelta(days=1))
        with db(path) as c:
            key = c.execute("SELECT id FROM experiment_records WHERE kind='drift_review'").fetchone()[0]
            review = needed(c,"drift_review",key)
            self.assertEqual(review["counts"],[20,20])
            self.assertEqual(review["status"],"investigate")
            self.assertAlmostEqual(review["gross_means"][0],1.025)
            self.assertEqual(c.execute("SELECT count(*) FROM reflections").fetchone()[0],1)
            self.assertEqual(c.execute("SELECT status FROM strategies").fetchone()[0],"champion")
