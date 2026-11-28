"""Point-in-time, session-based research on immutable historical panels.

The trusted loader supplies features only, with separate labels and vintage clocks.
An estimator sees training labels and only validation features before it is scored.
"""
from datetime import date, timedelta
import hashlib
import math
import numpy as np

import pandas as pd

from experiment_store import append, needed, read
from learning_loop import digest, require
from market_time import equity_sessions
from paper_store import db


def calendar(scope, first, last):
    if scope == "equity":
        return [s.label for s in equity_sessions(first, last + timedelta(days=1))]
    require(scope == "crypto", "unknown calendar")
    return [(first + timedelta(days=i)).isoformat() for i in range((last-first).days + 1)]


def session_close(scope, label):
    day = date.fromisoformat(label)
    if scope == "crypto":
        return pd.Timestamp(day + timedelta(days=1), tz="UTC")
    close = equity_sessions(day, day + timedelta(days=1))[0].cuts[-1]
    return pd.Timestamp(close, unit="s", tz="UTC")


def dataset_hash(frame):
    h = hashlib.sha256()
    h.update(digest([list(frame.columns), list(map(str, frame.dtypes)), list(frame.index.names)]).encode())
    h.update(pd.util.hash_pandas_object(frame, index=True).to_numpy().tobytes())
    return h.hexdigest()


def _sessions(frame, scope):
    ts = frame.index.get_level_values("timestamp")
    require(ts.tz is not None and not ts.hasnans and not frame.index.has_duplicates,
            "invalid historical index")
    return ts.tz_convert("America/New_York" if scope == "equity" else "UTC").strftime("%Y-%m-%d")


def register_dataset(path, frame, *, scope, features, provenance, horizon_sessions):
    require(type(horizon_sessions) is int and horizon_sessions >= 1, "declare maximum horizon in sessions")
    clocks = {"available_at", "label_end", "position_end", "label_available_at"}
    require(frame.index.names == ["symbol", "timestamp"] and len(frame) > 0, "invalid panel")
    require(features and len(features) == len(set(features)) and not (set(features) & (clocks | {"label"}))
            and frame.columns.is_unique and set(frame.columns) == set(features) | clocks | {"label"}, "explicit feature schema required")
    require(isinstance(provenance, dict) and all(provenance.get(k) for k in
            ("feed", "vintage", "feature_version", "universe", "quality_evidence")), "missing provenance")
    require(set(frame.index.get_level_values("symbol")) == set(provenance["universe"]), "different universe")
    ts = frame.index.get_level_values("timestamp")
    sessions = _sessions(frame, scope)
    for col in clocks:
        require(isinstance(frame[col].dtype, pd.DatetimeTZDtype) and not frame[col].isna().any(),
                "missing point-in-time clock")
    require((frame.available_at <= ts).all() and (frame.label_end >= ts).all()
            and (frame.position_end >= ts).all() and (frame.label_available_at >= frame.label_end).all(),
            "look-ahead or invalid label clock")
    days = calendar(scope, date.fromisoformat(min(sessions)), date.fromisoformat(max(sessions)))
    require(set(days) == set(sessions) and len(days) > 30, "incomplete session calendar")
    if scope == "equity":
        exchange = equity_sessions(date.fromisoformat(days[0]), date.fromisoformat(days[-1])+timedelta(days=1))
        index = np.array(days).searchsorted(sessions)
        seconds = ts.as_unit("s").asi8
        require((seconds >= np.array([s.opened for s in exchange])[index]).all()
                and (ts <= pd.to_datetime(np.array([s.cuts[-1] for s in exchange])[index], unit="s",utc=True)).all(),
                "decision outside its exchange session")
    for symbol in provenance["universe"]:
        require(set(sessions[frame.index.get_level_values("symbol") == symbol]) == set(days),
                "missing instrument session")
    local_zone = "America/New_York" if scope == "equity" else "UTC"
    last_end = frame[["label_end", "position_end"]].max(axis=1).dt.tz_convert(local_zone)
    full = np.array(calendar(scope, date.fromisoformat(days[0]), last_end.max().date()))
    spans = full.searchsorted(last_end.dt.strftime("%Y-%m-%d")) - full.searchsorted(sessions)
    require(spans.max() <= horizon_sessions, "holding or label horizon exceeds declared embargo")
    require(all(pd.api.types.is_numeric_dtype(frame[c]) and np.isfinite(frame[c]).all()
                for c in [*features, "label"]), "unavailable features or outcomes")
    manifest = dict(scope=scope, features=list(features), provenance=provenance, sessions=days,
                    horizon=horizon_sessions, content=dataset_hash(frame), reserve=days[-30:])
    ident = digest(manifest)
    with db(path, True) as c:
        if read(c, "dataset", ident) is not None:
            return ident
        # Legacy windows have no lower exposure bound. Their cut is a conservative watermark.
        legacy_cut = c.execute("SELECT max(cut) FROM development_batches").fetchone()[0]
        if legacy_cut and days[-30] <= legacy_cut[:10]:
            raise ValueError("prior exploratory inspection")
        for day in days:
            role = "reserved" if day in manifest["reserve"] else "development"
            # Scope/calendar keys survive symbol, feed, hypothesis and vendor renaming.
            append(c, "partition", scope + "/" + day, {"role": role})
            if role == "reserved":
                require(read(c, "consumption", scope + "/" + day) is None, "consumed holdout")
        append(c, "dataset", ident, manifest)
    return ident


