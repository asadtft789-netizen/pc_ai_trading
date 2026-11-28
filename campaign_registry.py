"""Prospective equity tournament registrations and immutable, matched closing receipts."""
import ast
from datetime import date, datetime, time as day_time, timedelta, timezone
import hashlib
import math
from pathlib import Path
import re
import time

from campaign_statistics import BLOCK_LENGTHS, CAMPAIGN_ALPHA, RESAMPLES
from experiment_store import append, needed, read
from learning_loop import digest, require
from market_time import equity_sessions
from paper_store import db
from phoenix_tournament import signature
from strategy_worker import FEATURES, validate_source

REGIMES = {"trend_up", "trend_down", "range_quiet", "vol_expansion", "unknown"}
GRIDS = {"fast_ema": (10, 20), "slow_ema": (50, 100), "momentum": (3, 5, 10, 20),
         "volatility": (20, 50, 100), "rsi": (20, 30, 40, 60, 70, 80),
         "relative_volume": (.75, 1, 1.25, 1.5, 2), "distance_atr": (.5, 1, 1.5, 2, 3)}


def sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def protocol():
    sources = ["campaign_registry.py", "campaign_statistics.py", "campaign_evidence.py",
               "phoenix_tournament.py", "strategy_worker.py"]
    return dict(version=1, implementation=digest([hashlib.sha256(Path(__file__).with_name(n).read_bytes()
                    ).hexdigest() for n in sources]), capital=10., alpha=CAMPAIGN_ALPHA, resamples=RESAMPLES,
                blocks=list(BLOCK_LENGTHS), allowance_bps=2, sessions=30, close_grace_seconds=120,
                verification_seconds=86400, minimum_neighbor_profit_fraction=.5)


def neighbors(parameters):
    variants = {}
    for name, value in sorted(parameters.items()):
        require(name in GRIDS and number(value) and value in GRIDS[name], "parameter outside approved grid")
        values = GRIDS[name]
        i = values.index(value)
        for adjacent in values[max(i-1, 0):i] + values[i+1:i+2]:
            variants[f"neighbor/{name}/{adjacent}"] = {**parameters, name: adjacent}
    return variants


def policy(spec):
    require(set(spec) == {"source", "guard", "parameters", "weights"} and
            isinstance(spec["parameters"], dict) and spec["weights"] == {}, "unsupported strategy specification")
    require(spec["guard"] is None or spec["guard"] in FEATURES, "unknown guard")
    neighbors(spec["parameters"])
    return digest([hashlib.sha256(signature(spec["source"], spec["guard"])).hexdigest(),
                   spec["parameters"]])


