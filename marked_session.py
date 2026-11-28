"""Canonical marked-session producer for the prospective campaign registry."""
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal

from config import PAPER_EQUITIES
from learning_loop import digest
from portfolio_events import ExecutionPolicy, QuotePortfolio, quote_ok

D = Decimal
REGIMES = frozenset(("trend_up", "trend_down", "range_quiet", "vol_expansion", "unknown"))


@dataclass(frozen=True)
class SessionTape:
    """Observed poll grid plus outcomes of evaluating each frozen strategy specification.

    decision_streams maps digest(spec) to {frame.at: tuple[Decision, ...]}.
    Include explicit empty streams for evaluated policies that have no decisions.
    Regimes are ordered (UTC epoch seconds, label) at the open and at each change.
    """
    frames: tuple
    decision_streams: dict
    regimes: tuple
    benchmark_open: D
    benchmark_close: D


def _epoch(at):
    if at.utcoffset() is None:
        raise ValueError("campaign grid requires an aware timestamp")
    return int(at.timestamp())


def _normalize(item):
    if isinstance(item, datetime):
        return item.astimezone(timezone.utc).isoformat()
    if isinstance(item, D):
        return format(item.normalize(), "f") if item.is_finite() else str(item)
    if isinstance(item, dict):
        return {str(_normalize(k)): _normalize(v) for k, v in item.items()}
    if isinstance(item, (tuple, list)):
        return [_normalize(v) for v in item]
    return item


def _scenario(policy, manifest, arm, scenario):
    base = replace(policy, scenario_bps=manifest["common"]["base_fill_bps"])
    spec = manifest["arms"].get(arm)
    delay, gap = 0, D(0)
    if scenario:
        name, setting = scenario
        if name == "cost":
            base = replace(base, scenario_bps=setting["fill_bps"])
        elif name == "gap":
            gap = D(str(setting["adverse_atr"]))
        elif name == "delay":
            delay = setting["eligible_quotes"]
        elif name.startswith("neighbor/"):
            spec = {**spec, "parameters": setting}
        else:
            raise ValueError("unknown frozen campaign scenario")
    return base, None if spec is None else digest(spec), delay, gap


def _play(model, frames, decisions, regimes):
    marks, episodes, by_symbol, by_regime = {}, [], {}, {}
    turnover = D(0)
    if model.position is not None:
        raise ValueError("session must start flat")
    entry = None
    regime_index = 0
    for frame in frames:
        t = _epoch(frame.at)
        while regime_index + 1 < len(regimes) and regimes[regime_index + 1][0] <= t:
            regime_index += 1
        for fill in model.step(replace(frame, decisions=decisions.get(frame.at, ()))):
            turnover += fill.qty * fill.price
            action = [fill.side, fill.at, fill.symbol, fill.qty, fill.price, fill.fee, fill.reason]
            if fill.side == "buy":
                entry = (fill, action, regimes[regime_index][1])
            else:
                opened, opening_action, regime = entry
                net = fill.qty * (fill.price - opened.price) - opened.fee - fill.fee
                by_symbol[fill.symbol] = by_symbol.get(fill.symbol, D(0)) + net
                by_regime[regime] = by_regime.get(regime, D(0)) + net
                if _epoch(opened.at) >= t:
                    raise ValueError("registry cannot represent a subsecond round trip")
                episodes.append([_epoch(opened.at), t, fill.symbol,
                                 digest(_normalize([opening_action, action]))])
                entry = None
        equity = model.account.equity
        if equity is None or equity < 0:
            raise ValueError("missing or invalid marked equity")
        marks[t] = float(equity)
    if model.position is not None:
        raise ValueError("unresolved session-close exposure")
    return dict(marks=[[at, value] for at, value in marks.items()], episodes=episodes, turnover=float(turnover),
                by_symbol={k: float(v) for k, v in by_symbol.items()},
                by_regime={k: float(v) for k, v in by_regime.items()})