def development_windows(path, frames):
    """Mask reserved sessions before the legacy exploratory review can see outcomes."""
    allowed = []
    with db(path, True) as c:
        for frame in frames:
            crypto = frame.index.get_level_values("symbol").str.contains("/", regex=False)
            keep = np.zeros(len(frame), dtype=bool)
            for scope, active in (("equity", ~crypto), ("crypto", crypto)):
                if not active.any():
                    continue
                days = _sessions(frame, scope)
                permitted = set()
                for day in set(days[active]):
                    key = scope + "/" + day
                    part = read(c, "partition", key)
                    if part is None:
                        append(c, "partition", key, {"role": "development"})
                    if part is None or part["role"] == "development":
                        permitted.add(day)
                keep |= active & days.isin(permitted)
            allowed.append(frame.loc[keep])
    return allowed


def _score_in_time_order(predict, frame, features, score):
    """Reveal one simultaneous feature cohort at a time, then score aligned predictions."""
    chronological = frame.sort_index(level=["timestamp","symbol"])
    predictions = []
    for _, rows in chronological.groupby(level="timestamp", sort=False):
        values = predict(rows[features].copy())
        if isinstance(values, pd.Series):
            require(values.index.equals(rows.index), "misaligned predictions")
        values = np.asarray(values)
        require(values.shape == (len(rows),) and np.isfinite(values).all(), "invalid predictions")
        predictions.extend(values.tolist())
    metric = score(chronological.label.copy(), np.array(predictions))
    require(isinstance(metric,(int,float)) and math.isfinite(metric), "invalid validation metric")
    return metric


def walk_forward(path, ident, frame, fit, score, *, candidate):
    """Persist the full frozen specification and terminal outcome, including failed fits."""
    require(isinstance(candidate, dict) and all(candidate.get(k) for k in
            ("specification", "code", "feature_schema", "fit_protocol", "score_protocol")),
            "incomplete development experiment")
    identity = digest(["six-fold-v1", ident, candidate])
    with db(path, True) as c:
        needed(c, "dataset", ident)
        old = read(c, "walk_result", identity)
        if old is not None:
            return old
        require(read(c, "walk_registration", identity) is None, "registered run pending or abandoned")
        append(c, "walk_registration", identity, {"dataset": ident, "candidate": candidate})
    try:
        result = _walk_forward(path, ident, frame, fit, score)
    except Exception as exc:
        result = {"status": "invalid", "reason": str(exc) if isinstance(exc, ValueError) else type(exc).__name__}
    result["experiment"] = identity
    with db(path, True) as c:
        append(c, "walk_result", identity, result)
    return result