def register(path, *, first, dataset, common, incumbent, baseline, challengers, routing, seed, scope="equity", clock=time.time):
    """Register full specs and all required comparisons atomically before the first open.

    A baseline is always frozen in this equity protocol. Identical control policies share
    evidence. Cash and incumbent remain explicit hypotheses even in the cold start.
    """
    require(scope in ("equity", "crypto"), "unknown market scope")
    require(type(first) is date and type(seed) is int and 0 <= seed < 2**63, "invalid start or seed")
    require(isinstance(challengers, list) and 1 <= len(challengers) <= 4, "one to four challengers required")
    require(set(common) == {"code", "feature_schema", "risk_policy", "execution_policy", "cost_policy",
                             "base_fill_bps", "universe", "benchmark", "delay_quotes", "gap_atr",
                             "minimum_effect", "power_target", "null_model", "producer", "verifier"}, "incomplete common manifest")
    require(all(sha(common[k]) for k in ("code", "feature_schema", "risk_policy", "execution_policy",
                                         "cost_policy", "null_model", "producer", "verifier")) and common["producer"] != common["verifier"], "missing artifact digest")
    universe = common["universe"]
    require(isinstance(universe, list) and len(set(universe)) == len(universe) > 0 and
            all(isinstance(s,str) and s and ("/" in s) == (scope == "crypto") for s in universe)
            and common["benchmark"] == ("SPY" if scope == "equity" else "BTC/USD"),
            "invalid scoped universe or benchmark")
    require(number(common["base_fill_bps"]) and common["base_fill_bps"] >= 0 and
            type(common["delay_quotes"]) is int and common["delay_quotes"] > 0 and
            number(common["gap_atr"]) and common["gap_atr"] > 0 and number(common["minimum_effect"])
            and common["minimum_effect"] > 0 and number(common["power_target"]) and
            0 < common["power_target"] < 1, "invalid stress or power design")
    require(isinstance(routing,dict) and set(routing) == {"generation","global","scopes","target"}
            and type(routing["generation"]) is int and routing["generation"] >= 0
            and isinstance(routing["scopes"],dict) and set(routing["scopes"]) == REGIMES - {"unknown"}
            and routing["target"] in ({"global"} | set(routing["scopes"]))
            and all(v is None or sha(v) for v in [routing["global"], *routing["scopes"].values()]),
            "complete versioned routing manifest required")
    route = routing["scopes"].get(routing["target"]) or routing["global"]
    require(route == (digest(incumbent) if incumbent is not None else None), "incumbent routing mismatch")
    if scope == "equity":
        days = equity_sessions(first, first + timedelta(days=60))[:30]
        require(len(days) == 30 and days[0].label == first.isoformat(), "start must be an eligible session")
        schedule = [[d.label, d.opened, d.cuts[-1]] for d in days]
    else:
        start = int(datetime.combine(first, day_time(), timezone.utc).timestamp())
        schedule = [[(first+timedelta(days=i)).isoformat(),start+i*86400,start+(i+1)*86400] for i in range(30)]
    shared = dict(protocol=protocol(), common=common, dataset=dataset, scope=scope, calendar=schedule,
                  routing=routing, seed=seed)
    arms = {}
    use_probes = any(specification and specification.get("parameters") for specification in
                     [incumbent, baseline, *(p["spec"] for p in challengers)])
    def behavior(specification):
        raw = policy(specification)
        if not use_probes:
            return raw
        key = digest([dataset, specification, common["code"], common["feature_schema"]])
        with db(path) as c:
            record = needed(c, "policy_probes", key)
        return digest(record)

    from strategy_worker import SEED
    cash_spec = dict(source=SEED.split(":")[0] + ":\n    return 0", guard=None, parameters={}, weights={})
    seen = {behavior(cash_spec)}
    for name, spec in (("incumbent", incumbent), ("baseline", baseline)):
        if name == "incumbent" and spec is None:
            continue
        seen.add(behavior(spec))
        arms[name] = spec
    parent = digest(incumbent)
    candidates = {}
    family = {}
    scenarios = {}
    for proposal in challengers:
        require(set(proposal) == {"spec", "parent", "hypothesis", "feature"} and proposal["parent"] == parent,
                "missing or mismatched candidate parent")
        spec = proposal["spec"]
        fp = behavior(spec)
        require(fp not in seen, "behaviorally equivalent challenger")
        seen.add(fp)
        ident = digest({**shared, **proposal})
        candidates[ident] = proposal
        arms[ident] = spec
        controls = ["cash", "incumbent", "baseline"]
        hypothesis, feature = proposal["hypothesis"], proposal["feature"]
        if hypothesis == "guard":
            require(spec["guard"] and feature is None, "missing guard change")
            ablated = dict(spec, guard=None)
        elif hypothesis == "feature":
            require(feature in FEATURES and feature != spec["guard"], "invalid feature ablation")
            # Frozen intervention: remove this Boolean signal input by setting it false.
            class DisableFeature(ast.NodeTransformer):
                def visit_Name(self, node):
                    return ast.Constant(False) if node.id == feature else node

            tree = DisableFeature().visit(validate_source(spec["source"]))
            ablated = dict(spec, source=ast.unparse(tree))
        else:
            require(hypothesis == "whole_policy" and feature is None, "unknown hypothesis")
            ablated = None
        if ablated is not None:
            require(behavior(ablated) != fp, "uninformative ablation")
            name = ident + "/ablation"
            controls.append(name)
            arms[name] = ablated
        family.update({ident + ":" + control: [ident, control] for control in controls})
        scenarios[ident] = {"cost": {"fill_bps": max(2*common["base_fill_bps"], common["base_fill_bps"]+2)},
                            "delay": {"eligible_quotes": common["delay_quotes"]},
                            "gap": {"adverse_atr": common["gap_atr"]}, **neighbors(spec["parameters"])}
    require(len(family) <= 12, "registered family exceeds twelve comparisons")
    frozen = dict(**shared, candidates=candidates, arms=arms, family=family, scenarios=scenarios)
    identity = digest(frozen)
    with db(path, True) as c:
        if read(c, "campaign", identity) is not None:
            return identity
        m = needed(c, "dataset", dataset)
        require(m["scope"] == scope and m["sessions"][-1] < schedule[0][0], "historical overlap or scope mismatch")
        if scope == "crypto":
            require(c.execute("SELECT 1 FROM experiment_records WHERE kind='campaign' LIMIT 1").fetchone(),
                    "first confirmatory campaign must be equity")
        at = clock()
        require(at < schedule[0][1], "registration after start")
        last_history = date.fromisoformat(m["sessions"][-1])
        from historical_validation import session_close
        require(session_close(scope, last_history.isoformat()).timestamp() <= at,
                "historical dataset extends beyond registration")
        require(set(m["provenance"]["universe"]) >= set(universe), "dataset lacks registered instruments")
        year = datetime.fromtimestamp(at, timezone.utc).year
        count = c.execute("SELECT count(*) FROM experiment_records WHERE kind='budget' AND id LIKE ?",
                          (str(year) + "/%",)).fetchone()[0]
        require(count < 12, "annual confirmatory budget exhausted")
        append(c, "budget", f"{year}/{count+1}", {"campaign": identity, "at": at})
        append(c, "campaign", identity, frozen)
    return identity


