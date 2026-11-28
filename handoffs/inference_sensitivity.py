"""Offline, synthetic-only investigation of F16. Never imported by Phoenix."""
import csv
import sys

import numpy as np
from scipy.stats import beta, norm, t


N = 30
FAMILIES = 50_000
ALPHA = .05 / 12
SD_BOUND = .001
RHO_BOUND = .7
EFFECT = .001
CROSS_CORRELATION = .8
SEED = 7180243
SCENARIOS = (
    ("gaussian_iid", 0., "gaussian"),
    ("gaussian_boundary", .7, "gaussian"),
    ("t5_boundary", .7, "t5"),
    ("gaussian_outside_envelope", .95, "gaussian"),
)


def simulate_panel(rng, batch, rho, innovations):
    # A shared scale per time step gives unit-variance Student innovations
    # while preserving the marginal covariance and common factor.
    burn = 100 if innovations == "t5" else 0
    size = (batch, N + burn, 12)
    noise = (np.sqrt(CROSS_CORRELATION) * rng.normal(size=(batch, N+burn, 1))
             + np.sqrt(1-CROSS_CORRELATION) * rng.normal(size=size))
    if innovations == "t5":
        noise *= np.sqrt(3 / rng.chisquare(5, size=(batch, N+burn, 1)))
    for i in range(1, N+burn):
        noise[:,i] = rho*noise[:,i-1] + np.sqrt(1-rho*rho)*noise[:,i]
    return SD_BOUND * noise[:,burn:]


def pvalues(x):
    # Exact GLS t with an externally fixed AR(1) coefficient, unknown scale.
    # First observation is stationary. (I-rho*L) whitens all subsequent rows.
    a = np.full(N, 1-RHO_BOUND)
    a[0] = np.sqrt(1-RHO_BOUND**2)
    z = np.empty_like(x)
    z[:,0] = a[0]*x[:,0]
    z[:,1:] = x[:,1:] - RHO_BOUND*x[:,:-1]
    aa = np.dot(a,a)
    mu = (z*a[None,:,None]).sum(axis=1)/aa
    ss = np.square(z - a[None,:,None]*mu[:,None,:]).sum(axis=1)
    statistic = mu/np.sqrt(ss/((N-1)*aa))
    # Common upper bound on Var(mean) for any covariances in the envelope.
    variance = SD_BOUND**2 * (N + 2*sum((N-k)*RHO_BOUND**k for k in range(1,N))) / N**2
    mean = x.mean(axis=1)
    yield "gaussian_gls_t", t.sf(statistic, N-1)
    yield "gaussian_covariance_bound", np.where(mean > 0, norm.sf(mean / np.sqrt(variance)), 1.)
    yield "cantelli_covariance_bound", variance / (variance + np.maximum(mean,0.)**2)


def rejected(pvalues):
    order = pvalues.argsort(axis=1,kind="stable")
    ordered = np.take_along_axis(pvalues,order,axis=1)
    passing = np.logical_and.accumulate(ordered <= ALPHA/np.arange(pvalues.shape[1],0,-1), axis=1)
    hits = np.empty_like(passing)
    np.put_along_axis(hits,order,passing,axis=1)
    return hits


def interval(hits):
    lower = beta.ppf(.025,hits,FAMILIES-hits+1) if hits else 0.
    upper = beta.ppf(.975,hits+1,FAMILIES-hits) if hits < FAMILIES else 1.
    return float(lower),float(upper)


def main():
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(["scenario","comparisons","configuration","method","events","families","rate",
                     "lower_95","upper_95"])
    for (name,rho,innovations), sequence in zip(SCENARIOS, np.random.SeedSequence(SEED).spawn(len(SCENARIOS))):
        rng = np.random.default_rng(sequence)
        counts = {}
        for start in range(0,FAMILIES,500):
            panel = simulate_panel(rng,min(500,FAMILIES-start),rho,innovations)
            for configuration in ("null","one_true_null","alternative"):
                shifts = np.full(12,EFFECT)
                if configuration == "null":
                    shifts[:] = 0.
                elif configuration == "one_true_null":
                    shifts[0] = 0.
                for method,p in pvalues(panel+shifts):
                    for size in (3,12):
                        hits = rejected(p[:,:size])
                        if configuration == "null":
                            event = hits.any(axis=1)
                        elif configuration == "one_true_null":
                            event = hits[:,0]
                        else:
                            event = hits.all(axis=1)
                        key = (name,size,configuration,method)
                        counts[key] = counts.get(key,0) + int(event.sum())
        for key,hits in sorted(counts.items()):
            writer.writerow([*key,hits,FAMILIES,hits/FAMILIES,*interval(hits)])


if __name__ == "__main__":
    main()
