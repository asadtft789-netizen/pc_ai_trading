"""Fixed-endpoint campaign adjudication. No order, routing write or activation operation."""
from datetime import date
import math
import time

from campaign_registry import frozen_campaign, protocol, sha
from campaign_statistics import CAMPAIGN_ALPHA, holm, paired_result
from experiment_store import append, needed, read
from learning_loop import digest, require
from paper_store import db


def differing_count(left, right):
    """Maximal overlapping excursions away from joint flat; count each divergent excursion once."""
    events = sorted([(a,b,s,h,0) for a,b,s,h in left] + [(a,b,s,h,1) for a,b,s,h in right])
    paths = [[], []]
    last = -1
    count = 0
    for a,b,s,h,arm in events:
        if a >= last:
            count += paths[0] != paths[1]
            paths = [[], []]
        paths[arm].append((a,b,s,h))
        last = max(last, b)
    return count + int(paths[0] != paths[1])


def _pnl(row):
    marks = row["marks"]
    return marks[-1][1] - marks[0][1]


def _series(rows, calendar, capital):
    prev = peak = capital
    dd = 0.
    days = {}
    attribution = {"by_symbol": {}, "by_regime": {}}
    profits = []
    exposure = {}
    total_seconds = 0
    for d, row in zip(calendar, rows, strict=True):
        if not math.isclose(prev, row["marks"][0][1], rel_tol=0., abs_tol=1e-10):
            raise ValueError("discontinuous allocated marked equity")
        for _, mark in row["marks"]:
            peak = max(peak, mark)
            dd = max(dd, 1-mark/peak)
        prev = row["marks"][-1][1]
        total_seconds += row["marks"][-1][0] - row["marks"][0][0]
        for start,end,symbol,_ in row["episodes"]:
            exposure[symbol] = exposure.get(symbol,0) + end-start
        days[d] = _pnl(row)
        profits.append(max(days[d], 0))
        for k, values in attribution.items():
            for label, value in row[k].items():
                values[label] = values.get(label, 0) + value
    return days, dict(net_pnl=prev-capital, max_drawdown=dd, turnover=sum(r["turnover"] for r in rows),
                      episodes=sum(len(r["episodes"]) for r in rows), **attribution,
                      exposure_seconds_by_symbol=exposure, time_in_market=sum(exposure.values())/total_seconds,
                      largest_positive_day_share=max(profits)/sum(profits) if sum(profits) else None)


def record_verification(path, identity, *, replay_panels, producer, verifier, data_quality, safety,
                        calibration, assumptions, clock=time.time):
    """Integration boundary for independent, artifact-backed verification of the sealed input.

    Digests reference retained artifacts from trusted adapters. The verification consumer is
    responsible for authenticating writers and for verifying artifacts before invoking this.
    Omitted or failed attestations cannot be promoted.
    """
    m = frozen_campaign(path, identity)
    require(producer == m["common"]["producer"] and verifier == m["common"]["verifier"] and producer != verifier, "independent replay required")
    require(set(replay_panels) == {d for d,_,_ in m["calendar"]} and all(sha(h) for h in replay_panels.values()),
            "full independent replay required")
    for receipt in (data_quality, safety, assumptions):
        require(set(receipt) == {"passed", "artifact"} and type(receipt["passed"]) is bool and sha(receipt["artifact"]),
                "missing verification artifact")
    with db(path, True) as c:
        require(clock() <= m["calendar"][-1][2] + m["protocol"]["verification_seconds"], "verification deadline passed")
        require(read(c, "decision", identity) is None, "campaign already closed")
        study = needed(c, "calibration", calibration)
        require(study["design"] == m["common"]["null_model"] and study["family_size"] == len(m["family"])
                and study["bootstrap_seed"] == m["seed"] and study["effect"] == m["common"]["minimum_effect"] and study["implementation"] ==
                m["protocol"]["implementation"], "different calibration design")
        for d,_,_ in m["calendar"]:
            needed(c, "shadow_session", identity + "/" + d)
        append(c, "verification", identity, dict(replay=replay_panels, producer=producer, verifier=verifier,
               data_quality=data_quality, safety=safety, calibration=calibration, assumptions=assumptions))


