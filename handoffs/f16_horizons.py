"""Reproducible planning study for F16. Runs exclusively on synthetic inputs."""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from scipy.special import ndtr
from scipy.stats import beta, norm

from inference_sensitivity import rejected


ALPHA = .05 / 12
HORIZONS = (30, 60, 120, 240, 480)
LOOKS = (60, 120, 240)
SPENDING = (.10, .20, .70)
FAMILY_SIZES = (3, 12)
REPLICATES = 30_000
SEED = 39229141
SCALE = .001
RHO_LIMIT = .8
POWER = .8
EFFECTS = (.25, .5, 1.)
# All locations and scales in the generator are expressed in units of SCALE.
SCENARIOS = (
    ("gaussian_moderate", .3, .8, "normal"),
    ("gaussian_persistent", .7, .8, "normal"),
    ("gaussian_envelope_boundary", .8, .2, "normal"),
    ("clustered_skewed", .5, .8, "clustered_skewed"),
    ("gaussian_beyond_envelope", .93, .8, "normal"),
)
# A shift of two SDs tests strong control when false nulls can reject early.
CONFIGURATIONS = ("global_null", "one_true_null", "half_true_null", "half_sd", "one_sd")


def variance_of_mean(n, rho):
    """Stationary unit-variance AR(1) mean variance or its lag-envelope bound."""
    return (n + 2*sum((n-k)*rho**k for k in range(1,n))) / n**2


def ar_mean_information(n, rho):
    """Precision of the GLS mean in a stationary Gaussian AR(1) of unit variance."""
    return (n*(1-rho)+2*rho)/(1+rho)


def minimum_sessions(variance, effect, critical, power):
    target_variance = (effect / (critical + norm.ppf(power)))**2
    lo, hi = 0, 1
    while variance(hi) > target_variance:
        hi *= 2
    while hi - lo > 1:
        n = (lo+hi)//2
        if variance(n) <= target_variance:
            hi = n
        else:
            lo = n
    return hi


def covariance_envelope_p(mean, n, gaussian):
    variance = variance_of_mean(n, RHO_LIMIT)
    positive = np.maximum(mean, 0.)
    if gaussian:
        return np.where(mean > 0, ndtr(-positive/math.sqrt(variance)), 1.)
    return variance / (variance + positive**2)


def spent_pvalue(previous, p, weight):
    """An anytime p-value across the declared looks by the union bound."""
    return np.minimum(previous, p / weight)


def planning_rows(writer):
    for rho in (0., .3, .7, .8, .93):
        for effect in EFFECTS:
            # Most-powerful level-alpha test of the simple Gaussian alternative.
            # A valid marginal Holm comparison cannot exceed this benchmark.
            upper = norm.cdf(effect*math.sqrt(ar_mean_information(30, rho))-norm.isf(ALPHA))
            for family in FAMILY_SIZES:
                # Per-test size alpha/m yields sufficient power using a union bound
                # across misses. The sufficient N applies under the stated model.
                required_marginal = 1-(1-POWER)/family
                critical = norm.isf(ALPHA/family)
                oracle = minimum_sessions(lambda n:1/ar_mean_information(n,rho), effect,
                                          critical, required_marginal)
                envelope = (minimum_sessions(lambda n:variance_of_mean(n,RHO_LIMIT), effect,
                                              critical, required_marginal) if rho <= RHO_LIMIT else "")
                writer.writerow((rho, effect, effect*SCALE*10_000, family, upper, oracle, envelope))


def generate(rng, size, rho, cross, innovations):
    """Unit-variance AR(1) paths with shared innovations across comparisons."""
    n = HORIZONS[-1]
    if innovations == "normal":
        total = n
        burn = 0
        common = rng.standard_normal((size,total,1))
        idiosyncratic = rng.standard_normal((size,total,FAMILY_SIZES[-1]))
    else:
        burn = 120
        total = n + burn
        common = rng.exponential(size=(size,total,1))-1
        idiosyncratic = rng.exponential(size=(size,total,FAMILY_SIZES[-1]))-1
    paths = math.sqrt(cross)*common + math.sqrt(1-cross)*idiosyncratic
    if burn:
        # Symmetric stationary two-state Markov volatility. Innovations retain
        # zero conditional mean and unit unconditional variance.
        switches = rng.random((size,total,1)) < .03
        initial_high = rng.integers(0,2,size=(size,1,1))
        high = (np.cumsum(switches, axis=1)+initial_high) % 2
        paths *= np.sqrt(np.where(high,1.8,.2))
    innovation_scale = math.sqrt(1-rho*rho)
    for day in range(1,total):
        paths[:,day] = rho*paths[:,day-1] + innovation_scale*paths[:,day]
    return paths[:,burn:]


