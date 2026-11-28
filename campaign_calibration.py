"""Offline operating-characteristic studies of the unchanged confirmatory statistic."""
import argparse
from datetime import date, timedelta
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from campaign_registry import number, protocol
from campaign_statistics import CAMPAIGN_ALPHA, holm, paired_result
from experiment_store import append
from learning_loop import digest, require
from paper_store import db


def design(rho, cross_correlation, daily_sd, repetitions, seed):
    require(all(number(x) for x in (rho, cross_correlation, daily_sd)) and abs(rho) < 1
            and 0 <= cross_correlation <= 1 and daily_sd > 0, "invalid stationary design")
    require(type(repetitions) is int and repetitions > 0 and type(seed) is int and seed >= 0,
            "invalid trial design")
    return dict(process="stationary Gaussian AR(1) with common factor", rho=rho, daily_sd=daily_sd,
                cross_correlation=cross_correlation, repetitions=repetitions, seed=seed,
                generator=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())


def upper_bound(hits, n, tail=.025):
    """Exact one-sided Clopper-Pearson upper bound by binomial CDF inversion."""
    if hits == n:
        return 1.
    if hits == 0:
        return -math.expm1(math.log(tail)/n)
    k = np.arange(hits+1)
    log_coefs = np.array([math.lgamma(n+1)-math.lgamma(i+1)-math.lgamma(n-i+1) for i in k])
    lo, hi = hits/n, 1.
    for _ in range(50):
        p = (lo+hi)/2
        log_terms = log_coefs + k*math.log(p) + (n-k)*math.log1p(-p)
        if np.exp(log_terms).sum() > tail:
            lo = p
        else:
            hi = p
    return hi


def simulate(model, family_size, effect, bootstrap_seed):
    """Each replicate redraws the *whole* family; alternative raises each paired mean by effect.

    'power' here is probability that the entire family rejects. This is conservative for
    a challenger with fewer required comparisons. The modeled effect is after noise/costs.
    """
    require(model == design(**{key:model[key] for key in
                              ("rho","cross_correlation","daily_sd","repetitions","seed")}), "unknown design")
    require(type(family_size) is int and 1 <= family_size <= 12 and number(effect) and effect > 0,
            "invalid family or effect")
    rng = np.random.default_rng(model["seed"])
    days = tuple(date(2000,1,1) + timedelta(days=i) for i in range(30))
    cash = dict.fromkeys(days, 0.)
    family = tuple(str(i) for i in range(family_size))
    false_positives = detections = 0
    rho, corr = model["rho"], model["cross_correlation"]
    for _ in range(model["repetitions"]):
        noise = (math.sqrt(corr)*rng.normal(size=(1,30)) +
                 math.sqrt(1-corr)*rng.normal(size=(family_size,30))) * model["daily_sd"]
        for j in range(1,30):
            noise[:,j] = rho*noise[:,j-1] + math.sqrt(1-rho*rho)*noise[:,j]
        for shift in (0., effect):
            results = {name:paired_result(days, dict(zip(days, noise[i]+shift)), cash, cash, 1., 20, 10,
                                          bootstrap_seed) for i,name in enumerate(family)}
            adjusted = holm(family, results)
            if shift == 0:
                false_positives += min(adjusted.values()) <= CAMPAIGN_ALPHA
            else:
                detections += max(adjusted.values()) <= CAMPAIGN_ALPHA
    n = model["repetitions"]
    return dict(design=digest(model), model=model, implementation=protocol()["implementation"],
                bootstrap_seed=bootstrap_seed, family_size=family_size, effect=effect,
                false_positive_families=false_positives, detected_families=detections,
                null_upper=upper_bound(false_positives,n), power_lower=1-upper_bound(n-detections,n))


def register_calibration(path, model, family_size, effect, bootstrap_seed):
    key = digest([model, family_size, effect, bootstrap_seed, protocol()["implementation"]])
    from experiment_store import read
    with db(path) as c:
        if read(c, "calibration", key) is not None:
            return key
    study = simulate(model, family_size, effect, bootstrap_seed)
    with db(path, True) as c:
        append(c, "calibration", key, study)
    return key


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--replicates", type=int, default=32)
    p.add_argument("--family", type=int, default=3)
    args = p.parse_args()
    for rho in (0., .7):
        m = design(rho, .8, .001, args.replicates, 314159)
        print(json.dumps(simulate(m, args.family, .001, 771), sort_keys=True), flush=True)