def _measure(m, panels):
    days = [date.fromisoformat(d) for d,_,_ in m["calendar"]]
    capital = m["protocol"]["capital"]
    cash = dict.fromkeys(days, 0.)
    arms = {}
    daily = {"cash": cash, "incumbent": cash}
    stats = {}
    for arm in m["arms"]:
        arms[arm] = [p["portfolios"][arm] for p in panels]
        daily[arm], stats[arm] = _series(arms[arm], days, capital)
    results = {}
    for name, (cand, control) in m["family"].items():
        rows = arms[cand]
        other = arms.get(control)
        different = sum(differing_count(row["episodes"], other[i]["episodes"] if other else [])
                        for i,row in enumerate(rows))
        turnover = {d:r["turnover"] for d,r in zip(days, rows)}
        result = paired_result(days, daily[cand], daily[control], turnover, capital, stats[cand]["episodes"],
                               different, m["seed"])
        delta = [(daily[cand][d] - daily[control][d] - .0002*turnover[d])/capital for d in days]
        result.update(differing_episodes=different, daily_increment=delta,
                      thirds=[sum(delta[i:i+10]) for i in range(0,30,10)])
        if "block_p" in result:
            result["block_p"] = {str(k): v for k, v in result["block_p"].items()}
        results[name] = result
    adjusted = holm(m["family"], results)
    for name in results:
        results[name]["holm_p"] = adjusted[name]
    diagnostics = {}
    for cand, scenarios in m["scenarios"].items():
        diagnostics[cand] = {}
        for s in scenarios:
            rows = [p["scenarios"][cand][s] for p in panels]
            _, diagnostics[cand][s] = _series(rows, days, capital)
    # Descriptive distribution/performance shift at the fixed endpoint. No interim looks.
    halves = [panels[:15],panels[15:]]
    mix = []
    for half in halves:
        coverage = {}
        for panel in half:
            for regime, seconds in panel["regime_seconds"].items():
                coverage[regime] = coverage.get(regime,0) + seconds
        mix.append({k:v/sum(coverage.values()) for k,v in coverage.items()})
    shift = .5 * sum(abs(mix[0].get(k,0)-mix[1].get(k,0)) for k in mix[0].keys() | mix[1].keys())
    for cand in m["candidates"]:
        delta = results[cand+":incumbent"]["daily_increment"]
        prior, recent = sum(delta[:15])/15, sum(delta[15:])/15
        stats[cand]["drift_review"] = dict(origin="prospective_shadow", window_sessions=15,
            regime_mix_total_variation=shift, mean_increments=[prior,recent],
            investigate=(prior > 0 and recent <= .8*prior), action="descriptive_review_only")
    return results, stats, diagnostics


def close_campaign(path, identity, *, clock=time.time):
    m = frozen_campaign(path, identity)
    with db(path) as c:
        old = read(c, "decision", identity)
        if old is not None:
            return old
        require(clock() > m["calendar"][-1][2] + m["protocol"]["close_grace_seconds"], "window still open")
        receipts = [read(c, "shadow_session", identity + "/" + d) for d,_,_ in m["calendar"]]
        checks = read(c, "verification", identity)
        study = needed(c, "calibration", checks["calibration"]) if checks else None
    panels = [r["panel"] for r in receipts if r]
    missing = [d for (d,_,_), r in zip(m["calendar"], receipts) if r is None]
    unknown = []
    if missing:
        unknown.append("incomplete_calendar")
    if m["protocol"] != protocol():
        unknown.append("protocol_implementation_changed")
    comparisons, stats, diagnostics = {}, {}, {}
    if not unknown:
        try:
            comparisons, stats, diagnostics = _measure(m, panels)
        except ValueError as exc:
            unknown.append(str(exc))
    if checks is None:
        unknown.append("missing_independent_verification")
    else:
        if any(digest(r["panel"]) != checks["replay"][d] for (d,_,_),r in zip(m["calendar"],receipts) if r):
            unknown.append("independent_replay_disagrees")
        for field in ("data_quality", "assumptions"):
            if not checks[field]["passed"]:
                unknown.append(field)
        if study["null_upper"] > CAMPAIGN_ALPHA or study["power_lower"] < m["common"]["power_target"]:
            unknown.append("insufficient_calibration_or_power")
    report = dict(campaign=identity, origin="prospective_shadow", missing_sessions=missing,
                  regime_seconds={regime:sum(p["regime_seconds"].get(regime,0) for p in panels)
                                  for regime in sorted({k for p in panels for k in p["regime_seconds"]})},
                  calendar=[d for d,_,_ in m["calendar"]], inputs=[digest(r) if r else None for r in receipts],
                  benchmark={"symbol":m["common"]["benchmark"],
                             "daily_return":[r["panel"]["benchmark_return"] if r else None for r in receipts]},
                  comparisons=comparisons, portfolios=stats, sensitivity=diagnostics, certificates={})
    certs = {}
    for cand in m["candidates"]:
        reasons = unknown.copy()
        if m["scope"] == "crypto":
            reasons.append("crypto_execution_profile_unverified")
        fails = []
        if checks and not checks["safety"]["passed"]:
            fails.append("safety_gate_failed")
        for name, (challenger,control) in m["family"].items():
            if challenger != cand or not comparisons:
                continue
            result = comparisons[name]
            if result["status"] != "measured":
                reasons.append(name + "/" + result["reason"])
            elif result["holm_p"] > CAMPAIGN_ALPHA:
                reasons.append(name + "/superiority_unestablished")
            elif sum(v > 0 for v in result["thirds"]) < 2:
                fails.append(name + "/insufficient_positive_thirds")
        if cand in stats:
            if stats[cand]["net_pnl"] <= 0 or stats[cand]["max_drawdown"] >= .10 - 1e-12:
                fails.append("net_performance_or_drawdown")
            for scenario, x in diagnostics[cand].items():
                if x["net_pnl"] <= 0 or x["max_drawdown"] >= .10 - 1e-12:
                    fails.append("failed_scenario/" + scenario)
                elif scenario.startswith("neighbor/") and x["net_pnl"] < (
                        m["protocol"]["minimum_neighbor_profit_fraction"] * stats[cand]["net_pnl"]):
                    fails.append("material_parameter_sensitivity/" + scenario)
        status = "inconclusive" if reasons else "rejected" if fails else "eligible"
        cert = dict(type="EvidenceCertificate/v1", candidate=cand, campaign=identity, status=status,
                    scope=m["scope"], evidence_class="prospective_shadow", routing=m["routing"],
                    parent=m["candidates"][cand]["parent"], protocol=m["protocol"], endpoint=m["calendar"][-1][2],
                    reasons=reasons + fails, evidence=digest({k:v for k,v in report.items() if k != "certificates"}),
                    verification=digest(checks) if checks else None)
        certs[cand] = cert
        report["certificates"][cand] = digest(cert)
    with db(path, True) as c:
        # Verification may be delivered concurrently with closure. Never certify a stale snapshot.
        if read(c, "decision", identity) is not None:
            return needed(c, "decision", identity)
        require(checks == read(c, "verification", identity), "verification changed; retry close")
        for cand, cert in certs.items():
            append(c, "certificate", report["certificates"][cand], cert)
        append(c, "decision", identity, report)
    return report