def _walk_forward(path, ident, frame, fit, score):
    """Six expanding fits. Score callback receives predictions after fitting, never holdout data."""
    with db(path) as c:
        m = needed(c, "dataset", ident)
    require(dataset_hash(frame) == m["content"], "dataset revision changed")
    days = m["sessions"][:-30]
    width = len(days) // 7
    gap = m["horizon"]
    if width <= gap or pd.Timestamp(days[0]) + pd.DateOffset(months=12) > pd.Timestamp(m["reserve"][0]):
        return {"status": "inconclusive", "reason": "insufficient_development_history"}
    labels = _sessions(frame, m["scope"])
    folds = []
    # A label or an open position must end inside its own window. Apply this at both ends.
    for k in range(6):
        split = len(days) - (6-k)*width
        train_days = days[:split-gap]
        valid_days = days[split:split+width]
        train_end = session_close(m["scope"], train_days[-1])
        valid_end = session_close(m["scope"], valid_days[-1])
        known = frame[["label_end", "position_end", "label_available_at"]].max(axis=1)
        train = frame.loc[labels.isin(train_days) & known.le(train_end)]
        valid = frame.loc[labels.isin(valid_days) & known.le(valid_end)]
        if train.empty or valid.empty:
            return {"status": "inconclusive", "reason": "empty_purged_fold", "folds": folds}
        predict = fit(train[m["features"]].copy(), train.label.copy())
        metric = _score_in_time_order(predict, valid, m["features"], score)
        folds.append(dict(training_end=train_days[-1], validation=valid_days,
                          training_rows=len(train), validation_rows=len(valid), metric=metric))
    return {"status": "historical_simulated", "folds": folds}


def inspect_holdout(path, ident, frame, family, fit, score):
    """Charge all reserved sessions before exposing any values, even if evaluation crashes."""
    with db(path) as c:
        m = needed(c, "dataset", ident)
    require(dataset_hash(frame) == m["content"], "dataset revision changed")
    require(isinstance(family, dict) and family and all(k == digest(v) for k,v in family.items()),
            "frozen holdout family required")
    run = digest([ident, family])
    with db(path, True) as c:
        completed = read(c, "holdout_result", run)
        if completed is not None:
            return completed
        for day in m["reserve"]:
            key = m["scope"] + "/" + day
            require(read(c, "consumption", key) is None, "consumed holdout")
            append(c, "consumption", key, {"dataset": ident, "family": run})
        append(c, "holdout_family", run, family)
    session = _sessions(frame, m["scope"])
    train_days = m["sessions"][:-(30 + m["horizon"])]
    mask = session.isin(m["reserve"])
    ends = frame[["label_end", "label_available_at", "position_end"]].max(axis=1)
    train_mask = session.isin(train_days)
    train = (frame.loc[train_mask & ends.le(session_close(m["scope"], train_days[-1]))]
             if train_days else frame.iloc[0:0])
    held = frame.loc[mask & ends.le(session_close(m["scope"], m["reserve"][-1]))]
    results = {}
    for candidate, spec in family.items():
        try:
            require(not train.empty and not held.empty, "empty purged sample")
            predict = fit(spec, train[m["features"]].copy(), train.label.copy())
            metric = _score_in_time_order(predict, held, m["features"], score)
            results[candidate] = {"status": "historical_simulated", "metric": metric}
        except Exception as exc:
            results[candidate] = {"status": "inconclusive", "reason": type(exc).__name__}
    report = {"status": "historical_simulated" if all(r["status"] == "historical_simulated"
              for r in results.values()) else "inconclusive", "family": run, "results": results}
    with db(path, True) as c:
        append(c, "holdout_result", run, report)
    return report


def register_probes(path, ident, frame, spec, code, feature_schema, evaluate):
    """Trusted policy adapter records behavior on the same ordered development-only probes."""
    with db(path) as c:
        m = needed(c, "dataset", ident)
    require(dataset_hash(frame) == m["content"], "probe dataset revision changed")
    allowed = _sessions(frame, m["scope"]).isin(m["sessions"][:-30])
    features = frame.loc[allowed, m["features"]].sort_index()
    require(not features.empty, "empty behavioral probe set")
    probes = features.iloc[np.linspace(0, len(features)-1, min(512,len(features)), dtype=int)]
    outputs = np.asarray(evaluate(spec, probes.copy()))
    require(outputs.shape == (len(probes),) and np.isin(outputs,[0,1]).all(), "invalid probe decisions")
    key = digest([ident, spec, code, feature_schema])
    with db(path, True) as c:
        append(c, "policy_probes", key, {"inputs":dataset_hash(probes), "signals":digest(outputs.astype(int).tolist())})
    return key