def configuration(name, family):
    mask = np.zeros(family,dtype=bool)
    if name == "global_null":
        mask[:] = True
    elif name == "one_true_null":
        mask[0] = True
    elif name == "half_true_null":
        mask[:family//2] = True
    effect = {"half_sd": .5, "one_sd": 1.}.get(name, 2.)
    return np.where(mask,0.,effect), mask


def events(p, true_nulls):
    hits = rejected(p)
    if true_nulls.any():
        return int(hits[:,true_nulls].any(axis=1).sum())
    return int(hits.all(axis=1).sum())


def simulate(rng, rho, cross, innovations):
    counts = {}
    for start in range(0,REPLICATES,128):
        size = min(128,REPLICATES-start)
        paths = generate(rng,size,rho,cross,innovations)
        cumulative = np.cumsum(paths,axis=1)
        for family in FAMILY_SIZES:
            for config in CONFIGURATIONS:
                shifts, true_nulls = configuration(config,family)
                adjusted = np.ones((size,family))
                for n in HORIZONS:
                    means = cumulative[:,n-1,:family]/n + shifts
                    for gaussian, method in ((True,"gaussian_bound"),(False,"cantelli_bound")):
                        p = covariance_envelope_p(means,n,gaussian)
                        key = (family, config, method, n)
                        counts[key] = counts.get(key,0) + events(p,true_nulls)
                        if gaussian and n in LOOKS:
                            adjusted = spent_pvalue(adjusted,p,SPENDING[LOOKS.index(n)])
                            key = (family, config, "gaussian_scheduled_looks", n)
                            counts[key] = counts.get(key,0) + events(adjusted,true_nulls)
    return counts


def design():
    root = Path(__file__).parent
    code = {name:hashlib.sha256((root/name).read_bytes()).hexdigest()
            for name in ("f16_horizons.py","inference_sensitivity.py")}
    return dict(purpose="synthetic research only", alpha=ALPHA, annual_campaigns=12, horizons=HORIZONS,
                looks=LOOKS, spending=SPENDING, family_sizes=FAMILY_SIZES, replicates=REPLICATES,
                seed=SEED, daily_sd=SCALE, rho_limit=RHO_LIMIT, planning_power=POWER,
                standardized_effects=EFFECTS, scenarios=SCENARIOS, configurations=CONFIGURATIONS,
                code_sha256=code)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output",type=Path)
    directory = parser.parse_args().output
    directory.mkdir(parents=True,exist_ok=True)
    spec = design()
    spec["study_sha256"] = hashlib.sha256(json.dumps(spec,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    (directory/"design.json").write_text(json.dumps(spec,indent=2)+"\n",encoding="utf-8")
    with (directory/"planning.csv").open("w",newline="",encoding="utf-8") as f:
        writer = csv.writer(f,lineterminator="\n")
        writer.writerow(("rho","effect_sd","effect_bps","comparisons","oracle_30_marginal_power_upper",
                         "oracle_sufficient_sessions","gaussian_envelope_sufficient_sessions"))
        planning_rows(writer)
    with (directory/"simulation.csv").open("w",newline="",encoding="utf-8") as f:
        writer = csv.writer(f,lineterminator="\n")
        writer.writerow(("scenario","comparisons","configuration","method","sessions","events","families",
                         "rate","pointwise_lower_95","pointwise_upper_95"))
        streams = np.random.SeedSequence(SEED).spawn(len(SCENARIOS))
        for (name,rho,cross,innovations), stream in zip(SCENARIOS,streams):
            for key,count in sorted(simulate(np.random.default_rng(stream),rho,cross,innovations).items()):
                lower = beta.ppf(.025,count,REPLICATES-count+1) if count else 0.
                upper = beta.ppf(.975,count+1,REPLICATES-count) if count < REPLICATES else 1.
                writer.writerow((name,*key,count,REPLICATES,count/REPLICATES,lower,upper))
            f.flush()


if __name__ == "__main__":
    main()