def verify_eligibility(path, certificate, *, candidate, routing, parent):
    """Read-only predicate for the supervisor's own next-boundary routing transaction."""
    with db(path) as c:
        cert = needed(c, "certificate", certificate)
        report = needed(c, "decision", cert["campaign"])
        m = needed(c, "campaign", cert["campaign"])
    return (digest(cert) == certificate and cert["status"] == "eligible" and cert["candidate"] == candidate
            and cert["routing"] == routing and cert["parent"] == parent and digest(m) == cert["campaign"]
            and cert["protocol"] == protocol() and report["certificates"].get(candidate) == certificate
            and digest({k:v for k,v in report.items() if k != "certificates"}) == cert["evidence"])


def supported_claim(path, claim, **evidence):
    """Allowlist of structured claims. Free text and historical/broker extrapolations remain unsupported."""
    if claim == "registered_prospective_superiority":
        if set(evidence) != {"certificate","candidate","routing","parent"}:
            return False
        try:
            return verify_eligibility(path, **evidence)
        except ValueError:
            return False
    fields = {"historical_holdout_metric":{"family","candidate","value"},
              "prospective_shadow_net_pnl":{"campaign","candidate","value"},
              "broker_confirmed_gross_lot_pnl":{"lot","strategy","value"}}
    if claim not in fields or set(evidence) != fields[claim]:
        return False
    value = evidence["value"]
    if type(value) not in (float,int) or not math.isfinite(value):
        return False
    if any(not isinstance(v,str) for k,v in evidence.items() if k != "value"):
        return False
    with db(path) as c:
        if claim == "historical_holdout_metric":
            report = read(c,"holdout_result",evidence["family"])
            record = report["results"].get(evidence["candidate"],{}) if report else {}
            measured = record.get("metric") if record.get("status") == "historical_simulated" else None
        elif claim == "prospective_shadow_net_pnl":
            report = read(c,"decision",evidence["campaign"])
            measured = (report["portfolios"].get(evidence["candidate"],{}).get("net_pnl")
                        if report and not report["missing_sessions"] else None)
        else:
            row = c.execute("""SELECT sum(o.gross) FROM outcomes o JOIN lots l ON l.id=o.lot
                JOIN fills f ON f.id=o.id JOIN intents i ON i.id=f.intent
                WHERE o.lot=? AND o.strategy=? AND l.strategy=o.strategy AND cast(l.qty AS REAL)=0
                  AND i.side='sell' AND i.lot=l.id AND i.symbol=o.symbol AND i.broker IS NOT NULL
                HAVING count(*)=(SELECT count(*) FROM outcomes WHERE lot=?)""",
                (evidence["lot"], evidence["strategy"], evidence["lot"])).fetchone()
            measured = row[0] if row else None
    return measured is not None and math.isclose(measured,value,rel_tol=1e-12,abs_tol=1e-12)



def expire_campaigns(path, *, clock=time.time):
    """Research tick closes unresolved registrations after their fixed verification deadline."""
    with db(path) as c:
        pending = [r[0] for r in c.execute("""SELECT c.id FROM experiment_records c
            WHERE c.kind='campaign' AND NOT EXISTS (SELECT 1 FROM experiment_records d
            WHERE d.kind='decision' AND d.id=c.id) ORDER BY c.id""")]
    closed = []
    for identity in pending:
        m = frozen_campaign(path, identity)
        if clock() > m["calendar"][-1][2] + m["protocol"]["verification_seconds"]:
            close_campaign(path, identity, clock=clock)
            closed.append(identity)
    return closed