def frozen_campaign(path, identity):
    with db(path) as c:
        manifest = needed(c, "campaign", identity)
    require(identity == digest(manifest), "campaign identity mismatch")
    return manifest


def check_portfolio(row, opened, close, universe):
    """Canonical producer contract. Dollar marks include all fees/costs and start/end flat."""
    require(set(row) == {"marks", "episodes", "turnover", "by_symbol", "by_regime"}, "incomplete portfolio")
    marks = row["marks"]
    require(isinstance(marks,list) and len(marks) >= 2 and marks[0][0] == opened and marks[-1][0] == close
            and all(type(t) is int and number(v) and v >= 0 for t,v in marks)
            and all(a[0] < b[0] for a,b in zip(marks,marks[1:])), "invalid ordered marked equity")
    require(number(row["turnover"]) and row["turnover"] >= 0, "invalid gross traded notional")
    last = opened-1
    for start, end, symbol, trace in row["episodes"]:
        require(type(start) is int and type(end) is int and opened <= start < end <= close
                and start >= last and symbol in universe and sha(trace), "invalid completed portfolio episode")
        last = end
    require(bool(row["episodes"]) == (row["turnover"] > 0), "inconsistent flat-session turnover")
    pnl = marks[-1][1] - marks[0][1]
    if not row["episodes"]:
        require(all(v == marks[0][1] for _,v in marks), "unexplained uninvested P&L")
    for col, keys in (("by_symbol", set(universe)), ("by_regime", REGIMES)):
        values = row[col]
        require(set(values) <= keys and all(number(v) for v in values.values()) and
                math.isclose(sum(values.values()), pnl, abs_tol=1e-10, rel_tol=1e-10), "unreconciled attribution")


def replay_inputs(manifest, event_digest):
    """Digests a canonical replay adapter must associate with its actual input configuration."""
    shared = {k:manifest[k] for k in ("common","dataset","scope","calendar","protocol","routing")}
    def binding(arm, scenario=None):
        return digest([shared, arm, manifest["arms"][arm], scenario, event_digest])
    return {"portfolios": {arm:binding(arm) for arm in manifest["arms"]},
            "scenarios": {arm:{name:binding(arm,[name,setting]) for name,setting in scenarios.items()}
                          for arm,scenarios in manifest["scenarios"].items()}}


def record_session(path, identity, day, payload, *, clock=time.time):
    """Trusted shadow adapter submits a single atomic panel for the full registered roster."""
    m = frozen_campaign(path, identity)
    times = {d:(a,b) for d,a,b in m["calendar"]}
    require(day in times, "unregistered session")
    opened, close = times[day]
    require(set(payload) == {"origin", "event_digest", "benchmark_return", "portfolios", "scenarios", "replay_inputs", "regime_seconds"}
            and payload["origin"] == "prospective_shadow" and sha(payload["event_digest"]) and
            number(payload["benchmark_return"]), "invalid shadow provenance")
    require(set(payload["portfolios"]) == set(m["arms"]) and set(payload["scenarios"]) == set(m["candidates"]),
            "unmatched portfolios")
    require(payload["replay_inputs"] == replay_inputs(m, payload["event_digest"]), "policy or event identity mismatch")
    coverage = payload["regime_seconds"]
    require(isinstance(coverage, dict) and set(coverage) <= REGIMES and
            all(type(v) is int and v >= 0 for v in coverage.values()) and sum(coverage.values()) == close-opened,
            "incomplete regime coverage")
    grid = [t for t,_ in next(iter(payload["portfolios"].values()))["marks"]]
    official = set(range(opened,close,3600)) | {close}
    require(official <= set(grid), "missing scheduled intraday marks")
    for row in payload["portfolios"].values():
        check_portfolio(row, opened, close, m["common"]["universe"])
        require([t for t,_ in row["marks"]] == grid, "unmatched mark event grid")
    for ident, rows in payload["scenarios"].items():
        require(set(rows) == set(m["scenarios"][ident]), "unmatched stress or neighborhood scenarios")
        for row in rows.values():
            check_portfolio(row, opened, close, m["common"]["universe"])
            require([t for t,_ in row["marks"]] == grid, "unmatched scenario event grid")
    with db(path, True) as c:
        key = identity + "/" + day
        existing = read(c, "shadow_session", key)
        if existing is not None:
            require(existing["panel"] == payload, "conflicting session")
            return
        at = clock()
        require(close <= at <= close + m["protocol"]["close_grace_seconds"], "missing timely closing receipt")
        require(read(c, "decision", identity) is None, "campaign closed")
        append(c, "shadow_session", key, {"at": at, "panel": payload})