def _validate(tape, opened, close, universe):
    frames = tuple(tape.frames)
    official = set(range(opened, close, 3600)) | {close}
    if (not frames or frames[0].at.timestamp() != opened or frames[-1].at.timestamp() != close
            or any(a.at >= b.at for a, b in zip(frames, frames[1:]))
            or not official <= {f.at.timestamp() for f in frames}):
        raise ValueError("incomplete common mark grid")
    if any(f.decisions or _epoch(f.session_open) != opened or _epoch(f.session_close) != close for f in frames):
        raise ValueError("expected a common quote-only session tape")
    for frame in frames:
        if frame.at.timestamp() in official and not universe <= {
                q.symbol for q in frame.quotes if quote_ok(q, frame.at, entry=False)
                and frame.session_open <= q.at <= frame.session_close}:
            raise ValueError("missing or stale scheduled quote")
    frame_times = {f.at for f in frames}
    for identity, stream in tape.decision_streams.items():
        if not set(stream) <= frame_times:
            raise ValueError("decision outside common grid")
        for at, batch in stream.items():
            if any(d.symbol not in universe or d.strategy != identity
                   or not (d.end <= d.published <= d.decided <= at and d.ready is True) for d in batch):
                raise ValueError("invalid arm decision stream")
    regimes = tuple(tape.regimes)
    if (not regimes or regimes[0][0] != opened
            or any(type(t) is not int or not opened <= t < close or r not in REGIMES for t, r in regimes)
            or any(a[0] >= b[0] for a, b in zip(regimes, regimes[1:]))):
        raise ValueError("incomplete regime timeline")
    coverage = {}
    for (t, regime), end in zip(regimes, [t for t, _ in regimes[1:]] + [close]):
        coverage[regime] = coverage.get(regime, 0) + end - t
    if not all(v.is_finite() and v > 0 for v in (tape.benchmark_open, tape.benchmark_close)):
        raise ValueError("invalid benchmark marks")
    return frames, regimes, coverage


def replay_marked_session(manifest, day, tapes, *, policy=None):
    """Return the exact payload for campaign_registry.record_session.

    tapes must include the registered calendar prefix through day. Replay that prefix
    to preserve each portfolio's cash, peak and latched halt without trusting a checkpoint.
    The upstream collector owns original quote/feature provenance and timely receipt.
    """
    from campaign_registry import replay_inputs

    if manifest["scope"] != "equity":
        raise ValueError("only equity campaign execution is supported")
    universe = set(manifest["common"]["universe"])
    policy = policy or ExecutionPolicy()
    if not universe or not universe <= PAPER_EQUITIES or policy.capital != D(str(manifest["protocol"]["capital"])):
        raise ValueError("portfolio policy does not match registered scope")
    roster = {arm: _scenario(policy, manifest, arm, None) for arm in manifest["arms"]}
    scenarios = {(arm, name): _scenario(policy, manifest, arm, (name, setting))
                 for arm, settings in manifest["scenarios"].items() for name, setting in settings.items()}
    models = {key: QuotePortfolio(spec[0], skip_entry_quotes=spec[2], adverse_stop_atr=spec[3])
              for key, spec in (roster | scenarios).items()}
    source = [asdict(policy)]
    ids = {spec[1] for spec in (roster | scenarios).values()} - {None}
    for label, opened, close in manifest["calendar"]:
        if label not in tapes:
            raise ValueError("missing registered session tape")
        tape = tapes[label]
        frames, regimes, coverage = _validate(tape, opened, close, universe)
        if not ids <= tape.decision_streams.keys():
            raise ValueError("missing frozen strategy or neighborhood decision stream")
        decisions = {key: {at: [asdict(d) for d in sorted(ds, key=lambda d: d.symbol)]
                           for at, ds in tape.decision_streams[key].items()} for key in sorted(ids)}
        source.append([label, [asdict(f) for f in frames], decisions, regimes,
                       tape.benchmark_open, tape.benchmark_close])
        panels = {}
        for key, spec in (roster | scenarios).items():
            panels[key] = _play(models[key], frames, tape.decision_streams.get(spec[1], {}), regimes)
        if label == day:
            event_digest = digest(_normalize(source))
            return dict(origin="prospective_shadow", event_digest=event_digest,
                        benchmark_return=float(tape.benchmark_close / tape.benchmark_open - 1),
                        portfolios={key: panels[key] for key in roster},
                        scenarios={arm: {name: panels[arm, name] for name in settings}
                                   for arm, settings in manifest["scenarios"].items()},
                        replay_inputs=replay_inputs(manifest, event_digest), regime_seconds=coverage)
    raise ValueError("unregistered session")
